"""Serve the Experio app's API: create a cabinet, change its settings or close it, with Tarik serving it (or stopping)
at once, without a restart.

Only the Experio app may call it, with its key (EXPERIO_API_KEY, in .env) as a Bearer
token, checked by server.py's Guard; a session of the pages is never enough. JSON in and out, every error in plain
French under "error". docs/experio-api.md is the guide for Experio's developers.

  GET    /api/cabinets            every cabinet Vault holds: {name, active}
  GET    /api/cabinets/{name}     its settings, a secret only as set or not
  POST   /api/cabinets            {name, settings}: a new cabinet (201), or a deactivated one reactivated (200)
  PATCH  /api/cabinets/{name}     {settings}: the settings given changed, the others kept
  DELETE /api/cabinets/{name}     deactivated: no longer served, its settings and data kept

A cabinet created or reactivated gets its schema and its mailbox watched; a deactivated one stops being served at
once. Every change is recorded in the Journal and in the cabinet's settings history (actor experio-api), never a
secret's value."""

import asyncio
import contextlib

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from engine import mail, runs
from engine.tools.core import cabinets
from server import audit, cabinet_settings

ACTOR = "experio-api"


def answer(body, status=200):
    """Return ``body`` as a JSON response with HTTP ``status``."""
    return JSONResponse(body, status_code=status)


def refused(why, status=400):
    """Return an error response: {"error": ``why``} with HTTP ``status``."""
    return answer({"error": why}, status)


async def payload(request):
    """Return the request's JSON object and its settings, as (body, settings).

    settings is the body's "settings" object ({} when absent), every value a string. Raises ValueError (in French)
    when the body is not JSON, settings is not an object of strings, or it sets a cabinet's own LLM: its address is
    where Tarik would send the cabinet's documents, so that stays with an admin on the Réglages page."""
    try:
        found = await request.json()
    except ValueError:
        raise ValueError("le corps n'est pas du JSON") from None
    settings = found.get("settings", {}) if isinstance(found, dict) else None
    if not isinstance(settings, dict) or not all(isinstance(value, str) for value in settings.values()):
        raise ValueError("settings : un objet de textes {RÉGLAGE: valeur}")
    if settings.get("LLM_CHOICE") == cabinet_settings.CUSTOM or set(settings) & set(cabinet_settings.CUSTOM_LLM):
        raise ValueError(
            "un LLM propre au cabinet se règle sur la page Réglages, pas par l'API (LLM_CHOICE : un LLM global)"
        )
    return found, settings


def shown(cabinet, data):
    """Return cabinet ``cabinet`` for the API: {name, active, settings}, a secret only as True (set) or False."""
    return {"name": cabinet, "active": cabinets.active(data), "settings": cabinet_settings.shown(data)}


async def serve(cabinet):
    """Make Tarik serve ``cabinet`` now: reload the cabinet list, create its schema, and start watching its mailbox.

    Returns True when its mailbox is watched, False when it has no mailbox (no IMAP_USER)."""
    await asyncio.to_thread(cabinets.reload_cabinets)
    await asyncio.to_thread(cabinets.create_schema, cabinet)
    return await asyncio.to_thread(mail.start_one, cabinet)


async def listing(request: Request):
    """GET /api/cabinets: return every cabinet Vault holds, deactivated ones included, as [{name, active}]."""
    known = await asyncio.to_thread(cabinets.vault_settings)
    return answer([{"name": name, "active": cabinets.active(own)} for name, own in sorted(known.items())])


async def one(request: Request):
    """GET, PATCH or DELETE /api/cabinets/{name}.

    GET returns the cabinet's settings (see shown), 404 when Vault has no such cabinet. PATCH is handled by change,
    DELETE by close."""
    cabinet = request.path_params["name"]
    if request.method == "PATCH":
        return await change(request, cabinet)
    if request.method == "DELETE":
        return await close(cabinet)
    try:
        data, _ = await asyncio.to_thread(cabinet_settings.latest, cabinet, False)
    except LookupError as error:
        return refused(str(error), 404)
    return answer(shown(cabinet, data))


class Exists(ValueError):
    """The cabinet already exists and is active."""


class Unverified(ValueError):
    """The cabinet was kept deactivated: its check failed. The message is the sentence for the person adding it."""


