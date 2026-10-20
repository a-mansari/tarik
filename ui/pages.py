"""The UI's pages, in French for the team: the runs of each cabinet, and each run step by step as it happens. Server
rendered (Jinja2), no build step; the words come from content/labels.json."""

import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from engine import graphs, prompts, runs
from engine.agent import text_of
from engine.content import LABELS
from tarik.core.cabinets import CABINETS

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


def graph_name(graph):
    return LABELS["graphs"].get(graph, graph)


def graph_description(graph):
    return LABELS["descriptions"]["graphs"].get(graph, "")


def status_name(status):
    return LABELS["statuses"].get(status, status)


templates.env.filters.update(
    when=runs.when, graph_name=graph_name, graph_description=graph_description, status_name=status_name
)
templates.env.globals.update(step_name=graphs.label)


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


def asked(run):
    """What the run was asked, in short: the client's message, or the workflow's own inputs that were given."""
    given = run["input"] or {}
    if run["graph"] == "agent":
        messages = given.get("messages") or []
        return {"Message": text_of(messages[-1].get("content")) if messages else ""}
    call = given.get("call") or {}
    hidden = {"attachments", "cabinet", "session_id"}  # shown elsewhere, or too long to read
    found = {key: value for key, value in call.items() if value not in ("", None, 0, False) and key not in hidden}
    return found or {key: value for key, value in given.items() if key != "call"}


async def home(request: Request):
    return RedirectResponse("/ui/runs")


async def runs_page(request: Request):
    query = request.query_params
    cabinet, graph = query.get("cabinet") or None, query.get("graph") or None
    found = await runs.recent(cabinet, graph, 100)
    context = {
        "runs": found,
        "cabinet": cabinet,
        "graph": graph,
        "cabinets": await runs.cabinets(),
        "graphs": runs.NAMES,
    }
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
        "conversation": conversation(output.get("messages")) if found["graph"] == "agent" else [],
        "labels": LABELS,
        # The model calls of this run: its conversation's, while it ran.
        "prompts": await prompts.calls(found["cabinet"], found["session_id"], found["started"], found["ended"])
        if found["graph"] == "agent" and found["cabinet"] in CABINETS
        else [],
    }
    return templates.TemplateResponse(request, "run.html", context)


async def prompts_page(request: Request):
    """The conversations whose model calls were saved, one cabinet at a time."""
    known = sorted(CABINETS)
    cabinet = request.query_params.get("cabinet")
    cabinet = cabinet if cabinet in known else (known[0] if known else None)
    found = await prompts.conversations(cabinet) if cabinet else []
    return templates.TemplateResponse(
        request, "prompts.html", {"cabinets": known, "cabinet": cabinet, "conversations": found}
    )


async def prompt_calls_page(request: Request):
    """A conversation's model calls, each opened to read its whole prompt."""
    cabinet, session = request.path_params["cabinet"], request.path_params["session_id"]
    if cabinet not in CABINETS:
        return templates.TemplateResponse(request, "missing.html", {}, status_code=404)
    found = await prompts.calls(cabinet, session)
    if not found:
        return templates.TemplateResponse(request, "missing.html", {}, status_code=404)
    return templates.TemplateResponse(
        request,
        "prompt_calls.html",
        {"cabinet": cabinet, "session_id": session, "calls": found, "open": request.query_params.get("call")},
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
        "runs": await runs.recent(graph=graph, limit=30),
        "labels": LABELS,
    }
    return templates.TemplateResponse(request, "workflow.html", context)


def login_page(request: Request, target, failed=False):
    context = {"next": target, "failed": failed}
    return templates.TemplateResponse(request, "login.html", context, status_code=401 if failed else 200)
