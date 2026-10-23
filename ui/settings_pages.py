"""The Cabinets page: each cabinet's own settings (app/cabinet_settings.py), read and changed by the team, in
plain words, each change kept (who, when, why). A secret is never sent to the page: only whether it is set, and a
field to replace it."""

import asyncio
from urllib.parse import parse_qs, quote

from starlette.requests import Request
from starlette.responses import RedirectResponse

from app import cabinet_settings
from engine import runs
from engine.content import LABELS
from tarik.core.model import ADAPTERS
from ui.pages import chosen, templates

# The page's sections, in order: which settings each shows.
SECTIONS = {
    "Modèle": ["MODEL_ADAPTER"],
    "Experio": ["DRY_RUN", "ODOO_USER", "ODOO_UID", "ODOO_PASSWORD", "ODOO_API_KEY"],
    "Boîte mail de Tarik": ["IMAP_USER", "IMAP_PASSWORD", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"],
    "Qui reçoit les emails de Tarik": ["MAIL_ALLOWED_RECIPIENTS", "MAIL_MANAGER"],
}
assert sorted(key for keys in SECTIONS.values() for key in keys) == sorted(cabinet_settings.FIELDS)


def fields(data):
    """The sections with their fields: [(title, [{key, kind, name, description, value}])]."""
    values, words = cabinet_settings.shown(data), LABELS.get("settings", {})
    return [
        (
            title,
            [
                {
                    "key": key,
                    "kind": cabinet_settings.FIELDS[key],
                    "optional": key in cabinet_settings.OPTIONAL,
                    "name": words.get(key, {}).get("name", key),
                    "description": words.get(key, {}).get("description", ""),
                    "value": values[key],
                }
                for key in keys
            ],
        )
        for title, keys in SECTIONS.items()
    ]


async def cabinet_page(request: Request):
    """The settings of the cabinet picked in the sidebar (GET), and a change to them (POST): refused when the sidebar
    picked another since the page showed its own."""
    cabinet = chosen(request)
    if cabinet is None:
        return templates.TemplateResponse(request, "missing.html", {}, status_code=404)
    error, given = None, {}
    if request.method == "POST":
        form = {key: values[0] for key, values in parse_qs((await request.body()).decode(), max_num_fields=40).items()}
        given = {key: value for key, value in form.items() if key in cabinet_settings.FIELDS}
        user = request.state.user
        try:
            if form.get("cabinet") != cabinet:
                raise ValueError("un autre cabinet a été choisi depuis l'ouverture de la page : rechargez-la")
            done = await cabinet_settings.save(runs.pool, cabinet, given, int(form.get("version", 0)), user["email"])
            return RedirectResponse(f"/ui/settings?saved={quote(','.join(done) or '-')}", status_code=303)
        except ValueError as refused:
            error = str(refused)
    try:
        data, version = await asyncio.to_thread(cabinet_settings.latest, cabinet)
    except Exception:
        context = {"cabinet": cabinet, "unreachable": True, "dry_run": None}
        return templates.TemplateResponse(request, "cabinet.html", context, status_code=503)
    sections = fields(data)
    if error:  # the form as it was sent, its secrets never sent back
        for _, items in sections:
            for item in items:
                if item["kind"] != "secret" and item["key"] in given:
                    item["value"] = given[item["key"]]
    saved = request.query_params.get("saved")
    context = {
        "cabinet": cabinet,
        "sections": sections,
        "version": version,
        "dry_run": data.get("DRY_RUN", "1") != "0",
        "error": error,
        "saved": None if saved is None else [key for key in saved.split(",") if key in cabinet_settings.FIELDS],
        "names": {key: LABELS.get("settings", {}).get(key, {}).get("name", key) for key in cabinet_settings.FIELDS},
        "history": await cabinet_settings.history(runs.pool, cabinet),
        "adapters": ADAPTERS,
    }
    return templates.TemplateResponse(request, "cabinet.html", context, status_code=400 if error else 200)
