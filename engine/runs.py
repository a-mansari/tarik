"""The run store: every run of a graph, kept in Postgres (schema orchestrator) with its input, each node's update as
it ended, and how it ended. The UI reads runs from here, live ones included; the agent's conversations sit next to
them (the LangGraph checkpointer's tables, same schema)."""

import asyncio
import contextlib
import contextvars
import json
import re
import time
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import BaseMessage
from langchain_core.runnables.config import var_child_runnable_config
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from engine import agent, graphs, live
from engine.content import LABELS
from engine.knowledge import cgnc
from engine.settings import DEFAULT_CABINET
from engine.tools.core import usage
from engine.tools.core.queries import Q

SCHEMA = "orchestrator"
NAMES = sorted([*graphs.COMPILED, "agent"])  # the agent: made per run
SLOTS = asyncio.Semaphore(20)  # runs going on at once in this process
FOLDED = {"routine"}  # graphs whose nested runs the runs list leaves to their own page: one per dossier
DETAIL_LIMIT = 50_000  # characters of a node's update kept: a scan's text or a long history is cut, never the run

TIME_ZONE = ZoneInfo("Africa/Casablanca")  # the cabinets' time, from the server's own time-zone data
pool = None  # set by open_store()


def when(moment):
    """Return a time (datetime or ISO text) as "20/10 10:33:22" in Casablanca time; "" for None. Every page formats
    times here, so they all agree."""
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    return moment.astimezone(TIME_ZONE).strftime("%d/%m %H:%M:%S") if moment else ""


def jsonable(value):
    """Return ``value`` as JSON can carry it: a message as its fields (without response and usage metadata), dicts
    and lists converted item by item, any other non-JSON value as text."""
    if isinstance(value, BaseMessage):
        return value.model_dump(exclude={"response_metadata", "usage_metadata"})
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)


def capped(value):
    """Return ``value`` as Jsonb, or {"cut": its JSON text cut to DETAIL_LIMIT characters} when longer."""
    text = json.dumps(jsonable(value), ensure_ascii=False)
    return Jsonb(jsonable(value)) if len(text) <= DETAIL_LIMIT else Jsonb({"cut": text[:DETAIL_LIMIT]})


UNTRUSTED_TAGS = re.compile(r"</?untrusted_data[^>]*>")


def readable(text):
    """Return a tool's answer for display: the <untrusted_data> tags removed and, for a JSON answer with a "message",
    that message alone. The step's detail keeps the answer as the model got it."""
    text = UNTRUSTED_TAGS.sub("", str(text)).strip()
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        text = json.loads(text).get("message") or text
    return text


def outcome_of(update, node=""):
    """Return (status, line) summing up a node's update: the step's status and one line of at most 300 characters.

    A stop result of a node listed in labels.json's waiting_steps (waiting for the client) shows as "wait"; the
    step's detail keeps the stop."""
    status, line = raw_outcome(update)
    if status == "stop" and node in LABELS.get("waiting_steps", []):
        status = "wait"
    return status, line


def raw_outcome(update):
    """Return (status, line) for a node's update: the last outcome's status and message, a run's summary, the agent's
    last message, or an email thread step (thread_outcome); ("", "") for a non-dict update."""
    if not isinstance(update, dict):
        return "", ""
    outcomes = update.get("outcomes")
    last = update.get("last") or (outcomes[-1] if outcomes else None)
    status, line = "", ""
    if isinstance(last, dict):
        status, line = last.get("status", ""), last.get("message", "")
    if update.get("message"):  # a run's summary
        line = update["message"]
    messages = update.get("messages") or []
    if messages and not line:
        line = agent_line(messages[-1])
    if not line:
        status, line = thread_outcome(update, status)
    return status, " ".join(str(line).split())[:300]


def agent_line(message):
    """Return the agent's message in one line: "Utilise <tools>" for tool calls, a tool's answer (readable()), or the
    agent's text."""
    calls = getattr(message, "tool_calls", None)
    if calls:
        return "Utilise " + ", ".join(call["name"] for call in calls)
    if getattr(message, "type", "") == "tool":
        return readable(message.text if isinstance(message.text, str) else message.text())
    return str(getattr(message, "text", "") or "")


def thread_outcome(update, status):
    """Return (status, line) for an email thread step (emails.Steps): the first of error, sent, manager,
    acknowledged, reply or refusal message as line; ``status`` when given, else "error", "stop" for a refused
    sender, or ""."""
    refusal = update.get("refusal") or {}
    said = [update.get(key) for key in ("error", "sent", "manager", "acknowledged", "reply")]
    said.append(refusal.get("message"))
    line = next((text for text in said if isinstance(text, str) and text), "")
    if status:
        return status, line
    if update.get("error"):
        return "error", line
    if refusal.get("refused"):
        return "stop", line
    return "", line


