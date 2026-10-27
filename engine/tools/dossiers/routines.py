"""Routines: a task Tarik runs again and again on a schedule for a cabinet, made, listed, paused, resumed or deleted
on the Routines page (server/api/routines.py) or by a manager in a conversation (the Dossier graph's mode routine,
after check_manager). engine/routines.py runs them.

A routine works on every dossier Tarik's own Experio account sees (DOSSIER_DOMAIN): that account's access is the
limit, as in the chat. It prepares or reports, never declares nor posts: « tva » and « ras » make the declarations of
the last ended period that do not exist yet (the Déclarations graph in its routine mode: one already there is only
reported, never refreshed), « progress » sends each dossier's progress (dossier_progress), « instruction » hands its
own ``prompt`` to Tarik's agent as a message, with nothing confirmed, declared nor posted and only the workflows that
prepare (agent.run_workflow). Each run's report goes by email to ``notify``: an address the cabinet allows, of
someone Experio itself shows every dossier to (live: Experio's staff, or a collaborator of the cabinet's admin
profile, the users Experio puts on every new dossier, base_client res_company.py create), checked again at each run.

Routines live in each cabinet's own schema: the Routines page and the tool only ever see the current cabinet's.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from engine.tools.core.app import ToolError, ok, question, routine, step_tool, stop
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.conversation import client_messages, own_words
from engine.tools.core.experio import odoo_call, refused
from engine.tools.core.queries import Q
from engine.tools.core.words import normalized
from engine.tools.dossiers.identity import client_wrote, confirmed
from engine.tools.dossiers.people import RANK, ROUTINE_SESSION, collaborator, level_of, sender_of, sender_reach
from engine.tools.emails.sending import recipient_allowed

ZONE = ZoneInfo("Africa/Casablanca")
KINDS = {
    "tva": "préparer la déclaration de TVA de la dernière période",
    "ras": "préparer les déclarations RAS de la dernière période",
    "progress": "envoyer l'avancement des dossiers",
    "instruction": "suivre sa consigne",
}
PROMPT_CHARACTERS = 2000  # an instruction's longest
# routines_listed's columns, in order: every reader takes a row as dict(zip(COLUMNS, row)).
COLUMNS = ("id", "kind", "every", "day", "hour", "notify", "enabled", "next_run_at", "created_by", "prompt")
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
ACTIONS = ("create", "list", "pause", "resume", "delete")
CHAT = "chat"  # created_by of a routine made in the chat, where nobody can tell who writes


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


def session_of(routine_id, due_at):
    """Return the conversation id of a routine's run for period ``due_at``: its runs together on the Executions
    page; the agent's tools refuse there what does not only prepare (agent.ROUTINE_WORKFLOWS)."""
    return f"{ROUTINE_SESSION}{routine_id}-{due_at.astimezone(ZONE):%Y%m%d%H}"


def described(kind, every, day, hour, notify, next_at, prompt=""):
    """Return a routine as a French line: « préparer la TVA, chaque mois, le 5 à 8 h, rapport à x@cabinet.ma,
    prochaine le 2026-11-05 08:00 »; an instruction routine's own words quoted."""
    what = f"{KINDS[kind]} « {prompt} »" if kind == "instruction" else KINDS[kind]
    return (
        f"{what}, {when(every, day, hour)}, rapport à `{notify}`, "
        f"prochaine le {next_at.astimezone(ZONE):%Y-%m-%d %H:%M}"
    )


def routines_of_cabinet():
    """Return the current cabinet's routines as dicts of COLUMNS, oldest first."""
    with cabinet_db() as db:
        return [dict(zip(COLUMNS, row, strict=True)) for row in db.execute(Q.routines_listed).fetchall()]


def listed(r):
    """Return one routine (routines_of_cabinet's) as « n° 3 : <described> (en pause) »."""
    shown = described(r["kind"], r["every"], r["day"], r["hour"], r["notify"], r["next_run_at"], r["prompt"])
    return f"n° {r['id']} : {shown}" + ("" if r["enabled"] else " (en pause)")


def routine_problem(kind, every, day, hour, prompt):
    """Return what is wrong with a routine's kind, schedule and instruction (French), "" when nothing: the checks
    the chat's tool and the Routines page share."""
    if kind not in KINDS:
        return f"quelle tâche : {', '.join(f'{k} ({w})' for k, w in KINDS.items())} ?"
    if every not in ("day", "week", "month"):
        return "à quel rythme : chaque jour, chaque semaine ou chaque mois ?"
    if not 0 <= hour <= 23:
        return "à quelle heure (0 à 23) ?"
    if hour == 0 and kind != "progress":  # midnight in Morocco is still the day before on the server (UTC)
        return "à quelle heure, de 1 à 23 (à minuit, la période terminée serait encore celle d'avant) ?"
    if every == "week" and not 0 <= day <= 6:
        return "quel jour de la semaine ?"
    if every == "month" and not 1 <= day <= 28:
        return "quel jour du mois (1 à 28, pour tomber chaque mois) ?"
    if kind == "instruction" and not prompt.strip():
        return "quelle consigne Tarik doit-il suivre à chaque fois ?"
    if len(prompt) > PROMPT_CHARACTERS:
        return f"une consigne plus courte ({PROMPT_CHARACTERS} caractères au plus) ?"
    return ""


