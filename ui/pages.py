"""The UI's pages, in French for the team: the runs of each cabinet, and each run step by step as it happens. Server
rendered (Jinja2), no build step; the words come from content/labels.json. The sidebar picks the cabinet the pages
show (a cookie, CHOSEN), each cabinet under its name in Experio with its database beside it."""

import json
import time
from pathlib import Path
from urllib.parse import parse_qs

from markdown_it import MarkdownIt
from markupsafe import Markup
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from engine import graphs, prompts, runs
from engine.agent import text_of
from engine.content import LABELS
from tarik.core.cabinets import CABINETS, using
from tarik.core.queries import Q
from tarik.emails.sending import cabinet_name

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

CHOSEN = "tarik_cabinet"  # the cookie holding the cabinet the pages show
NAMES, TRIED = {}, {}  # cabinet: its name in Experio; cabinet: when Experio last failed to give it
RETRY_SECONDS = 300


def display_name(cabinet):
    """The cabinet's name in Experio (its company), "" while Experio can't give it: asked again RETRY_SECONDS after a
    failure, so a page never waits on Experio twice in a row."""
    if (
        cabinet in CABINETS
        and cabinet not in NAMES
        and time.monotonic() - TRIED.get(cabinet, -RETRY_SECONDS) >= RETRY_SECONDS
    ):
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
    return picked if picked in CABINETS else (min(CABINETS) if CABINETS else None)


HOME = "/ui/accueil"  # the cabinet's dashboard: where logging in, the brand and picking a cabinet lead


def sidebar(request):
    """The sidebar's cabinets: [{name (its database), display, chosen}]."""
    now = chosen(request)
    return [{"name": c, "display": display_name(c), "chosen": c == now} for c in sorted(CABINETS)]


async def choose(request: Request):
    """A cabinet picked in the sidebar (posted: its name never in an address): kept in a cookie, then its
    dashboard."""
    cabinet = parse_qs((await request.body()).decode()).get("cabinet", [""])[0]
    answer = RedirectResponse(HOME, status_code=303)
    if cabinet in CABINETS:
        answer.set_cookie(CHOSEN, cabinet, max_age=365 * 86400, httponly=True, samesite="lax")
    return answer


def graph_name(graph):
    return LABELS["graphs"].get(graph, graph)


def graph_description(graph):
    return LABELS["descriptions"]["graphs"].get(graph, "")


def status_name(status):
    return LABELS["statuses"].get(status, status)


def markdown(text):
    """Tarik's reply as it means it (bold, lists, tables): its raw HTML never kept, unsafe links never made (what
    Experio holds, and so what Tarik says, was typed by clients)."""
    return Markup(MARKDOWN.render(text or ""))


MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")

templates.env.filters.update(
    markdown=markdown,
    when=runs.when,
    graph_name=graph_name,
    graph_description=graph_description,
    status_name=status_name,
)


def is_admin(request):
    """Whether the person on the page is an admin (server.py's admin_only): what only they may do is not offered."""
    user = getattr(request.state, "user", None)
    return bool(user) and user.get("role") == "admin"


templates.env.globals.update(step_name=graphs.label, is_admin=is_admin, sidebar=sidebar, display_name=display_name)


def asset(path):
    """A static file's address with its version (its last change): no browser keeps an old copy of a changed one."""
    return f"/ui/static/{path}?v={int((HERE / 'static' / path).stat().st_mtime)}"


templates.env.globals.update(asset=asset)  # a follow-up's Traitement steps too


