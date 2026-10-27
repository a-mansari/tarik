"""Each model call's tokens and cost, kept in the current cabinet's tarik_model_usage for the dashboard and the
Executions, Discussions and LLM Calls pages: the agent's calls (engine/agent.py) and the tools' (model.py), through
MiniMax or the Claude Code CLI."""

import contextvars

from engine.tools.core.app import log
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.queries import Q

# US dollars per million tokens: input, output, cache read. MiniMax-M3: platform.minimax.io/docs/guides/pricing-paygo
# (standard tier, 2026-10-26; no cache write price, so cache writes are counted as input). The Claude Code CLI prices
# its own calls (costUSD).
PRICES = {"MiniMax-M3": (0.30, 1.20, 0.06)}
LONG_PROMPT = 512_000  # MiniMax doubles every price for a call reading more input tokens than this

RUNNING = contextvars.ContextVar("running", default=None)  # the run whose steps are going on (engine/runs.py)
PROMPT_CALL = contextvars.ContextVar("prompt_call", default=None)  # the agent's call being made (engine/prompts.py)


def priced(model, fresh, cached, output):
    """Return the cost in US dollars of one call of ``model`` (PRICES), None when it has no price."""
    if model not in PRICES:
        return None
    rates = PRICES[model]
    double = 2 if fresh + cached > LONG_PROMPT else 1
    return double * (fresh * rates[0] + output * rates[1] + cached * rates[2]) / 1_000_000


def counted(model, fresh, cached, output, cost=None):
    """Keep one call's tokens in the current cabinet's tarik_model_usage, with the run (RUNNING) and the agent's
    prompt (PROMPT_CALL) it was made for.

    Args:
        model: the model as the provider names it.
        fresh: input tokens read fresh, cache writes included.
        cached: input tokens read from the provider's cache.
        output: output tokens, thinking included.
        cost: its cost in US dollars when the provider says it; else priced().

    Never raises: a failure is only logged, never the call's."""
    try:
        if cost is None:
            cost = priced(model, fresh, cached, output)
        with cabinet_db() as db:
            db.execute(Q.model_usage_counted, (model, fresh, cached, output, cost, RUNNING.get(), PROMPT_CALL.get()))
    except Exception as error:
        log(f"model usage not kept: {error!r}")


def anthropic_usage(model, usage):
    """Keep an Anthropic-style ``usage`` dict ({input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens}) of one call of ``model``."""
    fresh = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
    counted(model, fresh, usage.get("cache_read_input_tokens") or 0, usage.get("output_tokens") or 0)


def cli_usage(result):
    """Keep each model's tokens and cost from the Claude Code CLI's "result" event (its modelUsage)."""
    for model, used in (result.get("modelUsage") or {}).items():
        fresh = used.get("inputTokens", 0) + used.get("cacheCreationInputTokens", 0)
        counted(model, fresh, used.get("cacheReadInputTokens", 0), used.get("outputTokens", 0), used.get("costUSD"))
