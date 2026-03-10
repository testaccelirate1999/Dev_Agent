# raml_agent/tools.py
# ─────────────────────────────────────────────────────────────────────────────
# ADK FunctionTools for the RAML Agent.
#
# Each function is a plain Python callable — ADK reads the docstring to build
# the tool schema the LLM sees. Keep docstrings precise and args typed.
#
# Dependencies (rag, lesson_memory, session_store) are injected at startup
# by agent.py via _init_dependencies(). Never import them at module level.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

# Injected at startup by raml_agent/agent.py
_rag            = None
_lesson_memory  = None
_session_store  = None


def _init_dependencies(rag, lesson_memory, session_store):
    global _rag, _lesson_memory, _session_store
    _rag           = rag
    _lesson_memory = lesson_memory
    _session_store = session_store


# ─────────────────────────────────────────────────────────────────────────────

def fetch_raml_context(query: str) -> dict:
    """
    Search the RAML knowledge base for relevant examples and documentation.

    Call this FIRST before generating any RAML. Returns context to inject
    into generation so output follows established RAML 1.0 patterns.

    Args:
        query: What you need context for. E.g. "OAuth2 security scheme",
               "paginated collection resource", "nested data type".

    Returns:
        context_block (str): formatted context ready to pass to generate_raml_files.
        sources (list): source metadata [{file, type, score}].
        source_count (int): number of relevant chunks found.
    """
    from .raml_tools import _get_client  # noqa - ensures env checked early
    if _rag is None:
        return {"context_block": "", "sources": [], "source_count": 0}
    try:
        raw     = _rag.retrieve(query=query, top_k=5)
        context = _rag.retrieve_for_llm(query=query, top_k=5)
        sources = [
            {"file":   r["source_file"],
             "type":   r["source_type"],
             "detail": r.get("resource_path") or r.get("section", ""),
             "score":  round(r["score"], 3)}
            for r in raw
        ]
        return {"context_block": context,
                "sources": sources, "source_count": len(sources)}
    except Exception as e:
        return {"context_block": f"[RAG error: {e}]",
                "sources": [], "source_count": 0}


def fetch_learned_rules(query: str) -> dict:
    """
    Retrieve mandatory correction rules learned from past mistakes.

    Call this FIRST (alongside fetch_raml_context) before generating.
    Rules are hard constraints — violating them repeats known errors.

    Args:
        query: The user's request topic, used to find relevant lessons.

    Returns:
        rules_block (str): formatted <learned_rules> block for generation.
        lessons (list): raw lesson dicts [{correction, category, ...}].
        lesson_count (int): number of relevant lessons found.
    """
    if _lesson_memory is None:
        return {"rules_block": "", "lessons": [], "lesson_count": 0}
    try:
        lessons = _lesson_memory.retrieve(query=query)
        if not lessons:
            return {"rules_block": "", "lessons": [], "lesson_count": 0}
        rules = "\n".join(
            f"{i}. [{l['category'].upper()}] {l['correction']}"
            for i, l in enumerate(lessons, 1)
        )
        block = (
            "<learned_rules>\n"
            "MANDATORY — follow these rules from past corrections:\n\n"
            f"{rules}\n"
            "</learned_rules>"
        )
        return {"rules_block": block,
                "lessons": lessons, "lesson_count": len(lessons)}
    except Exception:
        return {"rules_block": "", "lessons": [], "lesson_count": 0}