async def open_store(conninfo):
    """Open the connection pool for the server's whole life and create the orchestrator schema and its tables, the
    knowledge's with them (engine/knowledge/)."""
    global pool
    pool = AsyncConnectionPool(
        f"{conninfo} options='-c search_path={SCHEMA}'", open=False, max_size=20, kwargs={"autocommit": True}
    )  # 20, not psycopg's 4: every page read and every agent model call's prompt save share it
    await pool.open()
    async with pool.connection() as db:
        await db.execute(Q.orchestrator_schema)
    await asyncio.to_thread(cgnc.load, conninfo)  # the CGNC Tarik looks up, reloaded when its files changed


async def end_cut_off():
    """End in error every run still queued or running (cut off by a restart). Only the server's start may call it:
    terminal commands open the store while the server runs."""
    async with pool.connection() as db:
        await db.execute(Q.runs_cut_off)


RUNNING = usage.RUNNING  # the run whose steps are going on: a run started in one of them is its child, and the model
# calls made in them are its (the context reaches every task the graph starts for its steps, and their tools' threads)
# True in an unattended run (a routine's period) and every run nested in it, the same way: nobody there to say yes.
# Its runs read it in their configurable ("unattended"), never from their conversation's id.
UNATTENDED = contextvars.ContextVar("unattended", default=False)


async def created(name, given, cabinet, session, thread=None, parent=None, parent_node=None):
    """Record a new queued run of graph ``name`` and return its id.

    Args:
        given: the run's input.
        cabinet, session: the cabinet and conversation it works for.
        thread: the agent's thread id, None for a workflow.
        parent, parent_node: the run and step it was started from, for a nested run.
    """
    run_id = str(uuid.uuid4())
    async with pool.connection() as db:
        await db.execute(
            Q.run_created,
            (run_id, name, cabinet, session, thread, Jsonb(jsonable(given)), parent, parent_node),
        )
    live.touched(run_id, cabinet, session)
    return run_id


async def start(name, payload, slots=SLOTS, unattended=False):
    """Record a new run of graph ``name`` and start it in the background; return (run_id, task). ``slots``: the
    semaphore it runs under (execute), None for a run whose work is limited otherwise (a routine's period).
    ``unattended``: a run nobody watches (a routine's period), it and every run nested in it (UNATTENDED).

    ``payload`` is the POST /runs body: {input, session_id?, thread_id?, cabinet?}. The session and cabinet default
    to the input's call, then to a new id and DEFAULT_CABINET; only the agent gets a thread (thread_id, else the
    session)."""
    given = payload.get("input") or {}
    call = given.get("call") or {}  # a workflow's "Appel" inputs: its conversation and cabinet are there
    session = payload.get("session_id") or call.get("session_id") or payload.get("thread_id") or str(uuid.uuid4())
    cabinet = payload.get("cabinet") or call.get("cabinet") or given.get("cabinet") or DEFAULT_CABINET
    thread = (payload.get("thread_id") or session) if name == "agent" else None
    run_id = await created(name, given, cabinet, session, thread)
    running = execute(run_id, name, cabinet, session, thread, given, slots=slots, unattended=unattended)
    task = asyncio.create_task(running)
    return run_id, task


async def nested(name, given, cabinet, session, thread=None, node=None):
    """Run graph ``name`` on ``given`` from inside a step of the current run and return its output.

    The nested run (a workflow the agent runs, a thread handed to the agent) is recorded as a run of its own, with
    the current run as parent and ``node`` as the step it started from. Its failure is raised, as a direct call to
    the graph would. Outside the server (no pool) the graph just runs, unrecorded."""
    if pool is None:  # outside the server (a script, a test): run as is, nothing recorded
        configurable = configurable_of(cabinet, session, thread)
        return await graph_of(name, configurable).ainvoke(given, {"configurable": configurable})
    run_id = await created(name, given, cabinet, session, thread, RUNNING.get(), node)
    # The step's own config travels in the context: left there, the graph would run as a part of the step's run, its
    # steps and output that run's. Cleared, it is a run of its own.
    detached = var_child_runnable_config.set(None)
    try:
        output, failure = await execute(run_id, name, cabinet, session, thread, given, slots=None)
    finally:
        var_child_runnable_config.reset(detached)
    if failure:
        raise failure
    return output


