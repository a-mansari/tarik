"""Read and change each cabinet's own settings, from the Cabinets page and the Experio API.

The settings: its Experio login, its mailbox, who may receive Tarik's emails, whether Tarik really writes to Experio
(DRY_RUN), its LLM (a global one it picks, or its own) and its memory. They live only in Vault
(secret/tarik/cabinets/<cabinet>). Vault is written with the page's own AppRole
(tarik-settings, vault/setup.sh), which may only read and update the cabinets' paths and the shared settings' two
(server/shared_settings.py, the general settings page), nothing else. A
secret (a password, a key) is never shown nor stored anywhere except Vault: it can only be replaced. Each change is
recorded (who, when, why, which settings: table settings_changes) and used at once. Any other key in the same Vault
path is kept unchanged."""

import asyncio
import re
import threading
from email.utils import parseaddr

import httpx
from psycopg.types.json import Jsonb

from engine.tools.core import cabinets, experio, model
from engine.tools.core.model import CLAUDE, CUSTOM, LEGACY, TYPES
from engine.tools.core.queries import Q
from server import audit
from server.accounts import EMAIL

ROLE = "/run/vault-settings"  # the page's AppRole files, mounted by docker-compose.yml

# The settings the page shows, in its order, each with what it holds: text, a secret (never shown, only replaced),
# a yes/no (DRY_RUN: 1 or 0), an HTTPS address, a positive integer, an address, or a comma-separated list of
# addresses and @domains.
FIELDS = {
    "CABINET_NAME": "text",
    "DRY_RUN": "flag",
    "EXPERIO_URL": "url",
    "ODOO_USER": "text",
    "ODOO_UID": "integer",
    "ODOO_PASSWORD": "secret",
    "ODOO_API_KEY": "secret",
    "IMAP_USER": "text",
    "IMAP_PASSWORD": "secret",
    "SMTP_USER": "text",
    "SMTP_PASSWORD": "secret",
    "SMTP_FROM": "sender",
    "MAIL_MANAGER": "address",
    "MAIL_ALLOWED_RECIPIENTS": "recipients",
    "LLM_CHOICE": "llm",
    "LLM_TYPE": "llm_type",
    "LLM_MODEL": "text",
    "LLM_URL": "web",
    "LLM_KEY": "secret",
    "CABINET_MEMORY_ENABLED": "toggle",
}
# Left empty: Tarik does without (LLM_CHOICE: Claude; EXPERIO_URL: ODOO_URL). The custom LLM's own are required when
# the cabinet picks "custom" (custom_gaps), not otherwise.
OPTIONAL = {
    "CABINET_NAME",
    "EXPERIO_URL",
    "SMTP_FROM",
    "MAIL_MANAGER",
    "MAIL_ALLOWED_RECIPIENTS",
    "ODOO_API_KEY",
    "LLM_CHOICE",
    "LLM_TYPE",
    "LLM_MODEL",
    "LLM_URL",
    "LLM_KEY",
    "CABINET_MEMORY_ENABLED",
}
CUSTOM_LLM = ("LLM_TYPE", "LLM_MODEL", "LLM_URL", "LLM_KEY")  # the cabinet's own LLM, when it picks CUSTOM
# Every cabinet setting on the page, but the API's own.
assert set(FIELDS) == cabinets.CABINET_SETTINGS - {"ACTIVE"}
LLM_NAME = re.compile(r"[a-z][a-z0-9_-]{0,30}")  # a global LLM's name

# As engine.tools.emails.sending reads the list: an address, an @domain, or a pattern with "*"
# ("test.*@experio.ma", "*").
RECIPIENT = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+|@[^@\s,]+\.[^@\s,]+|[^\s,]*\*[^\s,]*")

# The shared settings' kinds that are a pattern: an HTTPS address (its host first, no login@ in it, no backslash), and
# a certificate's SHA-256.
PATTERNS = {
    "web": (re.compile(r"https://[^\s/?#@\\]+([/?#]\S*)?"), "est une adresse HTTPS, sans espace"),
    "sha256": (re.compile(r"[0-9A-Fa-f]{64}"), "est l'empreinte SHA-256 du certificat : 64 caractères hexadécimaux"),
}

SECRETS = {key for key, kind in FIELDS.items() if kind == "secret"}

_lock = threading.Lock()
_token = {"token": "", "token_until": 0.0}


def latest(cabinet, served_only=True):
    """Return the cabinet's current settings and Vault's version of them, as (data, version).

    data holds Vault's keys, with LLM_CHOICE the LLM the cabinet had picked before the LLMs when it has not chosen one
    since (the adapter kept in Postgres, model.LEGACY). With ``served_only`` False, a deactivated cabinet is
    read too (the Experio API). Raises LookupError when Vault has no such cabinet (or, with ``served_only``, Tarik
    does not serve it)."""
    if cabinet not in cabinets.CABINETS and (served_only or not cabinets.NAME.fullmatch(cabinet)):
        raise LookupError("cabinet inconnu")
    found = _vault("GET", f"/v1/secret/data/tarik/cabinets/{cabinet}")
    if not found or not found["data"] or found["data"]["data"] is None:
        raise LookupError("cabinet inconnu")
    data = found["data"]["data"]
    if not data.get("LLM_CHOICE") and LEGACY.get(model.chosen_adapter(cabinet)):
        data = {**data, "LLM_CHOICE": LEGACY[model.chosen_adapter(cabinet)]}
    return data, found["data"]["metadata"]["version"]


