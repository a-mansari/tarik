"""The tool registry (TOOLS: every tool the graphs and the agent call, by name), the step results a step tool returns,
the refusal the client may read (ToolError), and the log.

A step result is a dict {"status", "message", ...}: ok (go on), wait (Experio is still reading: retry later),
question (ask the client) or stop (nothing more to do)."""

import functools
import inspect

TOOLS = {}  # name: the function


CRASHED = "échec technique, réessayez plus tard."  # what the client reads of a tool's crash (its cause is logged)


class ToolError(Exception):
    """A tool's refusal: its message is the only text of a failure that reaches the client or the agent."""


def tool(function):
    """Decorator: register ``function`` in TOOLS under its own name."""
    TOOLS[function.__name__] = function
    return function


def log(text):
    """Print ``text`` to the container's log, flushed at once; the agent never sees it."""
    print(text, flush=True)


def _result(status):
    """Return a maker of step results with ``status``: (message, **extra) -> {"status", "message", **extra}."""
    return lambda message, **extra: {"status": status, "message": message, **extra}


# What a step tool returns, so the workflow can route on ``status`` and the agent can read ``message``:
# ok (go on), wait (Experio still reading: retry), question (ask the client), stop (nothing more to do on it).
ok, wait, question, stop = map(_result, ("ok", "wait", "question", "stop"))


def routine(message, **extra):
    """Return a routine result: an ok result the client need not read (nothing to do, nothing found, the expected
    setting). It is recorded like any other result and left out of the run's summary (run_summary) unless it is
    where the invoice stands."""
    return ok(message, routine=True, **extra)


def with_ids(function):
    """Decorator: copy the invoice_id, file_id and dossier_id a tool was called with into its dict result (keys it
    already has are kept), so every result names the invoice, file or dossier it is about."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    def called(*args, **kwargs):
        result = function(*args, **kwargs)
        if isinstance(result, dict):
            given = signature.bind(*args, **kwargs).arguments
            for name in ("invoice_id", "file_id", "dossier_id"):
                if name in given:
                    result.setdefault(name, given[name])
        return result

    return called


def step_tool(function):
    """Decorator: register a workflow step as a tool (tool) whose results carry its ids (with_ids)."""
    return tool(with_ids(function))
