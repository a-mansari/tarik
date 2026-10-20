"""The agent's model: MiniMax-M3 with a thinking budget, at the provider's Anthropic-style address, with this
stack's own key (Vault secret/tarik/orchestrator: MODEL_API_KEY, MODEL_API_URL optional)."""

from langchain_anthropic import ChatAnthropic

from engine.content import AGENT
from tarik.core.cabinets import setting, using
from tarik.core.model import MODEL_URL


def model(cabinet):
    """The model, for a run of ``cabinet`` (the settings are read within it)."""
    with using(cabinet):
        key, url = setting("MODEL_API_KEY", ""), setting("MODEL_API_URL", MODEL_URL)
    if not key:
        raise RuntimeError("MODEL_API_KEY is not set in Vault secret/tarik/orchestrator: the agent has no model")
    return ChatAnthropic(
        model=AGENT["model"],  # content/agent.json
        anthropic_api_url=url,
        api_key=key,
        max_tokens=65536,  # the thinking included
        # Without it, the agent makes decisions the client did not (tests/llm, 2026-10-19: 3 % wrong without, 1 % with).
        thinking={"type": "enabled", "budget_tokens": 1024},
        timeout=300,
        max_retries=2,
    )
