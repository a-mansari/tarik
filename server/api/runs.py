"""Serve the run pages and what every page shares: the runs of each cabinet, each run step by step, the workflows, and
the LLM calls.

Each page's data is JSON (/ui/api/...), shown by the Vue app (server/web/), in French for the team; the words come
from content/labels.json. The sidebar chooses the cabinet the pages show (a cookie, CHOSEN), each cabinet listed
under its name in Experio with its database name beside it."""

import json
import time
from pathlib import Path

from markdown_it import MarkdownIt
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

from engine import graphs, prompts, runs
from engine.agent import ROUTINE_NAME, text_of
from engine.content import LABELS
from engine.tools.core.cabinets import CABINETS, using
from engine.tools.core.queries import Q
from engine.tools.dossiers.routines import title_in
from engine.tools.emails.sending import cabinet_name

HERE = Path(__file__).parent
WEB = HERE.parent / "web" / "dist"  # the pages, built (server/web/: npm run build)


def page(context, status_code=200):
    """Return ``context`` as a JSON response for the pages' app, with dates and other non-JSON values turned into text.
    Return ``context`` as a JSON response for the pages' app, with dates and other non-JSON values turned into text."""
    body = json.dumps(context, ensure_ascii=False, default=str)
    return JSONResponse(json.loads(body), status_code=status_code)


async def shell(request: Request):
    """Return the pages' app (server/web/dist/index.html), the same for every address under /ui/, the login and an
    invitation.

    The app then reads its data from /ui/api/...; nothing of a cabinet is in it. 503 when the pages are not built."""
    index = WEB / "index.html"
    if not index.exists():
        return PlainTextResponse("Les pages ne sont pas construites : cd server/web && npm ci && npm run build", 503)
    return FileResponse(index, headers={"cache-control": "no-cache"})


async def me(request: Request):
    """GET /ui/api/me: return what every page needs: who is logged in, whether they are an admin, the sidebar's
    cabinets, and labels.json's words."""
    user = request.state.user
    shown = {"name": user["name"], "email": user["email"], "role": user["role"]}
    return page({"user": shown, "admin": is_admin(request), "cabinets": sidebar(request), "labels": LABELS})


CHOSEN = "tarik_cabinet"  # the cookie holding the cabinet the pages show
NAMES, TRIED = {}, {}  # cabinet: its name in Experio; cabinet: when Experio last failed to give it
RETRY_SECONDS = 300


def failed_lately(cabinet):
    """True when Experio failed to give the cabinet's name less than RETRY_SECONDS ago."""
    return time.monotonic() - TRIED.get(cabinet, -RETRY_SECONDS) < RETRY_SECONDS


def display_name(cabinet):
    """Return the cabinet's name in Experio (its company), cached once read; "" while Experio cannot give it.

    After a failure, Experio is asked again only RETRY_SECONDS later, so a page never waits on Experio twice in a
    row."""
    if cabinet in CABINETS and cabinet not in NAMES and not failed_lately(cabinet):
        with using(cabinet):
            found = cabinet_name()
        if found:
            NAMES[cabinet] = found
        else:
            TRIED[cabinet] = time.monotonic()
    return NAMES.get(cabinet, "")


def warm_names():
    """Read every cabinet's name once at start (off the event loop), so the first pages need not wait for Experio."""
    for cabinet in sorted(CABINETS):
        display_name(cabinet)


def chosen(request):
    """Return the cabinet the pages show: the one chosen in the sidebar (cookie CHOSEN), else the first by name; None
    when no cabinet is served."""
    picked = request.cookies.get(CHOSEN)
    if picked in CABINETS:
        return picked
    return min(CABINETS) if CABINETS else None


HOME = "/ui/accueil"  # the cabinet's dashboard: where logging in, the brand and picking a cabinet lead


def sidebar(request):
    """Return the sidebar's cabinets: [{name (its database), display (its Experio name), chosen}]."""
    now = chosen(request)
    return [{"name": c, "display": display_name(c), "chosen": c == now} for c in sorted(CABINETS)]


async def choose(request: Request):
    """POST /ui/api/cabinet {cabinet}: remember the cabinet chosen in the sidebar in a cookie (never in an address); 400
    for an unknown cabinet."""
    cabinet = (await request.json()).get("cabinet", "")
    if cabinet not in CABINETS:
        return JSONResponse({"error": "cabinet inconnu"}, status_code=400)
    answer = JSONResponse({"ok": True})
    answer.set_cookie(CHOSEN, cabinet, max_age=365 * 86400, httponly=True, samesite="lax")
    return answer


def graph_name(graph):
    """Return the workflow's name from labels.json, else its key."""
    return LABELS["graphs"].get(graph, graph)


def markdown(text):
    """Return ``text`` rendered from Markdown to HTML (bold, lists, tables), for the client's messages as for Tarik's.

    Raw HTML is never kept and unsafe links are never made: a client types the text, or Experio holds what clients
    typed."""
    return MARKDOWN.render(text or "")


MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")


def is_admin(request):
    """True when the person on the page is an admin (server.py's admin_only): admin-only actions are then offered."""
    user = getattr(request.state, "user", None)
    return bool(user) and user.get("role") == "admin"


def thinking_of(content):
    """Return Tarik's thinking blocks in a message ``content`` joined into one text, "" when there is none."""
    if not isinstance(content, list):
        return ""
    blocks = [part for part in content if isinstance(part, dict) and part.get("type") == "thinking"]
    return "\n".join(part.get("thinking", "") for part in blocks)


def conversation(messages):
    """Return the agent's ``messages`` as the team reads them: [{who, text, ...}].

    Client messages, Tarik's (with its text as HTML, the tools it called, and its thinking kept apart, shown on
    demand), and each tool's answer."""
    shown = []
    for message in messages or []:
        kind = message.get("type")
        if kind == "human":  # a client's, or what a routine sent (ROUTINE_NAME)
            text = text_of(message.get("content"))
            who = "Routine" if message.get("name") == ROUTINE_NAME else "Client"
            shown.append({"who": who, "text": text, "text_html": str(markdown(text))})
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
    """Split a conversation (``shown``, from conversation) where this run began, as {earlier, conversation}.

    An agent run answers one message, in a conversation that may have had earlier runs (shown on their own pages);
    this run's part starts at the last client message."""
    starts = max((i for i, entry in enumerate(shown) if entry["who"] == "Client"), default=0)
    return {"earlier": shown[:starts], "conversation": shown[starts:]}


# Shown elsewhere, too long to read, inner, or an admin's own (a routine's report address: its Routines page)
HIDDEN = {"attachments", "cabinet", "session_id", "call", "done", "notify"}


def asked(run):
    """Return what ``run`` was asked, as the team reads it: {input name: value}.

    Each input is under its plain-words name (labels.json: inputs) and a coded value in words (input_values); an
    email thread is shown as its sender, subject, message and files; for the agent, its last message. Empty inputs
    and HIDDEN ones are left out."""
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
    """Return an input's ``value`` as the team reads it: Oui/Non for a bool, a list's items joined, a coded value's
    words."""
    if isinstance(value, bool):
        return "Oui" if value else "Non"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return LABELS["input_values"].get(key, {}).get(str(value), value)


async def home(request: Request):
    """GET /: redirect to HOME, the cabinet's dashboard."""
    return RedirectResponse(HOME)


def tokens(count):
    """Return a token count as read at a glance: "850", "12,4 k", "1,25 M"."""
    for size, unit in ((1_000_000, " M"), (1_000, " k")):
        if count >= size * 0.9995:  # 999 600 reads "1 M", never "1e+03 k"
            return f"{count / size:.3g}".replace(".", ",") + unit
    return str(count)


def dollars(cost):
    """Return a cost in US dollars as "3,21 $", under a cent as "< 0,01 $"."""
    return "< 0,01 $" if 0 < cost < 0.01 else f"{cost:.2f} $".replace(".", ",")


def cost_shown(cost, priced):
    """Return model calls' cost (None: no price known) as the team reads it; missing some model's price
    (``priced`` False), it ends in "+ ?"."""
    return "prix inconnu" if cost is None else dollars(cost) + ("" if priced else " + ?")


def usage_shown(fresh, cached, output, cost, priced):
    """Return model calls' tokens and cost as the team reads them: {tokens (all of them), detail (input, cached,
    output), cost (cost_shown)}."""
    return {
        "tokens": tokens(fresh + cached + output),
        "detail": f"{tokens(fresh + cached)} en entrée (dont {tokens(cached)} en cache), {tokens(output)} en sortie",
        "cost": cost_shown(cost, priced),
    }


async def spending(cabinet, statement, keys):
    """Return {key: usage_shown} of prompts.spent(): the model calls of each run, conversation or agent call."""
    found = await prompts.spent(cabinet, statement, keys)
    return {str(key): usage_shown(*row) for key, row in found.items()}


def dated(found, key="started"):
    """Return ``found`` rows (runs, model calls), each with its ``key`` time as the team reads it (runs.when) under
    "when"."""
    return [{**row, "when": runs.when(row[key])} for row in found]


async def runs_page(request: Request):
    """GET /ui/api/executions: return the latest 100 runs of the cabinet chosen in the sidebar (one workflow's with
    ?graph=), newest first.

    Each run is followed by the runs started in its steps; ``usage`` holds each run's model calls (usage_shown), its
    nested runs' included."""
    query = request.query_params
    cabinet, graph = chosen(request), query.get("graph") or None
    found = await runs.recent(cabinet, graph, 100) if cabinet else []
    spent = await spending(cabinet, Q.usage_of_runs, [run["run_id"] for run in found])
    return page({"runs": dated(found), "usage": spent, "cabinet": cabinet, "graph": graph, "graphs": runs.NAMES})


