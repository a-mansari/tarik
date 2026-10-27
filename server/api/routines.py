"""Serve the Routines page: the routines of the cabinet chosen in the sidebar (its own schema, never another's), turned
on or off, rescheduled, their instruction and report address changed, made or deleted by the team; each change in the
journal (audit).

A routine works on every dossier Tarik's Experio account sees. Its checks are the tool's own
(engine/tools/dossiers/routines.py routine_problem and address_problem)."""

from datetime import datetime

from starlette.requests import Request

from engine.calls import within
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.queries import Q
from engine.tools.dossiers.routines import (
    KINDS,
    WEEKDAYS,
    ZONE,
    address_problem,
    next_run,
    routine_problem,
    routines_of_cabinet,
    when,
)
from server import audit
from server.api.runs import chosen, page

FIELDS = ("enabled", "kind", "every", "day", "hour", "notify", "prompt")
SCHEDULE = ("every", "day", "hour")


def shown():
    """Return the current cabinet's routines for the page, each with its schedule in words and its last run (when it
    was due, the start of its report)."""
    with cabinet_db() as db:
        last = {row[0]: row[1:] for row in db.execute(Q.routine_last_runs).fetchall()}
    found = []
    for routine in routines_of_cabinet():
        due, report = last.get(routine["id"], (None, None))
        found.append(
            {
                **routine,
                "schedule": when(routine["every"], routine["day"], routine["hour"]),
                "next_run_at": routine["next_run_at"].astimezone(ZONE).strftime("%Y-%m-%d %H:%M"),
                "last_due": due and due.astimezone(ZONE).strftime("%Y-%m-%d %H:%M"),
                "last_report": report,
            }
        )
    return found


def cleaned(given):
    """Return the page's routine as typed values ({FIELDS}); raises ValueError or TypeError on a value unreadable."""
    return {
        "enabled": given.get("enabled") is True,
        "kind": str(given.get("kind") or ""),
        "every": str(given.get("every") or ""),
        "day": int(given.get("day") or 0),
        "hour": int(given.get("hour") if given.get("hour") is not None else 8),
        "notify": str(given.get("notify") or "").strip(),
        "prompt": str(given.get("prompt") or "").strip(),
    }


def changed(action, given, author):
    """Check, then make, change or delete one routine of the current cabinet as the page asks (``action`` create,
    save, delete). Returns (its id, the problem in French or ""). A routine keeps its next run unless its schedule
    changed or it is turned back on."""
    try:
        routine_id = int(given.get("id") or 0)
        values = cleaned(given) if action != "delete" else None
    except TypeError, ValueError:
        return 0, "une valeur est illisible"
    before = next((r for r in routines_of_cabinet() if r["id"] == routine_id), None)
    if action != "create" and not before:
        return 0, "cette routine n'existe plus : rechargez la page"
    switched = before and all(values[key] == before[key] for key in FIELDS if key != "enabled") if values else False
    if values and not (switched and not values["enabled"]):  # pausing is always allowed, even a routine gone bad
        problem = routine_problem(*(values[key] for key in ("kind", *SCHEDULE, "prompt")))
        problem = problem or address_problem(values["notify"])
        if problem:
            return 0, problem
    with cabinet_db() as db:
        if action == "delete":
            db.execute(Q.routine_removed, (routine_id,))
            return routine_id, ""
        upcoming = next_run(*(values[key] for key in SCHEDULE), datetime.now(ZONE))
        if action == "create":
            made = (*(values[key] for key in ("kind", *SCHEDULE, "notify")), author, upcoming)
            return db.execute(Q.routine_made, (*made, values["prompt"], values["enabled"])).fetchone()[0], ""
        moved = any(values[key] != before[key] for key in SCHEDULE) or (values["enabled"] and not before["enabled"])
        db.execute(
            Q.routine_changed,
            (*(values[key] for key in FIELDS), upcoming if moved else before["next_run_at"], routine_id),
        )
    return routine_id, ""


async def routines_page(request: Request):
    """GET or POST /ui/api/routines: return the routines of the cabinet chosen in the sidebar and the choices the page
    offers, after making a change when posted.

    POST takes JSON {cabinet, action (create, save, delete), routine {id, FIELDS}}; it is refused (400, the error
    said) when the sidebar has chosen another cabinet since the page opened, or the routine does not pass the tool's
    checks. 404 without any cabinet."""
    cabinet = chosen(request)
    if cabinet is None:
        return page({"error": "aucun cabinet"}, 404)
    context = {"cabinet": cabinet, "kinds": KINDS, "weekdays": WEEKDAYS, "error": None}
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