def conversation(messages):
    """The agent's messages as the team reads them: who said what, the tools Tarik called and what they answered,
    Tarik's thinking kept apart (shown on demand)."""
    shown = []
    for message in messages or []:
        kind = message.get("type")
        if kind == "human":
            shown.append({"who": "Client", "text": text_of(message.get("content"))})
        elif kind == "ai":
            content = message.get("content")
            thinking = (
                "\n".join(
                    part.get("thinking", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "thinking"
                )
                if isinstance(content, list)
                else ""
            )
            calls = [
                {"name": call["name"], "args": json.dumps(call.get("args") or {}, ensure_ascii=False)}
                for call in message.get("tool_calls") or []
            ]
            shown.append({"who": "Tarik", "text": text_of(content), "thinking": thinking, "calls": calls})
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
        return {LABELS["inputs"].get("message", "Message"): text_of(messages[-1].get("content")) if messages else ""}
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


async def runs_page(request: Request):
    query = request.query_params
    cabinet, graph = chosen(request), query.get("graph") or None
    found = await runs.recent(cabinet, graph, 100) if cabinet else []
    context = {"runs": found, "cabinet": cabinet, "graph": graph, "graphs": runs.NAMES}
    template = "runs_rows.html" if query.get("partial") else "runs.html"
    return templates.TemplateResponse(request, template, context)


async def run_page(request: Request):
    found = await runs.run(request.path_params["run_id"])
    if not found:
        return templates.TemplateResponse(request, "missing.html", {}, status_code=404)
    output = found["output"] or {}
    context = {
        "run": found,
        "asked": asked(found),
        "summary": output.get("message") if found["graph"] != "agent" else None,
        **ran_here(conversation(output.get("messages")) if found["graph"] == "agent" else []),
        "labels": LABELS,
        # The model calls of this run: its conversation's, while it ran.
        "prompts": await prompts.calls(found["cabinet"], found["session_id"], found["started"], found["ended"])
        if found["graph"] == "agent" and found["cabinet"] in CABINETS
        else [],
    }
    return templates.TemplateResponse(request, "run.html", context)


async def openings(cabinet, sessions):
    """{session: {first, subject, sender}} of the cabinet's conversations ``sessions``: their first message's words
    (cut, else its files), and for an email its subject and sender."""
    async with runs.pool.connection() as db:
        rows = await (await db.execute(Q.conversations_first_messages, (cabinet, sessions))).fetchall()
    found = {}
    for session, message, files, subject, sender in rows:
        words = " ".join((message or "").split())
        first = words[:90] + ("…" if len(words) > 90 else "") or ", ".join(files or [])
        found[session] = {"first": first, "subject": subject, "sender": sender}
    return found


async def titles(cabinet, sessions):
    """{session: its title}: an email's subject, else the first message's words, as the Discussions page shows them."""
    found = await openings(cabinet, sessions)
    return {session: opening["subject"] or opening["first"] for session, opening in found.items()}


async def prompts_page(request: Request):
    """The LLM logs: each model call Tarik made for the cabinet picked in the sidebar, latest first, with its
    conversation and its run; ``?before=`` pages back."""
    cabinet, before = chosen(request), request.query_params.get("before", "")
    found = await prompts.listed(cabinet, int(before) if before.isdigit() else None) if cabinet else []
    context = {
        "cabinet": cabinet,
        "calls": found,
        "titles": await titles(cabinet, list({call["session_id"] for call in found})) if found else {},
        "older": found[-1]["id"] if len(found) == 100 else None,
    }
    return templates.TemplateResponse(request, "prompts.html", context)


async def prompt_calls_page(request: Request):
    """A conversation's model calls (of the cabinet picked in the sidebar), each opened to read its whole prompt."""
    cabinet, session = chosen(request), request.path_params["session_id"]
    if cabinet is None:
        return templates.TemplateResponse(request, "missing.html", {}, status_code=404)
    found = await prompts.calls(cabinet, session)
    if not found:
        return templates.TemplateResponse(request, "missing.html", {}, status_code=404)
    return templates.TemplateResponse(
        request,
        "prompt_calls.html",
        {
            "cabinet": cabinet,
            "session_id": session,
            "title": (await titles(cabinet, [session])).get(session),
            "calls": found,
            "open": request.query_params.get("call"),
        },
    )


async def workflows_page(request: Request):
    counts = {name: sum(node["kind"] == "step" for node in graphs.shape(name)["nodes"]) for name in runs.NAMES}
    return templates.TemplateResponse(request, "workflows.html", {"graphs": runs.NAMES, "counts": counts})


async def workflow_page(request: Request):
    graph = request.path_params["graph"]
    if graph not in runs.NAMES:
        return templates.TemplateResponse(request, "missing.html", {}, status_code=404)
    run_id = request.query_params.get("run")
    found = await runs.run(run_id) if run_id else None
    context = {
        "graph": graph,
        "run": found,
        "run_id": found["run_id"] if found else None,
        "runs": await runs.recent(chosen(request), graph, 30),
        "labels": LABELS,
    }
    return templates.TemplateResponse(request, "workflow.html", context)


def login_page(request: Request, target, failed=False):
    context = {"next": target, "failed": failed}
    return templates.TemplateResponse(request, "login.html", context, status_code=401 if failed else 200)
