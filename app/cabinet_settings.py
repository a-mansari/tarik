"""Each cabinet's own settings, read and changed from the Cabinets page: its Experio login, its mailbox, who may
receive Tarik's emails, whether Tarik really writes to Experio (DRY_RUN). Vault (secret/tarik/cabinets/<cabinet>)
stays their only home: they are written there with the page's own AppRole (tarik-settings, vault/setup.sh), which may
only read and update the cabinets' paths, never the shared settings nor make a new cabinet. A secret (a password, a
key) is never shown nor kept anywhere but Vault: it is only replaced. Each change is recorded (who, when, why, which
settings: table settings_changes) and in use at once. Any other key in the same path is kept as it is."""

import asyncio
import re
import threading
from email.utils import parseaddr

import httpx
from psycopg.types.json import Jsonb

from app import audit
from app.accounts import EMAIL
from tarik.core import cabinets
from tarik.core.model import ADAPTERS
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
    "MODEL_ADAPTER": "adapter",
}
# Left empty: Tarik does without (MODEL_ADAPTER: the first of ADAPTERS).
OPTIONAL = {"SMTP_FROM", "MAIL_MANAGER", "MAIL_ALLOWED_RECIPIENTS", "ODOO_API_KEY", "MODEL_ADAPTER"}
assert set(FIELDS) == cabinets.CABINET_SETTINGS - {"ACTIVE"}  # every cabinet setting on the page, but the API's own

# As tarik.emails.sending reads the list: an address, an @domain, or a pattern with "*" ("test.*@experio.ma", "*").
RECIPIENT = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+|@[^@\s,]+\.[^@\s,]+|[^\s,]*\*[^\s,]*")

_lock = threading.Lock()
_token = {"token": "", "token_until": 0.0}


def latest(cabinet, served_only=True):
    """The cabinet's settings as Vault holds them now, and their version: (data, version). ``served_only`` False: a
    deactivated cabinet too (the Experio API). LookupError when Vault has no such cabinet."""
    if cabinet not in cabinets.CABINETS and (served_only or not cabinets.NAME.fullmatch(cabinet)):
        raise LookupError("cabinet inconnu")
    found = _vault("GET", f"/v1/secret/data/tarik/cabinets/{cabinet}")
    if not found or not found["data"] or found["data"]["data"] is None:
        raise LookupError("cabinet inconnu")
    return found["data"]["data"], found["data"]["metadata"]["version"]


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
    # The recipients, each entry as Tarik reads it: split at the commas, its spaces left out, an empty one skipped.
    wrong = [e.strip() for e in value.split(",") if e.strip() and not RECIPIENT.fullmatch(e.strip())]
    if kind == "recipients" and wrong:
        raise ValueError(f"{key} : ni adresse, ni @domaine, ni motif avec * : {', '.join(wrong)}")


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
        if value != (before.get(key) or (next(iter(ADAPTERS)) if kind == "adapter" else "")):  # unset: the first
            found[key] = value
    return found


def _vault(method, path, **kwargs):
    """Vault's answer with the page's own AppRole token, logged in again once 80% of its lease has passed."""
    with _lock:
        token = cabinets.vault_token(ROLE, _token)
    return cabinets.vault_request(method, path, token, **kwargs)


MOVED = "ces réglages ont changé depuis l'ouverture de la page : rechargez-la et recommencez"


async def save(pool, cabinet, given, seen, author, note="", active=None):
    """The cabinet's settings changed as ``given`` says (see changed), by ``author``, from the page that showed
    version ``seen``: refused (ValueError) when Vault holds a newer one, never overwritten (``seen`` None: the Experio
    API, over the latest). ``active`` True or False: the cabinet reactivated or deactivated with it (ACTIVE). Written
    to Vault, recorded, in use at once. The keys changed (a list); nothing is written when nothing changes."""
    data, version = await asyncio.to_thread(latest, cabinet, seen is not None)
    if seen is not None and version != seen:
        raise ValueError(MOVED)
    found = changed(data, given)
    if active is not None and cabinets.active(data) != active:
        found["ACTIVE"] = "1" if active else "0"
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
    await audit.record(author, "settings_saved", cabinet, keys=list(found), note=note.strip()[:300])
    return list(found)


def complete(given):
    """A new cabinet's settings from ``given``, each checked as typed (a secret as is): every one Tarik needs present,
    DRY_RUN 1 (simulation) unless given. ValueError (in plain French) otherwise; unknown keys are refused too."""
    if unknown := sorted(set(given) - set(FIELDS)):
        raise ValueError(f"réglages inconnus : {', '.join(unknown)}")
    found = changed({}, {"DRY_RUN": "1", **given})
    if missing := [key for key in FIELDS if key not in OPTIONAL and not found.get(key)]:
        raise ValueError(f"réglages manquants : {', '.join(missing)}")
    return found


async def create(pool, cabinet, given, author):
    """A new cabinet in Vault, its settings ``given`` (see complete), recorded: refused (ValueError) when Vault has it
    already, written only if absent (cas 0). The keys set."""
    if not cabinets.NAME.fullmatch(cabinet) or cabinet == "public" or cabinet.startswith("pg_"):
        raise ValueError("nom de cabinet : la base Experio exacte, en minuscules (lettres, chiffres, _)")
    data = complete(given)
    try:
        written = await asyncio.to_thread(
            _vault, "POST", f"/v1/secret/data/tarik/cabinets/{cabinet}", json={"options": {"cas": 0}, "data": data}
        )
    except httpx.HTTPStatusError as error:
        raise ValueError("ce cabinet existe déjà") from error
    secret = {key for key, kind in FIELDS.items() if kind == "secret"}
    changes = [{"key": key, "before": None, "after": None if key in secret else value} for key, value in data.items()]
    async with pool.connection() as db:
        await db.execute(Q.settings_changed, (cabinet, author, "Créé", Jsonb(changes), written["data"]["version"]))
    return list(data)


async def history(pool, cabinet, limit=30):
    """The cabinet's settings changes, latest first: {author, note, changes, version, at}."""
    async with pool.connection() as db:
        rows = await (await db.execute(Q.settings_history, (cabinet, limit))).fetchall()
    return [
        {"author": author, "note": note, "changes": changes, "version": version, "at": at}
        for author, note, changes, version, at in rows
    ]
