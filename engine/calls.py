"""Call Tarik's tools (engine/tools/) from the graphs, one cabinet at a time.

Each call runs in a worker thread (the tools are plain blocking Python) inside using(cabinet), so it only ever
touches that cabinet's schema and settings. Also holds the nodes every workflow begins and ends with."""

import asyncio
import contextvars
import inspect
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from engine.tools import catalogue  # noqa: F401 -- its import registers every tool
from engine.tools.core.app import CRASHED, TOOLS, ToolError, log
from engine.tools.core.cabinets import using

# At most PER_CABINET calls at once for one cabinet (wait_until_read holds one up to READ_WAIT), so a cabinet's long
# batch never takes every slot; at most TOTAL in all, the threads' pool sized to match.
PER_CABINET = int(os.getenv("TARIK_CALLS_PER_CABINET", "10"))
TOTAL = int(os.getenv("TARIK_CALLS_TOTAL", "40"))
POOL = ThreadPoolExecutor(TOTAL, thread_name_prefix="tool")
SLOTS = {}  # cabinet: its semaphore, made on its first call (in the running loop)
INVALID = "des informations manquent ou sont invalides pour cette etape."

# Steps whose ok answer is plumbing, not an outcome: recorded only when they fail or refuse, or say ``tell`` (an ok
# the client reads: the dossier taken for them, the attachment that never arrived).
QUIET_WHEN_OK = {
    "save_files",
    "files_to_process",
    "waiting_drafts",
    "waiting_setups",
    "check_sender",
    "check_creator",
    "identify_document",
    "split_invoices",
    "resolve_dossier",
}


class ToolFailure(Exception):
    """Raised when a tool refused (a ToolError) or crashed; its message is the only text the client may read."""


async def call(cabinet, tool, /, **arguments):
    """Run the tool named ``tool`` in ``cabinet`` with keyword ``arguments`` and return its result (a dict or a text).

    At most PER_CABINET calls run at once per cabinet.

    Raises:
        ToolFailure: the tool is unknown, the arguments don't fit its signature (INVALID), it refused (the
            ToolError's message) or it crashed (CRASHED). Other than a refusal, the cause is only logged.
    """
    function = TOOLS.get(tool)
    if function is None:
        raise ToolFailure(f"outil inconnu : {tool}")
    try:
        inspect.signature(function).bind(**arguments)
    except TypeError as error:  # the arguments don't fit: a workflow's or the model's mistake, never shown as is
        log(f"{cabinet} {tool}: {error}")
        raise ToolFailure(INVALID) from None
    slot = SLOTS.setdefault(cabinet, asyncio.Semaphore(PER_CABINET))
    async with slot:
        try:
            return await within(cabinet, lambda: function(**arguments))
        except ToolError as refusal:
            raise ToolFailure(str(refusal)) from None
        except Exception as error:
            log(f"{cabinet} {tool}: {type(error).__name__}: {error}")
            raise ToolFailure(CRASHED) from None


async def within(cabinet, function, /, *args):
    """Run ``function(*args)`` (a plain Tarik function, not a tool) the way a tool runs and return its result.

    It runs in the worker pool inside using(``cabinet``), in a copy of the caller's context (its run, for the model
    calls it makes: usage.RUNNING). No slot is taken and exceptions pass through as raised."""

    def run():
        with using(cabinet):
            return function(*args)

    return await asyncio.get_running_loop().run_in_executor(POOL, contextvars.copy_context().run, run)


async def step(cabinet, run_id, session, tool, /, **arguments):  # positional: a tool may take a run_id of its own
    """Call ``tool`` with keyword ``arguments`` as one step of run ``run_id`` and return its step result.

    Args:
        cabinet: the cabinet to run in.
        run_id: the run the outcome is recorded under (record_step), for its summary.
        session: the conversation's id (session_id), recorded with the outcome.
        tool: the tool's name.

    A ToolFailure becomes {"status": "error", "message": ...}, keeping any invoice_id, file_id or dossier_id it was
    given. The outcome is recorded, except an ok result of a QUIET_WHEN_OK tool without ``tell``. The first four
    are positional-only so a tool can take its own ``run_id`` keyword."""
    try:
        result = await call(cabinet, tool, **arguments)
    except ToolFailure as failure:
        ids = {key: arguments[key] for key in ("invoice_id", "file_id", "dossier_id") if key in arguments}
        result = {"status": "error", "message": str(failure), **ids}
    if not (tool in QUIET_WHEN_OK and result.get("status") == "ok" and not result.get("tell")):
        # An answer's outcome is the step it answers (answer_pending says which): recorded under answer_pending, a
        # question asked again could never be answered.
        origin = tool
        if tool == "answer_pending":
            origin = result.get("step") or tool
        await call(cabinet, "record_step", run_id=run_id, session_id=session, origin=origin, result=result)
    return result


async def run_step(state, tool, /, **arguments):
    """Call ``tool`` with keyword ``arguments`` through step(), taking the cabinet, session_id and run_id from the
    graph ``state`` (state["call"] and state["run_id"]). Returns the step result."""
    given = state["call"]
    return await step(given["cabinet"], state["run_id"], given["session_id"], tool, **arguments)


def failed(state):
    """True when the run's last outcome is not an ok result: the run then goes straight to its summary ("resume")."""
    return state["outcomes"][-1].get("status") != "ok"


def stops_at(target):
    """Decorator: mark a router whose route to ``target`` is taken only when a step was not ok.

    The canvas (engine.graphs.shape) leaves those routes out of the drawing; every other route stays drawn."""

    def marked(router):
        router.stops_at = target
        return router

    return marked


def ok_or_summary(following):
    """Return (router, routes) for add_conditional_edges: the router goes to ``following`` when the last step was
    ok, else to the summary ("resume").

    The routes [following, "resume"] are named so the drawn graph shows them (LangGraph cannot see inside a lambda)."""
    return stops_at("resume")(lambda s: "resume" if failed(s) else following), [following, "resume"]


# Nodes every workflow (Traitement, Dossier) begins and ends with.


def started(inputs):
    """Return a workflow's first graph node, for a workflow whose inputs are the key names ``inputs``.

    The node keeps only those keys of state["call"] (any other is dropped), sets run_id (the config's, else a new
    uuid; the summary's key) and saves the call's attachments (save_files), its result as the first outcome."""

    async def start(state, config):
        given = {key: state["call"].get(key) for key in inputs}
        run_id = str(config.get("run_id") or uuid.uuid4())
        session = given["session_id"]
        files = given.get("attachments") or []
        result = await step(given["cabinet"], run_id, session, "save_files", session_id=session, files=files)
        return {"call": given, "run_id": run_id, "outcomes": [result]}

    return start


async def sender_checked(state, dossier_id):
    """Check that the email's sender may work on ``dossier_id`` (check_sender) and return {"outcomes": [result]}."""
    result = await run_step(state, "check_sender", session_id=state["call"]["session_id"], dossier_id=dossier_id)
    return {"outcomes": [result]}


async def summary(state):
    """Graph node: a run's last node; sets "message" to the run's summary for the client (run_summary)."""
    result = await call(state["call"]["cabinet"], "run_summary", run_id=state["run_id"])
    return {"message": result["message"]}