async def run_page(request: Request):
    """GET /ui/api/executions/{run_id}: return one run step by step: what it was asked, its summary for the client, its
    conversation and its model calls, with their tokens and cost: ``usage`` the run's (its nested runs' included),
    ``call_usage`` each call's (usage_shown); 404 when unknown."""
    found = await runs.run(request.path_params["run_id"])
    if not found:
        return page({"error": "Exécution introuvable"}, 404)
    output = found["output"] or {}
    is_agent = found["graph"] == "agent"
    shown = conversation(output.get("messages")) if is_agent else []
    calls, usage, call_usage = [], None, {}  # the model calls of this run: its conversation's, while it ran
    if found["cabinet"] in CABINETS:
        cabinet = found["cabinet"]
        usage = (await spending(cabinet, Q.usage_of_runs, [found["run_id"]])).get(found["run_id"])
        if is_agent:
            calls = await prompts.calls(cabinet, found["session_id"], found["started"], found["ended"])
            call_usage = await spending(cabinet, Q.usage_of_calls, [call["id"] for call in calls])
    context = {
        "run": {**found, "when": runs.when(found["started"])},
        "asked": asked(found),
        "summary": None if is_agent else output.get("message"),
        **ran_here(shown),
        "prompts": dated(calls, "at"),
        "usage": usage,
        "call_usage": call_usage,
    }
    return page(context)


async def openings(cabinet, sessions):
    """Return {session: {first, subject, sender}} for the cabinet's conversations ``sessions``.

    first is the first message's words (cut at 90 characters), else its file names; subject and sender are an
    email's, None otherwise; routine is {id, name} of the routine that began the conversation (its instruction, or
    its report a reply answers): name what it was called then (its run's routine_name, else its email's subject
    without its schedule: before routine_name was kept), None for none."""
    async with runs.pool.connection() as db:
        rows = await (await db.execute(Q.conversations_first_messages, (cabinet, sessions))).fetchall()
    found = {}
    for session, message, files, subject, sender, routine in rows:
        words = " ".join((message or "").split())
        if len(words) > 90:
            words = words[:90] + "…"
        first = words or ", ".join(files or [])
        began = routine and {
            "id": routine.get("routine_id"),
            "name": routine.get("routine_name") or title_in(routine.get("subject") or ""),
        }
        found[session] = {"first": first, "subject": subject, "sender": sender, "routine": began or None}
    return found


async def titles(cabinet, sessions):
    """Return {session: title} as the Discussions page shows them: an email's subject, else the first message's words.
    Return {session: title} as the Discussions page shows them: an email's subject, else the first message's words."""
    found = await openings(cabinet, sessions)
    return {session: opening["subject"] or opening["first"] for session, opening in found.items()}


async def prompts_page(request: Request):
    """GET /ui/api/llm-calls: return the model calls Tarik made for the cabinet chosen in the sidebar, newest first,
    with their conversations' titles and each call's tokens and cost (``usage``, usage_shown).

    ?before= (a call id) pages back; ``older`` is set when a full page of 100 came back."""
    cabinet, before = chosen(request), request.query_params.get("before", "")
    found = await prompts.listed(cabinet, int(before) if before.isdigit() else None) if cabinet else []
    context = {
        "cabinet": cabinet,
        "calls": dated(found, "at"),
        "titles": await titles(cabinet, list({call["session_id"] for call in found})) if found else {},
        "older": found[-1]["id"] if len(found) == 100 else None,
        "usage": await spending(cabinet, Q.usage_of_calls, [call["id"] for call in found]),
    }
    return page(context)


async def prompt_calls_page(request: Request):
    """GET /ui/api/llm-calls/{session_id}: return a conversation's model calls (cabinet chosen in the sidebar), its
    title, and the tokens and cost (usage_shown) of each call (``usage``) and of the whole conversation (``total``,
    its tools' model calls included); 404 when it has none."""
    cabinet, session = chosen(request), request.path_params["session_id"]
    if cabinet is None:
        return page({"error": "Discussion introuvable"}, 404)
    found = await prompts.calls(cabinet, session)
    if not found:
        return page({"error": "Discussion introuvable"}, 404)
    title = (await titles(cabinet, [session])).get(session)
    usage = await spending(cabinet, Q.usage_of_calls, [call["id"] for call in found])
    total = (await spending(cabinet, Q.usage_of_sessions, [session])).get(session)
    context = {"cabinet": cabinet, "session_id": session, "title": title, "calls": dated(found, "at")}
    return page({**context, "usage": usage, "total": total})


async def workflows_page(request: Request):
    """GET /ui/api/workflows: return the workflows' names and how many steps each has."""
    counts = {name: sum(node["kind"] == "step" for node in graphs.shape(name)["nodes"]) for name in runs.NAMES}
    return page({"graphs": runs.NAMES, "counts": counts})


async def workflow_page(request: Request):
    """GET /ui/api/workflows/{graph}: return a workflow, with one of its runs to show over the canvas (?run=) and its 30
    latest runs to choose from; 404 for an unknown workflow."""
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
