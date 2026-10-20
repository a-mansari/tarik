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

from engine import agent, graphs
from engine.content import LABELS
from engine.settings import DEFAULT_CABINET

SCHEMA = "orchestrator"
TABLES = """
create table if not exists runs (
    id uuid primary key,
    graph text not null,
    cabinet text not null,
    session_id text not null,
    thread_id text,
    status text not null,  -- queued, running, done, error
    input jsonb not null,
    output jsonb,
    error text,
    started_at timestamptz not null default now(),
    ended_at timestamptz
);
create index if not exists runs_started on runs (started_at desc);
create index if not exists runs_session on runs (cabinet, session_id);
-- A run started inside another's step (a workflow the agent runs, a thread handed to the agent): its parent, and the
-- parent's step it started from.
alter table runs add column if not exists parent_id uuid references runs (id);
alter table runs add column if not exists parent_node text;
create index if not exists runs_parent on runs (parent_id);
create table if not exists steps (
    id bigserial primary key,
    run_id uuid not null references runs (id),
    at timestamptz not null default now(),
    path text not null,  -- the subgraphs it sits in: "" at the top, "fichier|facture" inside
    node text not null,
    status text not null,  -- the step's own: ok, question, stop, wait, error, or "" for plumbing
    summary text not null,  -- one line for the list
    detail jsonb  -- the node's whole update
);
create index if not exists steps_run on steps (run_id, id);
-- Which run of its part a step belongs to: the subgraphs' task ids ("fil:<id>|..."), so two invoices or two email
-- threads of one run, side by side, each keep their own order of steps (the canvas follows it).
alter table steps add column if not exists lane text;
-- Its round in its graph (LangGraph's superstep): a step follows a step of the round before, in its lane. Parallel
-- branches (an answer and the files at once) each keep their own order.
alter table steps add column if not exists round integer;
create table if not exists layouts (
    graph text primary key,
    layout jsonb not null,  -- {nodes: {id: {x, y}}, edges: {id: {points: [{x, y}], style}}}: how the team arranged it
    updated_at timestamptz not null default now()
);
"""
NAMES = sorted([*graphs.COMPILED, "agent"])  # the agent: made per run
DETAIL_LIMIT = 50_000  # characters of a node's update kept: a scan's text or a long history is cut, never the run

TIME_ZONE = ZoneInfo("Africa/Casablanca")  # the cabinets' time, from the server's own time-zone data
pool = None  # set by open_store()
live = {}  # run id -> asyncio.Event, set at each step of a run going on in this process


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
    last = update.get("last") or ((update.get("outcomes") or [None])[-1])
    status = last.get("status", "") if isinstance(last, dict) else ""
    line = (last or {}).get("message", "") if isinstance(last, dict) else ""
    if update.get("message"):  # a run's summary
        line = update["message"]
    messages = update.get("messages") or []
    if messages and not line:  # the agent: the tools it calls, what it says, or what a tool answered
        message = messages[-1]
        calls = getattr(message, "tool_calls", None)
        if calls:
            line = "Utilise " + ", ".join(call["name"] for call in calls)
        elif getattr(message, "type", "") == "tool":
            line = readable(message.text if isinstance(message.text, str) else message.text())
        else:
            line = str(getattr(message, "text", "") or "")
    return status, " ".join(str(line).split())[:300]


async def open_store(conninfo):
    """The schema, its tables and the pool, for the server's whole life."""
    global pool
    pool = AsyncConnectionPool(f"{conninfo} options='-c search_path={SCHEMA}'", open=False, kwargs={"autocommit": True})
    await pool.open()
    async with pool.connection() as db:
        await db.execute(f"create schema if not exists {SCHEMA}")
        await db.execute(TABLES)


RUNNING = contextvars.ContextVar("running", default=None)  # the run whose steps are going on: a run started in one
# of them is its child (the context reaches every task the graph starts for its steps)


async def created(name, given, cabinet, session, thread=None, parent=None, parent_node=None):
    """A new run of graph ``name`` recorded, queued; its id."""
    run_id = str(uuid.uuid4())
    async with pool.connection() as db:
        await db.execute(
            "insert into runs (id, graph, cabinet, session_id, thread_id, status, input, parent_id, parent_node) "
            "values (%s, %s, %s, %s, %s, 'queued', %s, %s, %s)",
            (run_id, name, cabinet, session, thread, Jsonb(jsonable(given)), parent, parent_node),
        )
    live[run_id] = asyncio.Event()
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
        configurable = {"cabinet": cabinet, "session_id": session, **({"thread_id": thread} if thread else {})}
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


def graph_of(name, configurable):
    """The graph a run of ``name`` runs: the agent is made for its cabinet and conversation."""
    return agent.make_agent({"configurable": configurable}) if name == "agent" else graphs.COMPILED[name]


SLOTS = asyncio.Semaphore(20)  # runs going on at once in this process