def live(notify):
    """True when ``notify`` may get a routine's reports, which cover every dossier: a member of Experio's staff, or a
    collaborator whose account is live (employee_state « active »: not invited and never in, not deactivated) and
    whose profile is the cabinet's admin (management_group admin: Experio shows a lesser one only its own dossiers).
    Checked again at each run: someone gone or moved down pauses their routines."""
    user = collaborator(notify)
    if not user or user["employee_type"] == "internal":
        return bool(user)
    profile = odoo_call("res.users", "read", [user["id"]], fields=["exp_profile_id"])[0]["exp_profile_id"]
    return user.get("employee_state") == "active" and RANK[level_of(profile)] >= RANK["admin"]


def address_problem(notify):
    """Return why a routine's reports may not go to ``notify`` (French), "" when they may."""
    if not recipient_allowed(notify):
        return f"`{notify}` n'est pas dans les adresses autorisées du cabinet (Réglages)"
    if not live(notify):
        return f"`{notify}` n'est ni un administrateur actif du cabinet dans Experio ni de l'équipe Experio"
    return ""


def mine(r, reach, sender):
    """True when the sender may see and manage routine ``r`` (routines_of_cabinet's): no limit (``reach`` None), or
    they made it (created_by)."""
    return reach is None or r["created_by"] == sender


@step_tool
def manage_routine(
    action: str,
    kind: str = "",
    every: str = "",
    day: int = 0,
    hour: int = 8,
    routine_id: int = 0,
    email: str = "",
    prompt: str = "",
    client_confirmed: bool = False,
    client_message: str = "",
    session_id: str = "",
) -> dict:
    """Make, list, pause, resume or delete the cabinet's routines (``action`` create, list, pause, resume, delete).

    create: ``kind`` (tva, ras, progress, instruction with its ``prompt``), ``every`` (day, week, month), ``day`` (a
    weekday 0-6 from Monday, or a day of the month 1-28) and ``hour`` (0-23, Casablanca time); it works on every
    dossier Tarik's Experio account sees, so only Experio's staff and the cabinet's admins make or resume one (in the
    chat, the team). Its reports go to the email's sender, or in the chat to the ``email`` the client wrote
    (address_problem). Read back, made once the client said yes (``client_confirmed``). pause, resume, delete:
    ``routine_id``; delete is read back first. A sender sees and manages only the routines they made (mine).

    Returns an ok result once done, a routine result for an empty list, a question result for what is missing or to
    confirm, a stop result for a sender who may not make one.

    Raises:
        ToolError: unknown ``action``."""
    if action not in ACTIONS:
        raise ToolError(f"action : {', '.join(ACTIONS)}.")
    with refused(f"manage_routine {action}", "Experio n'a pas pu verifier vos droits."):
        reach = sender_reach(session_id)
    sender = sender_of(session_id) or CHAT
    if action in ("create", "resume") and reach is not None and reach.get("rank", 0) < RANK["admin"]:
        return stop(
            "Une routine travaille sur tous les dossiers du cabinet : seuls ses administrateurs Experio et "
            "l'équipe Experio en créent ou en reprennent une (ou la page Routines). Rien n'est changé."
        )
    if action == "create":
        words = normalized("\n".join(client_messages(session_id, own_words(client_message))))
        given = (kind, every, day, hour, email.strip(), prompt.strip())
        return created(*given, sender, client_confirmed, words, session_id)
    rows = [r for r in routines_of_cabinet() if mine(r, reach, sender)]
    if action == "list":
        if not rows:
            return routine("Aucune routine.")
        return ok(f"{len(rows)} routine(s) :\n" + "\n".join(f"- {listed(row)}" for row in rows))
    row = next((r for r in rows if r["id"] == routine_id), None)
    if not row:
        return question(f"Quelle routine ? Aucune n° {routine_id} parmi les vôtres (demandez la liste).")
    if action == "delete" and not confirmed(session_id, client_confirmed, "je la supprime", f"n° {routine_id}"):
        return question(f"Routine {listed(row)}. Je la supprime ? oui / non")
    with cabinet_db() as db:
        if action == "delete":
            db.execute(Q.routine_removed, (routine_id,))
            return ok(f"Routine n° {routine_id} supprimée.")
        upcoming = next_run(row["every"], row["day"], row["hour"], datetime.now(ZONE))
        db.execute(Q.routine_enabled, (action == "resume", upcoming, routine_id))
    if action == "pause":
        return ok(f"Routine n° {routine_id} en pause.")
    return ok(f"Routine n° {routine_id} reprise : prochaine le {upcoming:%Y-%m-%d %H:%M}.")


def created(kind, every, day, hour, email, prompt, sender, client_confirmed, words, session_id):
    """manage_routine's create: check, read back, then save the routine (see manage_routine)."""
    problem = routine_problem(kind, every, day, hour, prompt)
    if problem:
        return question(f"Pour cette routine : {problem}")
    notify = sender if sender != CHAT else (email if email and client_wrote("email", email, words) else "")
    if not notify:
        return question("À quelle adresse e-mail envoyer ses rapports ?")
    with refused("manage_routine create", "Experio n'a pas pu verifier cette adresse."):
        problem = address_problem(notify)
    if problem:
        return question(f"{problem} : à quelle adresse envoyer ?")
    upcoming = next_run(every, day, hour, datetime.now(ZONE))
    recap = described(kind, every, day, hour, notify, upcoming, prompt)
    if not confirmed(session_id, client_confirmed, "vous confirmez", KINDS[kind], when(every, day, hour)):
        return question(
            f"Routine : {recap}, sur tous les dossiers du cabinet.\nRien n'est jamais déclaré ni comptabilisé sans "
            "vous. Vous confirmez ?"
        )
    with cabinet_db() as db:
        values = (kind, every, day, hour, notify, sender, upcoming, prompt, True)
        (new_id,) = db.execute(Q.routine_made, values).fetchone()
    return ok(f"Routine n° {new_id} créée : {recap}.")
