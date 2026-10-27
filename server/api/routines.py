"""Serve the Routines page: the routines of the cabinet chosen in the sidebar (its own schema, never another's), turned
on or off, rescheduled, their instruction, address and dossiers changed, made or deleted by the team; each change in
the journal (audit).

A routine's checks are the tool's own (engine/tools/dossiers/routines.py): its kind, schedule and instruction, an
address the cabinet allows, the cabinet's own dossiers."""

from datetime import datetime

from starlette.requests import Request

from engine.calls import within
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.queries import Q
from engine.tools.dossiers.routines import KINDS, WEEKDAYS, ZONE, next_run, schedule_problem, when
from engine.tools.dossiers.search import dossiers
from engine.tools.emails.sending import recipient_allowed
from server import audit
from server.api.runs import chosen, page

FIELDS = ("enabled", "kind", "dossier_ids", "every", "day", "hour", "notify", "prompt")


def listed():
    """Return the current cabinet's routines for the page, each with its last run: [{id, kind, dossier_ids, every,
    day, hour, notify, enabled, next_run_at, created_by, prompt, schedule, last_due, last_report}]."""
    with cabinet_db() as db:
        rows = db.execute(Q.routines_listed).fetchall()
        last = {row[0]: row[1:] for row in db.execute(Q.routine_last_runs).fetchall()}
    names = ("id", "kind", "dossier_ids", "every", "day", "hour", "notify", "enabled", "next_run_at")
    found = []
    for row in rows:
        item = dict(zip((*names, "created_by", "prompt"), row, strict=True))
        due, report = last.get(item["id"], (None, None))
        item.update(
            schedule=when(item["every"], item["day"], item["hour"]),
            next_run_at=item["next_run_at"].astimezone(ZONE).strftime("%Y-%m-%d %H:%M"),
            last_due=due and due.astimezone(ZONE).strftime("%Y-%m-%d %H:%M"),
            last_report=report,
        )
        found.append(item)
    return found


def problem_of(given, known):
    """Return what is wrong with a routine as the page sent it (French), "" when nothing. ``known``: the cabinet's
    dossier ids."""
    try:
        values = cleaned(given)
    except TypeError, ValueError:
        return "une valeur est illisible"
    problem = schedule_problem(values["kind"], values["every"], values["day"], values["hour"], values["prompt"])
    if problem:
        return problem
    if not values["dossier_ids"] and values["kind"] != "instruction":
        return "pour quels dossiers ?"
    if set(values["dossier_ids"]) - known:
        return "un dossier n'est pas un dossier du cabinet"
    if not recipient_allowed(values["notify"]):
        return f"`{values['notify']}` n'est pas dans les adresses autorisées du cabinet (Réglages)"
    return ""


def cleaned(given):
    """Return the page's routine as typed values ({FIELDS}); raises ValueError or TypeError on a value unreadable."""
    return {
        "enabled": given.get("enabled") is True,
        "kind": str(given.get("kind") or ""),
        "dossier_ids": sorted({int(d) for d in given.get("dossier_ids") or []}),
        "every": str(given.get("every") or ""),
        "day": int(given.get("day") or 0),
        "hour": int(given.get("hour") if given.get("hour") is not None else 8),
        "notify": str(given.get("notify") or "").strip(),
        "prompt": str(given.get("prompt") or "").strip(),
    }


def saved(action, given, author):
    """Make, change or delete one routine of the current cabinet as the page asks (``action`` create, save, delete);
    return its id. A routine changed gets its next run from its schedule as it is now."""
    with cabinet_db() as db:
        if action == "delete":
            db.execute(Q.routine_removed, (int(given["id"]),))
            return int(given["id"])
        values = cleaned(given)
        upcoming = next_run(values["every"], values["day"], values["hour"], datetime.now(ZONE))
        if action == "create":
            made = (values["kind"], values["dossier_ids"], values["every"], values["day"], values["hour"])
            made += (values["notify"], author, upcoming, values["prompt"])
            routine_id = db.execute(Q.routine_made, made).fetchone()[0]
            if not values["enabled"]:
                db.execute(Q.routine_enabled, (False, upcoming, routine_id))
            return routine_id
        changed = [values[key] for key in FIELDS]
        db.execute(Q.routine_changed, (*changed, upcoming, int(given["id"])))
        return int(given["id"])


async def routines_page(request: Request):
    """GET or POST /ui/api/routines: return the routines of the cabinet chosen in the sidebar, its dossiers and the
    choices the page offers, after making a change when posted.

    POST takes JSON {cabinet, action (create, save, delete), routine {id, FIELDS}}; it is refused (400, the error
    said) when the sidebar has chosen another cabinet since the page opened, or the routine does not pass the tool's
    own checks. 404 without any cabinet."""
    cabinet = chosen(request)
    if cabinet is None:
        return page({"error": "aucun cabinet"}, 404)
    try:
        known = await within(cabinet, dossiers)
    except Exception:
        return page({"cabinet": cabinet, "error": "Experio n'a pas pu donner les dossiers du cabinet."}, 503)
    error = None
    if request.method == "POST":
        try:
            form = await request.json()
        except ValueError:
            form = None
        form = form if isinstance(form, dict) else {}
        action, given = form.get("action"), form.get("routine")
        given = given if isinstance(given, dict) else {}
        if form.get("cabinet") != cabinet:
            error = "un autre cabinet a été choisi depuis l'ouverture de la page : rechargez-la"
        elif action not in ("create", "save", "delete"):
            error = "action inconnue"
        elif action != "delete":
            error = await within(cabinet, problem_of, given, {d["id"] for d in known}) or None
        if not error:
            author = request.state.user["email"]
            routine_id = await within(cabinet, saved, action, given, author)
            await audit.record(author, "routine_saved", f"{cabinet} n° {routine_id}", change=action)
    context = {
        "cabinet": cabinet,
        "routines": await within(cabinet, listed),
        "dossiers": known,
        "kinds": KINDS,
        "weekdays": WEEKDAYS,
        "error": error,
    }
    return page(context, 400 if error else 200)
