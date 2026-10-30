"""Serve Tarik's graphs, pages and APIs over HTTP, on permissively licensed parts only (starlette, uvicorn: BSD;
langgraph and its Postgres checkpointer: MIT; psycopg: LGPL, unmodified).

Every run and each of its steps is kept in Postgres (engine/runs.py), with the agent's conversations next to them.
The tools (engine/tools/) run in this process, each in its cabinet's schema of the same Postgres; the mail watcher
(engine/mail.py) starts runs for the mailboxes it answers.

  GET  /ok                        the server is up, and its graphs
  POST /gladden/{token}           Gladden's callback (engine/tools/core/gladden.py), open: its random token is its key
  POST /runs/{graph}              {input, thread_id?, cabinet?, session_id?, wait?}: wait (default) answers the run
                                  with its output; wait=false answers its run_id at once, the run going on meanwhile
  GET  /runs                      the latest runs (?cabinet=, ?graph=, ?limit=)
  GET  /runs/{run_id}             a run: its status, input, output or error, and its steps (?after=: only its steps
                                  after that one, and its status: what a page following it fetches at each change)
  GET  /runs/{run_id}/steps/{id}  one step's whole output
  GET  /graphs/{graph}            a workflow's shape for the canvas: its nodes, groups and edges
  GET|PUT|DELETE /graphs/{graph}/layout   how the team arranged its canvas (node places, edge bends and style)
  GET  /prompts/{call_id}         one model call's whole prompt
  /api/cabinets...                the Experio app's API (server/experio_api.py): cabinets created, changed, closed
  GET  /ui/live                   the pages' live updates (server-sent events, engine/live.py)
  GET  /ui/api/...                each page's data, as JSON (server/api/): the runs, each run step by step, the
                                  Discussion (its messages sent with POST), the content, the settings, the accounts
  GET  /ui/..., /login            the team's pages: the Vue app built from server/web/ (into server/web/dist), the
                                  same for each address

Every call except /ok, the login, an invitation, Gladden's callbacks and the pages' files needs a session (the
login's cookie; accounts are made on the Comptes page or with server/users.py) or, for scripts (run.py),
TARIK_GRAPH_TOKEN as a bearer token. A change (POST, PUT, DELETE) made with a session must come from these pages
themselves (its Origin or Referer), so another site cannot make it in someone's name. Changes that affect everyone's
Tarik need an admin's session (admin_only); a member reads and talks to Tarik."""

import asyncio
import contextlib
import hmac
import json
import os
import threading
from urllib.parse import quote

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from engine import agent, content, graphs, live, mail, prompts, routines, runs
from engine.tools.core import gladden
from engine.tools.core.cabinets import (
    CABINETS,
    VAULT_ENABLED,
    create_schemas,
    misplaced_in_vault,
    using,
    vault_settings,
)
from engine.tools.core.config import POSTGRES
from engine.tools.core.experio import account
from server import accounts, api, audit, experio_api, layouts

TOKEN = os.getenv("TARIK_GRAPH_TOKEN", "")
DATABASE = make_conninfo(**POSTGRES)  # the tools' own Postgres settings


OPEN = ("/ok", "/login", "/ui/api/login", "/ui/assets/", "/invitation/", "/ui/api/invitation/", "/gladden/")  # an
# invitation's page and a Gladden callback: their one-time token is their key
COOKIE = "tarik_session"


def admin_only(method, path):
    """True when the call (``method``, ``path``) needs an admin.

    Admin-only: the cabinets' settings (reading too: they show logins, mailboxes, the whitelist), behaviour and
    routines, the accounts, the Journal, a change to the content, a change to a canvas's layout, and a run started or
    stopped by hand. Discussion stays open to every member."""
    return (
        path.startswith(
            ("/ui/api/settings", "/ui/api/comportement", "/ui/api/routines", "/ui/api/comptes", "/ui/api/logs-serveur")
        )
        or (method == "POST" and path.startswith(("/ui/api/tarik-prompts", "/runs/", "/ui/api/executions/")))
        or (method in ("PUT", "DELETE") and path.startswith("/graphs/"))
    )


