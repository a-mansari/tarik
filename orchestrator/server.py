"""Tarik's orchestrator: its graphs served over HTTP on permissively licensed parts only (starlette, uvicorn: BSD;
langgraph and its Postgres checkpointer: MIT; psycopg: LGPL, unmodified). Every run and each of its steps is kept in
Postgres (engine/runs.py), the agent's conversations next to them.

  GET  /ok                        the server is up, and its graphs
  POST /runs/{graph}              {input, thread_id?, cabinet?, session_id?, wait?}: wait (default) answers the run
                                  with its output; wait=false answers its run_id at once, the run going on meanwhile
  GET  /runs                      the latest runs (?cabinet=, ?graph=, ?limit=)
  GET  /runs/{run_id}             a run: its status, input, output or error, and its steps
  GET  /runs/{run_id}/events      its steps as they end (server-sent events), from the first one
  GET  /runs/{run_id}/steps/{id}  one step's whole output
  GET  /ui/...                    the team's pages (ui/pages.py): the runs, each run step by step, live

With TARIK_GRAPH_TOKEN set, every API call but /ok needs it as a bearer token.
shortcut: the pages have no login yet: the server listens on the host only (127.0.0.1); a login comes before it is
reachable from anywhere else."""

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

from engine import agent, runs
from ui import pages

TOKEN = os.getenv("TARIK_GRAPH_TOKEN", "")
DATABASE = (
    f"host={os.getenv('POSTGRES_HOST', 'postgres')} dbname={os.environ['POSTGRES_DB']} "
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


async def step(request: Request):
    found = await runs.step_detail(request.path_params["run_id"], int(request.path_params["step_id"]))
    return JSONResponse(found) if found is not None else JSONResponse({"error": "unknown step"}, status_code=404)


@contextlib.asynccontextmanager
async def lifespan(app):
    """The run store and the agent's memory, in Postgres, for the server's whole life."""
    await runs.open_store(DATABASE)
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
