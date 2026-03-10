# dataweave_agent/agent.py
import os

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from .prompts import (
    DATAWEAVE_AGENT_INSTRUCTION,
    DATAWEAVE_AGENT_DESCRIPTION,
)
from ..shared.session_store import SessionStore

DATAWEAVE_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


def create_dataweave_agent(session_store: SessionStore) -> LlmAgent:
    """
    Build the DataWeave specialist LlmAgent.

    Args:
        session_store: Shared SessionStore from the root agent.
    """
    # TODO: initialise DataWeave-specific tools and inject session_store

    return LlmAgent(
        name        = "dataweave_agent",
        model       = LiteLlm(model=f"anthropic/{DATAWEAVE_MODEL}"),
        description = DATAWEAVE_AGENT_DESCRIPTION,
        instruction = DATAWEAVE_AGENT_INSTRUCTION,
        tools       = [],   # TODO: add DataWeave tools
    )
