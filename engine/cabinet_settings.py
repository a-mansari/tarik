"""Each cabinet's own settings, read and changed from the Cabinets page: its Experio login, its mailbox, who may
receive Tarik's emails, whether Tarik really writes to Experio (DRY_RUN). Vault (secret/tarik/cabinets/<cabinet>)
stays their only home: they are written there with the page's own AppRole (tarik-settings, vault/setup.sh), which may
only read and update the cabinets' paths, never the shared settings nor make a new cabinet. A secret (a password, a
key) is never shown nor kept anywhere but Vault: it is only replaced. Each change is recorded (who, when, why, which
settings: table settings_changes) and in use at once. The n8n stack's own keys in the same paths are kept as they
are."""

import asyncio
import re
import threading
import time
from email.utils import parseaddr
from pathlib import Path

import httpx
from psycopg.types.json import Jsonb

from tarik.core import cabinets
from tarik.core.queries import Q

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
}
OPTIONAL = {"SMTP_FROM", "MAIL_MANAGER", "MAIL_ALLOWED_RECIPIENTS", "ODOO_API_KEY"}  # left empty: Tarik does without
assert set(FIELDS) == cabinets.CABINET_SETTINGS  # every cabinet setting on the page, none other

ADDRESS = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")
RECIPIENT = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+|@[^@\s,]+\.[^@\s,]+")

_lock = threading.Lock()
_token = {"value": "", "until": 0.0}


def latest(cabinet):
    """The cabinet's settings as Vault holds them now, and their version: (data, version)."""
    if cabinet not in cabinets.CABINETS:
        raise ValueError("cabinet inconnu")
    found = _vault("GET", f"/v1/secret/data/tarik/cabinets/{cabinet}")["data"]
    return found["data"], found["metadata"]["version"]


def shown(data):
    """Settings as the page shows them: {key: value}, a secret only as True (set) or False."""
    return {key: bool(data.get(key)) if kind == "secret" else data.get(key, "") for key, kind in FIELDS.items()}


def checked(key, value):
    """ValueError, in plain French, when Tarik could not use ``value`` (already trimmed) for setting ``key``. Only
    checked, never rewritten inside: refused rather than tidied up."""
    kind = FIELDS[key]
    if value == "":
        if key in OPTIONAL:
            return
        raise ValueError(f"{key} ne peut pas être vide")
    if kind == "flag" and value not in ("0", "1"):
        raise ValueError(f"{key} vaut 1 (simulation) ou 0 (écritures réelles)")
    if kind == "integer" and not (value.isdigit() and int(value) > 0):
        raise ValueError(f"{key} est un nombre entier positif, sans espace (l'identifiant de Tarik dans Experio)")
    if kind == "address" and not ADDRESS.fullmatch(value):
        raise ValueError(f"{key} n'est pas une adresse email (sans espace autour)")
    if kind == "sender" and not ADDRESS.fullmatch(parseaddr(value)[1]):
        raise ValueError(f"{key} n'est pas un expéditeur (« Nom <adresse> » ou une adresse)")
    # The recipients: each entry as Tarik reads it, split at the commas, its spaces around left out.
    wrong = [e.strip() or "(entrée vide)" for e in value.split(",") if not RECIPIENT.fullmatch(e.strip())]
    if kind == "recipients" and wrong:
        raise ValueError(f"{key} : ni adresse ni @domaine : {', '.join(wrong)}")


def changed(before, given):
    """What ``given`` (the page's form: {key: text}) changes in the settings ``before``: {key: new value}. A setting
    loses the spaces around it (the page says so) and is otherwise kept as typed; a secret is kept exactly, its spaces
    its own, and left empty stays as it is. Any other difference, a space inside included, is a change."""
    found = {}
    for key, kind in FIELDS.items():
        if key not in given:
            continue
        value = given[key] if kind == "secret" else given[key].strip()
        if kind == "secret" and value == "":
            continue
        if kind != "secret":
            checked(key, value)
        if value != before.get(key, ""):
            found[key] = value
    return found


def _vault(method, path, **kwargs):
    """Vault's answer with the page's own AppRole token, logged in again once 80% of its lease has passed."""
    with _lock:
        if not _token["value"] or time.monotonic() > _token["until"]:
            ids = {name: Path(ROLE, name).read_text().strip() for name in ("role_id", "secret_id")}
            auth = cabinets._vault_request("POST", "/v1/auth/approle/login", json=ids)["auth"]
            _token.update(value=auth["client_token"], until=time.monotonic() + 0.8 * auth["lease_duration"])
        token = _token["value"]
    return cabinets._vault_request(method, path, token, **kwargs)


MOVED = "ces réglages ont changé depuis l'ouverture de la page : rechargez-la et recommencez"


async def save(pool, cabinet, given, seen, author, note=""):
    """The cabinet's settings changed as ``given`` says (see changed), by ``author``, from the page that showed
    version ``seen``: refused (ValueError) when Vault holds a newer one, never overwritten. Written to Vault, recorded,
    in use at once. The keys changed (a list); nothing is written when nothing changes."""
    data, version = await asyncio.to_thread(latest, cabinet)
    if version != seen:
        raise ValueError(MOVED)
    found = changed(data, given)
    if not found:
        return []
    path = f"/v1/secret/data/tarik/cabinets/{cabinet}"
    try:  # cas: only over the version read, whoever writes in between
        written = await asyncio.to_thread(
            _vault, "POST", path, json={"options": {"cas": version}, "data": {**data, **found}}
        )
    except httpx.HTTPStatusError as error:
        raise ValueError(MOVED) from error
    secret = {key for key, kind in FIELDS.items() if kind == "secret"}
    changes = [
        {"key": key, "before": None if key in secret else data.get(key, ""), "after": None if key in secret else value}
        for key, value in found.items()
    ]
    async with pool.connection() as db:
        await db.execute(
            Q.settings_changed, (cabinet, author, note.strip()[:300], Jsonb(changes), written["data"]["version"])
        )
    await asyncio.to_thread(cabinets.reread_vault)
    return list(found)


async def history(pool, cabinet, limit=30):
    """The cabinet's settings changes, latest first: {author, note, changes, version, at}."""
    async with pool.connection() as db:
        rows = await (await db.execute(Q.settings_history, (cabinet, limit))).fetchall()
    return [
        {"author": author, "note": note, "changes": changes, "version": version, "at": at}
        for author, note, changes, version, at in rows
    ]
