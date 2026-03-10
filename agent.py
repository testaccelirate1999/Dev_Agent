# agent.py
# ─────────────────────────────────────────────────────────────────────────────
# ADK entry point — must export `root_agent`.
#
# Run development UI:   adk web .
# Run API server:       adk api_server .
# Run in terminal:      adk run . "Create an Orders API"
# ─────────────────────────────────────────────────────────────────────────────

from master_agent.agent import create_master_agent

_master, _store = create_master_agent()

# ADK requires a module-level `root_agent` variable
root_agent = _master

# Expose store for FastAPI bridge (raml_server.py imports this)
session_store = _store
