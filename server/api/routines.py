"""Serve the Routines page: the routines of the cabinet chosen in the sidebar (its own schema, never another's), turned
on or off, rescheduled, their instruction and report address changed, made or deleted by the team; each change in the
journal (audit).

A routine works on every dossier Tarik's Experio account may open (search.opened_dossiers). Its checks are the
tool's own (engine/tools/dossiers/routines.py problem_of), and so is the saving (next_of, saved)."""

import asyncio
import time

from starlette.requests import Request

from engine.calls import within
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.queries import Q
from engine.tools.dossiers.routines import (
    FIELDS,
    KINDS,
    SCHEDULE,
    WEEKDAYS,
    WEEKEND,
    WORKDAYS,
    ZONE,
    admins,
    next_of,
    problem_of,
    routines_of_cabinet,
    saved,
    when,
)
from server import audit
from server.api.runs import chosen, page


def shown():
    """Return the current cabinet's routines for the page, each with its schedule in words and its last run (when it
    was due, and whether it still runs)."""
    with cabinet_db() as db:
        last = {row[0]: row[1:] for row in db.execute(Q.routine_last_runs).fetchall()}
    found = []
    for routine in routines_of_cabinet():
        due, running = last.get(routine["id"], (None, False))
        found.append(
            {
                **routine,
                "schedule": when(*(routine[key] for key in SCHEDULE)),
                "next_run_at": routine["next_run_at"].astimezone(ZONE).strftime("%Y-%m-%d %H:%M"),
                "last_due": due and due.astimezone(ZONE).strftime("%Y-%m-%d %H:%M"),
                "running": running,  # its last period not reported yet
            }
        )
    return found


def cleaned(given):
    """Return the page's routine as typed values ({FIELDS}); raises ValueError or TypeError on a value unreadable."""
    return {
        "enabled": given.get("enabled") is True,
        "kind": str(given.get("kind") or ""),
        "every": str(given.get("every") or ""),
        "days": sorted({int(day) for day in given.get("days") or []}),
        "hour": int(given.get("hour") if given.get("hour") is not None else 8),
        "minute": int(given.get("minute") or 0),
        "notify": str(given.get("notify") or "").strip(),
        "prompt": str(given.get("prompt") or "").strip(),
        "name": " ".join(str(given.get("name") or "").split()),  # one line: a subject
    }


def changed(action, given, author):
    """Check, then make, change or delete one routine of the current cabinet as the page asks (``action`` create,
    save, delete), with the chat's own checks and saving (problem_of, next_of, saved). Returns (its id, the problem
    in French or ""). Pausing is always allowed, even a routine gone bad."""
    try:
        routine_id = int(given.get("id") or 0)
        values = cleaned(given) if action != "delete" else None
    except TypeError, ValueError, OverflowError:
        return 0, "une valeur est illisible"
    before = next((r for r in routines_of_cabinet() if r["id"] == routine_id), None)
    if action != "create" and not before:
        return 0, "cette routine n'existe plus : rechargez la page"
    if action == "delete":
        with cabinet_db() as db:
            db.execute(Q.routine_removed, (routine_id,))
        return routine_id, ""
    pausing = before and not values["enabled"] and all(values[k] == before[k] for k in FIELDS if k != "enabled")
    problem = "" if pausing else problem_of(values)
    if problem:
        return 0, problem
    return saved(values, next_of(values, before), before, author), ""


MANAGERS_SECONDS = 300  # the cabinet's admins, read from Experio again after this
MANAGERS_WAIT = 10  # a page waits this long for them at most; Experio still answering, the next page gets them
MANAGERS = {}  # cabinet -> (when read, its admins)
ASKING = {}  # cabinet -> the one reading of its admins under way


async def managers_of(cabinet):
    """Return ``cabinet``'s admins in Experio who may get the reports (admins), kept MANAGERS_SECONDS; None when
    Experio does not answer within MANAGERS_WAIT. One reading per cabinet at a time: pages opened meanwhile wait for
    it, never start another (each holds a worker of the shared pool while Experio is slow)."""
    kept = MANAGERS.get(cabinet)
    if kept and time.monotonic() - kept[0] < MANAGERS_SECONDS:
        return kept[1]
    task = ASKING.get(cabinet)
    if task is None or task.done():
        task = ASKING[cabinet] = asyncio.ensure_future(within(cabinet, admins))
        task.add_done_callback(lambda done: done.cancelled() or done.exception())  # a failure read, never warned
    try:
        found = await asyncio.wait_for(asyncio.shield(task), MANAGERS_WAIT)
    except Exception:  # Experio slow or down: the address is typed, still checked on saving
        return None
    MANAGERS[cabinet] = (time.monotonic(), found)
    return found


async def routines_page(request: Request):
    """GET or POST /ui/api/routines: return the routines of the cabinet chosen in the sidebar and the choices the page
    offers, after making a change when posted. GET /ui/api/routines?managers: the cabinet's admins in Experio who may
    get the reports instead (managers_of), asked by the page once it shows, so Experio never holds it up.

    POST takes JSON {cabinet, action (create, save, delete), routine {id, FIELDS}}; it is refused (400, the error
    said) when the sidebar has chosen another cabinet since the page opened, or the routine does not pass the tool's
    checks. 404 without any cabinet."""
    cabinet = chosen(request)
    if cabinet is None:
        return page({"error": "aucun cabinet"}, 404)
    if request.method == "GET" and "managers" in request.query_params:
        return page({"cabinet": cabinet, "managers": await managers_of(cabinet)})
    context = {"cabinet": cabinet, "kinds": KINDS, "weekdays": WEEKDAYS, "error": None}
    context["presets"] = {"workdays": WORKDAYS, "weekend": WEEKEND}
    if request.method == "POST":
        try:
            form = await request.json()
        except ValueError:
            form = None
        form = form if isinstance(form, dict) else {}
        action, given = form.get("action"), form.get("routine")
        given = given if isinstance(given, dict) else {}
        if form.get("cabinet") != cabinet:
            context["error"] = "un autre cabinet a été choisi depuis l'ouverture de la page : rechargez-la"
        elif action not in ("create", "save", "delete"):
            context["error"] = "action inconnue"
        else:
            author = request.state.user["email"]
            try:
                routine_id, problem = await within(cabinet, changed, action, given, author)
                context["error"] = problem or None
            except Exception:  # Experio unreachable for its checks: nothing saved
                context["error"] = "Experio n'a pas pu vérifier l'adresse du rapport : rien n'est enregistré"
            if not context["error"]:
                await audit.record(author, "routine_saved", f"{cabinet} n° {routine_id}", change=action)
    context["routines"] = await within(cabinet, shown)
    return page(context, 400 if context["error"] else 200)