async def switch_off(cabinet, actor):
    """Deactivate ``cabinet`` after a failure, as far as it can be done: a half-made cabinet must not stop the server
    from starting. Never raises."""
    with contextlib.suppress(ValueError, LookupError, httpx.HTTPError, OSError):  # best effort, the cause is raised
        await cabinet_settings.save(
            runs.pool, cabinet, {}, None, actor, "Désactivé : mise en service échouée", active=False
        )
        await asyncio.to_thread(cabinets.reload_cabinets)


async def add(cabinet, settings, actor, verify=None):
    """Create ``cabinet`` with ``settings``, or reactivate a deactivated one with them (what ``settings`` leaves empty
    is kept), record it in the Journal under ``actor``, serve it, and return (keys set, created, mailbox watched).

    With ``verify`` (an async function of the cabinet's name returning "" or a sentence), the cabinet is first kept
    deactivated and served only once ``verify`` says "": otherwise Unverified is raised with its sentence and nothing
    is served. If serving fails, the cabinet is deactivated and the error raised.

    Raises Exists when the cabinet is already active, ValueError (in French) for an invalid name or settings."""
    try:
        data, _ = await asyncio.to_thread(cabinet_settings.latest, cabinet, False)
    except LookupError:
        data = None
    if data is not None and cabinets.active(data):
        raise Exists("ce cabinet existe déjà")
    created = data is None
    if created:
        keys = await cabinet_settings.create(runs.pool, cabinet, settings, actor, active=verify is None)
    else:
        kept = {key: value for key, value in settings.items() if value != ""}
        keys = await cabinet_settings.save(
            runs.pool, cabinet, kept, None, actor, "Réactivé", active=True if verify is None else None
        )
    await audit.record(actor, "cabinet_created" if created else "cabinet_reactivated", cabinet, keys=keys)
    try:
        if verify:
            problem = await verify(cabinet)
            if problem:
                raise Unverified(problem)
            await cabinet_settings.save(runs.pool, cabinet, {}, None, actor, "Connexion vérifiée", active=True)
        watched = await serve(cabinet)
    except Unverified:
        raise
    except Exception:
        await switch_off(cabinet, actor)
        raise
    return keys, created, watched


async def create(request: Request):
    """POST /api/cabinets: create a new cabinet, or reactivate a deactivated one with the settings given, and serve it.

    Returns {name, active, changed, mailbox_watched}: 201 for a new cabinet, 200 for a reactivated one; 409 when the
    cabinet already exists and is active; 400 for invalid settings or name."""
    try:
        found, settings = await payload(request)
        cabinet = str(found.get("name") or "")
        keys, created, watched = await add(cabinet, settings, ACTOR)
    except Exists:
        return refused("ce cabinet existe déjà : modifiez-le (PATCH)", 409)
    except ValueError as error:
        return refused(str(error))
    return answer(
        {"name": cabinet, "active": True, "changed": keys, "mailbox_watched": watched}, 201 if created else 200
    )


async def change(request, cabinet):
    """PATCH /api/cabinets/{name}: change the given settings of ``cabinet``, keep the others, and return {name,
    changed}.

    404 when the cabinet does not exist; 400 for an unknown or invalid setting. A served cabinet's mailbox is watched
    at once (a mailbox just given starts being read)."""
    try:
        _, settings = await payload(request)
        unknown = sorted(set(settings) - set(cabinet_settings.FIELDS))
        if unknown:
            raise ValueError(f"réglages inconnus : {', '.join(unknown)}")
        keys = await cabinet_settings.save(runs.pool, cabinet, settings, None, ACTOR, "Experio")
    except LookupError as error:
        return refused(str(error), 404)
    except ValueError as error:
        return refused(str(error))
    if cabinet in cabinets.CABINETS:  # a mailbox just given: watched now
        await asyncio.to_thread(mail.start_one, cabinet)
    return answer({"name": cabinet, "changed": keys})


async def close(cabinet):
    """DELETE /api/cabinets/{name}: deactivate ``cabinet`` and stop serving it; its settings and data are kept.

    Returns {name, active: false}; 404 when the cabinet does not exist. The Journal's cabinet_closed entry is
    written only when the cabinet was active."""
    try:
        keys = await cabinet_settings.save(runs.pool, cabinet, {}, None, ACTOR, "Désactivé", active=False)
    except LookupError as error:
        return refused(str(error), 404)
    await asyncio.to_thread(cabinets.reload_cabinets)
    if keys:
        await audit.record(ACTOR, "cabinet_closed", cabinet)
    return answer({"name": cabinet, "active": False})
