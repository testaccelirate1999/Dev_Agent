# Dev_Agent_system/agent.py
# ─────────────────────────────────────────────────────────────────────────────
# Root orchestrator + specialist sub-agents.
#
# Structure mirrors the project convention:
#   - One folder per agent, each owns its own prompts + tools
#   - Root only wires sub_agents together and holds session management tools
#   - shared/ holds cross-agent infra (RAG, lesson memory, session store)
#
# Run:  adk web .          →  ADK dev UI  at :8000
#       adk api_server .   →  ADK API server at :8000
#       adk run . "..."    →  single-turn terminal test
# ─────────────────────────────────────────────────────────────────────────────

import os
from pathlib import Path

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from .prompts                    import ROOT_AGENT_INSTRUCTION, ROOT_AGENT_DESCRIPTION
from .shared.session_store       import SessionStore
from .raml_agent.agent           import create_raml_agent
from .dataweave_agent.agent      import create_dataweave_agent
from .mule_flow_agent.agent      import create_mule_flow_agent

# ── Shared infrastructure ─────────────────────────────────────────────────────

OUTPUT_DIR    = Path(os.getenv("RAML_OUTPUT_DIR", "output"))
ROOT_MODEL    = os.getenv("ROOT_AGENT_MODEL",
                          os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"))

# Single SessionStore shared across all agents that need file state
session_store = SessionStore(OUTPUT_DIR)

# ── Session management tools (root-level, agent-agnostic) ─────────────────────

def create_project_session(project_name: str) -> dict:
    """
    Create a new project session before any file generation begins.

    Args:
        project_name: Human-readable name for the project. E.g. "Orders API".

    Returns:
        session_id (str), project_name (str), created_at (str).
    """
    session = session_store.create(project_name)
    return {"session_id":   session.session_id,
            "project_name": session.project_name,
            "created_at":   session.created_at}


def list_project_sessions() -> dict:
    """
    List all existing project sessions.

    Use this to find a session_id when the user refers to a previous project.

    Returns:
        sessions (list), count (int).
    """
    sessions = session_store.list_all()
    return {"sessions": sessions, "count": len(sessions)}


def get_project_status(session_id: str) -> dict:
    """
    Get current status of a project: file list, turn count, metadata.

    Args:
        session_id: The session to inspect.

    Returns:
        project_name, file_count, file_list, turn_count, session_id.
    """
    session = session_store.get(session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found"}
    return session.to_dict()


def delete_project_session(session_id: str) -> dict:
    """
    Delete a project session and all its files from disk.

    Args:
        session_id: The session to delete.

    Returns:
        deleted (bool), session_id (str).
    """
    if not session_store.get(session_id):
        return {"deleted": False, "error": "Session not found"}
    session_store.delete(session_id)
    return {"deleted": True, "session_id": session_id}


# ── Sub-agents ────────────────────────────────────────────────────────────────

raml_agent      = create_raml_agent(session_store=session_store)
dataweave_agent = create_dataweave_agent(session_store=session_store)
mule_flow_agent = create_mule_flow_agent(session_store=session_store)

# ── Root orchestrator ─────────────────────────────────────────────────────────

root_agent = Agent(
    name        = "dev_agent",
    model       = LiteLlm(model=f"anthropic/{ROOT_MODEL}"),
    description = ROOT_AGENT_DESCRIPTION,
    instruction = ROOT_AGENT_INSTRUCTION,
    tools       = [
        create_project_session,
        list_project_sessions,
        get_project_status,
        delete_project_session,
    ],
    sub_agents  = [raml_agent, dataweave_agent, mule_flow_agent],
)

# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Dev Agent System ===")
    print(f"Root : {root_agent.name}  (model: {ROOT_MODEL})")
    for sa in root_agent.sub_agents:
        print(f"  Sub: {sa.name}")
    print("Ready — run 'adk web .' to start.")
