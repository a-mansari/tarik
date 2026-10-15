"""Tarik's answer to an email thread: the cabinet's own native n8n agent (its AGENT_ID), asked through
n8n's MCP server (call_agent, at N8N_MCP_API_URL) in the thread's conversation. That conversation's id is the thread's
session (newSession, n8n/patch.js), under which its files and sender are already saved: the agent's
tools find them there. n8n's address and token stay on the server, as every setting."""

import json
import os

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, server
from tarik.core.cabinets import cabinet_db, setting
from tarik.dossiers.onboarding import remember_client_message


def call_n8n(tool, **arguments):
    """One tool of n8n's MCP server; its structured result."""
    answer = httpx.post(os.environ["N8N_MCP_API_URL"], timeout=600, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
        headers={"authorization": f"Bearer {setting('N8N_MCP_API_TOKEN')}", "accept": "application/json, text/event-stream"})
    answer.raise_for_status()
    text = answer.text
    if "data:" in text:
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
    text = (message or "(pas de message)") + (f"\n\nFichiers joints : {', '.join(files)}" if files else "")
    try:
        answer = call_n8n("call_agent", agentId=setting("AGENT_ID"), request={
            "type": "message", "message": text, "sessionId": session_id, "newSession": True})
    except Exception as error:
        print(f"ask_agent {session_id}: {error}", flush=True)
        raise ToolError("Tarik n'a pas pu repondre pour le moment.") from None
    if not answer.get("ok") or answer.get("status") != "completed" or not answer.get("response"):
        print(f"ask_agent {session_id}: {answer.get('status')} {answer.get('code')} {answer.get('message')}", flush=True)
        raise ToolError("Tarik n'a pas pu repondre pour le moment.")
    return ok("Reponse de Tarik.", output=answer["response"])
