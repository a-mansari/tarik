"""Serve the Logs serveur page (/ui/logs-serveur, admins only): who did what in the UI (server/audit.py), newest first,
filterable by action or person, paged back."""

from starlette.requests import Request

from engine import runs
from engine.content import LABELS
from server import audit
from server.api.runs import page

PAGE = 100


async def journal_page(request: Request):
    """Return the journal entries for the page, filtered by ?action= and ?actor=, older than ?before= (an entry id) when
    given.

    Also returns the actor and action lists for the filters, and ``older`` (the id to page back from) when a full
    page of PAGE entries came back."""
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
