# raml_agent/agent.py
# ─────────────────────────────────────────────────────────────────────────────
# RAML Agent — builds and returns the LlmAgent for RAML API design work.
#
# Owns: prompts, tools, RAG retriever, lesson memory, Anypoint publisher.
# Shares: session_store (injected from root so all agents use the same store).
#
# Usage (from Dev_Agent_system/agent.py):
#   from raml_agent.agent import create_raml_agent
#   raml_agent = create_raml_agent(session_store=store)
# ─────────────────────────────────────────────────────────────────────────────

import os
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from .prompts              import RAML_AGENT_INSTRUCTION, RAML_AGENT_DESCRIPTION
from .tools                import RAML_TOOLS, _init_dependencies
from ..shared.retriever    import RAGRetriever
from ..shared.lesson_memory import LessonMemory
from ..shared.session_store import SessionStore

RAML_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "raml-knowledge-base")


def create_raml_agent(session_store: SessionStore) -> LlmAgent:
    """
    Build the RAML specialist LlmAgent.

    Initialises RAG retriever and lesson memory, wires them into tools,
    then returns a configured LlmAgent ready to be added as a sub_agent.

    Args:
        session_store: Shared SessionStore from the root agent.
    """
    rag = None
    try:
        rag = RAGRetriever(index_name=INDEX_NAME, verbose=False)
        print("[RAMLAgent] RAG retriever ready")
    except Exception as e:
        print(f"[RAMLAgent] RAG unavailable (continuing without): {e}")

    lesson_memory = None
    try:
        lesson_memory = LessonMemory(index_name=INDEX_NAME, verbose=False)
        print("[RAMLAgent] Lesson memory ready")
    except Exception as e:
        print(f"[RAMLAgent] Lesson memory unavailable (continuing without): {e}")

    _init_dependencies(rag, lesson_memory, session_store)

    return LlmAgent(
        name        = "raml_agent",
        model       = LiteLlm(model=f"anthropic/{RAML_MODEL}"),
        description = RAML_AGENT_DESCRIPTION,
        instruction = RAML_AGENT_INSTRUCTION,
        tools       = RAML_TOOLS,
    )
