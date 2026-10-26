"""Read and change each cabinet's own settings, from the Cabinets page and the Experio API.

The settings: its Experio login, its mailbox, who may receive Tarik's emails, whether Tarik really writes to Experio
(DRY_RUN), and its model. They live only in Vault (secret/tarik/cabinets/<cabinet>), except the model (MODEL_ADAPTER,
not a secret), which is kept in the orchestrator's table cabinet_models. Vault is written with the page's own AppRole
(tarik-settings, vault/setup.sh), which may only read and update the cabinets' paths: never the shared settings. A
secret (a password, a key) is never shown nor stored anywhere except Vault: it can only be replaced. Each change is
recorded (who, when, why, which settings: table settings_changes) and used at once. Any other key in the same Vault
path is kept unchanged."""

import asyncio
import re
import threading
from email.utils import parseaddr

import httpx
from psycopg.types.json import Jsonb

from engine.tools.core import cabinets, model
from engine.tools.core.model import ADAPTERS
from engine.tools.core.queries import Q
from server import audit
from server.accounts import EMAIL

ROLE = "/run/vault-settings"  # the page's AppRole files, mounted by docker-compose.yml

# The settings the page shows, in its order, each with what it holds: text, a secret (never shown, only replaced),
# a yes/no (DRY_RUN: 1 or 0), a positive integer, an address, or a comma-separated list of addresses and @domains.
FIELDS = {
    "DRY_RUN": "flag",
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
    "MODEL_ADAPTER": "adapter",
}
# Left empty: Tarik does without (MODEL_ADAPTER: the first of ADAPTERS).
OPTIONAL = {"SMTP_FROM", "MAIL_MANAGER", "MAIL_ALLOWED_RECIPIENTS", "ODOO_API_KEY", "MODEL_ADAPTER"}
IN_POSTGRES = "MODEL_ADAPTER"  # kept in cabinet_models, not in Vault
# Every cabinet setting on the page, but the API's own.
assert set(FIELDS) - {IN_POSTGRES} == cabinets.CABINET_SETTINGS - {"ACTIVE"}

# As engine.tools.emails.sending reads the list: an address, an @domain, or a pattern with "*"
# ("test.*@experio.ma", "*").
RECIPIENT = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+|@[^@\s,]+\.[^@\s,]+|[^\s,]*\*[^\s,]*")

SECRETS = {key for key, kind in FIELDS.items() if kind == "secret"}

_lock = threading.Lock()
_token = {"token": "", "token_until": 0.0}


def latest(cabinet, served_only=True):
    """Return the cabinet's current settings and Vault's version of them, as (data, version).

    data holds Vault's keys plus MODEL_ADAPTER from Postgres. With ``served_only`` False, a deactivated cabinet is
    read too (the Experio API). Raises LookupError when Vault has no such cabinet (or, with ``served_only``, Tarik
    does not serve it)."""
    if cabinet not in cabinets.CABINETS and (served_only or not cabinets.NAME.fullmatch(cabinet)):
        raise LookupError("cabinet inconnu")
    found = _vault("GET", f"/v1/secret/data/tarik/cabinets/{cabinet}")
    if not found or not found["data"] or found["data"]["data"] is None:
        raise LookupError("cabinet inconnu")
    return {**found["data"]["data"], IN_POSTGRES: model.chosen_adapter(cabinet)}, found["data"]["metadata"]["version"]


def shown(data):
    """Return the settings as the page shows them: {key: value} for every FIELDS key, a secret only as True (set) or
    False."""
    return {key: bool(data.get(key)) if kind == "secret" else data.get(key, "") for key, kind in FIELDS.items()}


def checked(key, value):
    """Raise ValueError (in plain French) when Tarik could not use ``value`` (already trimmed) for setting ``key``.

    The value is only checked, never tidied up: an invalid one is refused. An empty value is accepted only for
    OPTIONAL settings."""
    kind = FIELDS[key]
    if value == "":
        if key in OPTIONAL:
            return
        raise ValueError(f"{key} ne peut pas être vide")
    if kind == "adapter" and value not in ADAPTERS:
        raise ValueError(f"{key} vaut l'un de : {', '.join(ADAPTERS)}")
    if kind == "flag" and value not in ("0", "1"):
        raise ValueError(f"{key} vaut 1 (simulation) ou 0 (écritures réelles)")
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


