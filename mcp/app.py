"""The MCP server every phase module registers its tools on."""

import functools
import inspect

from mcp.server.mcpserver import MCPServer


server = MCPServer("tarik")


# What a step tool returns, so the n8n workflow can route on ``status`` and the agent can read ``message``:
# ok (go on), wait (Experio still reading: retry), question (ask the client), stop (nothing more to do on it).
def ok(message, **extra):
    return {"status": "ok", "message": message, **extra}


def wait(message, **extra):
    return {"status": "wait", "message": message, **extra}


def question(message, **extra):
    return {"status": "question", "message": message, **extra}


def stop(message, **extra):
    return {"status": "stop", "message": message, **extra}


def with_ids(tool):
    """Copy the ids a step was called with (invoice, file, dossier) into its result, so each
    n8n item still says which invoice it is about after the MCP node replaced it."""
    signature = inspect.signature(tool)

    @functools.wraps(tool)
    def step(*args, **kwargs):
        result = tool(*args, **kwargs)
        if isinstance(result, dict):
            called = signature.bind(*args, **kwargs).arguments
            for name in ("invoice_id", "file_id", "dossier_id"):
                if name in called:
                    result.setdefault(name, called[name])
        return result
    return step
