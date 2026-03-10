# master_agent/agent.py
# ─────────────────────────────────────────────────────────────────────────────
# Master Agent — root LlmAgent that orchestrates everything.
#
# Responsibilities:
#   1. Route/classify user intent
#   2. Handle general API design questions directly
#   3. Manage project sessions (create/list/delete)
#   4. Delegate RAML generation/validation/fixing to raml_specialist via AgentTool
#   5. Delegate Anypoint push to raml_specialist via AgentTool
#
# The raml_specialist is wrapped as an AgentTool so the master calls it
# explicitly with full control over arguments (session_id, request, etc.)
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations
import os
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.agent_tool import AgentTool

from shared.session_store  import SessionStore
from raml_agent.agent      import create_raml_agent
from master_agent.tools    import ALL_MASTER_TOOLS, _set_store

# ── Config ────────────────────────────────────────────────────────────────────

# Master uses Sonnet for smarter routing; RAML sub-agent uses Haiku for speed/cost
MASTER_MODEL = os.getenv("MASTER_ANTHROPIC_MODEL",
                         os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"))
OUTPUT_DIR   = Path(os.getenv("RAML_OUTPUT_DIR", "output"))

MASTER_INSTRUCTION = """\
You are the API Design Platform assistant. You help users design, build,
and publish REST APIs using RAML 1.0 and Anypoint Platform.

════════════════════════════════════
YOUR CAPABILITIES:
════════════════════════════════════

1. GENERAL API DESIGN GUIDANCE
   Answer questions about REST API design, RAML 1.0 syntax, Anypoint Platform,
   security schemes, data modeling, versioning, pagination, etc.
   Handle these yourself — no tools needed.

2. PROJECT SESSION MANAGEMENT
   Use create_project_session / list_project_sessions / get_project_status /
   delete_project_session to manage the project lifecycle.

3. RAML GENERATION & VALIDATION (via raml_specialist tool)
   Delegate to raml_specialist for:
   - Generating new RAML projects
   - Editing or extending existing RAML
   - Fixing validation errors
   - Showing current file contents
   - Pushing projects to Anypoint Design Center

════════════════════════════════════
WORKFLOW FOR RAML REQUESTS:
════════════════════════════════════

Step 1 — Ensure a session exists:
  - If the user mentions an existing project, use list_project_sessions to find it
  - If this is a new project, use create_project_session first
  - Always confirm the session_id before calling raml_specialist

Step 2 — Call raml_specialist with a clear, complete request:
  Include in your call to raml_specialist:
    • The session_id
    • The user's full request (verbatim or expanded)
    • Any clarifications you've already gathered

Step 3 — Relay the response:
  Present the raml_specialist's output to the user, including:
    • The summary message
    • Validation status
    • Full file contents (raml_specialist always returns these)

════════════════════════════════════
ROUTING LOGIC:
════════════════════════════════════

Route to raml_specialist (via AgentTool) when user:
  - Wants to "create", "generate", "build", "design" an API
  - Wants to "edit", "update", "fix", "add to" existing RAML
  - Asks to "validate", "check errors", "push to Anypoint"
  - Asks to "show me the files", "what does the API look like"

Handle yourself when user:
  - Asks general questions: "what is RAML?", "how does OAuth2 work?"
  - Asks for advice: "what's the best way to model pagination?"
  - Asks about project status: "what sessions do I have?"

════════════════════════════════════
TONE & STYLE:
════════════════════════════════════
- Be concise and technical — users are API developers
- On RAML generation, always relay full file contents from raml_specialist
- If raml_specialist reports errors, explain them clearly to the user
"""


def create_master_agent() -> tuple[LlmAgent, SessionStore]:
    """
    Build the full multi-agent system.

    Returns:
        (master_agent, session_store) — pass session_store to FastAPI bridge.
    """
    # Shared session store — used by both master tools and RAML agent tools
    store = SessionStore(OUTPUT_DIR)

    # Wire master tools
    _set_store(store)

    # Build RAML specialist (shares the same session store)
    raml_agent = create_raml_agent(session_store=store)

    # Combine tools: master-level tools + raml_specialist as an AgentTool
    all_tools = ALL_MASTER_TOOLS + [AgentTool(agent=raml_agent)]

    master = LlmAgent(
        name        = "api_design_master",
        model       = LiteLlm(model=f"anthropic/{MASTER_MODEL}"),
        description = "Master API design assistant — routes requests, manages sessions, "
                      "and orchestrates RAML generation via the specialist sub-agent.",
        instruction = MASTER_INSTRUCTION,
        tools       = all_tools,
    )

    return master, store
