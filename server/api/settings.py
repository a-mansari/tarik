"""Serve the Cabinets page: each cabinet's own settings (server/cabinet_settings.py), read and changed by the team in
plain words, each change recorded (who, when, why).

A secret is never sent to the page: only whether it is set, and a field to replace it."""

import asyncio

from starlette.requests import Request

from engine import runs
from engine.content import LABELS
from engine.tools.core import cabinets
from engine.tools.core.model import ADAPTERS
from server import audit, cabinet_settings, experio_api
from server.api.accounts import form_of
from server.api.runs import chosen, page

# The page's sections, in order: which settings each shows.
SECTIONS = {
    "Modèle": ["MODEL_ADAPTER"],
    "Experio": ["CABINET_NAME", "DRY_RUN", "EXPERIO_URL", "ODOO_USER", "ODOO_UID", "ODOO_PASSWORD", "ODOO_API_KEY"],
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
        "inactive": await inactive(),
    }
    return page(context, 400 if error else 200)


SWITCH = asyncio.Lock()  # the last-active check and the write are one step


async def inactive():
    """Return the deactivated cabinets, to switch back on: [{name, display}], display the cabinet's CABINET_NAME."""
    _, known = await asyncio.to_thread(cabinets.vault_settings)
    return [
        {"name": name, "display": own.get("CABINET_NAME", "")}
        for name, own in sorted(known.items())
        if not cabinets.active(own)
    ]


async def activation(request: Request):
    """POST /ui/api/settings/active {cabinet, active: "1" | "0"}: switch a cabinet on or off. Off, Tarik reads none of
    its mail, answers none of its chats and starts none of its runs; its settings and data are kept.

    Returns {cabinet, active}; 400 for a bad request or the last active cabinet (the server needs one), 404 for an
    unknown cabinet."""
    form = await form_of(request, 2)
    cabinet, on = form.get("cabinet", ""), form.get("active") == "1"
    if form.get("active") not in ("0", "1"):
        return page({"error": "active : 1 ou 0"}, 400)
    author = request.state.user["email"]
    async with SWITCH:
        if not on and {cabinet} == cabinets.CABINETS:
            return page({"error": "c'est le dernier cabinet actif : Tarik en a besoin d'un"}, 400)
        try:
            keys = await cabinet_settings.save(
                runs.pool, cabinet, {}, None, author, "Activé" if on else "Désactivé", active=on
            )
        except LookupError:
            return page({"error": "cabinet inconnu"}, 404)
        except ValueError as refused:
            return page({"error": str(refused)}, 400)
        if on:
            await experio_api.serve(cabinet)
        else:
            await asyncio.to_thread(cabinets.reload_cabinets)
    if keys:
        await audit.record(author, "cabinet_reactivated" if on else "cabinet_closed", cabinet)
    return page({"cabinet": cabinet, "active": on})
