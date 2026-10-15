"""n8n's MCP server, for the tests that talk to the running n8n (skipped without its token)."""

import json
import os

import httpx
import pytest

from tarik.core import vault

URL = os.getenv("N8N_MCP_API_URL", "http://n8n:5678/mcp-server/http")
TOKEN = os.getenv("N8N_MCP_API_TOKEN") or (vault.settings()[0].get("N8N_MCP_API_TOKEN", "") if vault.ENABLED else "")


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

    def ask(self, agent_id, message, session_id=None, new=False):
        request = {"type": "message", "message": message, **({"sessionId": session_id} if session_id else {}),
                   **({"newSession": True} if new else {})}
        return self.tool("call_agent", agentId=agent_id, request=request)


@pytest.fixture(scope="module")
def n8n():
    if not TOKEN:
        pytest.skip("N8N_MCP_API_TOKEN not set")
    return N8nMcp()
