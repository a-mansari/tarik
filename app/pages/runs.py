"""The UI's pages, in French for the team: the runs of each cabinet, and each run step by step as it happens. Each
page's data as JSON (/ui/api/...), shown by the Vue app (web/); the words come from content/labels.json. The sidebar
picks the cabinet the pages show (a cookie, CHOSEN), each cabinet under its name in Experio with its database beside
it."""

import json
import time
from pathlib import Path

from markdown_it import MarkdownIt
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

from engine import graphs, prompts, runs
from engine.agent import text_of
from engine.content import LABELS
from tarik.core.cabinets import CABINETS, using
from tarik.core.queries import Q
from tarik.emails.sending import cabinet_name

HERE = Path(__file__).parent
WEB = HERE.parents[1] / "web" / "dist"  # the pages, built (web/: npm run build)


def page(context, status_code=200):
    """A page's data, for the pages' app (web/) to show: dates and other values as text."""
    body = json.dumps(context, ensure_ascii=False, default=str)
    return JSONResponse(json.loads(body), status_code=status_code)


async def shell(request: Request):
    """The pages' app, the same for every address under /ui/ (and the login's, an invitation's): it reads its data
    from /ui/api/... once it runs. Nothing of a cabinet is in it."""
    index = WEB / "index.html"
    if not index.exists():
        return PlainTextResponse("Les pages ne sont pas construites : cd web && npm ci && npm run build", 503)
    return FileResponse(index, headers={"cache-control": "no-cache"})


async def me(request: Request):
    """Who is on the pages, the sidebar's cabinets and labels.json's words: what every page needs."""
    user = request.state.user
    shown = {"name": user["name"], "email": user["email"], "role": user["role"]}
    return page({"user": shown, "admin": is_admin(request), "cabinets": sidebar(request), "labels": LABELS})


CHOSEN = "tarik_cabinet"  # the cookie holding the cabinet the pages show
NAMES, TRIED = {}, {}  # cabinet: its name in Experio; cabinet: when Experio last failed to give it
RETRY_SECONDS = 300


def failed_lately(cabinet):
    """Whether Experio failed to give the cabinet's name less than RETRY_SECONDS ago."""
    return time.monotonic() - TRIED.get(cabinet, -RETRY_SECONDS) < RETRY_SECONDS


def display_name(cabinet):
    """The cabinet's name in Experio (its company), "" while Experio can't give it: asked again RETRY_SECONDS after a
    failure, so a page never waits on Experio twice in a row."""
    if cabinet in CABINETS and cabinet not in NAMES and not failed_lately(cabinet):
        with using(cabinet):
            found = cabinet_name()
        if found:
            NAMES[cabinet] = found
        else:
            TRIED[cabinet] = time.monotonic()
    return NAMES.get(cabinet, "")


def warm_names():
    """Every cabinet's name read once (at start, off the event loop): the first pages need not wait for Experio."""
    for cabinet in sorted(CABINETS):
        display_name(cabinet)


def chosen(request):
    """The cabinet the pages show: the one picked in the sidebar, else the first; None without any."""
    picked = request.cookies.get(CHOSEN)
    if picked in CABINETS:
        return picked
    return min(CABINETS) if CABINETS else None


HOME = "/ui/accueil"  # the cabinet's dashboard: where logging in, the brand and picking a cabinet lead


def sidebar(request):
    """The sidebar's cabinets: [{name (its database), display, chosen}]."""
    now = chosen(request)
    return [{"name": c, "display": display_name(c), "chosen": c == now} for c in sorted(CABINETS)]


async def choose(request: Request):
    """A cabinet picked in the sidebar (posted: its name never in an address): kept in a cookie."""
    cabinet = (await request.json()).get("cabinet", "")
    if cabinet not in CABINETS:
        return JSONResponse({"error": "cabinet inconnu"}, status_code=400)
    answer = JSONResponse({"ok": True})
    answer.set_cookie(CHOSEN, cabinet, max_age=365 * 86400, httponly=True, samesite="lax")
    return answer


