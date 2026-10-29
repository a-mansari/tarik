"""Serve the Comportement page: how Tarik behaves for the cabinet chosen in the sidebar (its own schema, never
another's), each switch or delay changed by the team as a manager can ask Tarik in a conversation
(engine/tools/dossiers/behaviour.py); each change in the journal."""

from starlette.requests import Request

from engine import runs
from engine.calls import within
from engine.tools.core.app import ToolError
from engine.tools.dossiers.behaviour import SETTINGS, change, settings
from server import audit
from server.api.accounts import form_of
from server.api.runs import chosen, page


async def behaviour_page(request: Request):
    """GET or POST /ui/api/comportement: return the behaviour settings of the cabinet chosen in the sidebar, after
    changing one when posted.

    POST takes JSON {cabinet, key, value}; refused (400, the error said) when the sidebar has chosen another cabinet
    since the page opened, or the value is not one the setting takes. 404 without any cabinet."""
    cabinet = chosen(request)
    if cabinet is None:
        return page({"error": "aucun cabinet"}, 404)
    error = None
    if request.method == "POST":
        form = await form_of(request, 5)
        key, author = form.get("key"), request.state.user["email"]
        try:
            if form.get("cabinet") != cabinet:
                raise ToolError("un autre cabinet a été choisi depuis l'ouverture de la page : rechargez-la")
            if key not in SETTINGS:
                raise ToolError("réglage inconnu")
            await within(cabinet, change, key, form.get("value"), author)
            await audit.record(author, "settings_saved", cabinet, keys=[key])
        except ToolError as refused:
            error = str(refused)
    found = [
        {**item, "changed_at": item["changed_at"] and runs.when(item["changed_at"])}
        for item in await within(cabinet, settings)
    ]
    return page({"cabinet": cabinet, "settings": found, "error": error}, 400 if error else 200)
