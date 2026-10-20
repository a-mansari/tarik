"""Tarik's orchestrator: its graphs served over HTTP on permissively licensed parts only (starlette, uvicorn: BSD;
langgraph and its Postgres checkpointer: MIT; psycopg: LGPL, unmodified). Every run and each of its steps is kept in
Postgres (engine/runs.py), the agent's conversations next to them. The tools (tarik/) run in this process, each in
its cabinet's schema of the same Postgres; the mail watcher (engine/mail.py) starts runs for the mailboxes it answers.

  GET  /ok                        the server is up, and its graphs
  POST /runs/{graph}              {input, thread_id?, cabinet?, session_id?, wait?}: wait (default) answers the run
                                  with its output; wait=false answers its run_id at once, the run going on meanwhile
  GET  /runs                      the latest runs (?cabinet=, ?graph=, ?limit=)
  GET  /runs/{run_id}             a run: its status, input, output or error, and its steps
  GET  /runs/{run_id}/events      its steps as they end (server-sent events), from the first one
  GET  /runs/{run_id}/steps/{id}  one step's whole output
  GET  /graphs/{graph}            a workflow's shape for the canvas: its nodes, groups and edges
  GET|PUT|DELETE /graphs/{graph}/layout   how the team arranged its canvas (node places, edge bends and style)
  GET  /ui/...                    the team's pages (ui/pages.py): the runs, each run step by step, live

With TARIK_GRAPH_TOKEN set, every API call but /ok needs it as a bearer token.
shortcut: the pages have no login yet: the server listens on the host only (127.0.0.1); a login comes before it is
reachable from anywhere else."""

import asyncio
import contextlib
import json
import os

import uvicorn
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from engine import agent, graphs, layouts, mail, prompts, runs
from tarik.core.cabinets import CABINETS, VAULT_ENABLED, create_schemas, misplaced_in_vault, using, vault_settings
from tarik.core.experio import account
from ui import pages

TOKEN = os.getenv("TARIK_GRAPH_TOKEN", "")
DATABASE = (
    f"host={os.getenv('POSTGRES_HOST', 'db')} dbname={os.environ['POSTGRES_DB']} "
    f"user={os.environ['POSTGRES_USER']} password={os.environ['POSTGRES_PASSWORD']}"
)


def refused(request):
    if TOKEN and request.headers.get("authorization") != f"Bearer {TOKEN}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


async def ok(request: Request):
    return JSONResponse({"ok": True, "graphs": runs.NAMES})


async def start(request: Request):
    if failed := refused(request):
        return failed
    name = request.path_params["graph"]
    if name not in runs.NAMES:
        return JSONResponse({"error": f"unknown graph {name!r}"}, status_code=404)
    payload = await request.json()
    run_id, task = await runs.start(name, payload)
    if payload.get("wait", True):
        await task
        return JSONResponse(await runs.run(run_id))
    return JSONResponse({"run_id": run_id, "status": "queued"}, status_code=202)


async def listing(request: Request):
    if failed := refused(request):
        return failed
    query = request.query_params
    found = await runs.recent(query.get("cabinet"), query.get("graph"), int(query.get("limit", 50)))
    return JSONResponse(found)


async def one(request: Request):
    if failed := refused(request):
        return failed
    found = await runs.run(request.path_params["run_id"])
    return JSONResponse(found) if found else JSONResponse({"error": "unknown run"}, status_code=404)


async def events(request: Request):
    if failed := refused(request):
        return failed
    run_id = request.path_params["run_id"]
    if not await runs.run(run_id):
        return JSONResponse({"error": "unknown run"}, status_code=404)

    async def stream():
        last = 0
        while True:
            steps, (status, error) = await runs.steps_after(run_id, last)
            for found in steps:
                last = found["id"]
                yield f"event: step\ndata: {json.dumps(found, ensure_ascii=False)}\n\n"
            if status in ("done", "error"):
                yield f"event: end\ndata: {json.dumps({'status': status, 'error': error}, ensure_ascii=False)}\n\n"
                return
            await runs.changed(run_id)

    return StreamingResponse(stream(), media_type="text/event-stream")


