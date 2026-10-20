"""The tools every module registers (TOOLS, by name: what the graphs and the agent call), what a step tool returns,
the refusal a client may read (ToolError), and the log."""

import functools
import inspect

TOOLS = {}  # name: the function


class ToolError(Exception):
    """A tool's refusal: its message is the only text the client or the agent may read."""


def tool(function):
    """Decorator: ``function`` registered as a tool under its own name."""
    TOOLS[function.__name__] = function
    return function


def log(text):
    """Print ``text`` to the container's log at once (never to the agent)."""
    print(text, flush=True)


def _result(status):
    """A step result maker: message (str) and extra fields -> {"status": status, "message": message, **extra}."""
    return lambda message, **extra: {"status": status, "message": message, **extra}


# What a step tool returns, so the workflow can route on ``status`` and the agent can read ``message``:
# ok (go on), wait (Experio still reading: retry), question (ask the client), stop (nothing more to do on it).
ok, wait, question, stop = map(_result, ("ok", "wait", "question", "stop"))


def routine(message, **extra):
    """An ok outcome the client needn't read (nothing to do, nothing found, the expected setting): recorded as any
    other, left out of the run's summary unless it is where the invoice stands (run_summary)."""
    return ok(message, routine=True, **extra)


def with_ids(function):
    """Decorator: copy the ids a tool was called with (invoice_id, file_id, dossier_id) into its dict result, so
    every outcome says which invoice, file or dossier it is about."""
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
    """Decorator: a step of the workflows, registered as a tool, its ids copied into its result (with_ids)."""
    return tool(with_ids(function))
