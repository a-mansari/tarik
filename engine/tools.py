"""One call to a Tarik tool (tarik/), in the cabinet named: run in a worker thread (the tools are plain blocking
Python), inside using(cabinet), so it can only ever touch that cabinet's schema and settings."""

import asyncio
import inspect
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from tarik import catalogue  # noqa: F401 -- its import registers every tool
from tarik.core.app import CRASHED, TOOLS, ToolError, log
from tarik.core.cabinets import using

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
    """The tool refused (a ToolError), or failed: its message is the only text the client may read."""


async def call(cabinet, tool, /, **arguments):
    """The tool's result (its dict or its text); ToolFailure when it refused or failed (the cause logged)."""
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
    """``function(*args)``, one of tarik's plain functions (not a tool), run as a tool is: in its cabinet."""

    def run():
        with using(cabinet):
            return function(*args)

    return await asyncio.get_running_loop().run_in_executor(POOL, run)


async def step(cabinet, run_id, session, tool, /, **arguments):  # positional: a tool may take a run_id of its own
    """One step of a sequence: the tool called, its outcome (a failure as {status: error}) recorded for the run's
    summary (record_step). The ids it was given stay on a failure."""
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
    """step() in the run ``state`` is part of: its call's cabinet and conversation, its run id."""
    given = state["call"]
    return await step(given["cabinet"], state["run_id"], given["session_id"], tool, **arguments)


def failed(state):
    """True when the run's last outcome is not ok: the run goes no further than its summary ("resume")."""
    return state["outcomes"][-1].get("status") != "ok"


def ok_or_summary(following):
    """``following`` when the step before says ok, else the summary ("resume"). Its two ways out named, so the drawn
    graph shows them (LangGraph cannot see inside the lambda)."""
    return (lambda s: "resume" if failed(s) else following), [following, "resume"]


# Nodes every workflow (Traitement, Dossier) begins and ends with.


def started(inputs):
    """A run's first node, for a workflow taking ``inputs``: they are kept, the run gets its id (the summary's key),
    and the conversation's attachments are saved once each."""

    async def start(state, config):
        given = {key: state["call"].get(key) for key in inputs}
        run_id = str(config.get("run_id") or uuid.uuid4())
        session = given["session_id"]
        files = given.get("attachments") or []
        result = await step(given["cabinet"], run_id, session, "save_files", session_id=session, files=files)
        return {"call": given, "run_id": run_id, "outcomes": [result]}

    return start


async def sender_checked(state, dossier_id):
    """The email's sender may work on ``dossier_id`` (check_sender): its outcome."""
    result = await run_step(state, "check_sender", session_id=state["call"]["session_id"], dossier_id=dossier_id)
    return {"outcomes": [result]}


async def summary(state):
    """A run's last node: what the client reads of it (run_summary)."""
    result = await call(state["call"]["cabinet"], "run_summary", run_id=state["run_id"])
    return {"message": result["message"]}
