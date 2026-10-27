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
    assert next_run("day", 0, 8, at("2026-10-27 07:59")) == at("2026-10-27 08:00")
    assert next_run("day", 0, 8, at("2026-10-27 08:00")) == at("2026-10-28 08:00")  # its own time: the next one
    assert next_run("week", 0, 9, at("2026-10-27 10:00")) == at("2026-11-02 09:00")  # a Tuesday: next Monday
    assert next_run("month", 5, 8, at("2026-10-04 23:00")) == at("2026-10-05 08:00")
    assert next_run("month", 5, 8, at("2026-12-05 08:00")) == at("2027-01-05 08:00")  # past December


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
        "create", kind, "month", 5, hour, email=email, prompt=prompt, client_confirmed=yes, session_id="s"
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
    assert "pas un collaborateur actif" in make(True, email="parti@cabinet.ma")["message"]


@pytest.mark.functionality
def test_a_declaration_routine_at_midnight_is_asked_another_hour(chat):
    assert "de 1 à 23" in make(True, hour=0)["message"]  # midnight in Morocco: the day before on the server


@pytest.mark.security
@pytest.mark.parametrize(("rank", "made"), [(2, False), (3, True)])
def test_only_admins_and_staff_make_a_routine_and_each_manages_their_own(chat, monkeypatch, rank, made):
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
            "insert into tarik_routines (kind, every, day, hour, notify, created_by, next_run_at, prompt) "
            "values (%s, 'day', 0, 8, 'chef@cabinet.ma', 'chat', %s, %s) returning id",
            (kind, datetime.now(ZONE) - timedelta(hours=1), prompt),
        ).fetchone()[0]


@pytest.mark.functionality
def test_a_due_routine_is_claimed_once_and_moved_to_its_next_time(db_cabinet):
    routine_id = due_routine()
    due = clock.claimed()
    assert [row[0] for row in due] == [routine_id]
    assert clock.claimed() == due  # never reported (the server stopped): run again, the same period
    clock.record(routine_id, due[0][6], "fait")
    assert clock.claimed() == []  # reported: done
    with cabinet_db() as db:
        (upcoming,) = db.execute("select next_run_at from tarik_routines where id = %s", (routine_id,)).fetchone()
        db.execute("update tarik_routines set next_run_at = %s where id = %s", (due[0][6], routine_id))
    assert upcoming > datetime.now(ZONE) and upcoming.astimezone(ZONE).hour == 8
    assert clock.claimed() == []  # the same period due again: recorded, never run twice


@pytest.fixture
def running(db_cabinet, monkeypatch):
    """A routine's run on the test cabinet: dossiers 5 and 6 seen by Tarik, its address live; the tools called and
    the runs started."""
    state = {"calls": [], "started": []}
    monkeypatch.setattr(clock, "dossiers", lambda: [{"id": 5, "name": "ALPHA"}, {"id": 6, "name": "BETA"}])
    monkeypatch.setattr(clock, "live", lambda notify: True)

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
    monkeypatch.setattr(clock, "live", lambda notify: False)
    assert "Routine en pause" in asyncio.run(clock.ran(db_cabinet, row)) and running["calls"] == []
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


@pytest.mark.functionality
def test_only_a_live_collaborator_or_staff_gets_reports(monkeypatch):
    users = {
        "chef@cabinet.ma": {"employee_type": "employee", "employee_state": "active", "company_ids": [5]},
        "jane@experio.ma": {"employee_type": "internal", "employee_state": "new", "company_ids": []},
        "invite@cabinet.ma": {"employee_type": "employee", "employee_state": "new", "company_ids": [5]},
    }
    monkeypatch.setattr(routines, "collaborator", users.get)
    assert routines.live("chef@cabinet.ma") and routines.live("jane@experio.ma")
    assert not routines.live("invite@cabinet.ma") and not routines.live("parti@cabinet.ma")


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
