"""Routines: their schedule, made only by those who may (on every dossier Tarik sees) and on a yes, each period run
once, their report sent to a live collaborator, and a routine's run never confirming nor writing unattended."""

import asyncio
from datetime import datetime, timedelta

import pytest

from engine import routines as clock
from engine.tools.core.cabinets import cabinet_db
from engine.tools.dossiers import routines
from engine.tools.dossiers.routines import ZONE, next_run


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=ZONE)


@pytest.mark.functionality
def test_the_next_time_a_schedule_falls_on():
    assert next_run("day", [], 8, 0, at("2026-10-27 07:59")) == at("2026-10-27 08:00")
    assert next_run("day", [], 8, 0, at("2026-10-27 08:00")) == at("2026-10-28 08:00")  # its own time: the next one
    assert next_run("week", [0], 9, 0, at("2026-10-27 10:00")) == at("2026-11-02 09:00")  # a Tuesday: next Monday
    assert next_run("week", [1], 9, 0, at("2026-10-27 10:00")) == at("2026-11-03 09:00")  # its own day, past: a week on
    assert next_run("week", [0, 3], 9, 0, at("2026-10-27 10:00")) == at("2026-10-29 09:00")  # Tuesday: then Thursday
    assert next_run("week", [0, 1, 2, 3, 4], 9, 0, at("2026-10-31 10:00")) == at("2026-11-02 09:00")  # Saturday: Monday
    assert next_run("week", [5, 6], 9, 0, at("2026-11-01 10:00")) == at("2026-11-07 09:00")  # Sunday, past: Saturday
    assert next_run("month", [5], 8, 0, at("2026-10-04 23:00")) == at("2026-10-05 08:00")
    assert next_run("month", [5], 8, 0, at("2026-12-05 08:00")) == at("2027-01-05 08:00")  # past December
    assert next_run("day", [], 8, 30, at("2026-10-27 08:10")) == at("2026-10-27 08:30")  # its minute, not the hour


@pytest.mark.communication
def test_a_schedule_said_in_plain_words():
    assert routines.when("week", [0, 1, 2, 3, 4], 8, 0) == "en semaine (du lundi au vendredi) à 8 h"
    assert routines.when("week", [5, 6], 8, 0) == "le week-end à 8 h"
    assert routines.when("week", [0, 3, 4], 9, 0) == "chaque lundi, jeudi et vendredi à 9 h"
    assert routines.when("week", [2], 9, 0) == "chaque mercredi à 9 h"
    assert routines.when("week", list(range(7)), 9, 0) == "chaque jour à 9 h"
    assert routines.when("month", [5], 8, 0) == "chaque mois, le 5 à 8 h"
    assert routines.when("week", [5, 6], 8, 5) == "le week-end à 8 h 05"
    assert "59 minutes" in routines.routine_problem("tva", "day", [], 8, 60, "")
    assert "quels jours" in routines.routine_problem("tva", "week", [], 8, 0, "")
    assert "un seul" in routines.routine_problem("tva", "month", [5, 6], 8, 0, "")
    assert "pas de jour" in routines.routine_problem("tva", "day", [99], 8, 0, "")


@pytest.fixture
def chat(db_cabinet, monkeypatch):
    """The chat (no sender to limit); the conversation's words; addresses @cabinet.ma allowed and live."""
    said = {"words": "chaque mois le 5 a 8h, rapport a chef@cabinet.ma"}
    monkeypatch.setattr(routines, "sender_reach", lambda session_id: None)
    monkeypatch.setattr(routines, "sender_of", lambda session_id: None)
    monkeypatch.setattr(routines, "recipient_allowed", lambda address: address.endswith("@cabinet.ma"))
    monkeypatch.setattr(routines, "live", lambda address: address != "parti@cabinet.ma")
    monkeypatch.setattr(routines, "client_messages", lambda session_id, message: [said["words"]])
    monkeypatch.setattr(routines, "confirmed", lambda session_id, yes, *parts: yes)
    return said


