# raml_server.py
# ─────────────────────────────────────────────────────────────────────────────
# FastAPI bridge — preserves the original REST API surface for the UI.
#
# All existing endpoints are kept unchanged.
# Under the hood, chat requests are routed through the ADK Runner which
# calls the master_agent → raml_specialist pipeline.
#
# Run: uvicorn raml_server:app --reload --port 8001
#
# ADK dev UI (optional, separate port):
#   adk web .   →  http://localhost:8000
# ─────────────────────────────────────────────────────────────────────────────

import os, sys, json, asyncio
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

# ── ADK imports ───────────────────────────────────────────────────────────────
from google.adk.runners         import Runner
from google.adk.sessions        import InMemorySessionService
from google.genai                import types as genai_types

# ── Our agents & shared state ─────────────────────────────────────────────────
from agent import root_agent, session_store          # ADK root_agent + store
from shared.raml_tools   import tool_validate_raml
from shared.anypoint_publisher import AnypointPublisher, AnypointConfig

# ─────────────────────────────────────────────────────────────────────────────
# ADK Runner setup
# ─────────────────────────────────────────────────────────────────────────────

APP_NAME        = "raml-platform"
ADK_SESSION_SVC = InMemorySessionService()

adk_runner = Runner(
    agent           = root_agent,
    app_name        = APP_NAME,
    session_service = ADK_SESSION_SVC,
)


def _get_or_create_adk_session(session_id: str) -> str:
    """
    Map a RAML session_id to an ADK session.
    ADK sessions are per-conversation; we use session_id as both user_id
    and session_id for simplicity.
    """
    try:
        ADK_SESSION_SVC.get_session(
            app_name=APP_NAME, user_id=session_id, session_id=session_id)
    except Exception:
        ADK_SESSION_SVC.create_session(
            app_name=APP_NAME, user_id=session_id, session_id=session_id)
    return session_id


async def _run_adk_turn(session_id: str, message: str):
    """
    Run one turn through the ADK master agent and yield SSE-compatible events.
    Translates ADK events → original SSE event format.
    """
    _get_or_create_adk_session(session_id)
    content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=message)],
    )

    tool_in_progress = None
    final_text       = ""

    async for event in adk_runner.run_async(
        user_id    = session_id,
        session_id = session_id,
        new_message = content,
    ):
        # ── Tool start ────────────────────────────────────────────────────────
        if event.get_function_calls():
            for fc in event.get_function_calls():
                tool_in_progress = fc.name
                yield {
                    "type":  "step_start",
                    "label": _tool_label(fc.name),
                    "tool":  fc.name,
                }

        # ── Tool result ───────────────────────────────────────────────────────
        if event.get_function_responses():
            for fr in event.get_function_responses():
                result = fr.response or {}

                # Files event — whenever generate/fix returns files
                if "files" in result and isinstance(result["files"], dict):
                    raml_session = session_store.get(session_id)
                    yield {
                        "type":          "files",
                        "files":         result["files"],
                        "changed_files": result.get("changed_files", []),
                        "deleted_files": result.get("deleted_files", []),
                        "is_first_turn": result.get("is_first_turn", False),
                    }

                # Validation event
                if "error_count" in result and "warning_count" in result:
                    yield {"type": "validation", **result}

                yield {
                    "type":    "step_done",
                    "tool":    fr.name,
                    "summary": _tool_summary(fr.name, result),
                }

        # ── Final response ────────────────────────────────────────────────────
        if event.is_final_response():
            if event.content and event.content.parts:
                final_text = "".join(
                    p.text for p in event.content.parts if hasattr(p, "text")
                )

    # Always emit a done event at the end
    raml_session = session_store.get(session_id)
    val = tool_validate_raml(raml_session.files) if raml_session else {}
    yield {
        "type":              "done",
        "message":           final_text,
        "session":           raml_session.to_dict() if raml_session else {},
        "final_validation":  val,
    }


def _tool_label(name: str) -> str:
    labels = {
        "fetch_raml_context":     "Searching knowledge base…",
        "fetch_learned_rules":    "Loading learned rules…",
        "generate_raml_files":    "Generating RAML files…",
        "validate_raml_files":    "Validating RAML…",
        "fix_raml_errors":        "Auto-fixing errors…",
        "save_correction_lesson": "Saving correction…",
        "get_session_files":      "Loading files…",
        "push_to_anypoint":       "Pushing to Anypoint…",
        "raml_specialist":        "RAML specialist working…",
        "create_project_session": "Creating project session…",
    }
    return labels.get(name, f"{name}…")