def graph_name(graph):
    return LABELS["graphs"].get(graph, graph)


def markdown(text):
    """A message as its writer means it (bold, lists, tables), the client's as Tarik's: raw HTML never kept, unsafe
    links never made (a client types it, or Experio holds what clients typed)."""
    return MARKDOWN.render(text or "")


MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")


def is_admin(request):
    """Whether the person on the page is an admin (server.py's admin_only): what only they may do is not offered."""
    user = getattr(request.state, "user", None)
    return bool(user) and user.get("role") == "admin"


def thinking_of(content):
    """Tarik's thinking in a message's content (its thinking blocks joined), "" without any."""
    if not isinstance(content, list):
        return ""
    blocks = [part for part in content if isinstance(part, dict) and part.get("type") == "thinking"]
    return "\n".join(part.get("thinking", "") for part in blocks)


def conversation(messages):
    """The agent's messages as the team reads them: who said what, the tools Tarik called and what they answered,
    Tarik's thinking kept apart (shown on demand)."""
    shown = []
    for message in messages or []:
        kind = message.get("type")
        if kind == "human":
            text = text_of(message.get("content"))
            shown.append({"who": "Client", "text": text, "text_html": str(markdown(text))})
        elif kind == "ai":
            content = message.get("content")
            thinking = thinking_of(content)
            calls = [
                {"name": call["name"], "args": json.dumps(call.get("args") or {}, ensure_ascii=False)}
                for call in message.get("tool_calls") or []
            ]
            text = text_of(content)
            shown.append(
                {"who": "Tarik", "text": text, "text_html": str(markdown(text)), "thinking": thinking, "calls": calls}
            )
        elif kind == "tool":
            shown.append(
                {
                    "who": f"Outil {message.get('name', '')}",
                    "text": runs.readable(text_of(message.get("content"))),
                    "tool": True,
                }
            )
    return shown


def ran_here(shown):
    """A conversation split where this run began: an agent run is one message answered, in a conversation that may
    have had others before (other runs, their steps and calls on their own pages). Its part begins at the last
    client message."""
    starts = max((i for i, entry in enumerate(shown) if entry["who"] == "Client"), default=0)
    return {"earlier": shown[:starts], "conversation": shown[starts:]}


HIDDEN = {"attachments", "cabinet", "session_id", "call", "done"}  # shown elsewhere, too long to read, or inner


def asked(run):
    """What the run was asked, as the team reads it: {name: value}, each input under its name in plain words
    (labels.json: inputs) and a coded value in words (input_values); an email thread as its sender, subject, message
    and files. Inputs left empty are left out."""
    given = run["input"] or {}
    if run["graph"] == "agent":
        messages = given.get("messages") or []
        last = text_of(messages[-1].get("content")) if messages else ""
        return {LABELS["inputs"].get("message", "Message"): last}
    found = given.get("call") or given
    if isinstance(found.get("thread"), dict):
        thread = found["thread"]
        found = {
            "sender": thread.get("to"),
            "subject": thread.get("subject"),
            "message": thread.get("message"),
            "files": thread.get("files"),
        }
    return {
        LABELS["inputs"].get(key, key): shown_value(key, value)
        for key, value in found.items()
        if key not in HIDDEN and value not in ("", None, 0, False, [])
    }


def shown_value(key, value):
    """An input's value as the team reads it: yes or no, a list's items, a coded value's words."""
    if isinstance(value, bool):
        return "Oui" if value else "Non"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return LABELS["input_values"].get(key, {}).get(str(value), value)


async def home(request: Request):
    return RedirectResponse(HOME)


def dated(found, key="started"):
    """Rows (runs, model calls) each with its ``key`` time as the team reads it (runs.when), under "when"."""
    return [{**row, "when": runs.when(row[key])} for row in found]


