"""The agent's model: MiniMax-M3 with a thinking budget, reached with this stack's own key when Vault holds one
(secret/tarik/langgraph), else through n8n's "Tarik - Model" webhook."""

from functools import cached_property

import anthropic
import httpx2  # the Anthropic SDK's own HTTP package
from langchain_anthropic import ChatAnthropic

from engine.content import AGENT
from engine.settings import own, shared

# The model: with this stack's own key (Vault secret/tarik/langgraph), straight to the provider; without it, through
# "Tarik - Model" (workflows/build_tarik_model.py), where n8n sends the request on with the agents' own credential.
# That webhook answers in one piece: no streaming.
MODEL_HOOK = "http://n8n:5678/webhook/tarik-model"
MODEL_URL = "https://api.minimax.io/anthropic"  # the provider's Anthropic-style address, as "Tarik - Model" uses it


class ThroughN8n(ChatAnthropic):
    """ChatAnthropic whose requests go to "Tarik - Model" instead of the provider's /v1/messages."""

    @cached_property
    def _client(self) -> anthropic.Client:
        def to_hook(request):
            request.url = httpx2.URL(MODEL_HOOK)

        http = httpx2.Client(timeout=self.default_request_timeout, event_hooks={"request": [to_hook]})
        return anthropic.Client(**{**self._client_params, "http_client": http})

    @cached_property
    def _async_client(self) -> anthropic.AsyncClient:
        async def to_hook(request):
            request.url = httpx2.URL(MODEL_HOOK)

        http = httpx2.AsyncClient(timeout=self.default_request_timeout, event_hooks={"request": [to_hook]})
        return anthropic.AsyncClient(**{**self._client_params, "http_client": http})


def model():
    settings = own()
    if settings.get("MODEL_API_KEY"):
        reach = {"anthropic_api_url": settings.get("MODEL_API_URL") or MODEL_URL, "api_key": settings["MODEL_API_KEY"]}
        kind = ChatAnthropic
    else:
        reach = {
            "anthropic_api_url": "http://n8n:5678",  # the host the requests are sent to: MODEL_HOOK
            "api_key": "through-n8n",  # unused: n8n adds the real key
            "default_headers": {"X-Tarik-Webhook": shared()["MAIL_WEBHOOK_SECRET"]},  # the secret tarik-mcp and n8n share
            "disable_streaming": True,
        }
        kind = ThroughN8n
    return kind(
        model=AGENT["model"],  # content/agent.json
        **reach,
        max_tokens=16384,  # the thinking included
        # As n8n sends it (its reasoning setting gives M3 a 1024-token budget): without it, the agent makes decisions the
        # client did not (mcp/tests/llm, 2026-10-19: 3 % wrong without, 1 % with).
        thinking={"type": "enabled", "budget_tokens": 1024},
        timeout=300,
        max_retries=2,
    )
