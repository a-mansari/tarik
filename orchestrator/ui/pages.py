"""The UI's pages, in French for the team: the runs of each cabinet, and each run step by step as it happens. Server
rendered (Jinja2), no build step; the words come from content/labels.json."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from engine import runs
from engine.content import LABELS

HERE = Path(__file__).parent
TIME_ZONE = ZoneInfo("Africa/Casablanca")  # the cabinets' time
templates = Jinja2Templates(directory=HERE / "templates")


def when(iso):
    """A run's or a step's time as the team reads it: 20/10 10:33:22, Casablanca time."""
    return datetime.fromisoformat(iso).astimezone(TIME_ZONE).strftime("%d/%m %H:%M:%S") if iso else ""


def graph_name(graph):
    return LABELS["graphs"].get(graph, graph)


def step_name(graph, node):
    return LABELS["steps"].get(graph, {}).get(node, node)


def status_name(status):
    return LABELS["statuses"].get(status, status)


templates.env.filters.update(when=when, graph_name=graph_name, status_name=status_name)
templates.env.globals.update(step_name=step_name)


def text_of(content):
    """A message's text: its string, or its text parts."""
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content or [] if isinstance(part, dict) and part.get("type") == "text")


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
            thinking = "\n".join(
                part.get("thinking", "") for part in content if isinstance(part, dict) and part.get("type") == "thinking"
            ) if isinstance(content, list) else ""
            calls = [
                {"name": call["name"], "args": json.dumps(call.get("args") or {}, ensure_ascii=False)}
                for call in message.get("tool_calls") or []
            ]
            shown.append({"who": "Tarik", "text": text_of(content), "thinking": thinking, "calls": calls})
        elif kind == "tool":
            shown.append({"who": f"Outil {message.get('name', '')}", "text": runs.readable(text_of(message.get("content"))), "tool": True})
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
    context = {"runs": found, "cabinet": cabinet, "graph": graph, "cabinets": await runs.cabinets(), "graphs": runs.NAMES}
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
    }
    return templates.TemplateResponse(request, "run.html", context)