async def runs_page(request: Request):
    """The runs of the cabinet picked in the sidebar, latest first (``?graph=`` one workflow's), each with the runs
    started in its steps under it."""
    query = request.query_params
    cabinet, graph = chosen(request), query.get("graph") or None
    found = await runs.recent(cabinet, graph, 100) if cabinet else []
    return page({"runs": dated(found), "cabinet": cabinet, "graph": graph, "graphs": runs.NAMES})


async def run_page(request: Request):
    """One run step by step: what it was asked, what the client reads, its conversation, its model calls."""
    found = await runs.run(request.path_params["run_id"])
    if not found:
        return page({"error": "Exécution introuvable"}, 404)
    output = found["output"] or {}
    is_agent = found["graph"] == "agent"
    shown = conversation(output.get("messages")) if is_agent else []
    calls = []  # the model calls of this run: its conversation's, while it ran
    if is_agent and found["cabinet"] in CABINETS:
        calls = await prompts.calls(found["cabinet"], found["session_id"], found["started"], found["ended"])
    context = {
        "run": {**found, "when": runs.when(found["started"])},
        "asked": asked(found),
        "summary": None if is_agent else output.get("message"),
        **ran_here(shown),
        "prompts": dated(calls, "at"),
    }
    return page(context)


async def openings(cabinet, sessions):
    """{session: {first, subject, sender}} of the cabinet's conversations ``sessions``: their first message's words
    (cut, else its files), and for an email its subject and sender."""
    async with runs.pool.connection() as db:
        rows = await (await db.execute(Q.conversations_first_messages, (cabinet, sessions))).fetchall()
    found = {}
    for session, message, files, subject, sender in rows:
        words = " ".join((message or "").split())
        if len(words) > 90:
            words = words[:90] + "…"
        first = words or ", ".join(files or [])
        found[session] = {"first": first, "subject": subject, "sender": sender}
    return found


async def titles(cabinet, sessions):
    """{session: its title}: an email's subject, else the first message's words, as the Discussions page shows them."""
    found = await openings(cabinet, sessions)
    return {session: opening["subject"] or opening["first"] for session, opening in found.items()}


async def prompts_page(request: Request):
    """The LLM Calls page: each model call Tarik made for the cabinet picked in the sidebar, latest first, with its
    conversation and its run; ``?before=`` pages back."""
    cabinet, before = chosen(request), request.query_params.get("before", "")
    found = await prompts.listed(cabinet, int(before) if before.isdigit() else None) if cabinet else []
    context = {
        "cabinet": cabinet,
        "calls": dated(found, "at"),
        "titles": await titles(cabinet, list({call["session_id"] for call in found})) if found else {},
        "older": found[-1]["id"] if len(found) == 100 else None,
    }
    return page(context)


async def prompt_calls_page(request: Request):
    """A conversation's model calls (of the cabinet picked in the sidebar), each opened to read its whole prompt."""
    cabinet, session = chosen(request), request.path_params["session_id"]
    if cabinet is None:
        return page({"error": "Discussion introuvable"}, 404)
    found = await prompts.calls(cabinet, session)
    if not found:
        return page({"error": "Discussion introuvable"}, 404)
    title = (await titles(cabinet, [session])).get(session)
    return page({"cabinet": cabinet, "session_id": session, "title": title, "calls": dated(found, "at")})


async def workflows_page(request: Request):
    counts = {name: sum(node["kind"] == "step" for node in graphs.shape(name)["nodes"]) for name in runs.NAMES}
    return page({"graphs": runs.NAMES, "counts": counts})


async def workflow_page(request: Request):
    """A workflow's canvas, with one of its runs over it (``?run=``), and its recent runs to pick from."""
    graph = request.path_params["graph"]
    if graph not in runs.NAMES:
        return page({"error": "Workflow introuvable"}, 404)
    run_id = request.query_params.get("run")
    found = await runs.run(run_id) if run_id else None
    context = {
        "graph": graph,
        "run": found and {**found, "when": runs.when(found["started"])},
        "runs": dated(await runs.recent(chosen(request), graph, 30)),
    }
    return page(context)