class Guard:
    """ASGI middleware: let a call through only with a session or the right token, and put the caller in
    request.state.user.

    Open paths (OPEN) pass freely. /api/ needs the Experio app's key only. Other calls need the scripts' token or a
    session; without one, a page request is redirected to /login and any other gets 401. A session's change must come
    from these pages (same_origin, else 403), and a member is refused admin_only calls (403)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(OPEN):
            return await self.app(scope, receive, send)
        request = Request(scope)
        scope.setdefault("state", {})
        if scope["path"].startswith("/api/"):  # the Experio app's: its key only, never a session nor the scripts' token
            if not experio_key_shown(request):
                answer = JSONResponse({"error": "clé de l'API Experio absente ou fausse"}, status_code=401)
                return await answer(scope, receive, send)
            scope["state"]["user"] = {"email": experio_api.ACTOR, "name": "Experio", "role": "api"}
            return await self.app(scope, receive, send)
        if TOKEN and hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {TOKEN}"):
            scope["state"]["user"] = {"email": "script", "name": "Script", "role": "admin"}
            return await self.app(scope, receive, send)
        user = await accounts.user_of(request.cookies.get(COOKIE))
        is_page = not scope["path"].startswith(("/ui/api/", "/ui/live"))  # the pages' app, without any data
        if user is None:
            if request.method == "GET" and is_page and (scope["path"].startswith("/ui") or scope["path"] == "/"):
                answer = RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
            else:
                answer = JSONResponse({"error": "unauthorized"}, status_code=401)
            return await answer(scope, receive, send)
        if request.method not in ("GET", "HEAD") and not same_origin(request):
            return await JSONResponse({"error": "cross-site request refused"}, status_code=403)(scope, receive, send)
        scope["state"]["user"] = user
        if user["role"] != "admin" and admin_only(request.method, scope["path"]):
            answer = JSONResponse({"error": "réservé aux administrateurs"}, status_code=403)
            return await answer(scope, receive, send)
        return await self.app(scope, receive, send)


def experio_key_shown(request):
    """True when the request's Bearer token is the Experio app's key (EXPERIO_API_KEY, from Vault); always False when no
    key is set."""
    if VAULT_ENABLED:
        shared, _ = vault_settings()
        key = shared.get("EXPERIO_API_KEY", "")
    else:
        key = os.getenv("EXPERIO_API_KEY", "")
    return bool(key) and hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {key}")


def same_origin(request):
    """True when the request's Origin (or else Referer) is this server's own address, i.e. it comes from these pages."""
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    own = f"{request.url.scheme}://{request.headers.get('host', '')}"
    return source == own or source.startswith(own + "/")


def return_to(request):
    """Return the page to go back to after logging in: ?next= when it is a path on this server, else HOME (never another
    site)."""
    target = request.query_params.get("next") or api.runs.HOME
    if not target.startswith("/") or target.startswith(("//", "/\\")) or "\\" in target:  # « /\x » is « //x »
        return api.runs.HOME
    return target


async def login(request: Request):
    """POST /ui/api/login {email, password}: open a session and return {ok, next} with the session cookie set.

    Returns 403 for a cross-site request, 400 for invalid JSON, 429 while the address must wait after too many failures
    and 401 for a wrong login (both recorded as login_failed)."""
    if not same_origin(request):
        return JSONResponse({"error": "cross-site request refused"}, status_code=403)
    try:
        given = await request.json()
        email, password = str(given.get("email", "")).strip().lower(), str(given.get("password", ""))
    except ValueError, AttributeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    token = await accounts.log_in(email, password)
    if token is None:
        await audit.record(email[:200], "login_failed")
        if accounts.waiting(email):
            return JSONResponse({"error": "trop d'essais : attendez une minute"}, status_code=429)
        return JSONResponse({"error": "email ou mot de passe incorrect"}, status_code=401)
    await audit.record(email, "login")
    answer = JSONResponse({"ok": True, "next": return_to(request)})
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
    """POST /ui/api/logout: end the caller's session, clear the session cookie, and record the logout."""
    await audit.record(request.state.user["email"], "logout")
    await accounts.log_out(request.cookies.get(COOKIE))
    answer = JSONResponse({"ok": True})
    answer.delete_cookie(COOKIE)
    return answer


async def ok(request: Request):
    """GET /ok: return {ok: true, graphs: the graph names}, for health checks."""
    return JSONResponse({"ok": True, "graphs": runs.NAMES})


async def gladden_callback(request: Request):
    """POST /gladden/{token}: hand Gladden's classification to the identify_document call waiting on ``token``.

    Returns 400 for invalid JSON, 404 when no call waits on that token."""
    try:
        callback = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(callback, dict) or not gladden.answered(request.path_params["token"], callback):
        return JSONResponse({"error": "unknown token"}, status_code=404)
    return JSONResponse({"ok": True})


async def start(request: Request):
    """POST /runs/{graph}: start a run of ``graph`` by hand and record it in the Journal.

    With wait true (the default), waits for the run and returns it (see runs.run); with wait false, returns {run_id,
    status: "queued"} at once (202). 404 for an unknown graph, 403 for the Routine workflow (its schedule starts it)."""
    name = request.path_params["graph"]
    if name not in runs.NAMES:
        return JSONResponse({"error": f"unknown graph {name!r}"}, status_code=404)
    if name == "routine":  # a period is claimed by its schedule first (engine/routines.py): never one by hand
        return JSONResponse({"error": "a routine runs at its time only (Routines page)"}, status_code=403)
    payload = await request.json()
    run_id, task = await runs.start(name, payload)
    await audit.record(request.state.user["email"], "run_started", name, run_id=run_id)
    if payload.get("wait", True):
        await asyncio.wait({task})  # stopped meanwhile (runs.stop): its run says so
        return JSONResponse(await runs.run(run_id))
    return JSONResponse({"run_id": run_id, "status": "queued"}, status_code=202)


