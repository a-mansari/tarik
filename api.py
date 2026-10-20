"""The Experio app's API: it creates a cabinet, changes its settings or closes it, and Tarik serves it (or stops)
at once, no restart. Only the Experio app may call it: its key (EXPERIO_API_KEY, in Vault's secret/tarik/orchestrator)
as a Bearer token, checked by server.py's Guard; a session of the pages is never enough. JSON in and out, every
error in plain French under "error". docs/experio-api.md is the guide for Experio's developers.

  GET    /api/cabinets            every cabinet Vault holds: {name, active}
  GET    /api/cabinets/{name}     its settings, a secret only as set or not
  POST   /api/cabinets            {name, settings}: a new cabinet (201), or a deactivated one reactivated (200)
  PATCH  /api/cabinets/{name}     {settings}: the settings given changed, the others kept
  DELETE /api/cabinets/{name}     deactivated: no longer served, its settings and data kept

A cabinet created or reactivated gets its schema and its mailbox watched; one deactivated stops being served at
once. Each call is recorded in the Journal (actor experio-api) and in the cabinet's settings history, never a
secret's value."""

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse

from engine import audit, cabinet_settings, mail, runs
from tarik.core import cabinets

ACTOR = "experio-api"


def answer(body, status=200):
    return JSONResponse(body, status_code=status)


def refused(why, status=400):
    return answer({"error": why}, status)


async def payload(request):
    """The request's JSON object and its settings (a dict of text values); ValueError when it is not one."""
    try:
        found = await request.json()
    except ValueError:
        raise ValueError("le corps n'est pas du JSON") from None
    settings = found.get("settings", {}) if isinstance(found, dict) else None
    if not isinstance(settings, dict) or not all(isinstance(value, str) for value in settings.values()):
        raise ValueError("settings : un objet de textes {RÉGLAGE: valeur}")
    return found, settings


def shown(cabinet, data):
    return {"name": cabinet, "active": cabinets.active(data), "settings": cabinet_settings.shown(data)}


async def serve(cabinet):
    """Tarik serving the cabinet now: its schema, the cabinet list, its mailbox watched. Whether its mailbox is."""
    await asyncio.to_thread(cabinets.reload_cabinets)
    await asyncio.to_thread(cabinets.create_schema, cabinet)
    return await asyncio.to_thread(mail.start_one, cabinet)


async def listing(request: Request):
    _, known = await asyncio.to_thread(cabinets.vault_settings)
    return answer([{"name": name, "active": cabinets.active(own)} for name, own in sorted(known.items())])


async def one(request: Request):
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


async def create(request: Request):
    """A new cabinet, or a deactivated one reactivated with the settings given."""
    try:
        found, settings = await payload(request)
        cabinet = str(found.get("name") or "")
        try:
            data, _ = await asyncio.to_thread(cabinet_settings.latest, cabinet, False)
        except LookupError:
            data = None
        if data is not None and cabinets.active(data):
            return refused("ce cabinet existe déjà : modifiez-le (PATCH)", 409)
        if data is None:
            keys = await cabinet_settings.create(runs.pool, cabinet, settings, ACTOR)
            action, status = "cabinet_created", 201
        else:
            keys = await cabinet_settings.save(runs.pool, cabinet, settings, None, ACTOR, "Réactivé", active=True)
            action, status = "cabinet_reactivated", 200
    except ValueError as error:
        return refused(str(error))
    watched = await serve(cabinet)
    await audit.record(ACTOR, action, cabinet, keys=keys)
    return answer({"name": cabinet, "active": True, "changed": keys, "mailbox_watched": watched}, status)


async def change(request, cabinet):
    try:
        _, settings = await payload(request)
        if unknown := sorted(set(settings) - set(cabinet_settings.FIELDS)):
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
    try:
        keys = await cabinet_settings.save(runs.pool, cabinet, {}, None, ACTOR, "Désactivé", active=False)
    except LookupError as error:
        return refused(str(error), 404)
    await asyncio.to_thread(cabinets.reload_cabinets)
    if keys:
        await audit.record(ACTOR, "cabinet_closed", cabinet)
    return answer({"name": cabinet, "active": False})
