"""Every run of a graph, kept in Postgres (schema orchestrator): what it was asked, each node as it ended, and how it
ended. The UI reads runs from here, live ones included; the agent's conversations sit next to them (the LangGraph
checkpointer's tables, same schema)."""

import asyncio
import json
import time
import uuid

from langchain_core.messages import BaseMessage
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from engine import agent, dossier, emails, traitement
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
"""
GRAPHS = {"traitement": traitement.graph, "dossier": dossier.graph, "emails": emails.graph}  # the agent: made per run
NAMES = sorted([*GRAPHS, "agent"])
DETAIL_LIMIT = 50_000  # characters of a node's update kept: a scan's text or a long history is cut, never the run

pool = None  # set by open_store()
live = {}  # run id -> asyncio.Event, set at each step of a run going on in this process


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


def outcome_of(update):
    """A node's update in short: its step's status and the one line a person reads."""
    if not isinstance(update, dict):
        return "", ""
    last = update.get("last") or ((update.get("outcomes") or [None])[-1])
    status = last.get("status", "") if isinstance(last, dict) else ""
    line = (last or {}).get("message", "") if isinstance(last, dict) else ""
    if update.get("message"):  # a run's summary
        line = update["message"]
    messages = update.get("messages") or []
    if messages and not line:  # the agent: what it said, or the tools it called
        message = messages[-1]
        calls = getattr(message, "tool_calls", None)
        line = ", ".join(call["name"] for call in calls) if calls else str(getattr(message, "text", "") or "")
    return status, " ".join(str(line).split())[:300]


async def open_store(conninfo):
    """The schema, its tables and the pool, for the server's whole life."""
    global pool
    pool = AsyncConnectionPool(f"{conninfo} options='-c search_path={SCHEMA}'", open=False, kwargs={"autocommit": True})
    await pool.open()
    async with pool.connection() as db:
        await db.execute(f"create schema if not exists {SCHEMA}")
        await db.execute(TABLES)


async def start(name, payload):
    """Record a new run of graph ``name`` and start it in the background; its id."""
    run_id = str(uuid.uuid4())
    given = payload.get("input") or {}
    call = given.get("call") or {}  # a workflow's "Appel" inputs: its conversation and cabinet are there
    session = payload.get("session_id") or call.get("session_id") or payload.get("thread_id") or run_id
    cabinet = payload.get("cabinet") or call.get("cabinet") or given.get("cabinet") or DEFAULT_CABINET
    thread = (payload.get("thread_id") or session) if name == "agent" else None
    async with pool.connection() as db:
        await db.execute(
            "insert into runs (id, graph, cabinet, session_id, thread_id, status, input) values (%s, %s, %s, %s, %s, 'queued', %s)",
            (run_id, name, cabinet, session, thread, Jsonb(jsonable(payload.get("input") or {}))),
        )
    live[run_id] = asyncio.Event()
    task = asyncio.create_task(execute(run_id, name, cabinet, session, thread, payload.get("input") or {}))
    return run_id, task


async def execute(run_id, name, cabinet, session, thread, given, slots=asyncio.Semaphore(20)):
    configurable = {"cabinet": cabinet, "session_id": session, **({"thread_id": thread} if thread else {})}
    graph = agent.make_agent({"configurable": configurable}) if name == "agent" else GRAPHS[name]
    status, error, output = "done", None, None
    async with slots:  # shortcut: one process's runs at once (20); several servers would need a shared queue
        async with pool.connection() as db:
            await db.execute("update runs set status = 'running' where id = %s", (run_id,))
        try:
            async for namespace, mode, data in graph.astream(
                given, {"configurable": configurable, "run_id": run_id}, stream_mode=["updates", "values"], subgraphs=True
            ):
                if mode == "values" and not namespace:
                    output = data
                elif mode == "updates":
                    path = "|".join(part.split(":")[0] for part in namespace)
                    async with pool.connection() as db:
                        for node, update in (data or {}).items():
                            step_status, line = outcome_of(update)
                            await db.execute(
                                "insert into steps (run_id, path, node, status, summary, detail) values (%s, %s, %s, %s, %s, %s)",
                                (run_id, path, node, step_status, line, capped(update)),
                            )
                    live[run_id].set()
        except Exception as failure:  # the run's own failure: kept for whoever looks, the server goes on
            status, error = "error", f"{type(failure).__name__}: {failure}"
        async with pool.connection() as db:
            await db.execute(
                "update runs set status = %s, output = %s, error = %s, ended_at = now() where id = %s",
                (status, capped(output) if output is not None else None, error, run_id),
            )
    live.pop(run_id).set()


async def run(run_id):
    """A run as the API shows it, its steps included; None when unknown."""
    async with pool.connection() as db:
        found = await (await db.execute(
            "select id, graph, cabinet, session_id, thread_id, status, input, output, error, started_at, ended_at "
            "from runs where id = %s", (run_id,),
        )).fetchone()
        if not found:
            return None
        steps = await (await db.execute(
            "select id, at, path, node, status, summary from steps where run_id = %s order by id", (run_id,)
        )).fetchall()
    keys = ("run_id", "graph", "cabinet", "session_id", "thread_id", "status", "input", "output", "error", "started", "ended")
    shown = dict(zip(keys, found, strict=True))
    shown["run_id"] = str(shown["run_id"])
    ended = shown["ended"].timestamp() if shown["ended"] else time.time()
    shown["seconds"] = round(ended - shown["started"].timestamp(), 1)
    shown["started"], shown["ended"] = shown["started"].isoformat(), shown["ended"] and shown["ended"].isoformat()
    shown["steps"] = [
        {"id": i, "at": at.isoformat(), "path": path, "node": node, "status": status, "summary": summary}
        for i, at, path, node, status, summary in steps
    ]
    return shown


async def recent(cabinet=None, graph=None, limit=50):
    """The latest runs, newest first: what the runs list shows."""
    where, args = [], []
    for column, value in (("cabinet", cabinet), ("graph", graph)):
        if value:
            where.append(f"{column} = %s")
            args.append(value)
    clause = f"where {' and '.join(where)}" if where else ""
    async with pool.connection() as db:
        rows = await (await db.execute(
            f"select id, graph, cabinet, session_id, status, error, started_at, ended_at from runs {clause} "
            "order by started_at desc limit %s", (*args, limit),
        )).fetchall()
    keys = ("run_id", "graph", "cabinet", "session_id", "status", "error", "started", "ended")
    return [
        {**dict(zip(keys, row, strict=True)), "run_id": str(row[0]), "started": row[6].isoformat(), "ended": row[7] and row[7].isoformat()}
        for row in rows
    ]


async def steps_after(run_id, after):
    """The run's steps after step id ``after``, and whether the run has ended."""
    async with pool.connection() as db:
        steps = await (await db.execute(
            "select id, at, path, node, status, summary from steps where run_id = %s and id > %s order by id", (run_id, after)
        )).fetchall()
        state = await (await db.execute("select status, error from runs where id = %s", (run_id,))).fetchone()
    found = [{"id": i, "at": at.isoformat(), "path": p, "node": n, "status": s, "summary": line} for i, at, p, n, s, line in steps]
    return found, state


async def changed(run_id, seconds=1.0):
    """Wait for the run's next step in this process, or ``seconds`` (a run of another process is polled)."""
    event = live.get(run_id)
    if event is None:
        await asyncio.sleep(seconds)
        return
    event.clear()
    try:
        await asyncio.wait_for(event.wait(), seconds)
    except TimeoutError:
        pass
