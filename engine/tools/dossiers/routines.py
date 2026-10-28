"""Routines: a task Tarik runs again and again on a schedule for a cabinet, made, listed, paused, resumed or deleted
on the Routines page (server/api/routines.py) or by a manager in a conversation (the Dossier graph's mode routine,
after check_manager). engine/routines.py starts each period as a Routine run (engine/routine.py).

A routine works on every dossier Tarik's own Experio account may open (search.opened_dossiers): that account's
access is the limit. It prepares or reports, never declares nor posts: « tva » and « ras » make the declarations of
the last ended period that do not exist yet (the Déclarations graph in its routine mode: one already there is only
reported, never refreshed), « progress » sends each dossier's progress (dossier_progress_figures), « instruction »
hands its own ``prompt`` to Tarik's agent as a message, with nothing confirmed, declared nor posted and only the
workflows that prepare (agent.run_workflow). Each run's report goes by email to ``notify``: an address the cabinet
allows, of someone Experio itself shows every dossier to (live: Experio's staff, or a collaborator of the cabinet's
admin profile, the users Experio puts on every new dossier, base_client res_company.py create), checked again at
each run.

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
from engine.tools.dossiers.people import RANK, ROUTINE_SESSION, collaborator, exactly, sender_of, sender_reach
from engine.tools.emails.sending import allowed_recipients, recipient_allowed

ZONE = ZoneInfo("Africa/Casablanca")
KINDS = {
    "tva": "préparer la déclaration de TVA de la dernière période",
    "ras": "préparer les déclarations RAS de la dernière période",
    "progress": "envoyer l'avancement des dossiers",
    "instruction": "suivre sa consigne",
}
PROMPT_CHARACTERS = 2000  # an instruction's longest
# routines_listed's columns, in order: every reader takes a row as dict(zip(COLUMNS, row)).
COLUMNS = (
    "id",
    "kind",
    "every",
    "days",
    "hour",
    "minute",
    "notify",
    "enabled",
    "next_run_at",
    "created_by",
    "prompt",
    "name",
)
SCHEDULE = ("every", "days", "hour", "minute")  # a routine's time, in next_run's and when's order
# What may change in a routine, in routine_changed's order (the page's form, the chat's update).
FIELDS = ("enabled", "kind", "every", "days", "hour", "minute", "notify", "prompt", "name")
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
ACTIONS = ("create", "update", "list", "pause", "resume", "delete")
DEFAULT_HOUR = 8  # a routine made without an hour
NAME_CHARACTERS = 80  # a routine's name: its email's subject


def name_problem(name):
    """Return what is wrong with a routine's name (French), "" when nothing: one line (cleaned before), short."""
    return f"un nom de {NAME_CHARACTERS} caractères au plus" if len(name) > NAME_CHARACTERS else ""


CHAT = "chat"  # created_by of a routine made in the chat, where nobody can tell who writes


WORKDAYS, WEEKEND = [0, 1, 2, 3, 4], [5, 6]


def next_run(every, days, hour, minute, after):
    """Return the first time after ``after`` (an aware datetime) the schedule falls on, in Casablanca time: each day
    at ``hour``:``minute``; each week on each weekday of ``days`` (Monday 0); each month on its day ``days[0]`` (1
    to 28, in every month)."""
    local = after.astimezone(ZONE)
    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if every == "day":
        return at if at > local else at + timedelta(days=1)
    if every == "week":
        return next(
            at + timedelta(days=ahead)
            for ahead in range(8)
            if (at + timedelta(days=ahead)).weekday() in days and at + timedelta(days=ahead) > local
        )
    at = at.replace(day=days[0])
    if at > local:
        return at
    return at.replace(year=at.year + at.month // 12, month=at.month % 12 + 1)


def when(every, days, hour, minute):
    """Return the schedule in the client's words: « chaque mois, le 5 à 8 h », « en semaine à 8 h 30 », « chaque
    lundi et jeudi à 8 h »."""
    at = f"{hour} h {minute:02d}" if minute else f"{hour} h"
    days = sorted(days)
    if every == "day" or (every == "week" and len(days) == len(WEEKDAYS)):
        return f"chaque jour à {at}"
    if every == "week" and days == WORKDAYS:
        return f"en semaine (du lundi au vendredi) à {at}"
    if every == "week" and days == WEEKEND:
        return f"le week-end à {at}"
    if every == "week":
        names = [WEEKDAYS[day] for day in days]
        listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} et {names[-1]}"
        return f"chaque {listed} à {at}"
    return f"chaque mois, le {days[0]} à {at}"