def configurable_of(cabinet, session, thread):
    """Return a run's configurable: {cabinet, session_id}, plus thread_id when ``thread`` is set and unattended
    (True) in an unattended run (UNATTENDED)."""
    configurable = {"cabinet": cabinet, "session_id": session}
    if UNATTENDED.get():
        configurable["unattended"] = True
    if thread:
        configurable["thread_id"] = thread
    return configurable


def graph_of(name, configurable):
    """Return the compiled graph for ``name``; the agent is built per run for its cabinet and conversation."""
    return agent.make_agent({"configurable": configurable}) if name == "agent" else graphs.COMPILED[name]


async def execute(run_id, name, cabinet, session, thread, given, slots=SLOTS, unattended=False):
    """Run the recorded run ``run_id`` of graph ``name`` on ``given``, recording each step as it ends and the run's
    end; return (output, failure): output is the final state (None if none), failure the exception or None.

    ``slots`` is the semaphore limiting runs at once (SLOTS). A nested run passes None: its parent already holds
    a slot, and waiting for another could wait for itself. ``unattended`` marks it and the runs nested in it
    (UNATTENDED); a nested run inherits its parent's. A failure never propagates: it ends the run in error.

    Two maps are kept while streaming, for recorded(): ``inner`` maps a nested part's path ("facture",
    "fichier|facture") to the status of the last step that ran inside it, so the part's own step reads as that
    status; ``rounds`` maps (namespace, node) to the LangGraph step (superstep) its latest task started in."""
    marked = UNATTENDED.set(True) if unattended else None
    configurable = configurable_of(cabinet, session, thread)
    status, error, output, failure = "done", None, None, None
    inner = {}  # a part's path ("facture", "fichier|facture") -> the status of the last step that ran inside it
    rounds = {}  # (namespace, node) -> the round its latest task started in
    running = RUNNING.set(run_id)
    try:  # reset even when the store fails: the parent's later model calls are its own
        # shortcut: one process's runs at once (20); several servers would need a shared queue
        async with slots or contextlib.nullcontext():
            async with pool.connection() as db:
                await db.execute(Q.run_running, (run_id,))
            try:  # the agent made here too: no model configured fails the run, never leaves it queued
                graph = graph_of(name, configurable)
                async for namespace, mode, data in graph.astream(
                    given,
                    {"configurable": configurable, "run_id": run_id},
                    stream_mode=["updates", "values", "debug"],
                    subgraphs=True,
                ):
                    if mode == "debug":
                        if data["type"] == "task":  # a step starting: its round, read when its update comes
                            rounds[(namespace, data["payload"]["name"])] = data["step"]
                    elif mode == "values" and not namespace:
                        output = data
                    elif mode == "updates":
                        await recorded(run_id, namespace, data, rounds, inner)
                        live.touched(run_id, cabinet, session)
            except Exception as caught:  # the run's own failure: kept for whoever looks, the server goes on
                status, error, failure = "error", f"{type(caught).__name__}: {caught}", caught
            except asyncio.CancelledError:  # cut off by its caller (a routine's instruction too long): ended anyway
                async with pool.connection() as db:
                    await asyncio.shield(db.execute(Q.run_ended, ("error", None, "Interrompu", run_id)))
                raise
            async with pool.connection() as db:
                await db.execute(
                    Q.run_ended,
                    (status, capped(output) if output is not None else None, error, run_id),
                )
    finally:
        RUNNING.reset(running)
        if marked:
            UNATTENDED.reset(marked)
    live.touched(run_id, cabinet, session)
    return output, failure


async def recorded(run_id, namespace, data, rounds, inner):
    """Record each node's update in ``data`` as a step of run ``run_id``, inside the nested part ``namespace`` (a
    tuple of LangGraph namespaces, () at the top).

    ``rounds`` gives each step its round; ``inner`` is read and updated so a part ending takes the status of the
    last step inside it (both as execute() describes)."""
    path = "|".join(part.split(":")[0] for part in namespace)
    async with pool.connection() as db:
        for node, update in (data or {}).items():
            step_status, line = outcome_of(update, node)
            own = f"{path}|{node}" if path else node
            if own in inner:  # a part ending: it reads as the last step inside it did
                step_status = inner[own]
            inner[path] = step_status
            lane = "|".join(namespace)
            step = (run_id, path, lane, rounds.get((namespace, node)), node, step_status, line, capped(update))
            await db.execute(Q.step_recorded, step)


RUN_KEYS = (
    "run_id",
    "graph",
    "cabinet",
    "session_id",
    "thread_id",
    "status",
    "input",
    "output",
    "error",
    "started",
    "ended",
    "parent_id",
    "parent_node",
)


