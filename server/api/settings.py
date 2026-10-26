"""Serve the Cabinets page: each cabinet's own settings (server/cabinet_settings.py), read and changed by the team in
plain words, each change recorded (who, when, why).

A secret is never sent to the page: only whether it is set, and a field to replace it."""

import asyncio

from starlette.requests import Request

from engine import runs
from engine.content import LABELS
from engine.tools.core.model import ADAPTERS
from server import cabinet_settings
from server.api.accounts import form_of
from server.api.runs import chosen, page

# The page's sections, in order: which settings each shows.
SECTIONS = {
    "Modèle": ["MODEL_ADAPTER"],
    "Experio": ["DRY_RUN", "ODOO_USER", "ODOO_UID", "ODOO_PASSWORD", "ODOO_API_KEY"],
    "Boîte mail de Tarik": ["IMAP_USER", "IMAP_PASSWORD", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"],
    "Qui reçoit les emails de Tarik": ["MAIL_ALLOWED_RECIPIENTS", "MAIL_MANAGER"],
}
assert sorted(key for keys in SECTIONS.values() for key in keys) == sorted(cabinet_settings.FIELDS)


def field(key, value, words):
    """Return one setting as the page shows it: {key, kind, optional, name, description, value}, its words from
    labels.json (``words``)."""
    return {
        "key": key,
        "kind": cabinet_settings.FIELDS[key],
        "optional": key in cabinet_settings.OPTIONAL,
        "name": words.get(key, {}).get("name", key),
        "description": words.get(key, {}).get("description", ""),
        "value": value,
    }


def fields(data):
    """Return the page's sections with their fields: [(title, [field dicts])], a secret's value only True (set) or
    False."""
    values, words = cabinet_settings.shown(data), LABELS.get("settings", {})
    return [(title, [field(key, values[key], words) for key in keys]) for title, keys in SECTIONS.items()]


async def cabinet_page(request: Request):
    """GET or POST /ui/api/settings: return the settings of the cabinet chosen in the sidebar, after saving a change
    when posted.

    POST takes JSON {cabinet, version, <settings>}; it is refused when the sidebar has chosen another cabinet since
    the page opened, or when Vault holds a newer version (see cabinet_settings.save). After a refusal (400) the
    form's non-secret values are sent back as typed. 404 without any cabinet; 503 when Vault cannot be read."""
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