def changed(before, given):
    """Return what the form ``given`` ({key: text}) changes in the current settings ``before``, as {key: new value}.

    A non-secret value is trimmed of surrounding spaces, checked (see checked) and otherwise kept as typed; any other
    difference, a space inside included, counts as a change. A secret is kept exactly as typed, and an empty secret
    leaves the current one unchanged. Keys outside FIELDS are ignored."""
    found = {}
    for key, kind in FIELDS.items():
        if key not in given:
            continue
        value = given[key] if kind == "secret" else given[key].strip()
        if kind == "secret" and value == "":
            continue
        if kind != "secret":
            checked(key, value)
        unset = next(iter(ADAPTERS)) if kind == "adapter" else ""  # an adapter unset: the first
        if value != (before.get(key) or unset):
            found[key] = value
    return found


def _vault(method, path, **kwargs):
    """Send a request to Vault with the page's own AppRole token (renewed once 80% of its lease has passed) and return
    Vault's answer."""
    with _lock:
        token = cabinets.vault_token(ROLE, _token)
    return cabinets.vault_request(method, path, token, **kwargs)


def recorded(key, before, after):
    """Return one setting's change as settings_changes stores it: {key, before, after}, both values None for a secret.
    Return one setting's change as settings_changes stores it: {key, before, after}, both values None for a secret."""
    if key in SECRETS:
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
    vault = {key: value for key, value in found.items() if key != IN_POSTGRES}
    if vault:
        path = f"/v1/secret/data/tarik/cabinets/{cabinet}"
        kept = {key: value for key, value in data.items() if key != IN_POSTGRES}
        try:  # cas: only over the version read, whoever writes in between
            written = await asyncio.to_thread(
                _vault, "POST", path, json={"options": {"cas": version}, "data": {**kept, **vault}}
            )
        except httpx.HTTPStatusError as error:
            raise ValueError(MOVED) from error
        version = written["data"]["version"]
    changes = [recorded(key, data.get(key, ""), value) for key, value in found.items()]
    async with pool.connection() as db:
        if IN_POSTGRES in found:
            await db.execute(Q.model_adapter_set, (cabinet, found[IN_POSTGRES]))
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
    return found


async def create(pool, cabinet, given, author):
    """Create cabinet ``cabinet`` in Vault with the settings ``given`` (see complete), record it, and return the keys
    set.

    The write uses Vault's check-and-set with cas 0, which Vault accepts only when the path does not exist yet, so an
    existing cabinet is never overwritten. Raises ValueError for an invalid cabinet name or settings, or when the
    cabinet already exists in Vault."""
    if not cabinets.NAME.fullmatch(cabinet) or cabinet == "public" or cabinet.startswith("pg_"):
        raise ValueError("nom de cabinet : la base Experio exacte, en minuscules (lettres, chiffres, _)")
    data = complete(given)
    try:
        vault = {key: value for key, value in data.items() if key != IN_POSTGRES}
        written = await asyncio.to_thread(
            _vault, "POST", f"/v1/secret/data/tarik/cabinets/{cabinet}", json={"options": {"cas": 0}, "data": vault}
        )
    except httpx.HTTPStatusError as error:
        raise ValueError("ce cabinet existe déjà") from error
    changes = [recorded(key, None, value) for key, value in data.items()]
    async with pool.connection() as db:
        if IN_POSTGRES in data:
            await db.execute(Q.model_adapter_set, (cabinet, data[IN_POSTGRES]))
        await db.execute(Q.settings_changed, (cabinet, author, "Créé", Jsonb(changes), written["data"]["version"]))
    return list(data)


async def history(pool, cabinet, limit=30):
    """Return the cabinet's last ``limit`` settings changes, newest first: [{author, note, changes, version, at}]."""
    async with pool.connection() as db:
        rows = await (await db.execute(Q.settings_history, (cabinet, limit))).fetchall()
    return [
        {"author": author, "note": note, "changes": changes, "version": version, "at": at}
        for author, note, changes, version, at in rows
    ]
