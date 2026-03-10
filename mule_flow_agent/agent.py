# mule_flow_agent/agent.py
import os

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from .prompts import (
    MULE_FLOW_AGENT_INSTRUCTION,
    MULE_FLOW_AGENT_DESCRIPTION,
)
from ..shared.session_store import SessionStore

MULE_FLOW_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


def create_mule_flow_agent(session_store: SessionStore) -> LlmAgent:
    """
    Build the Mule Flow specialist LlmAgent.

    Args:
        session_store: Shared SessionStore from the root agent.
    """
    # TODO: initialise Mule Flow-specific tools and inject session_store

    return LlmAgent(
        name        = "mule_flow_agent",
        model       = LiteLlm(model=f"anthropic/{MULE_FLOW_MODEL}"),
        description = MULE_FLOW_AGENT_DESCRIPTION,
        instruction = MULE_FLOW_AGENT_INSTRUCTION,
        tools       = [],   # TODO: add Mule Flow tools
    )