async def listing(request: Request):
    """GET /runs: return the latest runs (runs.recent), filtered by ?cabinet= and ?graph=, at most ?limit= (default 50).
    GET /runs: return the latest runs (runs.recent), filtered by ?cabinet= and ?graph=, at most ?limit= (default 50)."""
    query = request.query_params
    found = await runs.recent(query.get("cabinet"), query.get("graph"), int(query.get("limit", 50)))
    return JSONResponse(found)


async def one(request: Request):
    """GET /runs/{run_id}: return the run with its steps, or 404.

    With ?after=<step id>, returns only {steps after that one, status, error}: what a page following the run fetches
    at each change."""
    run_id = request.path_params["run_id"]
    after = request.query_params.get("after", "")
    if after.isdigit():  # only its steps after that one, and where it stands: a page following it (app.js)
        steps, state = await runs.steps_after(run_id, int(after))
        if state is None:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        status, error = state
        return JSONResponse({"steps": steps, "status": status, "error": error})
    found = await runs.run(run_id)
    return JSONResponse(found) if found else JSONResponse({"error": "unknown run"}, status_code=404)


async def changes(request: Request):
    """GET /ui/live: stream the pages' live updates as server-sent events, one connection for every page of a browser
    (app.js).

    Follows the cabinets (?cabinet=), conversations (?session=) and runs (?run=) given, each repeatable. Each time
    one of them changes, sends a "change" event whose data lists the [kind, id] pairs that changed, at most two
    events a second; the page then fetches what it shows. Nothing is read from the database here. 400 when nothing is
    given to follow."""
    query = request.query_params
    keys = [("cabinet", name) for name in query.getlist("cabinet") if name in CABINETS]
    for kind in ("session", "run"):
        keys += [(kind, value) for value in query.getlist(kind) if value]
    if not keys:
        return JSONResponse({"error": "nothing to follow"}, status_code=400)

    async def stream():
        seen = live.versions(keys)
        while True:
            now = await live.changed(keys, seen)
            if now is None:
                yield ": still here\n\n"
                continue
            moved = [list(key) for key in keys if now[key] != seen[key]]
            seen = now
            yield f"event: change\ndata: {json.dumps(moved, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.5)  # the changes of the next half second, one fetch for them all

    return StreamingResponse(stream(), media_type="text/event-stream")


async def graph_shape(request: Request):
    """GET /graphs/{graph}: return the workflow's shape for the canvas (graphs.shape); 404 for an unknown graph."""
    name = request.path_params["graph"]
    if name not in runs.NAMES:
        return JSONResponse({"error": f"unknown graph {name!r}"}, status_code=404)
    return JSONResponse(graphs.shape(name))


async def layout(request: Request):
    """GET, PUT or DELETE /graphs/{graph}/layout: read, save, or forget a workflow's canvas layout.

    PUT returns the layout as stored (400 when it is invalid); DELETE returns an empty layout, so the automatic one
    is used. PUT and DELETE are recorded in the Journal. 404 for an unknown graph."""
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
    """GET /prompts/{call_id}: return one model call's whole prompt, for the cabinet chosen in the sidebar.

    The UI opens it only when asked, since it holds the clients' words. 404 when no cabinet is chosen or the call is
    unknown."""
    cabinet = api.runs.chosen(request)
    found = await prompts.prompt(cabinet, int(request.path_params["call_id"])) if cabinet else None
    if found is None:
        return JSONResponse({"error": "unknown call"}, status_code=404)
    return JSONResponse({**found, "at": runs.when(found["at"])})


async def step(request: Request):
    """GET /runs/{run_id}/steps/{step_id}: return one step's whole output, or 404."""
    found = await runs.step_detail(request.path_params["run_id"], int(request.path_params["step_id"]))
    return JSONResponse(found) if found is not None else JSONResponse({"error": "unknown step"}, status_code=404)


def check_cabinets():
    """Check at start that Vault is set, every cabinet is fully configured, and each schema is ready.

    Raises SystemExit (nothing is served) when Vault is off, no cabinet is configured, a setting is in the wrong
    Vault path, or a cabinet's Experio account check fails (engine.tools.core.experio.account)."""
    if not VAULT_ENABLED:
        raise SystemExit("VAULT_ADDR is not set: Tarik's settings are read from Vault only")
    if not CABINETS:
        raise SystemExit("no cabinet in Vault (secret/tarik/cabinets/<cabinet>): no cabinet to serve")
    misplaced = misplaced_in_vault(*vault_settings())
    if misplaced:
        raise SystemExit(f"settings not where they belong (see engine/tools/core/cabinets.py): {', '.join(misplaced)}")
    for name in sorted(CABINETS):
        with using(name):
            account()  # exact database name and Tarik's user id, or SystemExit
    create_schemas()