def session_of(routine_id, due_at):
    """Return the conversation id of a routine's run for period ``due_at``: its runs together on the Executions
    page. The period's runs are unattended (runs.UNATTENDED: only what prepares); a reply there later is not."""
    return f"{ROUTINE_SESSION}{routine_id}-{due_at.astimezone(ZONE):%Y%m%d%H%M}"


def described(kind, every, days, hour, minute, notify, next_at, prompt=""):
    """Return a routine as a French line: « préparer la TVA, chaque mois, le 5 à 8 h, rapport à x@cabinet.ma,
    prochaine le 2026-11-05 08:00 »; an instruction routine's own words quoted."""
    what = f"{KINDS[kind]} « {prompt} »" if kind == "instruction" else KINDS[kind]
    return (
        f"{what}, {when(every, days, hour, minute)}, rapport à `{notify}`, "
        f"prochaine le {next_at.astimezone(ZONE):%Y-%m-%d %H:%M}"
    )


def routines_of_cabinet():
    """Return the current cabinet's routines as dicts of COLUMNS, oldest first."""
    with cabinet_db() as db:
        return [dict(zip(COLUMNS, row, strict=True)) for row in db.execute(Q.routines_listed).fetchall()]


def listed(r):
    """Return one routine (routines_of_cabinet's) as « n° 3 : <described> (en pause) »."""
    shown = described(r["kind"], *(r[key] for key in SCHEDULE), r["notify"], r["next_run_at"], r["prompt"])
    named = f" « {r['name']} »" if r["name"] else ""
    return f"n° {r['id']}{named} : {shown}" + ("" if r["enabled"] else " (en pause)")


def routine_problem(kind, every, days, hour, minute, prompt):
    """Return what is wrong with a routine's kind, schedule and instruction (French), "" when nothing: the checks
    the chat's tool and the Routines page share."""
    if kind not in KINDS:
        return f"quelle tâche : {', '.join(f'{k} ({w})' for k, w in KINDS.items())} ?"
    if every not in ("day", "week", "month"):
        return "à quel rythme : chaque jour, chaque semaine ou chaque mois ?"
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return "à quelle heure (0 à 23 h, 0 à 59 minutes) ?"
    if hour == 0 and kind != "progress":  # midnight in Morocco is still the day before on the server (UTC)
        return "à quelle heure, de 1 à 23 (à minuit, la période terminée serait encore celle d'avant) ?"
    if every == "day" and days:
        return "pas de jour à choisir pour une routine de chaque jour"
    if every == "week" and (not days or not set(days) <= set(range(7))):
        return "quels jours de la semaine ?"
    if every == "month" and (len(days) != 1 or not 1 <= days[0] <= 28):
        return "quel jour du mois (un seul, de 1 à 28, pour tomber chaque mois) ?"
    if kind == "instruction" and not prompt.strip():
        return "quelle consigne Tarik doit-il suivre à chaque fois ?"
    if len(prompt) > PROMPT_CHARACTERS:
        return f"une consigne plus courte ({PROMPT_CHARACTERS} caractères au plus) ?"
    return ""


