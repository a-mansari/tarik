"""Serve the Routines page: the routines of the cabinet chosen in the sidebar (its own schema, never another's), turned
on or off, rescheduled, their instruction, address and dossiers changed, made or deleted by the team; each change in
the journal (audit).

A routine's checks are the tool's own (engine/tools/dossiers/routines.py routine_problem), plus what its runs will
need: an address the cabinet allows, a live Experio user who has each dossier it adds (allowed_dossiers). A dossier
already on the routine and closed since (archived, reset) may stay: its runs leave it out."""

from datetime import datetime

from starlette.requests import Request

from engine.calls import within
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.queries import Q
from engine.tools.dossiers.routines import (
    KINDS,
    WEEKDAYS,
    ZONE,
    allowed_dossiers,
    next_run,
    routine_problem,
    routines_of_cabinet,
    when,
)
from engine.tools.dossiers.search import dossiers
from engine.tools.emails.sending import recipient_allowed
from server import audit
from server.api.runs import chosen, page

FIELDS = ("enabled", "kind", "dossier_ids", "every", "day", "hour", "notify", "prompt")
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
        "dossier_ids": sorted({int(d) for d in given.get("dossier_ids") or []}),
        "every": str(given.get("every") or ""),
        "day": int(given.get("day") or 0),
        "hour": int(given.get("hour") if given.get("hour") is not None else 8),
        "notify": str(given.get("notify") or "").strip(),
        "prompt": str(given.get("prompt") or "").strip(),
    }


def problem_of(values, before):
    """Return what is wrong with routine ``values`` (cleaned) as the page sent it (French), "" when nothing.
    ``before``: the routine as stored (None for a new one); its dossiers may stay even if closed since."""
    problem = routine_problem(*(values[key] for key in ("kind", *SCHEDULE, "prompt", "dossier_ids")))
    if problem:
        return problem
    if not recipient_allowed(values["notify"]):
        return f"`{values['notify']}` n'est pas dans les adresses autorisées du cabinet (Réglages)"
    added = set(values["dossier_ids"]) - set(before["dossier_ids"] if before else [])
    allowed = allowed_dossiers(values["notify"], sorted(added))
    if allowed is None:
        return f"`{values['notify']}` n'est pas un collaborateur actif d'Experio : il ne recevrait aucun rapport"
    if added - set(allowed):
        return f"`{values['notify']}` n'a pas, dans Experio, tous les dossiers choisis (ou l'un d'eux est fermé)"
    return ""


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
    problem = problem_of(values, before) if values else ""
    if problem:
        return 0, problem
    with cabinet_db() as db:
        if action == "delete":
            db.execute(Q.routine_removed, (routine_id,))
            return routine_id, ""
        upcoming = next_run(*(values[key] for key in SCHEDULE), datetime.now(ZONE))
        if action == "create":
            made = (*(values[key] for key in ("kind", "dossier_ids", *SCHEDULE, "notify")), author, upcoming)
            return db.execute(Q.routine_made, (*made, values["prompt"], values["enabled"])).fetchone()[0], ""
        moved = any(values[key] != before[key] for key in SCHEDULE) or (values["enabled"] and not before["enabled"])
        db.execute(
            Q.routine_changed,
            (*(values[key] for key in FIELDS), upcoming if moved else before["next_run_at"], routine_id),
        )
    return routine_id, ""


async def routines_page(request: Request):
    """GET or POST /ui/api/routines: return the routines of the cabinet chosen in the sidebar and the choices the page
    offers (its dossiers, on GET only: a change never waits on Experio's whole list), after making a change when
    posted.

    POST takes JSON {cabinet, action (create, save, delete), routine {id, FIELDS}}; it is refused (400, the error
    said) when the sidebar has chosen another cabinet since the page opened, or the routine does not pass the checks
    (problem_of). 404 without any cabinet; 503 when Experio cannot give the dossiers."""
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
                context["error"] = "Experio n'a pas pu vérifier l'adresse et les dossiers : rien n'est enregistré"
            if not context["error"]:
                await audit.record(author, "routine_saved", f"{cabinet} n° {routine_id}", change=action)
    else:
        try:
            context["dossiers"] = await within(cabinet, dossiers)
        except Exception:
            return page({**context, "error": "Experio n'a pas pu donner les dossiers du cabinet."}, 503)
    context["routines"] = await within(cabinet, shown)
    return page(context, 400 if context["error"] else 200)
