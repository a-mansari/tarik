"""Each cabinet's agent has its own memory: measured on the running n8n with the real model, not read
from n8n's code. Two throwaway agents, A and B, alike but for their name: A is told a secret, then
B, a new conversation with A, and B in A's own conversation are asked for it.

Needs n8n's MCP server (Settings > n8n MCP: on, and an access token):
  N8N_MCP_API_TOKEN   the token (Vault secret/tarik/shared, or the environment)
  N8N_MCP_API_URL     n8n's MCP address from this container (docker-compose.yml)
  N8N_MCP_PROJECT_ID  the project the throwaway agents are made in, default the first agent's (deleted at the end)
  N8N_MCP_CREDENTIAL  the model credential's id, default MiniMaxAnthr0p1c
Skipped without a token. Run: docker compose exec -T tarik-mcp python -m pytest tests/n8n"""

import json
import os
import uuid

import httpx
import pytest

from tarik.core import vault

URL = os.getenv("N8N_MCP_API_URL", "http://n8n:5678/mcp-server/http")
TOKEN = os.getenv("N8N_MCP_API_TOKEN") or (vault.settings()[0].get("N8N_MCP_API_TOKEN", "") if vault.ENABLED else "")
PROJECT = os.getenv("N8N_MCP_PROJECT_ID", "")
CREDENTIAL = os.getenv("N8N_MCP_CREDENTIAL", "MiniMaxAnthr0p1c")

pytestmark = pytest.mark.skipif(not TOKEN, reason="N8N_MCP_API_TOKEN not set")

INSTRUCTIONS = ("Tu es un agent de test. Quand on te donne un code secret, retiens-le et reponds OK. "
                "Quand on te demande le code secret, donne-le exactement s'il t'a ete donne, sinon reponds AUCUN. "
                "N'invente jamais de code.")


class N8nMcp:
    """n8n's MCP server, spoken over plain HTTP JSON-RPC."""

    def __init__(self):
        self.http = httpx.Client(timeout=300, headers={"authorization": f"Bearer {TOKEN}",
                                                       "accept": "application/json, text/event-stream"})
        answer = self.post({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "tarik-tests", "version": "1"}}})
        self.session = answer.headers.get("mcp-session-id")
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def post(self, message):
        headers = {"mcp-session-id": self.session} if getattr(self, "session", None) else {}
        answer = self.http.post(URL, json=message, headers=headers)
        answer.raise_for_status()
        return answer

    def tool(self, tool, /, **arguments):
        answer = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
        text = answer.text
        if "data:" in text:
            text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
        result = json.loads(text)["result"]
        content = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        return content

    def ask(self, agent_id, message, session_id=None):
        request = {"type": "message", "message": message, **({"sessionId": session_id} if session_id else {})}
        return self.tool("call_agent", agentId=agent_id, request=request)


@pytest.fixture(scope="module")
def n8n():
    return N8nMcp()


@pytest.fixture(scope="module")
def agents(n8n):
    project = PROJECT or n8n.tool("search_agents")["data"][0]["projectId"]
    made = []
    for name in ("A", "B"):
        created = n8n.tool("create_agent", projectId=project, name=f"Test memoire {name} {uuid.uuid4().hex[:6]}", config={
            "model": "anthropic/MiniMax-M3", "credential": CREDENTIAL, "instructions": INSTRUCTIONS,
            "memory": {"enabled": True, "storage": "n8n"}})
        made.append(created.get("agentId") or created.get("id") or created["agent"]["id"])
    yield made
    for agent_id in made:
        n8n.tool("delete_agent", agentId=agent_id)


@pytest.fixture(scope="module")
def told(n8n, agents):
    """A, told a fresh secret in its conversation: the secret and that conversation's id."""
    secret = f"ZK{uuid.uuid4().hex[:8].upper()}"
    answer = n8n.ask(agents[0], f"Le code secret est {secret}. Retiens-le.")
    assert answer.get("ok"), answer
    session = answer["sessionId"]
    recalled = n8n.ask(agents[0], "Quel est le code secret ?", session)
    if secret not in json.dumps(recalled):
        pytest.skip("A does not recall its own secret: the model, not the memory, failed; nothing to compare")
    return secret, session


def test_another_agent_never_knows_it(n8n, agents, told):
    secret, _ = told
    answer = n8n.ask(agents[1], "Quel est le code secret ?")
    assert answer.get("ok"), answer
    assert secret not in json.dumps(answer)


def test_another_agent_cannot_enter_its_conversation(n8n, agents, told):
    secret, session = told
    answer = n8n.ask(agents[1], "Quel est le code secret ?", session)
    assert secret not in json.dumps(answer)
    assert not answer.get("ok") and answer.get("code") == "session_not_found", answer


def test_a_new_conversation_starts_empty(n8n, agents, told):
    secret, _ = told
    answer = n8n.ask(agents[0], "Quel est le code secret ?")
    assert answer.get("ok"), answer
    assert secret not in json.dumps(answer)