def make(yes, email="chef@cabinet.ma", hour=8, kind="tva", prompt=""):
    return routines.manage_routine(
        "create", kind, "month", [5], hour, email=email, prompt=prompt, client_confirmed=yes, session_id="s"
    )


@pytest.mark.security
def test_a_routine_is_read_back_then_made_on_the_clients_yes(chat):
    asked = make(False)
    assert asked["status"] == "question" and "Vous confirmez" in asked["message"]
    assert "préparer la déclaration de TVA de la dernière période, chaque mois, le 5 à 8 h" in asked["message"]
    assert "rapport à `chef@cabinet.ma`" in asked["message"] and "sur tous les dossiers du cabinet" in asked["message"]
    with cabinet_db() as db:
        assert db.execute("select count(*) from tarik_routines").fetchone() == (0,)
    done = make(True)
    assert done["status"] == "ok" and done["message"].startswith("Routine n° ")
    listed = routines.manage_routine("list")
    assert "1 routine(s)" in listed["message"] and "prochaine le" in listed["message"]


@pytest.mark.security
def test_a_report_address_not_written_not_allowed_or_not_live_is_asked(chat):
    assert make(True, email="autre@ailleurs.ma")["message"] == "À quelle adresse e-mail envoyer ses rapports ?"
    chat["words"] += " ou moi@gmail.com ou parti@cabinet.ma"
    assert "pas dans les adresses autorisées" in make(True, email="moi@gmail.com")["message"]
    assert "ni un administrateur actif du cabinet" in make(True, email="parti@cabinet.ma")["message"]


@pytest.mark.functionality
def test_a_declaration_routine_at_midnight_is_asked_another_hour(chat):
    assert "de 1 à 23" in make(True, hour=0)["message"]  # midnight in Morocco: the day before on the server


@pytest.mark.security
@pytest.mark.parametrize(("rank", "made"), [(2, False), (3, True)])
def test_only_admins_and_staff_make_or_resume_a_routine_and_each_manages_their_own(chat, monkeypatch, rank, made):
    make(True)  # made in the chat
    reach = {"id": 7, "name": "Chef", "company_ids": [5], "rank": rank}
    monkeypatch.setattr(routines, "sender_reach", lambda session_id: reach)
    monkeypatch.setattr(routines, "sender_of", lambda session_id: "chef@cabinet.ma")
    result = make(True)
    assert (result["status"] == "ok") is made  # an approver may not: a routine covers the whole cabinet
    if not made:
        assert result["status"] == "stop" and "administrateurs" in result["message"]
    shown = routines.manage_routine("list", session_id="s")
    assert ("1 routine(s)" in shown["message"]) is made  # the chat's routine is never theirs
    with cabinet_db() as db:
        (other,) = db.execute("select min(id) from tarik_routines").fetchone()
    assert "Quelle routine" in routines.manage_routine("pause", routine_id=other, session_id="s")["message"]


@pytest.mark.functionality
def test_a_routine_paused_resumed_and_deleted_after_a_yes(chat):
    make(True)
    with cabinet_db() as db:
        (routine_id,) = db.execute("select id from tarik_routines").fetchone()
    routines.manage_routine("pause", routine_id=routine_id)
    assert "(en pause)" in routines.manage_routine("list")["message"]
    assert "reprise : prochaine le" in routines.manage_routine("resume", routine_id=routine_id)["message"]
    assert "Je la supprime" in routines.manage_routine("delete", routine_id=routine_id)["message"]
    routines.manage_routine("delete", routine_id=routine_id, client_confirmed=True)
    assert routines.manage_routine("list")["routine"]


@pytest.mark.functionality
def test_an_instruction_routine_needs_its_words(chat):
    assert "quelle consigne" in make(True, kind="instruction")["message"]
    said = "Dis-moi les factures en brouillon."
    asked = make(False, kind="instruction", prompt=said)
    assert "suivre sa consigne « Dis-moi les factures en brouillon. »" in asked["message"]
    assert make(True, kind="instruction", prompt=said)["status"] == "ok"
    with cabinet_db() as db:
        assert db.execute("select kind, prompt from tarik_routines").fetchone() == ("instruction", said)


