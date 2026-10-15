"""What the cabinet isolation tests share besides their fixtures (conftest.py): the two throwaway
cabinets' names, a file payload, tarik-mcp served on a local port, and one tool call over HTTP."""

import base64
import contextlib
import json
import socket
import threading
import time

import httpx
import uvicorn

from tarik.core.app import server
from tarik.core.cabinets import current

A, B = "tarik_test_a", "tarik_test_b"


def payload(session_id, *files):
    return base64.b64encode(json.dumps({"session_id": session_id, "files": [
        {"id": source, "file_name": name, "mime_type": "application/pdf", "data": base64.b64encode(data).decode()}
        for source, name, data in files]}).encode()).decode()


def which_cabinet(delay: float = 0.0) -> str:
    """Test tool: the cabinet this call runs for, after a pause that makes calls overlap."""
    time.sleep(delay)
    return current()


@contextlib.contextmanager
def serving(app):
    """``app`` on a free local port, in a thread: its base URL."""
    if "which_cabinet" not in {tool.name for tool in server._tool_manager.list_tools()}:
        server.tool()(which_cabinet)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    running = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    while not running.started:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        running.should_exit = True
        thread.join(5)


def call(base, path, tool, arguments, client=None, headers=None):
    """One tools/call over the streamable HTTP transport; the tool's result, or the HTTP status."""
    answer = (client or httpx).post(f"{base}{path}", timeout=60, headers={
        "accept": "application/json, text/event-stream", "content-type": "application/json", **(headers or {})},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    if answer.status_code != 200:
        return answer.status_code
    data = next(line[5:] for line in answer.text.splitlines() if line.startswith("data:"))
    result = json.loads(data)["result"]
    assert not result.get("isError"), result
    structured = result.get("structuredContent")
    if structured is None:
        text = result["content"][0]["text"]
        return json.loads(text) if text.startswith("{") else text
    return structured["result"] if set(structured) == {"result"} else structured
