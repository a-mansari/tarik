"""Every run of a graph, kept in Postgres (schema orchestrator): what it was asked, each node as it ended, and how it
ended. The UI reads runs from here, live ones included; the agent's conversations sit next to them (the LangGraph
checkpointer's tables, same schema)."""

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
from engine.settings import DEFAULT_CABINET
from engine.tools.core.queries import Q

SCHEMA = "orchestrator"
NAMES = sorted([*graphs.COMPILED, "agent"])  # the agent: made per run
DETAIL_LIMIT = 50_000  # characters of a node's update kept: a scan's text or a long history is cut, never the run

TIME_ZONE = ZoneInfo("Africa/Casablanca")  # the cabinets' time, from the server's own time-zone data
pool = None  # set by open_store()


def when(moment):
    """A time as the team reads it, 20/10 10:33:22 in Casablanca: formatted here only, so every page agrees."""
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    return moment.astimezone(TIME_ZONE).strftime("%d/%m %H:%M:%S") if moment else ""


def jsonable(value):
    """``value`` as JSON carries it: a message as its fields, the rest as is (or as text)."""
    if isinstance(value, BaseMessage):
        return value.model_dump(exclude={"response_metadata", "usage_metadata"})
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)


def capped(value):
    text = json.dumps(jsonable(value), ensure_ascii=False)
    return Jsonb(jsonable(value)) if len(text) <= DETAIL_LIMIT else Jsonb({"cut": text[:DETAIL_LIMIT]})


UNTRUSTED_TAGS = re.compile(r"</?untrusted_data[^>]*>")


def readable(text):
    """A tool's answer as a person reads it: without the untrusted-data wrapper the model sees, and its message alone
    when it answered in JSON. Only for display: the step's detail keeps the answer as the model got it."""
    text = UNTRUSTED_TAGS.sub("", str(text)).strip()
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        text = json.loads(text).get("message") or text
    return text


def outcome_of(update, node=""):
    """A node's update in short: its step's status as the team reads it and the one line a person reads. A stop that
    means waiting for the client (content/labels.json, waiting_steps) reads as waiting; the detail keeps it as is."""
    status, line = raw_outcome(update)
    if status == "stop" and node in LABELS.get("waiting_steps", []):
        status = "wait"
    return status, line


def raw_outcome(update):
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
    """The agent's step in one line: the tools it calls, what it says, or what a tool answered."""
    calls = getattr(message, "tool_calls", None)
    if calls:
        return "Utilise " + ", ".join(call["name"] for call in calls)
    if getattr(message, "type", "") == "tool":
        return readable(message.text if isinstance(message.text, str) else message.text())
    return str(getattr(message, "text", "") or "")


def thread_outcome(update, status):
    """An email thread's step: its status (``status`` when it has one) and what it says (emails.Steps), in the
    order a person reads it."""
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
    """The schema, its tables and the pool, for the server's whole life."""
    global pool
    pool = AsyncConnectionPool(f"{conninfo} options='-c search_path={SCHEMA}'", open=False, kwargs={"autocommit": True})
    await pool.open()
    async with pool.connection() as db:
        await db.execute(Q.orchestrator_schema)


async def end_cut_off():
    """The runs a restart cut off, ended in error: only the server's start may call it (the terminal commands open the
    store while it runs)."""
    async with pool.connection() as db:
        await db.execute(Q.runs_cut_off)


RUNNING = contextvars.ContextVar("running", default=None)  # the run whose steps are going on: a run started in one
# of them is its child (the context reaches every task the graph starts for its steps)


async def created(name, given, cabinet, session, thread=None, parent=None, parent_node=None):
    """A new run of graph ``name`` recorded, queued; its id."""
    run_id = str(uuid.uuid4())
    async with pool.connection() as db:
        await db.execute(
            Q.run_created,
            (run_id, name, cabinet, session, thread, Jsonb(jsonable(given)), parent, parent_node),
        )
    live.touched(run_id, cabinet, session)
    return run_id