def shown(data, fields=FIELDS):
    """Return the settings as the page shows them: {key: value} for every ``fields`` key, a secret only as True (set)
    or False."""
    return {key: bool(data.get(key)) if kind == "secret" else data.get(key, "") for key, kind in fields.items()}


def checked(key, value, fields=FIELDS, optional=OPTIONAL):
    """Raise ValueError (in plain French) when Tarik could not use ``value`` (already trimmed) for setting ``key``.

    The value is only checked, never tidied up: an invalid one is refused. An empty value is accepted only for
    ``optional`` settings. ``fields`` and ``optional`` are the cabinets' (FIELDS, OPTIONAL) unless the shared
    settings' page gives its own (server/shared_settings.py)."""
    kind = fields[key]
    if value == "":
        if key in optional:
            return
        raise ValueError(f"{key} ne peut pas être vide")
    if kind == "llm" and value != CUSTOM and not LLM_NAME.fullmatch(value):
        raise ValueError(f"{key} est le nom d'un LLM global (minuscules, chiffres, - et _) ou {CUSTOM}")
    if kind == "llm_type" and value not in TYPES:
        raise ValueError(f"{key} vaut l'un de : {', '.join(TYPES)}")
    if kind == "toggle" and value not in ("0", "1"):
        raise ValueError(f"{key} vaut 1 (activée) ou 0 (désactivée)")
    if kind == "flag" and value not in ("0", "1"):
        raise ValueError(f"{key} vaut 1 (simulation) ou 0 (écritures réelles)")
    if kind == "url" and not experio.OWN_EXPERIO.fullmatch(value):
        raise ValueError(f"{key} est l'adresse HTTPS d'Experio, sans chemin")
    if kind in PATTERNS and not PATTERNS[kind][0].fullmatch(value):
        raise ValueError(f"{key} {PATTERNS[kind][1]}")
    if kind == "integer" and not (value.isdigit() and int(value) > 0):
        raise ValueError(f"{key} est un nombre entier positif, sans espace (l'identifiant de Tarik dans Experio)")
    if kind == "address" and not EMAIL.fullmatch(value):
        raise ValueError(f"{key} n'est pas une adresse email (sans espace autour)")
    if kind == "sender" and not EMAIL.fullmatch(parseaddr(value)[1]):
        raise ValueError(f"{key} n'est pas un expéditeur (« Nom <adresse> » ou une adresse)")
    if kind != "recipients":
        return
    # The recipients, each entry as Tarik reads it: split at the commas, its spaces left out, an empty one skipped.
    entries = [entry.strip() for entry in value.split(",")]
    wrong = [entry for entry in entries if entry and not RECIPIENT.fullmatch(entry)]
    if wrong:
        raise ValueError(f"{key} : ni adresse, ni @domaine, ni motif avec * : {', '.join(wrong)}")


# The addresses Tarik sends secrets to, and the secrets: a changed address needs them typed again.
GUARDED = {"EXPERIO_URL": ("ODOO_PASSWORD", "ODOO_API_KEY"), "LLM_URL": ("LLM_KEY",)}


def custom_gaps(data):
    """Raise ValueError (in plain French) when ``data`` (a cabinet's settings) picks its own LLM (CUSTOM) and lacks
    something to run it: a model, a key, and for an API an address."""
    if data.get("LLM_CHOICE") != CUSTOM:
        return
    needs = ["LLM_MODEL", "LLM_KEY"] + (
        ["LLM_URL"] if (data.get("LLM_TYPE") or next(iter(TYPES))) == "anthropic-api" else []
    )
    missing = [key for key in needs if not data.get(key)]
    if missing:
        raise ValueError(f"un LLM propre au cabinet demande : {', '.join(missing)}")


def changed(before, given, fields=FIELDS, optional=OPTIONAL):
    """Return what the form ``given`` ({key: text}) changes in the current settings ``before``, as {key: new value}.

    A non-secret value is trimmed of surrounding spaces, checked (see checked) when it differs from the stored one and
    otherwise kept as typed; any other
    difference, a space inside included, counts as a change. A secret is kept exactly as typed, and an empty secret
    leaves the current one unchanged. Keys outside FIELDS are ignored.

    Raises ValueError when an address changes (GUARDED) without the secrets already set typed again: Tarik sends them
    to that address, so changing it must not hand the stored ones to another server."""
    found = {}
    for key, kind in fields.items():
        if key not in given:
            continue
        value = given[key] if kind == "secret" else given[key].strip()
        if kind == "secret" and value == "":
            continue
        unset = {"llm": CLAUDE, "llm_type": next(iter(TYPES)), "toggle": "1"}.get(kind, "")  # unset: Claude, on, ...
        same = value == (before.get(key) or unset)
        if same and value:  # unchanged: not checked again, so a stored value blocks no other change
            continue
        if kind != "secret":
            checked(key, value, fields, optional)
        if not same:
            found[key] = value
    for url, secrets in GUARDED.items():
        kept = [key for key in secrets if before.get(key) and not given.get(key)]
        if url in found and kept:
            raise ValueError(f"pour changer {url}, retapez aussi {', '.join(kept)}")
    return found


