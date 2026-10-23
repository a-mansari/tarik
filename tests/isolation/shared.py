"""What the cabinet isolation tests share besides their fixtures (conftest.py): the two throwaway cabinets' names,
the files a conversation saves, and one tool call as the graphs make it (engine/calls.py)."""

import asyncio
import base64
import time

from engine.calls import ToolFailure, call
from engine.tools.core.app import TOOLS, tool
from engine.tools.core.cabinets import current
from tests import testdb

A, B = testdb.schema("a"), testdb.schema("b")  # in the test database, named for this run


def payload(session_id, *files):
    """save_files' arguments: the conversation and its files (source id, name, bytes)."""
    return {
        "session_id": session_id,
        "files": [
            {"id": source, "file_name": name, "mime_type": "application/pdf", "data": base64.b64encode(data).decode()}
            for source, name, data in files
        ],
    }


def which_cabinet(delay: float = 0.0) -> str:
    """Test tool: the cabinet this call runs for, after a pause that makes calls overlap."""
    time.sleep(delay)
    return current()


if "which_cabinet" not in TOOLS:
    tool(which_cabinet)


def call_text(cabinet, name, arguments):
    """The tool's answer as the agent or the client could read it: its result, or its refusal's text."""
    try:
        return str(asyncio.run(call(cabinet, name, **arguments)))
    except ToolFailure as failure:
        return str(failure)