async def execute(run_id, name, cabinet, session, thread, given, slots=SLOTS):
    """Run the graph, each step recorded as it ends: (its output, its failure or None). ``slots`` None: a run inside
    another's step, which already holds a slot (waiting for one could wait for itself)."""
    configurable = {"cabinet": cabinet, "session_id": session, **({"thread_id": thread} if thread else {})}
    status, error, output, failure = "done", None, None, None
    inner = {}  # a part's path ("facture", "fichier|facture") -> the status of the last step that ran inside it
    rounds = {}  # (namespace, node) -> the round its latest task started in
    running = RUNNING.set(run_id)
    async with (
        slots or contextlib.nullcontext()
    ):  # shortcut: one process's runs at once (20); several servers would need a shared queue
        async with pool.connection() as db:
            await db.execute("update runs set status = 'running' where id = %s", (run_id,))
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
                    path = "|".join(part.split(":")[0] for part in namespace)
                    async with pool.connection() as db:
                        for node, update in (data or {}).items():
                            step_status, line = outcome_of(update, node)
                            own = f"{path}|{node}" if path else node
                            if own in inner:  # a part ending: it reads as the last step inside it did
                                step_status = inner[own]
                            inner[path] = step_status
                            await db.execute(
                                "insert into steps (run_id, path, lane, round, node, status, summary, detail) "
                                "values (%s, %s, %s, %s, %s, %s, %s, %s)",
                                (
                                    run_id,
                                    path,
                                    "|".join(namespace),
                                    rounds.get((namespace, node)),
                                    node,
                                    step_status,
                                    line,
                                    capped(update),
                                ),
                            )
                    live[run_id].set()
        except Exception as caught:  # the run's own failure: kept for whoever looks, the server goes on
            status, error, failure = "error", f"{type(caught).__name__}: {caught}", caught
        async with pool.connection() as db:
            await db.execute(
                "update runs set status = %s, output = %s, error = %s, ended_at = now() where id = %s",
                (status, capped(output) if output is not None else None, error, run_id),
            )
    RUNNING.reset(running)
    live.pop(run_id).set()
    return output, failure


async def run(run_id):
    """A run as the API shows it, its steps included; None when unknown."""
    async with pool.connection() as db:
        found = await (
            await db.execute(
                "select id, graph, cabinet, session_id, thread_id, status, input, output, error, started_at, ended_at, "
                "parent_id, parent_node from runs where id = %s",
                (run_id,),
            )
        ).fetchone()
        if not found:
            return None
        steps = await (
            await db.execute(
                "select id, at, path, lane, round, node, status, summary from steps where run_id = %s order by id",
                (run_id,),
            )
        ).fetchall()
        children = await (
            await db.execute(
                "select id, graph, status, started_at, parent_node from runs where parent_id = %s order by started_at",
                (run_id,),
            )
        ).fetchall()
        parent = None
        if found[11]:
            parent = await (await db.execute("select graph from runs where id = %s", (found[11],))).fetchone()
    keys = (
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
    shown = dict(zip(keys, found, strict=True))
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
    shown["steps"] = [
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
        for i, at, path, lane, round_, node, status, summary in steps
    ]
    return shown


async def recent(cabinet=None, graph=None, limit=50):
    """The latest runs started on their own, newest first, each with the runs started in its steps (and theirs) right
    after it, indented by depth: what the runs list shows. A graph filter lists that graph's runs, children too."""
    where, args = ([] if graph else ["parent_id is null"]), []
    for column, value in (("cabinet", cabinet), ("graph", graph)):
        if value:
            where.append(f"{column} = %s")
            args.append(value)
    columns = "id, graph, cabinet, session_id, status, error, started_at, ended_at, parent_id"
    async with pool.connection() as db:
        rows = await (
            await db.execute(
                f"select {columns} from runs where {' and '.join(where)} order by started_at desc limit %s",
                (*args, limit),
            )
        ).fetchall()
        below = {}  # parent id -> its children, oldest first
        level = [row[0] for row in rows]
        while level and not graph:
            found = await (
                await db.execute(f"select {columns} from runs where parent_id = any(%s) order by started_at", (level,))
            ).fetchall()
            for row in found:
                below.setdefault(row[8], []).append(row)
            level = [row[0] for row in found]
    keys = ("run_id", "graph", "cabinet", "session_id", "status", "error", "started", "ended")

    def shown(row, depth):
        return {
            **dict(zip(keys, row[:8], strict=True)),
            "run_id": str(row[0]),
            "started": row[6].isoformat(),
            "ended": row[7] and row[7].isoformat(),
            "seconds": round((row[7] or datetime.now(UTC)).timestamp() - row[6].timestamp(), 1),
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
        steps = await (
            await db.execute(
                "select id, at, path, lane, round, node, status, summary from steps "
                "where run_id = %s and id > %s order by id",
                (run_id, after),
            )
        ).fetchall()
        state = await (await db.execute("select status, error from runs where id = %s", (run_id,))).fetchone()
    found = [
        {
            "id": i,
            "at": at.isoformat(),
            "when": when(at),
            "path": p,
            "lane": lane,
            "round": round_,
            "node": n,
            "status": s,
            "summary": line,
        }
        for i, at, p, lane, round_, n, s, line in steps
    ]
    return found, state


async def changed(run_id, seconds=1.0):
    """Wait for the run's next step in this process, or ``seconds`` (a run of another process is polled)."""
    event = live.get(run_id)
    if event is None:
        await asyncio.sleep(seconds)
        return
    event.clear()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), seconds)


async def step_detail(run_id, step_id):
    """One step's whole update (its node's output), None when unknown."""
    async with pool.connection() as db:
        found = await (
            await db.execute("select detail from steps where run_id = %s and id = %s", (run_id, step_id))
        ).fetchone()
    if not found:
        return None
    return found[0] if found[0] is not None else {"etape": "Cette étape n'a rien renvoyé."}


async def cabinets():
    """The cabinets that have runs, for the runs list's filter."""
    async with pool.connection() as db:
        return [row[0] for row in await (await db.execute("select distinct cabinet from runs order by 1")).fetchall()]