def admins_where(*domain):
    """Return the cabinet's admins in Experio among the users ``domain`` selects, as [{id, name, login}] by name: one
    rule for the page's list and for the address check (live). A collaborator (not one of Experio's technical
    accounts, is_agent, as its own Collaborateurs list leaves them out), whose account is active, whose profile is
    admin (management_group, exp.user.profile) and who is admin on every dossier too (no « Profils par Dossier » row
    below admin: Experio's rights there come from it first, res_users.py _compute_groups_id)."""
    found = odoo_call(
        "res.users",
        "search_read",
        [
            ("employee_type", "=", "employee"),
            ("is_agent", "=", False),
            ("employee_state", "=", "active"),
            ("exp_profile_id.management_group", "=", "admin"),
            *domain,
        ],
        fields=["name", "login"],
        order="name",
    )
    if not found:
        return []
    lower = odoo_call(
        "exp.user.company.profile",
        "search_read",
        [("user_id", "in", [user["id"] for user in found]), ("profile_id.management_group", "!=", "admin")],
        fields=["user_id"],
    )
    demoted = {row["user_id"][0] for row in lower}
    return [user for user in found if user["id"] not in demoted]


def admins():
    """Return the cabinet's admins who may get a routine's reports (admins_where), among the addresses the cabinet
    allows, as [{name, email}]: Experio mails them at their login. Experio's staff may get them too, typed."""
    allowed = allowed_recipients()
    return [
        {"name": user["name"], "email": user["login"]}
        for user in admins_where()
        if recipient_allowed(user["login"], allowed)
    ]


def live(notify):
    """True when ``notify`` may get a routine's reports, which cover every dossier: the login of one of the cabinet's
    admins (admins_where: Experio shows a lesser profile only its own dossiers), or a member of Experio's staff.
    Checked again at each run: someone gone, moved down or demoted on a dossier pauses their routines."""
    if admins_where(("login", "=ilike", exactly(notify))):
        return True
    user = collaborator(notify)
    return bool(user) and user["employee_type"] == "internal"


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
    days: list | None = None,
    hour: int = -1,
    minute: int = -1,
    routine_id: int = 0,
    email: str = "",
    prompt: str = "",
    name: str = "",
    client_confirmed: bool = False,
    client_message: str = "",
    session_id: str = "",
) -> dict:
    """Make, change, list, pause, resume or delete the cabinet's routines (``action`` create, update, list, pause,
    resume, delete).

    create: ``kind`` (tva, ras, progress, instruction with its ``prompt``), ``every`` (day, week, month), ``days``
    (weekdays 0-6 from Monday each week, or the one day 1-28 each month), ``hour`` (0-23, Casablanca time; -1: 8 h)
    and ``minute`` (-1: 0), its optional ``name`` (its email's subject); it works on every dossier Tarik's Experio
    account sees, so only Experio's staff and the cabinet's admins make, change or resume one (in the chat, the team).
    Its reports go to the email's sender, or in the chat to the ``email`` the client wrote (address_problem). Read
    back, made once the client said yes (``client_confirmed``). update: ``routine_id`` and only what changes (an
    empty text, no days, -1 keep the routine's own), read back the same way. pause, resume, delete: ``routine_id``;
    delete is read back first. A sender sees and manages only the routines they made (mine).

    Returns an ok result once done, a routine result for an empty list, a question result for what is missing or to
    confirm, a stop result for a sender who may not make one.

    Raises:
        ToolError: unknown ``action``."""
    if action not in ACTIONS:
        raise ToolError(f"action : {', '.join(ACTIONS)}.")
    with refused(f"manage_routine {action}", "Experio n'a pas pu verifier vos droits."):
        reach = sender_reach(session_id)
    sender = sender_of(session_id) or CHAT
    if action in ("create", "update", "resume") and reach is not None and reach.get("rank", 0) < RANK["admin"]:
        return stop(
            "Une routine travaille sur tous les dossiers du cabinet : seuls ses administrateurs Experio et "
            "l'équipe Experio en créent, en modifient ou en reprennent une (ou la page Routines). Rien n'est changé."
        )
    if action == "create":
        words = normalized("\n".join(client_messages(session_id, own_words(client_message))))
        hour, minute = DEFAULT_HOUR if hour < 0 else hour, max(minute, 0)
        name = " ".join(name.split())  # one line: a subject
        given = (kind, every, sorted(set(days or [])), hour, minute, email.strip(), prompt.strip(), name)
        return created(*given, sender, client_confirmed, words, session_id)
    rows = [r for r in routines_of_cabinet() if mine(r, reach, sender)]
    if action == "list":
        if not rows:
            return routine("Aucune routine.")
        return ok(f"{len(rows)} routine(s) :\n" + "\n".join(f"- {listed(row)}" for row in rows))
    row = next((r for r in rows if r["id"] == routine_id), None)
    if not row:
        return question(f"Quelle routine ? Aucune n° {routine_id} parmi les vôtres (demandez la liste).")
    if action == "update":
        words = normalized("\n".join(client_messages(session_id, own_words(client_message))))
        asked = {"kind": kind, "every": every, "days": days, "hour": hour, "minute": minute, "prompt": prompt}
        asked |= {"name": name, "email": email}
        return updated(row, asked, sender, client_confirmed, words, session_id)
    if action == "delete" and not confirmed(session_id, client_confirmed, "je la supprime", f"n° {routine_id}"):
        return question(f"Routine {listed(row)}. Je la supprime ? oui / non")
    if action == "resume":  # turned back on: checked as the page checks it (problem_of)
        with refused("manage_routine resume", "Experio n'a pas pu vérifier cette routine."):
            problem = problem_of(row)
        if problem:
            return question(f"Pour reprendre la routine n° {routine_id} : {problem}")
    with cabinet_db() as db:
        if action == "delete":
            db.execute(Q.routine_removed, (routine_id,))
            return ok(f"Routine n° {routine_id} supprimée.")
        # resumed: its next time from now (next_of); paused: its own kept, moved again when resumed
        upcoming = next_of({**row, "enabled": action == "resume"}, {**row, "enabled": False})
        db.execute(Q.routine_enabled, (action == "resume", upcoming, routine_id))
    if action == "pause":
        return ok(f"Routine n° {routine_id} en pause.")
    return ok(f"Routine n° {routine_id} reprise : prochaine le {upcoming:%Y-%m-%d %H:%M}.")


