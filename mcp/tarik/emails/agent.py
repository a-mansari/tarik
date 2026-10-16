"""Tarik's answer to an email thread: the cabinet's own native n8n agent (its AGENT_ID), asked through
n8n's MCP server (call_agent, at N8N_MCP_API_URL) in the thread's conversation. That conversation's id is
the thread's session (newSession, n8n/patch.js), under which its files and sender are already saved: the
agent's tools find them there. n8n's address and token stay on the server, as every setting."""

import json
import os

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import log, ok, server
from tarik.core.cabinets import cabinet_db, setting
from tarik.core.config import N8N_TIMEOUT
from tarik.core.conversation import FILES_NOTE, NO_TEXT, remember_client_message

NO_ANSWER = "Tarik n'a pas pu repondre pour le moment."


def call_n8n(tool, **arguments):
    """One tool of n8n's MCP server, called with ``arguments``; its structured result (dict)."""
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    headers = {
        "authorization": f"Bearer {setting('N8N_MCP_API_TOKEN')}",
        "accept": "application/json, text/event-stream",
    }
    answer = httpx.post(os.environ["N8N_MCP_API_URL"], timeout=N8N_TIMEOUT, json=request, headers=headers)
    answer.raise_for_status()
    text = answer.text
    if "data:" in text:  # an event stream: the answer is its "data:" line
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    result = json.loads(text)["result"]
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


@server.tool()
def ask_agent(session_id: str, message: str, files: list[str] | None = None) -> dict:
    """Tarik's reply to an email thread's new messages (``message``, with the names of the ``files`` that came
    with them), from the cabinet's agent, in the thread's own conversation (``session_id``)."""
    with cabinet_db() as db:
        if not db.execute("select 1 from tarik_mail_threads where session_id = %s", (session_id,)).fetchone():
            raise ToolError("Conversation email inconnue.")
    remember_client_message(session_id, message)
    text = (message or NO_TEXT) + (f"{FILES_NOTE}{', '.join(files)}" if files else "")
    request = {"type": "message", "message": text, "sessionId": session_id, "newSession": True}
    try:
        answer = call_n8n("call_agent", agentId=setting("AGENT_ID"), request=request)
    except Exception as error:
        log(f"ask_agent {session_id}: {error}")
        raise ToolError(NO_ANSWER) from None
    if not answer.get("ok") or answer.get("status") != "completed" or not answer.get("response"):
        log(f"ask_agent {session_id}: {answer.get('status')} {answer.get('code')} {answer.get('message')}")
        raise ToolError(NO_ANSWER)
    return ok("Reponse de Tarik.", output=answer["response"])
