"""Serve the Cabinets page: each cabinet's own settings (server/cabinet_settings.py), read and changed by the team in
plain words, each change recorded (who, when, why).

A secret is never sent to the page: only whether it is set, and a field to replace it."""

import asyncio
import http.client
import xmlrpc.client

import httpx
from starlette.requests import Request

from engine import memory, runs
from engine.content import LABELS
from engine.tools.core import cabinets, experio
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
    "Emails ignorés": ["MAIL_IGNORED_SENDERS"],
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


async def memory_state():
    """Return Tarik's memory as the general page shows it: {on, failures}, failures [{cabinet, at, kind, error}] (at in
    words) or None when Hindsight cannot be reached; so a Claude token gone bad, which Hindsight only learns while it
    extracts, is seen here."""
    failed = await memory.failures()
    return {
        "on": memory.on(),
        "failures": None if failed is None else [{**one, "at": runs.when(one["at"])} for one in failed],
    }


async def general_page(request: Request):
    """GET or POST /ui/api/settings/general: return the settings every cabinet shares, after saving a change when
    posted. POST takes JSON {version, note?, <settings>}; refused (400) when Vault holds a newer version
    (shared_settings.save) or a value is not usable, the non-secret values then sent back as typed. 503 when Vault
    cannot be read, or the page's role may not read these paths (vault/setup.sh). The answer holds the memory's state
    (memory_state)."""
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
        "memory": await memory_state(),
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


REFUSED = "Experio a refusé ce login, cette clé, ou cet identifiant d'utilisateur (ODOO_UID) : vérifiez-les."
NO_ANSWER = "Experio ne répond pas, ou cette adresse n'est pas la bonne : vérifiez EXPERIO_URL."
UNKNOWN_BASE = "Experio n'a pas accepté la demande : la base de ce cabinet existe-t-elle ? Vérifiez son nom."
MISSING = "Un réglage nécessaire à la connexion manque : {key}."


async def checked_login(cabinet):
    """Return "" when Tarik logs in to ``cabinet``'s Experio with the settings Vault holds for it, else a sentence
    saying what to check (never Experio's own words). The cabinet is not served yet: the login is tried on its own."""
    data, _ = await asyncio.to_thread(cabinet_settings.latest, cabinet, False)
    url = (data.get("EXPERIO_URL") or experio.ODOO_URL).rstrip("/")
    try:
        if not experio.OWN_EXPERIO.fullmatch(url):
            return NO_ANSWER
        expected = int(data["ODOO_UID"])
        key = data.get("ODOO_API_KEY") or data["ODOO_PASSWORD"]
        await asyncio.to_thread(experio.check_login, cabinet, url, data["ODOO_USER"], key, expected)
    except PermissionError:
        return REFUSED
    except xmlrpc.client.Fault:  # Experio failed for this request: the database does not exist, for one
        return UNKNOWN_BASE
    except KeyError as missing:
        return MISSING.format(key=missing.args[0])
    except ValueError, OSError, http.client.HTTPException, xmlrpc.client.ProtocolError:
        return NO_ANSWER
    return ""


async def new_cabinet_page(request: Request):
    """GET or POST /ui/api/settings/new: the form that adds a cabinet, and its creation when posted.

    GET returns {sections, adapters}: every setting, empty (DRY_RUN starts in simulation). POST takes JSON {name,
    <settings>}: the cabinet is created in Vault deactivated (or a deactivated one reactivated, what the form leaves
    empty kept), Tarik's login to its Experio is tried, and only when it works is the cabinet switched on and served
    with its mailbox watched. When it fails the cabinet stays deactivated, so a typing mistake never stops the server
    from starting and nothing of it runs, and the answer is 400 with the sentence to fix; send the form again to retry.
    Returns {name, mailbox_watched} (201) when it works; 400 for an invalid or reserved name, an invalid setting or a
    cabinet already active. One cabinet is added at a time (SWITCH)."""
    if request.method == "GET":
        return page({"sections": fields({}), "adapters": ADAPTERS})
    form = await form_of(request, 40)
    cabinet = form.get("name", "").strip()
    author = request.state.user["email"]
    given = {key: value for key, value in form.items() if key in cabinet_settings.FIELDS}
    async with SWITCH:
        try:
            _, _, watched = await experio_api.add(cabinet, given, author, verify=checked_login)
        except experio_api.Exists:
            return page({"error": "ce cabinet existe déjà : ses réglages sont dans la page Réglages"}, 400)
        except experio_api.Unverified as problem:
            return page(
                {"error": f"{problem} Le cabinet est créé mais désactivé : corrigez et validez à nouveau."}, 400
            )
        except ValueError as refused:
            return page({"error": str(refused)}, 400)
    return page({"name": cabinet, "mailbox_watched": watched}, 201)