def due_routine(kind="progress", prompt=""):
    """Make a routine due an hour ago, its reports to chef@cabinet.ma; return its id."""
    with cabinet_db() as db:
        return db.execute(
            "insert into tarik_routines (kind, every, days, hour, notify, created_by, next_run_at, prompt) "
            "values (%s, 'day', '{}', 8, 'chef@cabinet.ma', 'chat', %s, %s) returning id",
            (kind, datetime.now(ZONE) - timedelta(hours=1), prompt),
        ).fetchone()[0]


@pytest.mark.functionality
def test_a_due_routine_is_claimed_once_and_moved_to_its_next_time(db_cabinet):
    routine_id = due_routine()
    due = clock.claimed()
    assert [row[0] for row in due] == [routine_id]
    assert clock.claimed() == due  # never reported (the server stopped): run again, the same period
    clock.record(routine_id, due[0][7], "fait")
    assert clock.claimed() == []  # reported: done
    with cabinet_db() as db:
        (upcoming,) = db.execute("select next_run_at from tarik_routines where id = %s", (routine_id,)).fetchone()
        db.execute("update tarik_routines set next_run_at = %s where id = %s", (due[0][7], routine_id))
    assert upcoming > datetime.now(ZONE) and upcoming.astimezone(ZONE).hour == 8
    assert clock.claimed() == []  # the same period due again: recorded, never run twice


@pytest.mark.functionality
def test_a_period_runs_once_here_whatever_changes_meanwhile_and_each_minute_has_its_own_conversation(monkeypatch):
    begun = []

    async def ran(cabinet, row):
        begun.append(row)

    monkeypatch.setattr(clock, "ran", ran)
    due = datetime(2026, 10, 27, 8, 0, tzinfo=ZONE)
    row = (3, "progress", "day", [], 8, 0, "chef@cabinet.ma", due, "")

    async def twice():
        clock.started("c", row)
        clock.started("c", (*row[:6], "autre@cabinet.ma", *row[7:]))  # its address changed while it runs
        await asyncio.gather(*clock.PENDING)

    asyncio.run(twice())
    assert begun == [row]
    assert routines.session_of(3, due) != routines.session_of(3, due.replace(minute=30))  # moved to 8:30 that day


@pytest.fixture
def running(db_cabinet, monkeypatch):
    """A routine's run on the test cabinet: dossiers 5 and 6 seen by Tarik, its address live; the tools called and
    the runs started."""
    state = {"calls": [], "started": []}
    monkeypatch.setattr(clock, "opened_dossiers", lambda: [{"id": 5, "name": "ALPHA"}, {"id": 6, "name": "BETA"}])
    monkeypatch.setattr(clock, "address_problem", lambda notify: "")

    async def call(cabinet, tool, **arguments):
        state["calls"].append((tool, arguments))
        return f"avancement {arguments.get('dossier_id')}" if tool == "dossier_progress" else {"status": "ok"}

    async def start(name, payload):
        state["started"].append((name, payload))
        done = asyncio.get_running_loop().create_future()
        given = payload["input"]
        reply = given["message"] if name == "discussion" else f"TVA du dossier {given['call']['dossier_id']} préparée."
        done.set_result(({"reply": f"fait : {reply}", "message": reply}, None))
        return "run", done

    monkeypatch.setattr(clock, "call", call)
    monkeypatch.setattr(clock.runs, "start", start)
    return state


@pytest.mark.functionality
def test_a_routine_works_on_every_dossier_tarik_sees_and_its_report_goes_to_its_address(running, db_cabinet):
    routine_id = due_routine("tva")
    (row,) = clock.claimed()
    report = asyncio.run(clock.ran(db_cabinet, row))
    assert report == "TVA du dossier 5 préparée.\n\nTVA du dossier 6 préparée."
    assert [p["input"]["call"]["routine"] for _, p in running["started"]] == [True, True]  # made, never taken up
    (tool, sent) = running["calls"][-1]
    assert tool == "send_email" and sent["to"] == "chef@cabinet.ma" and sent["body"] == report
    assert sent["attach_session"].startswith(f"routine-{routine_id}-")  # the XML the runs made
    with cabinet_db() as db:
        assert db.execute("select report from tarik_routine_runs").fetchone() == (report,)


