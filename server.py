"""Tarik's server: its graphs served over HTTP on permissively licensed parts only (starlette, uvicorn: BSD;
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

Every call but /ok, /login and the static files needs a session (the login page's cookie: users.py makes the
accounts) or, for scripts (run.py), TARIK_GRAPH_TOKEN as a bearer token. A change (POST, PUT, DELETE) made with a
session must come from these pages themselves (its Origin or Referer): another site cannot make it in someone's name."""

import asyncio
import contextlib
import hmac
import json
import os
from urllib.parse import parse_qs, quote

import uvicorn
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from engine import accounts, agent, content, graphs, layouts, mail, prompts, runs
from tarik.core.cabinets import CABINETS, VAULT_ENABLED, create_schemas, misplaced_in_vault, using, vault_settings
from tarik.core.config import POSTGRES
from tarik.core.experio import account
from ui import content_pages, pages

TOKEN = os.getenv("TARIK_GRAPH_TOKEN", "")
DATABASE = make_conninfo(**POSTGRES)  # the tools' own Postgres settings


OPEN = ("/ok", "/login", "/ui/static/")
COOKIE = "tarik_session"


class Guard:
    """Every call needs a session or the scripts' token; the person behind a session goes in request.state.user."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(OPEN):
            return await self.app(scope, receive, send)
        request = Request(scope)
        scope.setdefault("state", {})
        if TOKEN and hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {TOKEN}"):
            scope["state"]["user"] = {"email": "script", "name": "Script"}
            return await self.app(scope, receive, send)
        user = await accounts.user_of(request.cookies.get(COOKIE))
        if user is None:
            if request.method == "GET" and (scope["path"].startswith("/ui") or scope["path"] == "/"):
                answer = RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
            else:
                answer = JSONResponse({"error": "unauthorized"}, status_code=401)
            return await answer(scope, receive, send)
        if request.method not in ("GET", "HEAD") and not same_origin(request):
            return await JSONResponse({"error": "cross-site request refused"}, status_code=403)(scope, receive, send)
        scope["state"]["user"] = user
        return await self.app(scope, receive, send)


def same_origin(request):
    """The request comes from these pages: its Origin (or else Referer) is this server's own address."""
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    own = f"{request.url.scheme}://{request.headers.get('host', '')}"
    return source == own or source.startswith(own + "/")


async def login(request: Request):
    """The login page (GET), and logging in (POST): a session cookie, then back to the page asked for."""
    target = request.query_params.get("next") or "/ui/runs"
    if not target.startswith("/") or target.startswith("//"):
        target = "/ui/runs"  # never sent to another site
    if request.method == "GET":
        return pages.login_page(request, target)
    if not same_origin(request):
        return JSONResponse({"error": "cross-site request refused"}, status_code=403)
    form = parse_qs((await request.body()).decode(), max_num_fields=5)  # a plain form: no multipart library
    token = await accounts.log_in(form.get("email", [""])[0], form.get("password", [""])[0])
    if token is None:
        return pages.login_page(request, target, failed=True)
    answer = RedirectResponse(target, status_code=303)
    answer.set_cookie(
        COOKIE,
        token,
        max_age=accounts.SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return answer


async def logout(request: Request):
    await accounts.log_out(request.cookies.get(COOKIE))
    answer = RedirectResponse("/login", status_code=303)
    answer.delete_cookie(COOKIE)
    return answer


async def ok(request: Request):
    return JSONResponse({"ok": True, "graphs": runs.NAMES})


async def start(request: Request):
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
    query = request.query_params
    found = await runs.recent(query.get("cabinet"), query.get("graph"), int(query.get("limit", 50)))
    return JSONResponse(found)


async def one(request: Request):
    found = await runs.run(request.path_params["run_id"])
    return JSONResponse(found) if found else JSONResponse({"error": "unknown run"}, status_code=404)


async def events(request: Request):
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
    found = await prompts.prompt(cabinet, int(request.path_params["call_id"])) if cabinet in CABINETS else None
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
    await content.setup(runs.pool)
    mail.start(asyncio.get_running_loop())
    async with AsyncPostgresSaver.from_conn_string(f"{DATABASE} options='-c search_path={runs.SCHEMA}'") as saver:
        await saver.setup()
        agent.CHECKPOINTER = saver
        yield
    await runs.pool.close()


app = Starlette(
    routes=[
        Route("/ok", ok),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
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
        Route("/ui/contenu", content_pages.contents_page),
        Route("/ui/contenu-export", content_pages.export_contents),
        Route("/ui/contenu-import", content_pages.import_contents, methods=["POST"]),
        Route("/ui/contenu/{path:path}", content_pages.content_page, methods=["GET", "POST"]),
        Route("/ui/workflows", pages.workflows_page),
        Route("/ui/workflows/{graph}", pages.workflow_page),
        Route("/", pages.home),
        Route("/ui", pages.home),
        Route("/ui/runs", pages.runs_page),
        Route("/ui/runs/{run_id}", pages.run_page),
        Mount("/ui/static", StaticFiles(directory=pages.HERE / "static"), name="static"),
    ],
    middleware=[Middleware(Guard)],
    lifespan=lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=2024, log_level="warning")