@contextlib.asynccontextmanager
async def lifespan(app):
    """Context manager: set up for the server's whole life the cabinets, the run store, the content, the mail watcher,
    the routines' clock and the agent's memory (the LangGraph Postgres checkpointer).

    Also ends the runs a previous stop cut off (runs.end_cut_off), and closes the run store on the way out."""
    check_cabinets()
    await runs.open_store(DATABASE)
    await runs.end_cut_off()
    await content.setup(runs.pool)
    mail.start(asyncio.get_running_loop())
    clock = asyncio.create_task(routines.clock())  # the routines' clock, for the server's whole life
    threading.Thread(target=api.runs.warm_names, daemon=True).start()  # the sidebar's names, read off the first pages
    async with AsyncPostgresSaver.from_conn_string(f"{DATABASE} options='-c search_path={runs.SCHEMA}'") as saver:
        await saver.setup()
        agent.CHECKPOINTER = saver
        yield
    clock.cancel()
    await runs.pool.close()


app = Starlette(
    routes=[
        Route("/ok", ok),
        Route("/gladden/{token}", gladden_callback, methods=["POST"]),
        Route("/api/cabinets", experio_api.listing),
        Route("/api/cabinets", experio_api.create, methods=["POST"]),
        Route("/api/cabinets/{name}", experio_api.one, methods=["GET", "PATCH", "DELETE"]),
        Route("/ui/api/login", login, methods=["POST"]),
        Route("/ui/api/logout", logout, methods=["POST"]),
        Route("/ui/api/me", api.runs.me),
        Route("/ui/api/cabinet", api.runs.choose, methods=["POST"]),
        Route("/runs", listing),
        Route("/runs/{graph}", start, methods=["POST"]),
        Route("/runs/{run_id}", one),
        Route("/runs/{run_id}/steps/{step_id}", step),
        Route("/graphs/{graph}", graph_shape),
        Route("/graphs/{graph}/layout", layout, methods=["GET", "PUT", "DELETE"]),
        Route("/prompts/{call_id:int}", prompt),
        Route("/ui/api/llm-calls", api.runs.prompts_page),
        Route("/ui/api/llm-calls/{session_id}", api.runs.prompt_calls_page),
        Route("/ui/api/invitation/{token}", api.accounts.invitation_page, methods=["GET", "POST"]),
        Route("/ui/api/comptes", api.accounts.accounts_page, methods=["GET", "POST"]),
        Route("/ui/api/logs-serveur", api.journal.journal_page),
        Route("/ui/live", changes),
        Route("/ui/api/settings", api.settings.cabinet_page, methods=["GET", "POST"]),
        Route("/ui/api/comportement", api.behaviour.behaviour_page, methods=["GET", "POST"]),
        Route("/ui/api/routines", api.routines.routines_page, methods=["GET", "POST"]),
        Route("/ui/api/tarik-prompts", api.content.contents_page),
        Route("/ui/api/tarik-prompts-export", api.content.export_contents),
        Route("/ui/api/tarik-prompts-import", api.content.import_contents, methods=["POST"]),
        Route("/ui/api/tarik-prompts/{path:path}", api.content.content_page, methods=["GET", "POST"]),
        Route("/ui/api/discussion", api.chat.chat_page),
        Route("/ui/api/discussion/{session_id}", api.chat.conversation_page),
        Route("/ui/api/discussion/{session_id}", api.chat.send, methods=["POST"]),
        Route("/ui/api/discussion/{session_id}/fichier", api.chat.download),
        Route("/ui/api/workflows", api.runs.workflows_page),
        Route("/ui/api/workflows/{graph}", api.runs.workflow_page),
        Route("/ui/api/accueil", api.chat.dashboard_page),
        Route("/ui/api/harnais", api.harness.harness_page),
        Route("/ui/api/executions", api.runs.runs_page),
        Route("/ui/api/executions/{run_id}", api.runs.run_page),
        Route("/ui/api/executions/{run_id}/stop", api.runs.stop_page, methods=["POST"]),
        Mount("/ui/assets", StaticFiles(directory=api.runs.WEB / "assets", check_dir=False), name="assets"),
        Route("/", api.runs.home),
        Route("/login", api.runs.shell),
        Route("/invitation/{token}", api.runs.shell),
        Route("/ui", api.runs.shell),
        Route("/ui/{rest:path}", api.runs.shell),
    ],
    middleware=[Middleware(Guard)],
    lifespan=lifespan,
)