@pytest.mark.functionality
def test_an_instruction_is_handed_to_the_agent_once_in_the_routines_own_session(running, db_cabinet):
    routine_id = due_routine("instruction", "Liste les brouillons.")
    (row,) = clock.claimed()
    assert asyncio.run(clock.ran(db_cabinet, row)) == "fait : Liste les brouillons."
    ((name, payload),) = running["started"]
    assert name == "discussion" and payload["session_id"].startswith(f"routine-{routine_id}-")


@pytest.mark.security
def test_a_routine_whose_address_is_gone_is_paused_and_sends_nothing(running, db_cabinet, monkeypatch):
    routine_id = due_routine()
    (row,) = clock.claimed()
    monkeypatch.setattr(clock, "address_problem", lambda notify: "`chef@cabinet.ma` est parti")
    assert asyncio.run(clock.ran(db_cabinet, row)) == "Routine en pause : `chef@cabinet.ma` est parti."
    assert running["calls"] == []
    with cabinet_db() as db:
        assert db.execute("select enabled from tarik_routines where id = %s", (routine_id,)).fetchone() == (False,)


@pytest.mark.communication
def test_a_failing_dossier_reads_as_tarik_says_never_as_the_error(running, db_cabinet, monkeypatch):
    due_routine("tva")
    (row,) = clock.claimed()

    async def start(name, payload):
        raise ConnectionError("postgres://tarik:secret@db/tarik refused")

    monkeypatch.setattr(clock.runs, "start", start)
    report = asyncio.run(clock.ran(db_cabinet, row))
    assert "secret" not in report and report.startswith("Dossier n° 5 : echec technique")


@pytest.fixture
def experio_users(monkeypatch):
    """Experio's users as admins_where reads them: Chef an admin, Démis an admin demoted on one dossier by a
    « Profils par Dossier » row; Jane of Experio's staff; the domains asked."""
    asked = []

    def odoo_call(model, method, domain, **kw):
        asked.append(domain)
        if model == "exp.user.company.profile":
            return [{"user_id": [2, "Démis"]}]
        admins = [{"id": 1, "name": "Chef", "login": "chef@cabinet.ma"}]
        admins.append({"id": 2, "name": "Démis", "login": "demis@cabinet.ma"})
        logins = [d[2].lower() for d in domain if d[0] == "login"]
        return [user for user in admins if not logins or user["login"] in logins]

    monkeypatch.setattr(routines, "odoo_call", odoo_call)
    staff = {"jane@experio.ma": {"employee_type": "internal"}, "junior@cabinet.ma": {"employee_type": "employee"}}
    monkeypatch.setattr(routines, "collaborator", staff.get)
    return asked


@pytest.mark.security
def test_only_a_live_admin_on_every_dossier_or_staff_gets_reports_of_every_dossier(experio_users):
    assert routines.live("chef@cabinet.ma") and routines.live("jane@experio.ma")
    assert not routines.live("demis@cabinet.ma")  # lower on a dossier: Experio shows them less there
    assert not routines.live("junior@cabinet.ma") and not routines.live("parti@cabinet.ma")
    rule = experio_users[0]
    assert ("is_agent", "=", False) in rule  # Experio's technical accounts left out
    assert ("employee_state", "=", "active") in rule
    assert ("exp_profile_id.management_group", "=", "admin") in rule


@pytest.mark.functionality
def test_the_cabinets_admins_offered_are_the_same_rule_among_allowed_addresses(experio_users, monkeypatch):
    monkeypatch.setattr(routines, "allowed_recipients", lambda: ["@cabinet.ma"])
    assert routines.admins() == [{"name": "Chef", "email": "chef@cabinet.ma"}]