def generate_raml_files(session_id: str, request: str,
                         context_block: str = "",
                         rules_block: str = "") -> dict:
    """
    Generate or update RAML 1.0 project files for the given session.

    Call AFTER fetch_raml_context and fetch_learned_rules.
    Writes all files to disk and returns full file contents + summary.

    Args:
        session_id:    Active session identifier.
        request:       The user's API design request, verbatim.
        context_block: Value of context_block from fetch_raml_context.
        rules_block:   Value of rules_block from fetch_learned_rules.

    Returns:
        message (str): one-line summary of what was generated or changed.
        files (dict): {path: content} for ALL files in the project.
        changed_files (list): paths created or modified this turn.
        deleted_files (list): paths removed this turn.
        is_first_turn (bool): True if this is the first generation.
    """
    from .raml_tools import generate_raml

    session = _session_store.get(session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found"}

    is_first      = not bool(session.files)
    result        = generate_raml(
        request       = request,
        context       = context_block,
        lessons_block = rules_block,
        current_files = session.files if not is_first else {},
    )
    new_files     = result.get("files", [])
    changed_files = result.get("changed_files", []) or [f["path"] for f in new_files]
    deleted_files = result.get("deleted_files", [])
    message       = result.get("message", "Done.")

    session.write_files(new_files, deleted_files)
    session.history.append({"role": "user",      "content": request})
    session.history.append({"role": "assistant", "content": message})
    session.save()

    return {
        "message":       message,
        "files":         dict(session.files),
        "changed_files": changed_files,
        "deleted_files": deleted_files,
        "is_first_turn": is_first,
    }


def validate_raml_files(session_id: str) -> dict:
    """
    Run static RAML 1.0 validation on all files in the session.

    No LLM call — purely static analysis. Call after generate_raml_files.

    Args:
        session_id: Active session identifier.

    Returns:
        valid (bool): True if zero hard errors.
        error_count (int): number of hard errors.
        warning_count (int): number of warnings.
        errors (list): [{file, line, severity, message, rule}].
    """
    from .raml_tools import validate_raml

    session = _session_store.get(session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found"}
    return validate_raml(session.files)


def fix_raml_errors(session_id: str, rules_block: str = "") -> dict:
    """
    Automatically fix validation errors in the current session.

    Fixes hard errors only (not warnings). Run validate_raml_files first
    to check whether fixing is needed.

    Args:
        session_id:  Active session identifier.
        rules_block: Pass the same rules_block used in generate_raml_files.

    Returns:
        message (str): description of fixes applied.
        files (dict): updated {path: content} for all project files.
        changed_files (list): paths that were modified.
        errors_remaining (int): hard error count after fixing.
    """
    from .raml_tools import validate_raml, fix_raml_errors as _fix

    session = _session_store.get(session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found"}

    val         = validate_raml(session.files)
    hard_errors = [e for e in val["errors"] if e["severity"] == "error"]
    if not hard_errors:
        return {"message": "No errors to fix.", "files": dict(session.files),
                "changed_files": [], "errors_remaining": 0}

    result      = _fix(errors=hard_errors, current_files=session.files,
                       lessons_block=rules_block)
    fix_files   = result.get("files", [])
    fix_changed = result.get("changed_files", []) or [f["path"] for f in fix_files]

    session.write_files(fix_files, result.get("deleted_files", []))
    session.save()

    val2 = validate_raml(session.files)
    return {
        "message":          result.get("message", "Fix applied."),
        "files":            dict(session.files),
        "changed_files":    fix_changed,
        "errors_remaining": val2["error_count"],
    }


def get_session_files(session_id: str) -> dict:
    """
    Retrieve all current file contents for a session without regenerating.

    Use when the user asks to view or reference existing files.

    Args:
        session_id: Active session identifier.

    Returns:
        files (dict): {path: content} for all files.
        file_count (int): total number of files.
        file_list (list): just the paths, for quick overview.
    """
    session = _session_store.get(session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found"}
    return {
        "files":      dict(session.files),
        "file_count": len(session.files),
        "file_list":  list(session.files.keys()),
    }


def save_correction_lesson(session_id: str, last_agent_reply: str,
                            user_feedback: str) -> dict:
    """
    Detect if the user is correcting a mistake and persist it as a lesson.

    Call on feedback/edit turns only — not on the first generation turn.
    Saved lessons are immediately active for all future sessions.

    Args:
        session_id:       Active session identifier.
        last_agent_reply: The agent's previous response text.
        user_feedback:    The user's follow-up message.

    Returns:
        saved (bool): True if a correction lesson was extracted and saved.
        lesson (dict | None): {id, mistake, correction, category} or None.
    """
    from .raml_tools import extract_and_save_lesson

    session      = _session_store.get(session_id)
    project_name = session.project_name if session else ""
    result       = extract_and_save_lesson(
        lesson_memory    = _lesson_memory,
        last_agent_reply = last_agent_reply,
        user_feedback    = user_feedback,
        project_name     = project_name,
    )
    return {"saved": result is not None, "lesson": result}


def push_to_anypoint(session_id: str,
                     skip_validation: bool = False) -> dict:
    """
    Push all session files to Anypoint Platform Design Center.

    Requires ANYPOINT_USERNAME, ANYPOINT_PASSWORD, ANYPOINT_ORG_ID in env.

    Args:
        session_id:      Active session identifier.
        skip_validation: Skip pre-push validation errors if True.

    Returns:
        success (bool), project_id (str), project_url (str),
        action (str: "created"|"updated"), file_count (int).
        On failure: success=False, error (str).
    """
    from .anypoint_publisher import AnypointPublisher, AnypointConfig

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


# ── Exported list for agent.py ────────────────────────────────────────────────

RAML_TOOLS = [
    fetch_raml_context,
    fetch_learned_rules,
    generate_raml_files,
    validate_raml_files,
    fix_raml_errors,
    get_session_files,
    save_correction_lesson,
    push_to_anypoint,
]
