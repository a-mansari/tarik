"""Read and change the extra global LLMs, from the general settings page (an admin's): the LLMs, besides Claude and
MiniMax (always there), that any cabinet may pick on its Réglages page.

They live in one Vault path, secret/tarik/llms: {name: {type, model, url, key}}. They are written with the settings
AppRole (tarik-settings, vault/setup.sh) as the other settings are: check-and-set on the path's version, a key never
shown nor recorded, only replaced. Each change is recorded (settings_changes, under LLMS) and used at once. A cabinet
that picked an LLM removed since runs on Claude."""

import asyncio

from psycopg.types.json import Jsonb

from engine.tools.core import cabinets
from engine.tools.core.model import CLAUDE, CUSTOM, MINIMAX, TYPES
from engine.tools.core.queries import Q
from server import audit, cabinet_settings
from server.cabinet_settings import LLM_NAME, MOVED, PATTERNS, cas_write

LLMS = "_llms"  # settings_changes' cabinet column for these: no cabinet's name can start with "_" (cabinets.NAME)
PATH = "/v1/secret/data/tarik/llms"
RESERVED = {CLAUDE, MINIMAX, CUSTOM}  # the global LLMs that are always there, and the cabinet's own
MOST = 30


def latest():
    """Return the extra LLMs and their Vault version as (data, version): data {name: {type, model, url, key}}, version
    0 when the path is not made yet. Raises on any Vault error."""
    found = ((cabinet_settings._vault("GET", PATH) or {}).get("data")) or {}
    return found.get("data") or {}, found.get("metadata", {}).get("version", 0)


def shown(data):
    """Return the LLMs as the page shows them: [{name, type, model, url, key_set}], by name; a key only as set."""
    return [
        {"name": name, "type": one.get("type", ""), "model": one.get("model", ""), "url": one.get("url", ""),
         "key_set": bool(one.get("key"))}
        for name, one in sorted(data.items())
    ]  # fmt: skip


def summary(one):
    """Return an LLM in words, without its key, for the history."""
    return f"{one['type']} · {one['model']} · {one['url']}".rstrip(" ·") if one else ""


def checked(given, before):
    """Return the LLMs {name: {type, model, url, key}} the page's ``given`` (a list of such dicts, a key "" keeping the
    stored one) makes of ``before``. Raises ValueError (in plain French) for anything Tarik could not use: a name
    taken or invalid, an unknown type, no model, no key, an API without an HTTPS address, or a changed address whose
    key is not typed again (Tarik sends the key there)."""
    if not isinstance(given, list) or len(given) > MOST:
        raise ValueError(f"au plus {MOST} LLMs")
    found = {}
    for one in given:
        if not isinstance(one, dict) or not all(isinstance(value, str) for value in one.values()):
            raise ValueError("LLM illisible")
        name, kind = one.get("name", "").strip(), one.get("type", "")
        model, url, key = one.get("model", "").strip(), one.get("url", "").strip(), one.get("key", "")
        if not LLM_NAME.fullmatch(name) or name in RESERVED:
            raise ValueError(
                f"{name or '(sans nom)'} : un nom en minuscules, chiffres, - et _, autre que {', '.join(RESERVED)}"
            )
        if name in found:
            raise ValueError(f"{name} : ce nom est déjà pris")
        if kind not in TYPES:
            raise ValueError(f"{name} : le type vaut l'un de : {', '.join(TYPES)}")
        if not model:
            raise ValueError(f"{name} : le modèle ne peut pas être vide")
        if kind == "anthropic-api" and not PATTERNS["web"][0].fullmatch(url):
            raise ValueError(f"{name} : une API demande une adresse HTTPS, sans espace")
        url = url if kind == "anthropic-api" else ""
        old = before.get(name) or {}
        key = key or old.get("key", "")
        if not key:
            raise ValueError(f"{name} : la clé (ou le jeton) ne peut pas être vide")
        if old and old.get("key") and one.get("key", "") == "" and old.get("url", "") != url:
            raise ValueError(f"{name} : pour changer l'adresse, retapez aussi la clé")
        found[name] = {"type": kind, "model": model, "url": url, "key": key}
    return found


async def save(pool, given, seen, author):
    """Replace the extra LLMs by ``given`` (see checked) and return what changed as {saved: [names], fell_back:
    [cabinets that had picked a removed LLM, now on Claude]}.

    ``seen``: the Vault version the page showed (an int); ValueError (MOVED) when Vault now holds a newer one. The
    write uses check-and-set on it. The change is recorded and the settings are read again, so it is used at once."""
    before, version = await asyncio.to_thread(latest)
    if seen != version:
        raise ValueError(MOVED)
    after = checked(given, before)
    names = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )  # a key replaced counts: the dicts differ
    if not names:
        return {"saved": [], "fell_back": []}
    _, every = await asyncio.to_thread(cabinets.vault_settings)
    held = {own.get("LLM_CHOICE") for own in every.values()}
    for name in sorted(after.keys() - before.keys()):
        if name in held:  # a cabinet still names it (an LLM removed): it would use this one without being asked
            raise ValueError(f"{name} : un cabinet a encore ce nom pour LLM ; choisissez un autre nom")
    version = await asyncio.to_thread(cas_write, PATH, version, after)
    changes = [
        {"key": name, "before": summary(before.get(name)), "after": summary(after.get(name)) if name in after else ""}
        for name in names
    ]
    async with pool.connection() as db:
        await db.execute(Q.settings_changed, (LLMS, author, "", Jsonb(changes), version))
    await asyncio.to_thread(cabinets.reread_vault)
    await audit.record(author, "settings_saved", LLMS, keys=names)
    gone = set(before) - set(after)
    return {
        "saved": names,
        "fell_back": sorted(name for name, own in every.items() if own.get("LLM_CHOICE") in gone),
    }
