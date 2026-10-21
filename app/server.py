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
  /api/cabinets...                the Experio app's API (app/api.py): cabinets created, changed, closed
  GET  /ui/live                    the pages' live updates (server-sent events, engine/live.py)
  GET  /ui/...                    the team's pages (ui/pages.py): the runs, each run step by step, live; the
                                  Discussion page (ui/chat_pages.py), its messages sent with POST

Every call but /ok, /login and the static files needs a session (the login page's cookie: app/users.py makes the
accounts) or, for scripts (run.py), TARIK_GRAPH_TOKEN as a bearer token. A change (POST, PUT, DELETE) made with a
session must come from these pages themselves (its Origin or Referer): another site cannot make it in someone's name.
What changes everyone's Tarik needs an admin's session (admin_only): a member reads and talks to Tarik."""

import asyncio
import contextlib
import hmac
import json
import os
import threading
from urllib.parse import parse_qs, quote

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app import accounts, api, audit, layouts
from engine import agent, content, graphs, live, mail, prompts, runs
from tarik.core.cabinets import CABINETS, VAULT_ENABLED, create_schemas, misplaced_in_vault, using, vault_settings
from tarik.core.config import POSTGRES
from tarik.core.experio import account
from ui import accounts_pages, chat_pages, content_pages, journal_pages, pages, settings_pages

TOKEN = os.getenv("TARIK_GRAPH_TOKEN", "")
DATABASE = make_conninfo(**POSTGRES)  # the tools' own Postgres settings


OPEN = ("/ok", "/login", "/ui/static/", "/invitation/")  # an invitation's page: its one-time link is its key
COOKIE = "tarik_session"


def admin_only(method, path):
    """Whether this call is an admin's to make: the cabinets' settings (read too: logins, mailboxes, whitelist), the
    accounts, a change to the content or to a canvas's layout, a run started by hand. Discussion stays every
    member's."""
    return (
        path.startswith(("/ui/settings", "/ui/comptes", "/ui/logs-serveur"))
        or (method == "POST" and path.startswith(("/ui/tarik-prompts", "/runs/")))
        or (method in ("PUT", "DELETE") and path.startswith("/graphs/"))
    )


class Guard:
    """Every call needs a session or the scripts' token; the person behind a session goes in request.state.user."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(OPEN):
            return await self.app(scope, receive, send)
        request = Request(scope)
        scope.setdefault("state", {})
        if scope["path"].startswith("/api/"):  # the Experio app's: its key only, never a session nor the scripts' token
            if not experio_key_shown(request):
                return await JSONResponse({"error": "clé de l'API Experio absente ou fausse"}, status_code=401)(
                    scope, receive, send
                )
            scope["state"]["user"] = {"email": api.ACTOR, "name": "Experio", "role": "api"}
            return await self.app(scope, receive, send)
        if TOKEN and hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {TOKEN}"):
            scope["state"]["user"] = {"email": "script", "name": "Script", "role": "admin"}
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
        if user["role"] != "admin" and admin_only(request.method, scope["path"]):
            if request.method == "GET":
                answer = pages.templates.TemplateResponse(request, "forbidden.html", {}, status_code=403)
            else:
                answer = JSONResponse({"error": "réservé aux administrateurs"}, status_code=403)
            return await answer(scope, receive, send)
        return await self.app(scope, receive, send)


def experio_key_shown(request):
    """The request shows the Experio app's key (Vault: EXPERIO_API_KEY) as its Bearer token. No key set: refused."""
    key = vault_settings()[0].get("EXPERIO_API_KEY", "") if VAULT_ENABLED else os.getenv("EXPERIO_API_KEY", "")
    return bool(key) and hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {key}")


def same_origin(request):
    """The request comes from these pages: its Origin (or else Referer) is this server's own address."""
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    own = f"{request.url.scheme}://{request.headers.get('host', '')}"
    return source == own or source.startswith(own + "/")


async def login(request: Request):
    """The login page (GET), and logging in (POST): a session cookie, then back to the page asked for."""
    target = request.query_params.get("next") or pages.HOME
    if not target.startswith("/") or target.startswith("//"):
        target = pages.HOME  # never sent to another site
    if request.method == "GET":
        return pages.login_page(request, target)
    if not same_origin(request):
        return JSONResponse({"error": "cross-site request refused"}, status_code=403)
    form = parse_qs((await request.body()).decode(), max_num_fields=5)  # a plain form: no multipart library
    email = form.get("email", [""])[0].strip().lower()
    token = await accounts.log_in(email, form.get("password", [""])[0])
    if token is None:
        await audit.record(email[:200], "login_failed")
        return pages.login_page(request, target, failed=True)
    await audit.record(email, "login")
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
    await audit.record(request.state.user["email"], "logout")
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
    await audit.record(request.state.user["email"], "run_started", name, run_id=run_id)
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
        last, seen = 0, live.versions([("run", run_id)])
        while True:  # the steps read only when the run changed (engine/live.py), else a sign of life every 25 s
            steps, (status, error) = await runs.steps_after(run_id, last)
            for found in steps:
                last = found["id"]
                yield f"event: step\ndata: {json.dumps(found, ensure_ascii=False)}\n\n"
            if status in ("done", "error"):
                yield f"event: end\ndata: {json.dumps({'status': status, 'error': error}, ensure_ascii=False)}\n\n"
                return
            while (now := await live.changed([("run", run_id)], seen)) is None:
                yield ": still here\n\n"
            seen = now

    return StreamingResponse(stream(), media_type="text/event-stream")


