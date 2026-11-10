"""Serve the Cabinets page: each cabinet's own settings (server/cabinet_settings.py), read and changed by the team in
plain words, each change recorded (who, when, why).

A secret is never sent to the page: only whether it is set, and a field to replace it."""

import asyncio

import httpx
from starlette.requests import Request

from engine import runs
from engine.content import LABELS
from engine.tools.core import cabinets
from engine.tools.core.model import ADAPTERS
from server import audit, cabinet_settings, experio_api, shared_settings
from server.api.accounts import form_of
from server.api.runs import CHOSEN, chosen, page, switched_off

# The page's sections, in order: which settings each shows.
SECTIONS = {
    "Modèle": ["MODEL_ADAPTER"],
    "Experio": ["CABINET_NAME", "DRY_RUN", "EXPERIO_URL", "ODOO_USER", "ODOO_UID", "ODOO_PASSWORD", "ODOO_API_KEY"],
    "Boîte mail de Tarik": ["IMAP_USER", "IMAP_PASSWORD", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"],
    "Qui reçoit les emails de Tarik": ["MAIL_ALLOWED_RECIPIENTS", "MAIL_MANAGER"],
}
assert sorted(key for keys in SECTIONS.values() for key in keys) == sorted(cabinet_settings.FIELDS)


def field(key, value, words, fields=cabinet_settings.FIELDS, optional=cabinet_settings.OPTIONAL):
    """Return one setting as the page shows it: {key, kind, optional, name, description, value}, its words from
    labels.json (``words``); ``fields`` and ``optional`` are the cabinets' unless the general page gives its own."""
    return {
        "key": key,
        "kind": fields[key],
        "optional": key in optional,
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
    picked = request.cookies.get(CHOSEN)
    if picked in switched_off(request):  # a deactivated cabinet: only its switch, to turn it back on
        return page({"cabinet": picked, "active": False}, 400 if request.method == "POST" else 200)
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
        return page({"cabinet": cabinet, "active": True, "unreachable": True, "dry_run": None}, 503)
    sections = fields(data)
    if error:
        retyped(sections, given)
    context = {
        "cabinet": cabinet,
        "sections": sections,
        "version": version,
        "dry_run": data.get("DRY_RUN", "1") != "0",
        "error": error,
        "saved": saved,
        "names": {key: LABELS.get("settings", {}).get(key, {}).get("name", key) for key in cabinet_settings.FIELDS},
        "history": await changes_of(cabinet),
        "adapters": ADAPTERS,
        "active": True,
    }
    return page(context, 400 if error else 200)


# The general page's sections (the settings every cabinet shares), each with its name and what it does, in plain words.
GENERAL_SECTIONS = {
    "Serveur mail": {
        "IMAP_HOST": ("Serveur IMAP", "Où Tarik lit les emails, pour tous les cabinets."),
        "IMAP_PORT": ("Port IMAP", "993 en général."),
        "IMAP_FOLDER": ("Dossier lu", "Le dossier de la boîte que Tarik lit (INBOX)."),
        "SMTP_HOST": ("Serveur SMTP", "Par où Tarik envoie ses emails."),
        "SMTP_PORT": ("Port SMTP", "465 (SSL) ou 587 (STARTTLS)."),
        "MAIL_CERT_SHA256": (
            "Empreinte du certificat",
            "Seulement si le serveur mail a un certificat auto-signé : son empreinte SHA-256.",
        ),
    },
    "Modèle": {
        "MODEL_API_URL": ("Adresse du modèle", "L'adresse du fournisseur du modèle. Vide : celle du .env."),
        "MODEL_API_KEY": ("Clé du modèle", "Pour un cabinet dont le modèle n'est pas Claude Code."),
        "CLAUDE_CODE_OAUTH_TOKEN": (
            "Connexion Claude Code",
            "Pour un cabinet dont le modèle est Claude Code : le jeton de `claude setup-token`.",
        ),
    },
    "Experio": {
        "EXPERIO_API_KEY": (
            "Clé de l'API Tarik",
            "La clé que l'app Experio montre pour créer, modifier ou fermer des cabinets. La changer coupe le lien "
            "jusqu'à ce qu'Experio ait la nouvelle.",
        ),
        "PUBLIC_URL": (
            "Adresse publique de Tarik",
            "Pour les liens vers les formulaires que Tarik envoie aux clients.",
        ),
    },
    "Gladden": {
        "GLADDEN_URL": ("Adresse de Gladden", "Le classeur de pièces d'Experio. Vide : Gladden n'est pas utilisé."),
        "GLADDEN_CALLBACK_URL": ("Adresse de retour", "L'adresse de ce serveur, telle que Gladden l'atteint."),
    },
    "WhatsApp (Sparrow)": {
        "SPARROW_URL": ("Adresse de Sparrow", "Le relais WhatsApp d'Experio. Vide : pas de WhatsApp."),
        "SPARROW_SENDER_ID": ("Expéditeur", "L'identifiant de l'expéditeur WhatsApp d'Experio, chez Sparrow."),
        "SPARROW_TOKEN": ("Jeton de Sparrow", "Le jeton principal de Sparrow."),
    },
}
assert sorted(key for keys in GENERAL_SECTIONS.values() for key in keys) == sorted(shared_settings.FIELDS)
GENERAL_WORDS = {
    key: {"name": name, "description": text} for keys in GENERAL_SECTIONS.values() for key, (name, text) in keys.items()
}
GENERAL_SECTIONS = {title: list(keys) for title, keys in GENERAL_SECTIONS.items()}  # the page's order: the keys only


def general_fields(data):
    """Return the general page's sections: [(title, [field dicts])], a secret's value only True (set) or False."""
    values = cabinet_settings.shown(data, shared_settings.FIELDS)
    return [
        (
            title,
            [field(key, values[key], GENERAL_WORDS, shared_settings.FIELDS, shared_settings.OPTIONAL) for key in keys],
        )
        for title, keys in GENERAL_SECTIONS.items()
    ]


def retyped(sections, given):
    """Put back in ``sections`` (fields) the non-secret values the form ``given`` held, as typed: after a refusal the
    form is not lost. A secret is never sent back."""
    for _, items in sections:
        for item in items:
            if item["kind"] != "secret" and item["key"] in given:
                item["value"] = given[item["key"]]


async def changes_of(key):
    """Return the last settings changes of ``key`` (a cabinet, or the general settings) with their time in words."""
    return [{**item, "at": runs.when(item["at"])} for item in await cabinet_settings.history(runs.pool, key)]


async def general_page(request: Request):
    """GET or POST /ui/api/settings/general: return the settings every cabinet shares, after saving a change when
    posted. POST takes JSON {version, note?, <settings>}; refused (400) when Vault holds a newer version
    (shared_settings.save) or a value is not usable, the non-secret values then sent back as typed. 503 when Vault
    cannot be read, or the page's role may not read these paths (vault/setup.sh)."""
    error, given, saved = None, {}, None
    if request.method == "POST":
        form = await form_of(request, 40)
        given = {key: value for key, value in form.items() if key in shared_settings.FIELDS}
        try:
            saved = await shared_settings.save(
                runs.pool, given, form.get("version", ""), request.state.user["email"], form.get("note", "")
            )
        except ValueError as refused:
            error = str(refused)
        except httpx.HTTPError:  # Vault down or sealed while saving
            error = "Vault ne répond pas : rien n'est sûr de ce qui a été enregistré, rechargez la page"
    try:
        stores, versions = await asyncio.to_thread(shared_settings.latest)
    except Exception:
        return page({"unreachable": True}, 503)
    sections = general_fields(shared_settings.merged(stores))
    if error:
        retyped(sections, given)
    context = {
        "sections": sections,
        "version": shared_settings.version_of(versions),
        "error": error,
        "saved": saved,
        "names": {key: words["name"] for key, words in GENERAL_WORDS.items()},
        "history": await changes_of(shared_settings.GENERAL),
    }
    return page(context, 400 if error else 200)


SWITCH = asyncio.Lock()  # the last-active check and the write are one step


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
