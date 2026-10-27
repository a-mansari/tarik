"""Routines: a task Tarik runs again and again on a schedule for a cabinet, made, listed, paused, resumed or deleted
by a manager (the Dossier graph's mode routine, after check_manager). engine/routines.py runs them.

A routine prepares or reports, never declares nor posts: « tva » and « ras » prepare the declarations of the last
ended period (the Déclarations graph, without declarer), « progress » sends each dossier's progress
(dossier_progress). Each run's report goes by email to ``notify``: the manager who made it.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from engine.tools.core.app import ToolError, ok, question, routine, step_tool
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.conversation import client_messages, own_words
from engine.tools.core.experio import refused, require_dossier
from engine.tools.core.queries import Q
from engine.tools.core.words import normalized, said
from engine.tools.dossiers.identity import confirmed
from engine.tools.dossiers.people import sender_of, sender_reach

ZONE = ZoneInfo("Africa/Casablanca")
KINDS = {
    "tva": "préparer la déclaration de TVA de la dernière période",
    "ras": "préparer les déclarations RAS de la dernière période",
    "progress": "envoyer l'avancement",
}
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
ACTIONS = ("create", "list", "pause", "resume", "delete")


def next_run(every, day, hour, after):
    """Return the first time after ``after`` (an aware datetime) the schedule falls on, in Casablanca time: each day
    at ``hour``; each week on weekday ``day`` (Monday 0); each month on day ``day`` (1 to 28, in every month)."""
    local = after.astimezone(ZONE)
    at = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if every == "day":
        return at if at > local else at + timedelta(days=1)
    if every == "week":
        at += timedelta(days=(day - at.weekday()) % 7)
        return at if at > local else at + timedelta(days=7)
    at = at.replace(day=day)
    if at > local:
        return at
    return at.replace(year=at.year + at.month // 12, month=at.month % 12 + 1)


def when(every, day, hour):
    """Return the schedule in the client's words: « chaque mois, le 5 à 8 h »."""
    if every == "day":
        return f"chaque jour à {hour} h"
    if every == "week":
        return f"chaque {WEEKDAYS[day]} à {hour} h"
    return f"chaque mois, le {day} à {hour} h"


def described(row, names=None):
    """Return one routine (a routines_listed row) as a French line: « n° 3 : préparer la TVA, chaque mois, le 5 à
    8 h, dossiers `A`, `B`, rapport à x@cabinet.ma, prochaine le 2026-11-05 08:00 (en pause) »."""
    routine_id, kind, dossier_ids, every, day, hour, notify, enabled, next_at = row
    dossiers = ", ".join(f"`{(names or {}).get(d) or require_dossier(d)}`" for d in dossier_ids)
    line = f"n° {routine_id} : {KINDS[kind]}, {when(every, day, hour)}, dossier(s) {dossiers}, rapport à `{notify}`"
    line += f", prochaine le {next_at.astimezone(ZONE):%Y-%m-%d %H:%M}"
    return line + ("" if enabled else " (en pause)")


def schedule_problem(kind, every, day, hour):
    """Return what is wrong with a new routine's kind and schedule (French), "" when nothing."""
    if kind not in KINDS:
        return f"quelle tâche : {', '.join(f'{k} ({w})' for k, w in KINDS.items())} ?"
    if every not in ("day", "week", "month"):
        return "à quel rythme : chaque jour, chaque semaine ou chaque mois ?"
    if not 0 <= hour <= 23:
        return "à quelle heure (0 à 23) ?"
    if every == "week" and not 0 <= day <= 6:
        return "quel jour de la semaine ?"
    if every == "month" and not 1 <= day <= 28:
        return "quel jour du mois (1 à 28, pour tomber chaque mois) ?"
    return ""


def own_routine(routine_id, reach):
    """Return routine ``routine_id`` (a routines_listed row) when it is the cabinet's and every dossier it runs on is
    within the sender's ``reach`` (sender_reach; None: no limit); else None."""
    with cabinet_db() as db:
        row = db.execute(Q.routine_of, (routine_id,)).fetchone()
    if row and reach is not None and not set(row[2]) <= set(reach.get("company_ids") or []):
        return None
    return row