def _vault(method, path, **kwargs):
    """Send a request to Vault with the page's own AppRole token (renewed once 80% of its lease has passed) and return
    Vault's answer."""
    with _lock:
        token = cabinets.vault_token(ROLE, _token)
    return cabinets.vault_request(method, path, token, **kwargs)


def recorded(key, before, after, secrets=SECRETS):
    """Return one setting's change as settings_changes stores it: {key, before, after}, both values None for a key in
    ``secrets`` (the cabinets' unless the shared settings' page gives its own)."""
    if key in secrets:
        return {"key": key, "before": None, "after": None}
    return {"key": key, "before": before, "after": after}


MOVED = "ces réglages ont changé depuis l'ouverture de la page : rechargez-la et recommencez"


async def save(pool, cabinet, given, seen, author, note="", active=None):
    """Apply the form ``given`` to the cabinet's settings (see changed) and return the keys changed (list; [] when
    nothing changes).

    Args:
        seen: the Vault version the page showed; ValueError (MOVED) when Vault now holds a newer one. None (the
            Experio API) writes over the latest version.
        author, note: who made the change and why, for settings_changes and the Journal.
        active: True or False to reactivate or deactivate the cabinet in the same write (ACTIVE); None leaves it.
    The Vault write uses check-and-set on the version read (cas), so a concurrent write makes it fail with ValueError
    (MOVED) instead of being overwritten. The change is recorded in settings_changes and the Journal and used at
    once."""
    data, version = await asyncio.to_thread(latest, cabinet, seen is not None)
    if seen is not None and version != seen:
        raise ValueError(MOVED)
    found = changed(data, given)
    if active is not None and cabinets.active(data) != active:
        found["ACTIVE"] = "1" if active else "0"
    if not found:
        return []
    custom_gaps({**data, **found})
    path = f"/v1/secret/data/tarik/cabinets/{cabinet}"
    try:  # cas: only over the version read, whoever writes in between
        written = await asyncio.to_thread(
            _vault, "POST", path, json={"options": {"cas": version}, "data": {**data, **found}}
        )
    except httpx.HTTPStatusError as error:
        raise ValueError(MOVED) from error
    version = written["data"]["version"]
    changes = [recorded(key, data.get(key, ""), value) for key, value in found.items()]
    async with pool.connection() as db:
        await db.execute(Q.settings_changed, (cabinet, author, note.strip()[:300], Jsonb(changes), version))
    await asyncio.to_thread(cabinets.reread_vault)
    await audit.record(author, "settings_saved", cabinet, keys=list(found), note=note.strip()[:300])
    return list(found)


def complete(given):
    """Return a new cabinet's settings from ``given``, each checked as typed (a secret as is), with DRY_RUN 1
    (simulation) unless given.

    Raises ValueError (in plain French) for unknown keys or a missing required setting."""
    unknown = sorted(set(given) - set(FIELDS))
    if unknown:
        raise ValueError(f"réglages inconnus : {', '.join(unknown)}")
    found = changed({}, {"DRY_RUN": "1", **given})
    missing = [key for key in FIELDS if key not in OPTIONAL and not found.get(key)]
    if missing:
        raise ValueError(f"réglages manquants : {', '.join(missing)}")
    custom_gaps(found)
    return found


async def create(pool, cabinet, given, author, active=True):
    """Create cabinet ``cabinet`` in Vault with the settings ``given`` (see complete), record it, and return the keys
    set.

    The write uses Vault's check-and-set with cas 0, which Vault accepts only when the path does not exist yet, so an
    existing cabinet is never overwritten. Raises ValueError for an invalid cabinet name or settings, or when the
    cabinet already exists in Vault. ``active`` False creates it deactivated (ACTIVE "0"), to be switched on once it has
    been checked."""
    if not cabinets.NAME.fullmatch(cabinet):
        raise ValueError(
            "nom de cabinet : la base Experio exacte, en minuscules (lettres, chiffres, _), une lettre d'abord"
        )
    if cabinets.reserved(cabinet):
        raise ValueError("nom de cabinet : ce nom est réservé (un schéma de Tarik ou de Postgres)")
    data = complete(given)
    try:
        vault = data | ({} if active else {"ACTIVE": "0"})
        written = await asyncio.to_thread(
            _vault, "POST", f"/v1/secret/data/tarik/cabinets/{cabinet}", json={"options": {"cas": 0}, "data": vault}
        )
    except httpx.HTTPStatusError as error:
        raise ValueError("ce cabinet existe déjà") from error
    changes = [recorded(key, None, value) for key, value in data.items()]
    async with pool.connection() as db:
        await db.execute(Q.settings_changed, (cabinet, author, "Créé", Jsonb(changes), written["data"]["version"]))
    return list(data)
