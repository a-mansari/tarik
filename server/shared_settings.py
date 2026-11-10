"""Read and change the settings every cabinet shares, from the general settings page (an admin's).

They live in Vault, in two paths: secret/tarik/orchestrator (the model's key and address, the Claude login, the key the
Experio app uses on /api/) and secret/tarik/shared (the mail server, Gladden, Sparrow, this server's public address).
They are written with the same AppRole as the Cabinets page (tarik-settings, vault/setup.sh), as cabinet_settings does:
check-and-set on each path's version, a secret never shown nor recorded, only replaced, each change recorded (who,
when, why: settings_changes, under GENERAL) and used at once. Any other key in a path is kept unchanged."""

import asyncio

import httpx
from psycopg.types.json import Jsonb

from engine.tools.core import cabinets
from engine.tools.core.queries import Q
from server import audit, cabinet_settings
from server.cabinet_settings import MOVED, changed, recorded

GENERAL = "_general"  # settings_changes' cabinet column for these: no cabinet's name can start with "_" (cabinets.NAME)

# Each setting the page shows with what it holds (cabinet_settings.checked's kinds), and the Vault path it lives in.
FIELDS = {
    "IMAP_HOST": "text",
    "IMAP_PORT": "integer",
    "IMAP_FOLDER": "text",
    "SMTP_HOST": "text",
    "SMTP_PORT": "integer",
    "MAIL_CERT_SHA256": "sha256",
    "MODEL_API_URL": "web",
    "MODEL_API_KEY": "secret",
    "CLAUDE_CODE_OAUTH_TOKEN": "secret",
    "EXPERIO_API_KEY": "secret",
    "PUBLIC_URL": "web",
    "GLADDEN_URL": "web",
    "GLADDEN_CALLBACK_URL": "web",
    "SPARROW_URL": "web",
    "SPARROW_SENDER_ID": "text",
    "SPARROW_TOKEN": "secret",
}
OPTIONAL = {
    "MAIL_CERT_SHA256",
    "MODEL_API_URL",
    "MODEL_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "PUBLIC_URL",
    "GLADDEN_URL",
    "GLADDEN_CALLBACK_URL",
    "SPARROW_URL",
    "SPARROW_SENDER_ID",
    "SPARROW_TOKEN",
}
ORCHESTRATOR = {"MODEL_API_URL", "MODEL_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "EXPERIO_API_KEY"}  # the other: shared
SECRETS = {key for key, kind in FIELDS.items() if kind == "secret"}
PATHS = {"orchestrator": "/v1/secret/data/tarik/orchestrator", "shared": "/v1/secret/data/tarik/shared"}
assert set(FIELDS) == cabinets.SHARED_SETTINGS

# A new address must not get the stored secret Tarik sends there: it is typed again (as cabinet_settings does for
# EXPERIO_URL).
SENT_TO = {"MODEL_API_URL": "MODEL_API_KEY", "SPARROW_URL": "SPARROW_TOKEN"}


def path_of(key):
    """Return the name of the Vault path (a key of PATHS) that holds shared setting ``key``."""
    return "orchestrator" if key in ORCHESTRATOR else "shared"


def latest():
    """Return what Vault holds as (stores, versions): each path's keys {"orchestrator": {...}, "shared": {...}} and its
    version {"orchestrator": n, "shared": n}, 0 for a path not made yet. Raises on any Vault error."""
    stores, versions = {}, {}
    for name, path in PATHS.items():
        found = ((cabinet_settings._vault("GET", path) or {}).get("data")) or {}
        stores[name] = found.get("data") or {}
        versions[name] = found.get("metadata", {}).get("version", 0)
    return stores, versions


def merged(stores):
    """Return both paths' keys as one dict (the shared settings, as cabinets.read_vault does)."""
    return {**stores["shared"], **stores["orchestrator"]}


def version_of(versions):
    """Return ``versions`` as the one text the page sends back ("<orchestrator>.<shared>")."""
    return f"{versions['orchestrator']}.{versions['shared']}"


def shown(data):
    """Return the settings as the page shows them: {key: value} for every FIELDS key, a secret only as True (set) or
    False."""
    return {key: bool(data.get(key)) if kind == "secret" else data.get(key, "") for key, kind in FIELDS.items()}


async def save(pool, given, seen, author, note=""):
    """Apply the form ``given`` to the shared settings (cabinet_settings.changed) and return the keys changed (list;
    [] when nothing changes).

    ``seen``: the version text the page showed (version_of); ValueError (MOVED) when Vault now holds a newer one in
    either path. A changed address in SENT_TO needs its secret typed again (ValueError). Each path written is written
    with check-and-set on the version read, so a concurrent write fails with ValueError (MOVED) instead of being
    overwritten."""
    stores, versions = await asyncio.to_thread(latest)
    data = merged(stores)
    if seen != version_of(versions):
        raise ValueError(MOVED)
    found = changed(data, given, FIELDS, OPTIONAL)
    for url, secret in SENT_TO.items():
        if url in found and data.get(secret) and not given.get(secret):
            raise ValueError(f"pour changer {url}, retapez aussi {secret}")
    if not found:
        return []
    written = 0
    for name, path in PATHS.items():
        mine = {key: value for key, value in found.items() if path_of(key) == name}
        if not mine:
            continue
        body = {"options": {"cas": versions[name]}, "data": {**stores[name], **mine}}  # the path's other keys kept
        try:
            answer = await asyncio.to_thread(cabinet_settings._vault, "POST", path, json=body)
        except httpx.HTTPStatusError as error:
            raise ValueError(MOVED) from error
        written = answer["data"]["version"]
    changes = [recorded(key, data.get(key, ""), value, SECRETS) for key, value in found.items()]
    async with pool.connection() as db:
        await db.execute(Q.settings_changed, (GENERAL, author, note.strip()[:300], Jsonb(changes), written))
    await asyncio.to_thread(cabinets.reread_vault)
    await audit.record(author, "settings_saved", GENERAL, keys=list(found), note=note.strip()[:300])
    return list(found)
