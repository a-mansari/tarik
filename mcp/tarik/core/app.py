"""The MCP server every tool module registers its tools on, what its step tools return, and the log."""

import functools
import inspect

from mcp.server.mcpserver import MCPServer

server = MCPServer("tarik")


def log(text):
    """Print ``text`` to the container's log at once (never to the agent)."""
    print(text, flush=True)


def _result(status):
    """A step result maker: message (str) and extra fields -> {"status": status, "message": message, **extra}."""
    return lambda message, **extra: {"status": status, "message": message, **extra}


# What a step tool returns, so the n8n workflow can route on ``status`` and the agent can read ``message``:
# ok (go on), wait (Experio still reading: retry), question (ask the client), stop (nothing more to do on it).
ok, wait, question, stop = map(_result, ("ok", "wait", "question", "stop"))


def routine(message, **extra):
    """An ok outcome the client needn't read (nothing to do, nothing found, the expected setting): recorded as any
    other, left out of the run's summary unless it is where the invoice stands (run_summary)."""
    return ok(message, routine=True, **extra)


def with_ids(tool):
    """Decorator: copy the ids a step was called with (invoice_id, file_id, dossier_id) into its dict result,
    so each n8n item still says which invoice it is about after the MCP node replaced it."""
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
