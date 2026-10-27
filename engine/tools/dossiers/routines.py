"""Routines: a task Tarik runs again and again on a schedule for a cabinet, made, listed, paused, resumed or deleted
by a manager (the Dossier graph's mode routine, after check_manager). engine/routines.py runs them.

A routine prepares or reports, never declares nor posts: « tva » and « ras » make the declarations of the last ended
period that do not exist yet (the Déclarations graph in its routine mode: one already there is only reported, never
refreshed), « progress » sends each dossier's progress (dossier_progress), « instruction » hands its own
``prompt`` to Tarik's agent as a message, run with the rights of ``notify`` (people.sender_of reads a routine's
session as it) and with nothing confirmed, declared nor posted (agent.run_workflow). Each run's report goes by email
to ``notify``, an address the cabinet allows, on the dossiers that address may still see (allowed_dossiers).

Routines live in each cabinet's own schema: the Routines page and the tool only ever see the current cabinet's.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from engine.tools.core.app import ToolError, ok, question, routine, step_tool
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import DOSSIER_DOMAIN
from engine.tools.core.conversation import client_messages, own_words
from engine.tools.core.experio import odoo_call, refused
from engine.tools.core.queries import Q
from engine.tools.core.words import normalized
from engine.tools.dossiers.identity import client_wrote, confirmed
from engine.tools.dossiers.people import ROUTINE_SESSION, collaborator, scoped, sender_of, sender_reach
from engine.tools.emails.sending import recipient_allowed

ZONE = ZoneInfo("Africa/Casablanca")
KINDS = {
    "tva": "préparer la déclaration de TVA de la dernière période",
    "ras": "préparer les déclarations RAS de la dernière période",
    "progress": "envoyer l'avancement",
    "instruction": "suivre sa consigne",
}
PROMPT_CHARACTERS = 2000  # an instruction's longest
INSTRUCTION_DOSSIERS = 50  # an instruction's most dossiers: each named in its message, at each run
# routines_listed's columns, in order: every reader takes a row as dict(zip(COLUMNS, row)).
COLUMNS = ("id", "kind", "dossier_ids", "every", "day", "hour", "notify", "enabled", "next_run_at", "created_by")
COLUMNS += ("prompt",)
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


def dossier_names(ids):
    """Return {id: name} of the cabinet's dossiers among ``ids``, in one Experio call; a dossier gone is left out."""
    if not ids:
        return {}
    found = odoo_call("res.company", "search_read", [*DOSSIER_DOMAIN, ("id", "in", list(ids))], fields=["name"])
    return {dossier["id"]: dossier["name"] for dossier in found}


def session_of(routine_id, due_at):
    """Return the conversation id of a routine's run for period ``due_at``: its runs together on the Executions
    page, and its sender read as the routine's address (people.sender_of)."""
    return f"{ROUTINE_SESSION}{routine_id}-{due_at.astimezone(ZONE):%Y%m%d%H}"


def described(kind, dossier_ids, every, day, hour, notify, next_at, names, prompt=""):
    """Return a routine as a French line: « préparer la TVA, chaque mois, le 5 à 8 h, dossier(s) `A`, `B`, rapport
    à x@cabinet.ma, prochaine le 2026-11-05 08:00 ». ``names``: dossier_names of its dossiers; ``prompt``: an
    instruction routine's own words, quoted."""
    what = f"{KINDS[kind]} « {prompt} »" if kind == "instruction" else KINDS[kind]
    dossiers = ", ".join(f"`{names.get(d, f'n° {d}')}`" for d in dossier_ids)
    return (
        f"{what}, {when(every, day, hour)}{f', dossier(s) {dossiers}' if dossiers else ''}, rapport à `{notify}`, "
        f"prochaine le {next_at.astimezone(ZONE):%Y-%m-%d %H:%M}"
    )


def routines_of_cabinet():
    """Return the current cabinet's routines as dicts of COLUMNS, oldest first."""
    with cabinet_db() as db:
        return [dict(zip(COLUMNS, row, strict=True)) for row in db.execute(Q.routines_listed).fetchall()]


def listed(r, names):
    """Return one routine (routines_of_cabinet's) as « n° 3 : <described> (en pause) »."""
    schedule = (r["every"], r["day"], r["hour"])
    shown = described(r["kind"], r["dossier_ids"], *schedule, r["notify"], r["next_run_at"], names, r["prompt"])
    return f"n° {r['id']} : {shown}" + ("" if r["enabled"] else " (en pause)")


def routine_problem(kind, every, day, hour, prompt, dossier_ids):
    """Return what is wrong with a routine's kind, schedule, instruction and number of dossiers (French), "" when
    nothing: the checks the chat's tool and the Routines page share."""
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
    if not dossier_ids:  # an instruction too: they are all its runs may reach (people.sender_user)
        return "pour quels dossiers ?"
    if kind == "instruction" and len(dossier_ids) > INSTRUCTION_DOSSIERS:
        return f"au plus {INSTRUCTION_DOSSIERS} dossiers pour une consigne (ils sont tous nommés à Tarik) ?"
    return ""


def foreign(dossier_ids, reach):
    """Return the dossiers of ``dossier_ids`` outside the sender's ``reach`` (sender_reach; None: no limit)."""
    return [] if reach is None else [d for d in dossier_ids if d not in reach["company_ids"]]


def mine(r, reach, sender):
    """True when the sender may see and manage routine ``r`` (routines_of_cabinet's): no limit (``reach`` None), or
    they made it (created_by) and every dossier it runs on is theirs."""
    return reach is None or (r["created_by"] == sender and not foreign(r["dossier_ids"], reach))