def reported_to(sender, email, words):
    """Return the address a routine's reports go to: the email's sender, or in the chat the ``email`` the client wrote
    themselves (never one a model chose); "" when there is none."""
    if sender != CHAT:
        return sender
    return email if email and client_wrote("email", email, words) else ""


# --- One routine checked and saved: the Routines page and the chat alike ---


def problem_of(values):
    """Return what is wrong with routine ``values`` ({FIELDS}) in French, "" when nothing: its task, schedule and
    instruction (routine_problem), its name (name_problem), its report's address (address_problem: Experio read)."""
    problem = routine_problem(*(values[key] for key in ("kind", *SCHEDULE, "prompt"))) or name_problem(values["name"])
    return problem or address_problem(values["notify"])


def next_of(values, before=None):
    """Return the next run of routine ``values``: its own time from now when new, when its schedule changed or when
    it is turned back on; else ``before``'s (a period already due is never skipped by an unrelated change)."""
    moved = before is None or any(values[key] != before[key] for key in SCHEDULE)
    if moved or (values["enabled"] and not before["enabled"]):
        return next_run(*(values[key] for key in SCHEDULE), datetime.now(ZONE))
    return before["next_run_at"]


def saved(values, upcoming, before=None, author=""):
    """Save routine ``values`` ({FIELDS}) with its next run ``upcoming``: a new one made by ``author``, or ``before``
    changed. Return its id."""
    with cabinet_db() as db:
        if before is None:
            made = (*(values[key] for key in ("kind", *SCHEDULE, "notify")), author, upcoming)
            more = (values["prompt"], values["enabled"], values["name"])
            return db.execute(Q.routine_made, (*made, *more)).fetchone()[0]
        db.execute(Q.routine_changed, (*(values[key] for key in FIELDS), upcoming, before["id"]))
    return before["id"]