@pytest.mark.security
@pytest.mark.parametrize(("session", "kept"), [("routine-3-2026102708", False), ("chat-" + "0" * 32, True)])
def test_a_routines_run_never_confirms_declares_nor_posts(monkeypatch, session, kept):
    from types import SimpleNamespace

    from engine import agent, runs

    handed = []

    async def nested(name, given, cabinet, session_id, node=None):
        handed.append(given["call"])
        return {"message": "fait"}

    monkeypatch.setattr(runs, "nested", nested)
    runtime = SimpleNamespace(state={"messages": []}, config={"configurable": {"session_id": session, "cabinet": "c"}})
    asked = {"client_confirmed": True, "declarer": True, "comptabiliser": "oui"}
    asyncio.run(agent.run_workflow("declarations", runtime, asked))
    (call,) = handed
    said = (call["client_confirmed"], call["declarer"], call["comptabiliser"] == "oui")
    assert said == (kept, kept, kept) and call.get("routine", False) is not kept


@pytest.mark.security
@pytest.mark.parametrize(("workflow", "runs_it"), [("declarations", True), ("export", True), ("dossier", False)])
def test_a_routines_run_starts_only_what_prepares(monkeypatch, workflow, runs_it):
    from types import SimpleNamespace

    from engine import agent, runs

    async def nested(name, given, cabinet, session_id, node=None):
        return {"message": f"{name} fait"}

    monkeypatch.setattr(runs, "nested", nested)
    session = {"session_id": "routine-3-2026102708", "cabinet": "c"}
    runtime = SimpleNamespace(state={"messages": []}, config={"configurable": session})
    answer = asyncio.run(agent.run_workflow(workflow, runtime, {}))
    assert (answer == f"{workflow} fait") is runs_it and (answer == agent.NOT_IN_ROUTINE) is not runs_it


@pytest.mark.security
def test_an_approver_resumes_no_routine_either(chat, monkeypatch):
    make(True)
    with cabinet_db() as db:
        db.execute("update tarik_routines set created_by = 'chef@cabinet.ma', enabled = false")
        (routine_id,) = db.execute("select id from tarik_routines").fetchone()
    monkeypatch.setattr(routines, "sender_reach", lambda session_id: {"id": 7, "company_ids": [], "rank": 2})
    monkeypatch.setattr(routines, "sender_of", lambda session_id: "chef@cabinet.ma")
    assert routines.manage_routine("resume", routine_id=routine_id, session_id="s")["status"] == "stop"
    assert routines.manage_routine("pause", routine_id=routine_id, session_id="s")["status"] == "ok"  # stopping is


@pytest.mark.functionality
def test_a_long_report_goes_as_a_file_and_quiet_dossiers_are_only_counted(running, db_cabinet, monkeypatch):
    routine_id = due_routine("tva")
    (row,) = clock.claimed()
    kept = []
    monkeypatch.setattr(clock, "keep_file", lambda session, name, data: kept.append((name, data)))
    monkeypatch.setattr(clock, "BODY_CHARACTERS", 10)
    monkeypatch.setattr(clock, "opened_dossiers", lambda: [{"id": d, "name": str(d)} for d in (5, 6, 7)])

    async def part(cabinet, kind, dossier_id, session):
        return clock.NOTHING if dossier_id == 7 else f"TVA du dossier {dossier_id} préparée."

    monkeypatch.setattr(clock, "dossier_part", part)
    body = asyncio.run(clock.ran(db_cabinet, row))
    ((name, data),) = kept
    assert name == f"routine-{routine_id}.txt" and data.decode().endswith("1 dossier(s) sans rien à préparer.")
    assert body == "TVA du dos\n\n(Suite dans le fichier joint.)"


@pytest.mark.functionality
def test_a_period_still_running_is_never_claimed_again_behind_it(db_cabinet):
    routine_id = due_routine()
    (row,) = clock.claimed()
    with cabinet_db() as db:  # its next time already came, the first period not reported yet
        db.execute("update tarik_routines set next_run_at = now() - interval '1 minute' where id = %s", (routine_id,))
    assert [r[7] for r in clock.claimed()] == [row[7]]  # the unfinished one only, no second period
