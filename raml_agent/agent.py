# raml_agent/agent.py
# ─────────────────────────────────────────────────────────────────────────────
# RAML Agent — Google ADK LlmAgent
#
# This is the specialist sub-agent. It:
#   1. Fetches RAG context + learned rules (tools, no LLM)
#   2. Generates RAML files (LLM via tool)
#   3. Validates the output (tool, no LLM)
#   4. Fixes errors if found (LLM via tool)
#   5. Saves correction lessons on feedback turns (background, tool)
#
# Always returns BOTH:
#   - A structured summary message (text)
#   - Full file contents (in tool result / session state)
#
# Model: Claude via LiteLLM bridge (ANTHROPIC_MODEL env var).
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations
import os
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from shared.retriever      import RAGRetriever
from shared.lesson_memory  import LessonMemory
from shared.session_store  import SessionStore
from raml_agent.tools      import ALL_RAML_TOOLS, _set_dependencies

# ── Config ────────────────────────────────────────────────────────────────────

CLAUDE_MODEL  = os.getenv("ANTHROPIC_MODEL",    "claude-haiku-4-5-20251001")
INDEX_NAME    = os.getenv("PINECONE_INDEX_NAME", "raml-knowledge-base")
OUTPUT_DIR    = Path(os.getenv("RAML_OUTPUT_DIR", "output"))

RAML_AGENT_INSTRUCTION = """\
You are a RAML 1.0 API design specialist. Your job is to generate, validate,
and fix RAML API specification projects.

════════════════════════════════════
MANDATORY TOOL SEQUENCE (every generation turn):
════════════════════════════════════
1. fetch_learned_rules   — load hard constraints from past corrections
2. fetch_raml_context    — retrieve relevant RAML examples/docs from knowledge base
3. generate_raml_files   — generate or update the RAML project
4. validate_raml_files   — check for validation errors
5. fix_raml_errors       — if error_count > 0, fix them (then re-validate)
6. save_correction_lesson — ONLY on feedback turns (not first generation)

════════════════════════════════════
RESPONSE FORMAT (always include both):
════════════════════════════════════
After completing the tool sequence, respond with:

**Summary:** [one paragraph explaining what was generated/changed]

**Files Generated:**
- [list each file path]

**Validation:** [error_count errors, warning_count warnings — or "✓ Clean"]

**File Contents:**
[For each file, show the full content in a code block labelled with the path]

════════════════════════════════════
RULES:
════════════════════════════════════
- ALWAYS run the full tool sequence — never skip fetch_raml_context or fetch_learned_rules
- ALWAYS show full file contents in the response (not just a summary)
- On feedback turns, pass the previous assistant response as last_agent_reply
  to save_correction_lesson
- If the user asks to see files without generating, use get_session_files
- If the user asks to push to Anypoint, use push_to_anypoint
"""


def create_raml_agent(session_store: SessionStore = None) -> LlmAgent:
    """
    Build and return the RAML specialist LlmAgent.

    Sets up RAG retriever, lesson memory, and session store as tool dependencies.
    Safe to call once at startup — singletons are reused.

    Args:
        session_store: Optional pre-built SessionStore. Created if not provided.
    """
    # ── Infrastructure ────────────────────────────────────────────────────────
    store = session_store or SessionStore(OUTPUT_DIR)

    rag = None
    try:
        rag = RAGRetriever(index_name=INDEX_NAME, verbose=False)
        print("[RAMLAgent] RAG retriever ready")
    except Exception as e:
        print(f"[RAMLAgent] RAG unavailable (continuing without): {e}")

    lessons = None
    try:
        lessons = LessonMemory(index_name=INDEX_NAME, verbose=False)
        print("[RAMLAgent] Lesson memory ready")
    except Exception as e:
        print(f"[RAMLAgent] Lesson memory unavailable (continuing without): {e}")

    # Inject into tools module
    _set_dependencies(rag, lessons, store)

    # ── ADK Agent ─────────────────────────────────────────────────────────────
    agent = LlmAgent(
        name        = "raml_specialist",
        model       = LiteLlm(model=f"anthropic/{CLAUDE_MODEL}"),
        description = (
            "Generates, validates, and fixes RAML 1.0 API specifications. "
            "Use for: creating API projects, editing RAML files, fixing "
            "validation errors, pushing to Anypoint Design Center."
        ),
        instruction = RAML_AGENT_INSTRUCTION,
        tools       = ALL_RAML_TOOLS,
    )

    return agent