async def graph_shape(request: Request):
    name = request.path_params["graph"]
    if name not in runs.NAMES:
        return JSONResponse({"error": f"unknown graph {name!r}"}, status_code=404)
    return JSONResponse(graphs.shape(name))


async def layout(request: Request):
    """A workflow's saved arrangement: read (GET), saved (PUT), or forgotten for the automatic one (DELETE)."""
    name = request.path_params["graph"]
    if name not in runs.NAMES:
        return JSONResponse({"error": f"unknown graph {name!r}"}, status_code=404)
    if request.method == "PUT":
        try:
            return JSONResponse(await layouts.save(name, await request.json()))
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            return JSONResponse({"error": f"layout refused: {error}"}, status_code=400)
    if request.method == "DELETE":
        await layouts.forget(name)
        return JSONResponse({"nodes": {}, "edges": {}})
    return JSONResponse(await layouts.read(name))


async def prompt(request: Request):
    """One model call's whole prompt (the UI opens it when asked: it holds the clients' words)."""
    cabinet = request.path_params["cabinet"]
    found = (
        await prompts.prompt(cabinet, int(request.path_params["call_id"]))
        if cabinet in await prompts.cabinets()
        else None
    )
    if found is None:
        return JSONResponse({"error": "unknown call"}, status_code=404)
    return JSONResponse({**found, "at": runs.when(found["at"])})


async def step(request: Request):
    found = await runs.step_detail(request.path_params["run_id"], int(request.path_params["step_id"]))
    return JSONResponse(found) if found is not None else JSONResponse({"error": "unknown step"}, status_code=404)


def check_cabinets():
    """At start: every cabinet fully configured and its schema ready, or nothing is served."""
    if not VAULT_ENABLED:
        raise SystemExit("VAULT_ADDR is not set: Tarik's settings are read from Vault only")
    if not CABINETS:
        raise SystemExit("no cabinet in Vault (secret/tarik/cabinets/<cabinet>): no cabinet to serve")
    misplaced = misplaced_in_vault(*vault_settings())
    if misplaced:
        raise SystemExit(f"settings not where they belong (see tarik/core/cabinets.py): {', '.join(misplaced)}")
    for name in sorted(CABINETS):
        with using(name):
            account()  # exact database name and Tarik's user id, or SystemExit
    create_schemas()


@contextlib.asynccontextmanager
async def lifespan(app):
    """The cabinets checked, the run store and the agent's memory, in Postgres, and the mail watcher, for the
    server's whole life."""
    check_cabinets()
    await runs.open_store(DATABASE)
    mail.start(asyncio.get_running_loop())
    async with AsyncPostgresSaver.from_conn_string(f"{DATABASE} options='-c search_path={runs.SCHEMA}'") as saver:
        await saver.setup()
        agent.CHECKPOINTER = saver
        yield
    await runs.pool.close()


app = Starlette(
    routes=[
        Route("/ok", ok),
        Route("/runs", listing),
        Route("/runs/{graph}", start, methods=["POST"]),
        Route("/runs/{run_id}", one),
        Route("/runs/{run_id}/events", events),
        Route("/runs/{run_id}/steps/{step_id}", step),
        Route("/graphs/{graph}", graph_shape),
        Route("/graphs/{graph}/layout", layout, methods=["GET", "PUT", "DELETE"]),
        Route("/prompts/{cabinet}/{call_id:int}", prompt),
        Route("/ui/prompts", pages.prompts_page),
        Route("/ui/prompts/{cabinet}/{session_id}", pages.prompt_calls_page),
        Route("/ui/workflows", pages.workflows_page),
        Route("/ui/workflows/{graph}", pages.workflow_page),
        Route("/", pages.home),
        Route("/ui", pages.home),
        Route("/ui/runs", pages.runs_page),
        Route("/ui/runs/{run_id}", pages.run_page),
        Mount("/ui/static", StaticFiles(directory=pages.HERE / "static"), name="static"),
    ],
    lifespan=lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=2024, log_level="warning")
