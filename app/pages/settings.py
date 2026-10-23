"""The Cabinets page: each cabinet's own settings (app/cabinet_settings.py), read and changed by the team, in
plain words, each change kept (who, when, why). A secret is never sent to the page: only whether it is set, and a
field to replace it."""

import asyncio

from starlette.requests import Request

from app import cabinet_settings
from app.pages.accounts import form_of
from app.pages.runs import chosen, page
from engine import runs
from engine.content import LABELS
from tarik.core.model import ADAPTERS

# The page's sections, in order: which settings each shows.
SECTIONS = {
    "Modèle": ["MODEL_ADAPTER"],
    "Experio": ["DRY_RUN", "ODOO_USER", "ODOO_UID", "ODOO_PASSWORD", "ODOO_API_KEY"],
    "Boîte mail de Tarik": ["IMAP_USER", "IMAP_PASSWORD", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"],
    "Qui reçoit les emails de Tarik": ["MAIL_ALLOWED_RECIPIENTS", "MAIL_MANAGER"],
}
assert sorted(key for keys in SECTIONS.values() for key in keys) == sorted(cabinet_settings.FIELDS)


def field(key, value, words):
    """One setting as the page shows it: {key, kind, optional, name, description, value}."""
    return {
        "key": key,
        "kind": cabinet_settings.FIELDS[key],
        "optional": key in cabinet_settings.OPTIONAL,
        "name": words.get(key, {}).get("name", key),
        "description": words.get(key, {}).get("description", ""),
        "value": value,
    }


def fields(data):
    """The sections with their fields: [(title, [{key, kind, name, description, value}])], a secret's value only
    whether it is set (True or False), never itself."""
    values, words = cabinet_settings.shown(data), LABELS.get("settings", {})
    return [(title, [field(key, values[key], words) for key in keys]) for title, keys in SECTIONS.items()]


async def cabinet_page(request: Request):
    """The settings of the cabinet picked in the sidebar (GET), and a change to them (POST, JSON): refused when the
    sidebar picked another since the page showed its own. A change answers the page again, with what it saved."""
    cabinet = chosen(request)
    if cabinet is None:
        return page({"error": "aucun cabinet"}, 404)
    error, given, saved = None, {}, None
    if request.method == "POST":
        form = await form_of(request, 40)
        given = {key: value for key, value in form.items() if key in cabinet_settings.FIELDS}
        user = request.state.user
        try:
            if form.get("cabinet") != cabinet:
                raise ValueError("un autre cabinet a été choisi depuis l'ouverture de la page : rechargez-la")
            saved = await cabinet_settings.save(runs.pool, cabinet, given, int(form.get("version", 0)), user["email"])
        except ValueError as refused:
            error = str(refused)
    try:
        data, version = await asyncio.to_thread(cabinet_settings.latest, cabinet)
    except Exception:
        return page({"cabinet": cabinet, "unreachable": True, "dry_run": None}, 503)
    sections = fields(data)
    if error:  # the form as it was sent, its secrets never sent back
        for _, items in sections:
            for item in items:
                if item["kind"] != "secret" and item["key"] in given:
                    item["value"] = given[item["key"]]
    history = await cabinet_settings.history(runs.pool, cabinet)
    context = {
        "cabinet": cabinet,
        "sections": sections,
        "version": version,
        "dry_run": data.get("DRY_RUN", "1") != "0",
        "error": error,
        "saved": saved,
        "names": {key: LABELS.get("settings", {}).get(key, {}).get("name", key) for key in cabinet_settings.FIELDS},
        "history": [{**item, "at": runs.when(item["at"])} for item in history],
        "adapters": ADAPTERS,
    }
    return page(context, 400 if error else 200)