def _tool_summary(name: str, result: dict) -> str:
    if "error" in result:
        return f"Error: {result['error']}"
    if name == "fetch_raml_context":
        return f"{result.get('source_count', 0)} relevant chunks found"
    if name == "fetch_learned_rules":
        return f"{result.get('lesson_count', 0)} rules injected"
    if name == "generate_raml_files":
        return result.get("message", "Generated")
    if name == "validate_raml_files":
        ec = result.get("error_count", 0)
        wc = result.get("warning_count", 0)
        return f"✓ Clean — {wc} warning(s)" if ec == 0 else f"{ec} error(s), {wc} warning(s)"
    if name == "fix_raml_errors":
        rem = result.get("errors_remaining", "?")
        return result.get("message", f"{rem} errors remaining")
    if name == "push_to_anypoint":
        return "Pushed ✓" if result.get("success") else result.get("error", "Push failed")
    return "Done"


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="RAML Agent API", version="5.0-adk")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    print("\n" + "="*60)
    print("  RAML Platform v5.0 — ADK Multi-Agent  |  :8001")
    print("  ADK Dev UI (optional): adk web .       |  :8000")
    print("="*60)
    routes = sorted(
        f"  {list(r.methods)} {r.path}"
        for r in app.routes if hasattr(r, "methods")
    )
    for r in routes:
        print(r)
    print("="*60 + "\n")


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── Request models ────────────────────────────────────────────────────────────

class CreateSessionReq(BaseModel):
    project_name: str

class ChatReq(BaseModel):
    message: str

class EditFileReq(BaseModel):
    content: str

class PushReq(BaseModel):
    project_name:    str  = ""
    skip_validation: bool = False


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "version": "5.0-adk",
            "agent": root_agent.name}


# ── Sessions ──────────────────────────────────────────────────────────────────

@app.post("/sessions")
def create_session(req: CreateSessionReq):
    session = session_store.create(project_name=req.project_name)
    return session.to_dict()


@app.get("/sessions")
def list_sessions():
    return {"sessions": session_store.list_all()}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    if not session_store.get(session_id):
        raise HTTPException(404, "Session not found")
    session_store.delete(session_id)
    return {"deleted": session_id}


# ── Chat — ADK agent loop over SSE ───────────────────────────────────────────

@app.post("/sessions/{session_id}/chat")
def chat(session_id: str, req: ChatReq):
    """
    Streams the ADK master-agent loop as SSE events.
    Event types match the original API: step_start, step_done, files,
    validation, done, error.
    """
    if not session_store.get(session_id):
        raise HTTPException(404, "Session not found")

    def stream():
        try:
            # Run async ADK loop in a new event loop inside this sync generator
            loop = asyncio.new_event_loop()
            try:
                gen = _run_adk_turn(session_id, req.message)
                while True:
                    try:
                        event = loop.run_until_complete(gen.__anext__())
                        yield sse(event)
                    except StopAsyncIteration:
                        break
            finally:
                loop.close()
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse({"type": "error", "msg": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Manual file edit ──────────────────────────────────────────────────────────

@app.put("/sessions/{session_id}/files/{file_path:path}")
def edit_file(session_id: str, file_path: str, req: EditFileReq):
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    session.files[file_path] = req.content
    full = session.project_dir / file_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(req.content, encoding="utf-8")
    session.save()
    val = tool_validate_raml(session.files)
    return {"path": file_path, "saved": True, "validation": val}


# ── File access ───────────────────────────────────────────────────────────────

@app.get("/sessions/{session_id}/files")
def get_files(session_id: str):
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return {"files": [
        {"path": p, "size": len(c), "lines": c.count("\n") + 1}
        for p, c in session.files.items()
    ]}


@app.get("/sessions/{session_id}/files/{file_path:path}")
def get_file(session_id: str, file_path: str):
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if file_path not in session.files:
        raise HTTPException(404, f"'{file_path}' not found")
    content = session.files[file_path]
    return {"path": file_path, "content": content,
            "lines": content.count("\n") + 1}


@app.get("/sessions/{session_id}/download")
def download_zip(session_id: str):
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    zip_bytes = session_store.get_zip(session_id)
    filename  = f"{session.project_name.replace(' ', '-')}.zip"
    return Response(
        content=zip_bytes, media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── On-demand validation ──────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/validate")
def validate(session_id: str):
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    try:
        return tool_validate_raml(session.files)
    except Exception as e:
        raise HTTPException(500, f"Validation error: {e}")


# ── Push to Anypoint ──────────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/push")
def push(session_id: str, req: PushReq):
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if not session.files:
        raise HTTPException(400, "No files to push")
    try:
        config    = AnypointConfig.from_env()
        publisher = AnypointPublisher(config, verbose=True)
        result    = publisher.push(
            project_name    = req.project_name or session.project_name,
            files           = session.files,
            skip_validation = req.skip_validation,
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Push failed: {e}")


# ── Lessons ────────────────────────────────────────────────────────────────────

@app.get("/lessons")
def list_lessons():
    from raml_agent.tools import _lesson_memory
    if _lesson_memory is None:
        return {"lessons": [], "count": 0,
                "warning": "Lesson memory not connected"}
    return {"lessons": _lesson_memory.list_all(),
            "count":   _lesson_memory.count}


@app.delete("/lessons/{lesson_id}")
def delete_lesson(lesson_id: str):
    from raml_agent.tools import _lesson_memory
    if _lesson_memory is None:
        raise HTTPException(503, "Lesson memory not available")
    _lesson_memory.delete(lesson_id)
    return {"deleted": lesson_id}
