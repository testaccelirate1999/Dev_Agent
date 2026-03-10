# raml_agent/tools.py
# ─────────────────────────────────────────────────────────────────────────────
# ADK FunctionTool wrappers for all RAML capabilities.
#
# Each function is a plain Python function with full docstrings — ADK reads
# these docstrings to generate the tool schema the LLM sees.
#
# The RAG retriever and lesson memory are injected at agent construction time
# via module-level singletons (set by raml_agent/agent.py on startup).
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations
import json
from typing import Optional

# These are set by agent.py before any tool is called
_rag_retriever   = None
_lesson_memory   = None
_session_store   = None   # shared.session_store.SessionStore


def _set_dependencies(rag, lessons, store):
    global _rag_retriever, _lesson_memory, _session_store
    _rag_retriever = rag
    _lesson_memory = lessons
    _session_store = store


# ─────────────────────────────────────────────────────────────────────────────
# Tool: fetch_raml_context
# ─────────────────────────────────────────────────────────────────────────────

def fetch_raml_context(query: str) -> dict:
    """
    Search the RAML knowledge base for relevant examples and documentation.

    Use this FIRST before generating any RAML. The context helps produce
    correct RAML 1.0 syntax and follows established patterns.

    Args:
        query: Natural language description of what you need context for.
               E.g. "OAuth2 security scheme", "data type with nested object".

    Returns:
        dict with keys:
          - context_block (str): formatted context to inject into generation
          - sources (list): list of source metadata dicts {file, type, score}
          - source_count (int): number of relevant chunks found
    """
    from shared.raml_tools import tool_fetch_context
    context, sources = tool_fetch_context(_rag_retriever, query)
    return {
        "context_block": context,
        "sources":       sources,
        "source_count":  len(sources),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool: fetch_learned_rules
# ─────────────────────────────────────────────────────────────────────────────

def fetch_learned_rules(query: str) -> dict:
    """
    Retrieve mandatory correction rules learned from past mistakes.

    Always call this before generating RAML. Lessons are hard constraints —
    violating them repeats known errors.

    Args:
        query: The user's request or topic, used to find relevant lessons.

    Returns:
        dict with keys:
          - rules_block (str): formatted <learned_rules> block for the prompt
          - lessons (list): raw lesson dicts {correction, category, ...}
          - lesson_count (int): number of relevant lessons found
    """
    from shared.raml_tools import tool_fetch_lessons
    block, lessons = tool_fetch_lessons(_lesson_memory, query)
    return {
        "rules_block":  block,
        "lessons":      lessons,
        "lesson_count": len(lessons),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool: generate_raml_files
# ─────────────────────────────────────────────────────────────────────────────

def generate_raml_files(
    session_id:    str,
    request:       str,
    context_block: str = "",
    rules_block:   str = "",
) -> dict:
    """
    Generate or update RAML 1.0 project files for the given session.

    Call AFTER fetch_raml_context and fetch_learned_rules.
    Writes files to disk and returns full file contents.

    Args:
        session_id:    Active session identifier.
        request:       The user's API design request (verbatim).
        context_block: Context string from fetch_raml_context (optional).
        rules_block:   Rules string from fetch_learned_rules (optional).

    Returns:
        dict with keys:
          - message (str): one-line summary of what was generated/changed
          - files (dict): {path: content} for ALL files in the project
          - changed_files (list): paths of files created or modified
          - deleted_files (list): paths of files removed
          - is_first_turn (bool): True if this is the first generation
    """
    from shared.raml_tools import tool_generate_raml

    session = _session_store.get(session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found"}

    is_first = not bool(session.files)
    gen = tool_generate_raml(
        request       = request,
        context       = context_block,
        lessons_block = rules_block,
        current_files = session.files if not is_first else {},
    )

    new_files     = gen.get("files", [])
    changed_files = gen.get("changed_files", []) or [f["path"] for f in new_files]
    deleted_files = gen.get("deleted_files", [])
    agent_message = gen.get("message", "Done.")

    session.write_files(new_files, deleted_files)
    session.history.append({"role": "user",      "content": request})
    session.history.append({"role": "assistant", "content": agent_message})
    session.save()

    return {
        "message":       agent_message,
        "files":         dict(session.files),
        "changed_files": changed_files,
        "deleted_files": deleted_files,
        "is_first_turn": is_first,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool: validate_raml_files
# ─────────────────────────────────────────────────────────────────────────────

def validate_raml_files(session_id: str) -> dict:
    """
    Run static RAML 1.0 validation on all files in the session.

    Call this after generate_raml_files to check for errors.
    No LLM call — purely static analysis.

    Args:
        session_id: Active session identifier.

    Returns:
        dict with keys:
          - valid (bool): True if no hard errors
          - error_count (int): number of errors
          - warning_count (int): number of warnings
          - errors (list): list of {file, line, severity, message, rule}
    """
    from shared.raml_tools import tool_validate_raml

    session = _session_store.get(session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found"}

    return tool_validate_raml(session.files)


# ─────────────────────────────────────────────────────────────────────────────
# Tool: fix_raml_errors
# ─────────────────────────────────────────────────────────────────────────────

def fix_raml_errors(session_id: str, rules_block: str = "") -> dict:
    """
    Automatically fix validation errors found in the current session.

    Only fixes hard errors (not warnings). Call validate_raml_files first
    to check if there are errors worth fixing.

    Args:
        session_id:  Active session identifier.
        rules_block: Rules from fetch_learned_rules (pass same value as
                     used in generate_raml_files).

    Returns:
        dict with keys:
          - message (str): description of fixes applied
          - files (dict): updated {path: content} for all project files
          - changed_files (list): paths that were modified
          - errors_remaining (int): hard errors after fix attempt
    """
    from shared.raml_tools import tool_fix_errors, tool_validate_raml

    session = _session_store.get(session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found"}

    val         = tool_validate_raml(session.files)
    hard_errors = [e for e in val["errors"] if e["severity"] == "error"]
    if not hard_errors:
        return {
            "message":          "No errors to fix.",
            "files":            dict(session.files),
            "changed_files":    [],
            "errors_remaining": 0,
        }

    fix = tool_fix_errors(
        errors        = hard_errors,
        current_files = session.files,
        lessons_block = rules_block,
    )

    fix_files   = fix.get("files", [])
    fix_changed = fix.get("changed_files", []) or [f["path"] for f in fix_files]

    session.write_files(fix_files, fix.get("deleted_files", []))
    session.save()

    # Re-validate to report remaining errors
    val2 = tool_validate_raml(session.files)
    return {
        "message":          fix.get("message", "Fix applied."),
        "files":            dict(session.files),
        "changed_files":    fix_changed,
        "errors_remaining": val2["error_count"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool: save_correction_lesson
# ─────────────────────────────────────────────────────────────────────────────

def save_correction_lesson(
    session_id:       str,
    last_agent_reply: str,
    user_feedback:    str,
) -> dict:
    """
    Detect if the user is correcting a mistake and save it as a lesson.

    Call this on feedback/edit turns (not the first turn). If the user
    is correcting a mistake (not just adding features), the lesson is
    saved to Pinecone and applied to all future sessions.

    Args:
        session_id:       Active session identifier.
        last_agent_reply: The agent's previous response text.
        user_feedback:    The user's follow-up message.

    Returns:
        dict with keys:
          - saved (bool): True if a lesson was extracted and saved
          - lesson (dict|None): {id, mistake, correction, category} or None
    """
    from shared.raml_tools import tool_save_lesson

    session = _session_store.get(session_id)
    project_name = session.project_name if session else ""

    result = tool_save_lesson(
        lesson_memory   = _lesson_memory,
        last_agent_reply = last_agent_reply,
        user_feedback   = user_feedback,
        project_name    = project_name,
    )
    return {"saved": result is not None, "lesson": result}


# ─────────────────────────────────────────────────────────────────────────────
# Tool: get_session_files
# ─────────────────────────────────────────────────────────────────────────────

def get_session_files(session_id: str) -> dict:
    """
    Retrieve all current file contents for a session.

    Use this when the user asks to see, explain, or reference existing files.

    Args:
        session_id: Active session identifier.

    Returns:
        dict with keys:
          - files (dict): {path: content} for all files
          - file_count (int): total number of files
          - file_list (list): just the paths, for quick overview
    """
    session = _session_store.get(session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found"}

    return {
        "files":      dict(session.files),
        "file_count": len(session.files),
        "file_list":  list(session.files.keys()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool: push_to_anypoint
# ─────────────────────────────────────────────────────────────────────────────

def push_to_anypoint(
    session_id:      str,
    skip_validation: bool = False,
) -> dict:
    """
    Push all session files to Anypoint Platform Design Center.

    Requires ANYPOINT_USERNAME, ANYPOINT_PASSWORD, ANYPOINT_ORG_ID in env.
    Runs pre-push validation unless skip_validation=True.

    Args:
        session_id:      Active session identifier.
        skip_validation: If True, skip pre-push validation errors.

    Returns:
        dict with keys:
          - success (bool)
          - project_id (str)
          - project_url (str)
          - action (str): "created" or "updated"
          - file_count (int)
          - error (str): present only on failure
    """
    from shared.anypoint_publisher import AnypointPublisher, AnypointConfig

    session = _session_store.get(session_id)
    if not session:
        return {"success": False, "error": f"Session '{session_id}' not found"}
    if not session.files:
        return {"success": False, "error": "No files to push"}

    try:
        config    = AnypointConfig.from_env()
        publisher = AnypointPublisher(config, verbose=False)
        result    = publisher.push(
            project_name    = session.project_name,
            files           = session.files,
            skip_validation = skip_validation,
        )
        return {"success": True, **result}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# All tools exported for ADK registration
# ─────────────────────────────────────────────────────────────────────────────

ALL_RAML_TOOLS = [
    fetch_raml_context,
    fetch_learned_rules,
    generate_raml_files,
    validate_raml_files,
    fix_raml_errors,
    save_correction_lesson,
    get_session_files,
    push_to_anypoint,
]