async def start(name, payload):
    """Record a new run of graph ``name`` and start it in the background; its id."""
    given = payload.get("input") or {}
    call = given.get("call") or {}  # a workflow's "Appel" inputs: its conversation and cabinet are there
    session = payload.get("session_id") or call.get("session_id") or payload.get("thread_id") or str(uuid.uuid4())
    cabinet = payload.get("cabinet") or call.get("cabinet") or given.get("cabinet") or DEFAULT_CABINET
    thread = (payload.get("thread_id") or session) if name == "agent" else None
    run_id = await created(name, given, cabinet, session, thread)
    task = asyncio.create_task(execute(run_id, name, cabinet, session, thread, given))
    return run_id, task


async def nested(name, given, cabinet, session, thread=None, node=None):
    """Graph ``name`` run inside a step of the run going on (a workflow the agent runs, a thread handed to the
    agent), recorded as a run of its own, that run its parent and ``node`` the step it started from. Its output, or
    its failure raised, as the graph's own call would."""
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
    """What a run's graph is configured with: its cabinet, its conversation, and the agent's thread when it has one."""
    configurable = {"cabinet": cabinet, "session_id": session}
    if thread:
        configurable["thread_id"] = thread
    return configurable


def graph_of(name, configurable):
    """The graph a run of ``name`` runs: the agent is made for its cabinet and conversation."""
    return agent.make_agent({"configurable": configurable}) if name == "agent" else graphs.COMPILED[name]


SLOTS = asyncio.Semaphore(20)  # runs going on at once in this process


async def execute(run_id, name, cabinet, session, thread, given, slots=SLOTS):
    """Run the graph, each step recorded as it ends: (its output, its failure or None). ``slots`` None: a run inside
    another's step, which already holds a slot (waiting for one could wait for itself)."""
    configurable = configurable_of(cabinet, session, thread)
    status, error, output, failure = "done", None, None, None
    inner = {}  # a part's path ("facture", "fichier|facture") -> the status of the last step that ran inside it
    rounds = {}  # (namespace, node) -> the round its latest task started in
    running = RUNNING.set(run_id)
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
        async with pool.connection() as db:
            await db.execute(
                Q.run_ended,
                (status, capped(output) if output is not None else None, error, run_id),
            )
    RUNNING.reset(running)
    live.touched(run_id, cabinet, session)
    return output, failure


async def recorded(run_id, namespace, data, rounds, inner):
    """Each node's update in ``data`` recorded as a step of the run, in the part ``namespace`` names. ``inner`` and
    ``rounds`` as execute() keeps them."""
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
    """A run as the API shows it, its steps included; None when unknown."""
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
    """The latest runs started on their own, newest first, each with the runs started in its steps (and theirs) right
    after it, indented by depth: what the runs list shows. A graph filter lists that graph's runs, children too."""
    keys = ("run_id", "graph", "cabinet", "session_id", "status", "error", "started", "ended")
    async with pool.connection() as db:
        asked = {"cabinet": cabinet or None, "graph": graph or None, "limit": limit}
        rows = await (await db.execute(Q.runs_recent, asked)).fetchall()
        below = {}  # parent id -> its children, oldest first
        level = [row[0] for row in rows]
        while level and not graph:
            found = await (await db.execute(Q.runs_under, (level,))).fetchall()
            for row in found:
                parent_id = row[len(keys)]  # Q.runs_under's rows: a run's fields, then its parent's id
                below.setdefault(parent_id, []).append(row)
            level = [row[0] for row in found]

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
    """The run's steps after step id ``after``, and whether the run has ended."""
    async with pool.connection() as db:
        steps = await (await db.execute(Q.steps_after, (run_id, after))).fetchall()
        state = await (await db.execute(Q.run_state, (run_id,))).fetchone()
    return shown_steps(steps), state


def shown_steps(rows):
    """The steps (Q.steps_after's rows) as the API shows them."""
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
    """One step's whole update (its node's output), None when unknown."""
    async with pool.connection() as db:
        found = await (await db.execute(Q.step_detail, (run_id, step_id))).fetchone()
    if not found:
        return None
    return found[0] if found[0] is not None else {"etape": "Cette étape n'a rien renvoyé."}


async def cabinets():
    """The cabinets that have runs, for the runs list's filter."""
    async with pool.connection() as db:
        return [row[0] for row in await (await db.execute(Q.runs_cabinets)).fetchall()]