def recap_of(values, upcoming):
    """Return routine ``values`` as read back to the client: « « Rappel Manager », <described> »."""
    shown = described(values["kind"], *(values[key] for key in SCHEDULE), values["notify"], upcoming, values["prompt"])
    return (f"« {values['name']} », " if values["name"] else "") + shown


def created(kind, every, days, hour, minute, email, prompt, name, sender, client_confirmed, words, session_id):
    """manage_routine's create: check, read back, then save the routine (see manage_routine)."""
    values = {"enabled": True, "kind": kind, "every": every, "days": days, "hour": hour, "minute": minute}
    values |= {"notify": reported_to(sender, email, words), "prompt": prompt, "name": name}
    if not values["notify"]:
        return question("À quelle adresse e-mail envoyer ses rapports ?")
    with refused("manage_routine create", "Experio n'a pas pu vérifier cette routine."):
        problem = problem_of(values)
    if problem:
        return question(f"Pour cette routine : {problem}")
    upcoming = next_of(values)
    recap = recap_of(values, upcoming)
    read_back = (
        f"Routine : {recap}, sur tous les dossiers du cabinet.\nRien n'est jamais déclaré ni comptabilisé sans vous. "
        "Vous confirmez ?"
    )
    if not confirmed(session_id, client_confirmed, read_back):  # this very read-back, whole: never another's yes
        return question(read_back)
    return ok(f"Routine n° {saved(values, upcoming, author=sender)} créée : {recap}.")


def updated(row, asked, sender, client_confirmed, words, session_id):
    """manage_routine's update: routine ``row`` with what ``asked`` changes (an empty text, no days, -1 keep its
    own), checked as a new one (problem_of), read back, saved once the client said yes (saved). Its next time moves
    only when its schedule changes (next_of)."""
    new = dict(row)
    for key in ("kind", "every", "prompt", "name"):
        if str(asked[key] or "").strip():
            new[key] = str(asked[key]).strip()
    new["name"] = " ".join(new["name"].split())  # one line: a subject
    if new["kind"] != "instruction":  # only an instruction has words of its own: another kind's would stay unseen
        new["prompt"] = row["prompt"]
    for key in ("hour", "minute"):
        if asked[key] is not None and asked[key] >= 0:
            new[key] = asked[key]
    if asked["days"]:
        new["days"] = sorted(set(asked["days"]))
    elif new["every"] != row["every"]:  # another rhythm: its old days mean something else (a 5th is no Saturday)
        new["days"] = []
    if asked["email"].strip() and sender != CHAT and asked["email"].strip().lower() != sender.lower():
        return stop(
            "Par e-mail, les rapports d'une routine vont à son expéditeur : pour une autre adresse, passez par "
            "la page Routines. Rien n'est changé."
        )
    if asked["email"].strip():
        new["notify"] = reported_to(sender, asked["email"].strip(), words)
        if not new["notify"]:  # an address the client did not write: never one a model chose
            return question("À quelle adresse e-mail envoyer ses rapports ? Écrivez-la telle quelle.")
    if new == row:
        return question(f"Que changer dans la routine n° {row['id']} ? Rien de nouveau n'a été donné.")
    with refused("manage_routine update", "Experio n'a pas pu vérifier cette routine."):
        problem = problem_of(new)
    if problem:
        return question(f"Pour la routine n° {row['id']} : {problem}")
    upcoming = next_of(new, row)
    recap = recap_of(new, upcoming)
    # The yes must answer this very read-back, whole: its task, time, words, name and address as read to the client.
    read_back = f"Routine n° {row['id']}, après modification : {recap}. Je la modifie ?"
    if not confirmed(session_id, client_confirmed, read_back):
        return question(read_back)
    saved(new, upcoming, row)
    return ok(f"Routine n° {row['id']} modifiée : {recap}.")
