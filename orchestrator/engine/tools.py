"""One call to a tarik-mcp tool at the cabinet's address: the same calls the n8n MCP Client nodes make."""

import asyncio
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from engine.settings import MCP

TIMEOUT = 700.0  # seconds: over ask_agent's 600 s and wait_until_read's 180 s


# Steps the n8n workflows record only when they fail or refuse (their ok answer is plumbing, not an outcome).
QUIET_WHEN_OK = {"save_files", "files_to_process", "waiting_drafts", "dossier_questions", "waiting_setups", "check_sender", "identify_document", "split_invoices", "resolve_dossier"}


class ToolFailure(Exception):
    """The tool refused (a ToolError): its message is the only text the client may read."""


# Tools a second call answers the same as the first: reads and plumbing. Only these are called again when the
# connection drops after the call was sent (it may have run). Any other tool fails as before: its error is recorded
# and a follow-up takes the draft up again (waiting_drafts), so nothing is written twice.
REPEATABLE = {
    "experio_connection", "list_dossiers", "find_dossier", "list_invoices", "list_collaborators", "route_message",
    "resolve_dossier", "files_to_process", "waiting_drafts", "waiting_setups", "dossier_questions", "run_summary",
    "record_step", "save_files", "check_sender", "claim_threads",
    "finish_thread",  # n8n retries it (3 tries, 5 s apart): left unfinished, its reply would be sent twice
}
ATTEMPTS, PAUSE = 3, 1.0  # seconds, doubled each time


async def call(cabinet, tool, /, **arguments):
    """The tool's result: its dict (structured content) or its text; ToolFailure when it failed. A dropped
    connection is tried again: always before the call was sent, after it only for a REPEATABLE tool."""
    for attempt in range(ATTEMPTS):
        sent = False
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as http:
                async with streamable_http_client(f"{MCP}/c/{cabinet}/mcp", http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        sent = True
                        result = await session.call_tool(tool, arguments, read_timeout_seconds=TIMEOUT)
            break
        except Exception:  # the connection (often an ExceptionGroup from the MCP client's task group)
            if attempt + 1 == ATTEMPTS or (sent and tool not in REPEATABLE):
                raise
            await asyncio.sleep(PAUSE * 2**attempt)
    text = "".join(getattr(block, "text", "") for block in result.content)
    if result.is_error:
        raise ToolFailure(text)
    structured = result.structured_content
    if isinstance(structured, dict) and "result" in structured and len(structured) == 1:
        return structured["result"]  # a tool returning a str
    return structured if structured is not None else json.loads(text)


async def step(cabinet, run_id, session, tool, /, **arguments):  # positional: a tool may take a run_id of its own
    """One step of a sequence, as the n8n workflows run it: the tool called, its outcome (a failure as
    {status: error}) recorded for the run's summary (record_step). The ids it was given stay on a failure."""
    try:
        result = await call(cabinet, tool, **arguments)
    except Exception as failure:  # refused, or the connection lost for good: recorded as n8n's error output does
        ids = {key: arguments[key] for key in ("invoice_id", "file_id", "dossier_id") if key in arguments}
        message = str(failure) if isinstance(failure, ToolFailure) else f"{tool} : connexion perdue ({type(failure).__name__})."
        result = {"status": "error", "message": message, **ids}
    if not (tool in QUIET_WHEN_OK and result.get("status") == "ok"):
        # An answer's outcome is the step it answers (answer_pending says which), as n8n records it from its router:
        # recorded under answer_pending, a question asked again could never be answered.
        origin = result.get("step") or tool if tool == "answer_pending" else tool
        await call(cabinet, "record_step", run_id=run_id, session_id=session, origin=origin, result=result)
    return result