@step_tool
def manage_routine(
    action: str,
    kind: str = "",
    dossier_ids: list | None = None,
    every: str = "",
    day: int = 0,
    hour: int = 8,
    routine_id: int = 0,
    email: str = "",
    client_confirmed: bool = False,
    client_message: str = "",
    session_id: str = "",
) -> dict:
    """Make, list, pause, resume or delete the cabinet's routines (``action`` create, list, pause, resume, delete).

    create: ``kind`` (tva, ras, progress), ``dossier_ids``, ``every`` (day, week, month), ``day`` (a weekday 0-6 from
    Monday, or a day of the month 1-28) and ``hour`` (0-23, Casablanca time). Its reports go to the email's sender,
    or in the chat to the ``email`` the client wrote. Read back, made once the client said yes (``client_confirmed``).
    pause, resume, delete: ``routine_id``; delete is read back first. A sender acts only on routines whose dossiers
    are all theirs (sender_reach).

    Returns an ok result once done, a routine result for the list, a question result for what is missing or to
    confirm.

    Raises:
        ToolError: unknown ``action``, or a dossier unknown in Experio."""
    if action not in ACTIONS:
        raise ToolError(f"action : {', '.join(ACTIONS)}.")
    with refused(f"manage_routine {action}", "Experio n'a pas pu verifier vos droits."):
        reach = sender_reach(session_id)
    if action == "list":
        with cabinet_db() as db:
            rows = db.execute(Q.routines_listed).fetchall()
        mine = [r for r in rows if reach is None or set(r[2]) <= set(reach.get("company_ids") or [])]
        if not mine:
            return routine("Aucune routine pour vos dossiers.")
        return ok(f"{len(mine)} routine(s) :\n" + "\n".join(f"- {described(r)}" for r in mine))
    if action == "create":
        schedule = (kind, dossier_ids or [], every, day, hour)
        return created(*schedule, email, reach, client_confirmed, client_message, session_id)
    row = own_routine(routine_id, reach)
    if not row:
        return question(f"Quelle routine ? Aucune n° {routine_id} sur vos dossiers (demandez la liste).")
    if action == "delete" and not confirmed(session_id, client_confirmed, "je la supprime", f"n° {routine_id}"):
        return question(f"Routine {described(row)}. Je la supprime ? oui / non")
    with cabinet_db() as db:
        if action == "delete":
            db.execute(Q.routine_removed, (routine_id,))
            return ok(f"Routine n° {routine_id} supprimée.")
        upcoming = next_run(*row[3:6], datetime.now(ZONE))
        db.execute(Q.routine_enabled, (action == "resume", upcoming, routine_id))
    if action == "pause":
        return ok(f"Routine n° {routine_id} en pause.")
    return ok(f"Routine n° {routine_id} reprise : prochaine le {upcoming:%Y-%m-%d %H:%M}.")


def created(kind, dossier_ids, every, day, hour, email, reach, client_confirmed, client_message, session_id):
    """manage_routine's create: check, read back, then save the routine (see manage_routine)."""
    problem = schedule_problem(kind, every, day, hour)
    if problem:
        return question(f"Pour cette routine : {problem}")
    if not dossier_ids:
        return question("Pour quels dossiers ?")
    names = {d: require_dossier(d) for d in dossier_ids}
    if reach is not None:
        foreign = [names[d] for d in dossier_ids if d not in (reach.get("company_ids") or [])]
        if foreign:
            listed = ", ".join(f"`{n}`" for n in foreign)
            return question(f"Ces dossiers ne vous sont pas attribués dans Experio : {listed}. Sans eux ?")
    notify = sender_of(session_id) or ""
    words = normalized("\n".join(client_messages(session_id, own_words(client_message))))
    if not notify and email.strip() and said(normalized(email.strip()), words):
        notify = email.strip()
    if not notify:
        return question("À quelle adresse e-mail envoyer ses rapports ?")
    upcoming = next_run(every, day, hour, datetime.now(ZONE))
    row = (0, kind, dossier_ids, every, day, hour, notify, True, upcoming)
    recap = described(row, names).removeprefix("n° 0 : ")
    if not confirmed(session_id, client_confirmed, "vous confirmez", KINDS[kind], when(every, day, hour)):
        return question(f"Routine : {recap}.\nRien n'est jamais déclaré ni comptabilisé sans vous. Vous confirmez ?")
    with cabinet_db() as db:
        (new_id,) = db.execute(
            Q.routine_made, (kind, dossier_ids, every, day, hour, notify, notify, upcoming)
        ).fetchone()
    return ok(f"Routine n° {new_id} créée : {recap}.")