def allowed_dossiers(notify, dossier_ids):
    """Return the dossiers of ``dossier_ids`` whose data may still go to ``notify``, checked again at each run and by
    the Routines page: theirs in Experio and still open (people.scoped); None for an address that is no live
    collaborator nor staff (someone gone, an outside address)."""
    user = scoped(collaborator(notify), dossier_ids)
    return None if user is None else user["company_ids"]


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
    prompt: str = "",
    client_confirmed: bool = False,
    client_message: str = "",
    session_id: str = "",
) -> dict:
    """Make, list, pause, resume or delete the cabinet's routines (``action`` create, list, pause, resume, delete).

    create: ``kind`` (tva, ras, progress, instruction with its ``prompt``), ``dossier_ids`` (optional for an
    instruction), ``every`` (day, week, month), ``day`` (a weekday 0-6 from Monday, or a day of the month 1-28) and
    ``hour`` (0-23, Casablanca time). Its reports go to the email's sender,
    or in the chat to the ``email`` the client wrote; the cabinet must allow that address. Read back, made once the
    client said yes (``client_confirmed``). pause, resume, delete: ``routine_id``; delete is read back first.
    A sender sees and manages only the routines they made, on their own dossiers (mine).

    Returns an ok result once done, a routine result for an empty list, a question result for what is missing or to
    confirm.

    Raises:
        ToolError: unknown ``action``."""
    if action not in ACTIONS:
        raise ToolError(f"action : {', '.join(ACTIONS)}.")
    with refused(f"manage_routine {action}", "Experio n'a pas pu verifier vos droits."):
        reach = sender_reach(session_id)
    sender = sender_of(session_id) or CHAT
    if action == "create":
        return created(
            kind=kind,
            dossier_ids=dossier_ids or [],
            every=every,
            day=day,
            hour=hour,
            email=email,
            prompt=prompt.strip(),
            reach=reach,
            sender=sender,
            client_confirmed=client_confirmed,
            words=normalized("\n".join(client_messages(session_id, own_words(client_message)))),
            session_id=session_id,
        )
    rows = [r for r in routines_of_cabinet() if mine(r, reach, sender)]
    with refused(f"manage_routine {action}", "Experio n'a pas pu donner les dossiers."):
        names = dossier_names({d for r in rows for d in r["dossier_ids"]})
    if action == "list":
        if not rows:
            return routine("Aucune routine pour vos dossiers.")
        return ok(f"{len(rows)} routine(s) :\n" + "\n".join(f"- {listed(row, names)}" for row in rows))
    row = next((r for r in rows if r["id"] == routine_id), None)
    if not row:
        return question(f"Quelle routine ? Aucune n° {routine_id} parmi les vôtres (demandez la liste).")
    if action == "delete" and not confirmed(session_id, client_confirmed, "je la supprime", f"n° {routine_id}"):
        return question(f"Routine {listed(row, names)}. Je la supprime ? oui / non")
    with cabinet_db() as db:
        if action == "delete":
            db.execute(Q.routine_removed, (routine_id,))
            return ok(f"Routine n° {routine_id} supprimée.")
        upcoming = next_run(row["every"], row["day"], row["hour"], datetime.now(ZONE))
        db.execute(Q.routine_enabled, (action == "resume", upcoming, routine_id))
    if action == "pause":
        return ok(f"Routine n° {routine_id} en pause.")
    return ok(f"Routine n° {routine_id} reprise : prochaine le {upcoming:%Y-%m-%d %H:%M}.")


def created(*, kind, dossier_ids, every, day, hour, email, prompt, reach, sender, client_confirmed, words, session_id):
    """manage_routine's create: check, read back, then save the routine (see manage_routine)."""
    problem = routine_problem(kind, every, day, hour, prompt, dossier_ids)
    if problem:
        return question(f"Pour cette routine : {problem}")
    with refused("manage_routine create", "Experio n'a pas pu donner les dossiers."):
        names = dossier_names(dossier_ids)
    unknown = [d for d in dossier_ids if d not in names]
    if unknown:
        raise ToolError(f"Dossier(s) inconnu(s) dans Experio : {', '.join(map(str, unknown))}.")
    outside = foreign(dossier_ids, reach)
    if outside:
        listed_names = ", ".join(f"`{names[d]}`" for d in outside)
        return question(f"Ces dossiers ne vous sont pas attribués dans Experio : {listed_names}. Sans eux ?")
    email = email.strip()
    notify = sender if sender != CHAT else (email if email and client_wrote("email", email, words) else "")
    if not notify:
        return question("À quelle adresse e-mail envoyer ses rapports ?")
    if not recipient_allowed(notify):
        return question(f"`{notify}` n'est pas dans les adresses autorisées du cabinet : à quelle adresse envoyer ?")
    upcoming = next_run(every, day, hour, datetime.now(ZONE))
    recap = described(kind, dossier_ids, every, day, hour, notify, upcoming, names, prompt)
    if not confirmed(session_id, client_confirmed, "vous confirmez", KINDS[kind], when(every, day, hour)):
        return question(f"Routine : {recap}.\nRien n'est jamais déclaré ni comptabilisé sans vous. Vous confirmez ?")
    with cabinet_db() as db:
        values = (kind, dossier_ids, every, day, hour, notify, sender, upcoming, prompt, True)
        (new_id,) = db.execute(Q.routine_made, values).fetchone()
    return ok(f"Routine n° {new_id} créée : {recap}.")