async def changes(request: Request):
    """The pages' live updates (server-sent events): a "change" each time something they show changes, a cabinet's
    runs (?cabinet=) or a conversation (?session=), at most two a second; the page then fetches what it shows. Nothing
    is read from the database here."""
    query = request.query_params
    keys = [("cabinet", query["cabinet"])] if query.get("cabinet") in CABINETS else []
    keys += [("session", query["session"])] if query.get("session") else []
    if not keys:
        return JSONResponse({"error": "nothing to follow"}, status_code=400)

    async def stream():
        seen = live.versions(keys)
        while True:
            now = await live.changed(keys, seen)
            if now is None:
                yield ": still here\n\n"
                continue
            seen = now
            yield "event: change\ndata: {}\n\n"
            await asyncio.sleep(0.5)  # the changes of the next half second, one fetch for them all

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
            saved = await layouts.save(name, await request.json())
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            return JSONResponse({"error": f"layout refused: {error}"}, status_code=400)
        await audit.record(request.state.user["email"], "layout_saved", name)
        return JSONResponse(saved)
    if request.method == "DELETE":
        await layouts.forget(name)
        await audit.record(request.state.user["email"], "layout_reset", name)
        return JSONResponse({"nodes": {}, "edges": {}})
    return JSONResponse(await layouts.read(name))


async def prompt(request: Request):
    """One model call's whole prompt, of the cabinet picked in the sidebar (the UI opens it when asked: it holds the
    clients' words)."""
    cabinet = pages.chosen(request)
    found = await prompts.prompt(cabinet, int(request.path_params["call_id"])) if cabinet else None
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
    await runs.end_cut_off()
    await content.setup(runs.pool)
    mail.start(asyncio.get_running_loop())
    threading.Thread(target=pages.warm_names, daemon=True).start()  # the sidebar's names, read off the first pages
    async with AsyncPostgresSaver.from_conn_string(f"{DATABASE} options='-c search_path={runs.SCHEMA}'") as saver:
        await saver.setup()
        agent.CHECKPOINTER = saver
        yield
    await runs.pool.close()


app = Starlette(
    routes=[
        Route("/ok", ok),
        Route("/api/cabinets", api.listing),
        Route("/api/cabinets", api.create, methods=["POST"]),
        Route("/api/cabinets/{name}", api.one, methods=["GET", "PATCH", "DELETE"]),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/runs", listing),
        Route("/runs/{graph}", start, methods=["POST"]),
        Route("/runs/{run_id}", one),
        Route("/runs/{run_id}/events", events),
        Route("/runs/{run_id}/steps/{step_id}", step),
        Route("/graphs/{graph}", graph_shape),
        Route("/graphs/{graph}/layout", layout, methods=["GET", "PUT", "DELETE"]),
        Route("/prompts/{call_id:int}", prompt),
        Route("/ui/llm-logs", pages.prompts_page),
        Route("/ui/llm-logs/{session_id}", pages.prompt_calls_page),
        Route("/invitation/{token}", accounts_pages.invitation_page, methods=["GET", "POST"]),
        Route("/ui/comptes", accounts_pages.accounts_page, methods=["GET", "POST"]),
        Route("/ui/logs-serveur", journal_pages.journal_page),
        Route("/ui/live", changes),
        Route("/ui/cabinet", pages.choose, methods=["POST"]),
        Route("/ui/settings", settings_pages.cabinet_page, methods=["GET", "POST"]),
        Route("/ui/tarik-prompts", content_pages.contents_page),
        Route("/ui/tarik-prompts-export", content_pages.export_contents),
        Route("/ui/tarik-prompts-import", content_pages.import_contents, methods=["POST"]),
        Route("/ui/tarik-prompts/{path:path}", content_pages.content_page, methods=["GET", "POST"]),
        Route("/ui/discussion", chat_pages.chat_page),
        Route("/ui/discussion/{session_id}", chat_pages.conversation_page),
        Route("/ui/discussion/{session_id}", chat_pages.send, methods=["POST"]),
        Route("/ui/discussion/{session_id}/fichier", chat_pages.download),
        Route("/ui/workflows", pages.workflows_page),
        Route("/ui/workflows/{graph}", pages.workflow_page),
        Route("/", pages.home),
        Route("/ui", pages.home),
        Route("/ui/accueil", chat_pages.dashboard_page),
        Route("/ui/executions", pages.runs_page),
        Route("/ui/executions/{run_id}", pages.run_page),
        Mount("/ui/static", StaticFiles(directory=pages.HERE / "static"), name="static"),
    ],
    middleware=[Middleware(Guard)],
    lifespan=lifespan,
)
