"""The Logs serveur page (/ui/logs-serveur), an admin's: who did what in the UI, latest first (app/audit.py), one
action or one person at a time, paged back."""

from starlette.requests import Request

from app import audit
from engine import runs
from engine.content import LABELS
from ui.pages import page

PAGE = 100


async def journal_page(request: Request):
    """The entries (?action=, ?actor=, ?before= to page back), their times as the team reads them."""
    query = request.query_params
    action = query.get("action") if query.get("action") in audit.ACTIONS else None
    before = int(query["before"]) if query.get("before", "").isdigit() else None
    entries, actors = await audit.listed(action, query.get("actor") or None, before, PAGE)
    context = {
        "entries": [{**entry, "at": runs.when(entry["at"])} for entry in entries],
        "actors": actors,
        "actions": [(name, LABELS.get("audit", {}).get(name, name)) for name in audit.ACTIONS],
        "words": LABELS.get("audit", {}),
        "action": action,
        "actor": query.get("actor") or "",
        "older": entries[-1]["id"] if len(entries) == PAGE else None,
    }
    return page(context)