async def run(run_id):
    """Return a run as the API shows it (RUN_KEYS plus parent_graph, children, seconds and steps); None when
    unknown."""
    async with pool.connection() as db:
        found = await (await db.execute(Q.run_found, (run_id,))).fetchone()
        if not found:
            return None
        shown = dict(zip(RUN_KEYS, found, strict=True))
        steps = await (await db.execute(Q.steps_after, (run_id, 0))).fetchall()  # every step: their ids start at 1
        children = await (await db.execute(Q.run_children, (run_id,))).fetchall()
        parent = None
        if shown["parent_id"]:
            parent = await (await db.execute(Q.run_graph, (shown["parent_id"],))).fetchone()
    shown["run_id"] = str(shown["run_id"])
    shown["parent_id"] = shown["parent_id"] and str(shown["parent_id"])
    shown["parent_graph"] = parent and parent[0]
    # The runs started in its steps, each with the step it started from (the canvas's panel links them).
    shown["children"] = [
        {"run_id": str(i), "graph": graph, "status": status, "when": when(at), "node": node}
        for i, graph, status, at, node in children
    ]
    ended = shown["ended"].timestamp() if shown["ended"] else time.time()
    shown["seconds"] = round(ended - shown["started"].timestamp(), 1)
    shown["started"], shown["ended"] = shown["started"].isoformat(), shown["ended"] and shown["ended"].isoformat()
    shown["steps"] = shown_steps(steps)
    return shown


async def recent(cabinet=None, graph=None, limit=50):
    """Return the runs list: the latest ``limit`` top-level runs, newest first, each followed by the runs nested in
    it (recursively) with their "depth". With a ``graph`` filter, that graph's runs, nested ones included, without
    their children. ``cabinet`` filters by cabinet. A routine's runs (one per dossier) are left to its own page."""
    keys = ("run_id", "graph", "cabinet", "session_id", "status", "error", "started", "ended")
    async with pool.connection() as db:
        asked = {"cabinet": cabinet or None, "graph": graph or None, "limit": limit}
        rows = await (await db.execute(Q.runs_recent, asked)).fetchall()
        below = {}  # parent id -> its children, oldest first
        level = [row[0] for row in rows if row[1] not in FOLDED]
        while level and not graph:
            found = await (await db.execute(Q.runs_under, (level,))).fetchall()
            for row in found:
                parent_id = row[len(keys)]  # Q.runs_under's rows: a run's fields, then its parent's id
                below.setdefault(parent_id, []).append(row)
            level = [row[0] for row in found if row[1] not in FOLDED]

    def shown(row, depth):
        fields = dict(zip(keys, row[: len(keys)], strict=True))
        started, ended = fields["started"], fields["ended"]
        return {
            **fields,
            "run_id": str(fields["run_id"]),
            "started": started.isoformat(),
            "ended": ended and ended.isoformat(),
            "seconds": round((ended or datetime.now(UTC)).timestamp() - started.timestamp(), 1),
            "depth": depth,
        }

    listed = []

    def walk(row, depth):
        listed.append(shown(row, depth))
        for child in below.get(row[0], []):
            walk(child, depth + 1)

    for row in rows:
        walk(row, 0)
    return listed


async def steps_after(run_id, after):
    """Return (steps, state): the run's steps after step id ``after`` (shown_steps) and its (status, error) row, None
    when the run is unknown."""
    async with pool.connection() as db:
        steps = await (await db.execute(Q.steps_after, (run_id, after))).fetchall()
        state = await (await db.execute(Q.run_state, (run_id,))).fetchone()
    return shown_steps(steps), state


def shown_steps(rows):
    """Return Q.steps_after's rows as the API shows them: [{id, at, when, path, lane, round, node, status, summary}]."""
    return [
        {
            "id": i,
            "at": at.isoformat(),
            "when": when(at),
            "path": path,
            "lane": lane,
            "round": round_,
            "node": node,
            "status": status,
            "summary": summary,
        }
        for i, at, path, lane, round_, node, status, summary in rows
    ]


async def step_detail(run_id, step_id):
    """Return one step's whole update (its node's output); None when unknown, a short note when the node returned
    nothing."""
    async with pool.connection() as db:
        found = await (await db.execute(Q.step_detail, (run_id, step_id))).fetchone()
    if not found:
        return None
    return found[0] if found[0] is not None else {"etape": "Cette étape n'a rien renvoyé."}


async def cabinets():
    """Return the cabinets that have runs, for the runs list's filter."""
    async with pool.connection() as db:
        return [row[0] for row in await (await db.execute(Q.runs_cabinets)).fetchall()]
