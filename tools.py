# master_agent/tools.py
# ─────────────────────────────────────────────────────────────────────────────
# Master Agent tools — things the master handles directly without delegating.
#
# The master agent uses AgentTool(raml_specialist) for all RAML work.
# These tools handle everything else:
#   - Session lifecycle (create / list / delete)
#   - General API design advice (no RAML generation)
#   - Project status overview
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

_session_store = None   # shared.session_store.SessionStore


def _set_store(store):
    global _session_store
    _session_store = store


# ─────────────────────────────────────────────────────────────────────────────
# Tool: create_project_session
# ─────────────────────────────────────────────────────────────────────────────

def create_project_session(project_name: str) -> dict:
    """
    Create a new RAML project session.

    Must be called BEFORE any RAML generation. Returns a session_id that
    must be passed to all raml_specialist tool calls.

    Args:
        project_name: Human-readable name for the API project.
                      E.g. "Orders API", "Payment Gateway", "User Management".

    Returns:
        dict with keys:
          - session_id (str): unique identifier for this project session
          - project_name (str): confirmed project name
          - created_at (str): ISO timestamp
    """
    session = _session_store.create(project_name)
    return {
        "session_id":   session.session_id,
        "project_name": session.project_name,
        "created_at":   session.created_at,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool: list_project_sessions
# ─────────────────────────────────────────────────────────────────────────────

def list_project_sessions() -> dict:
    """
    List all existing project sessions.

    Use this to find an existing session_id when the user refers to a
    previous project ("my orders API", "the one I made yesterday").

    Returns:
        dict with keys:
          - sessions (list): list of session summary dicts
          - count (int): total number of sessions
    """
    sessions = _session_store.list_all()
    return {"sessions": sessions, "count": len(sessions)}


# ─────────────────────────────────────────────────────────────────────────────
# Tool: get_project_status
# ─────────────────────────────────────────────────────────────────────────────

def get_project_status(session_id: str) -> dict:
    """
    Get the current status of a project: files, validation state, history.

    Use this when the user asks "what have we built so far?", "show me the
    project status", or needs a summary before continuing work.

    Args:
        session_id: The session to inspect.

    Returns:
        dict with keys:
          - project_name (str)
          - file_count (int)
          - file_list (list): paths of all files
          - turn_count (int): number of conversation turns
          - session_id (str)
    """
    session = _session_store.get(session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found"}
    return session.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# Tool: delete_project_session
# ─────────────────────────────────────────────────────────────────────────────

def delete_project_session(session_id: str) -> dict:
    """
    Delete a project session and all its files.

    Args:
        session_id: The session to delete.

    Returns:
        dict with keys:
          - deleted (bool)
          - session_id (str)
    """
    if not _session_store.get(session_id):
        return {"deleted": False, "error": "Session not found"}
    _session_store.delete(session_id)
    return {"deleted": True, "session_id": session_id}


# ─────────────────────────────────────────────────────────────────────────────
# All master-level tools (RAML specialist added via AgentTool in agent.py)
# ─────────────────────────────────────────────────────────────────────────────

ALL_MASTER_TOOLS = [
    create_project_session,
    list_project_sessions,
    get_project_status,
    delete_project_session,
]
