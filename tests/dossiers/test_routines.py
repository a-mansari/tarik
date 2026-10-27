"""Routines: their schedule, made and managed only on a manager's own dossiers and yes, each period run once, and
their report sent."""

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
    """The chat (no sender to limit) on dossiers 5 and 6; the conversation's words."""
    said = {"words": "chaque mois le 5 a 8h, rapport a chef@cabinet.ma"}
    monkeypatch.setattr(routines, "sender_reach", lambda session_id: None)
    monkeypatch.setattr(routines, "sender_of", lambda session_id: None)
    names = {5: "ALPHA", 6: "BETA"}
    monkeypatch.setattr(routines, "dossier_names", lambda ids: {d: names[d] for d in ids if d in names})
    monkeypatch.setattr(routines, "recipient_allowed", lambda address: address.endswith("@cabinet.ma"))
    monkeypatch.setattr(routines, "client_messages", lambda session_id, message: [said["words"]])
    monkeypatch.setattr(routines, "confirmed", lambda session_id, yes, *parts: yes)
    return said


def make(yes, email="chef@cabinet.ma", dossiers=(5, 6), hour=8, kind="tva", prompt=""):
    schedule = ("create", kind, list(dossiers), "month", 5, hour)
    return routines.manage_routine(*schedule, email=email, prompt=prompt, client_confirmed=yes, session_id="s")


@pytest.mark.security
def test_a_routine_is_read_back_then_made_on_the_clients_yes(chat):
    asked = make(False)
    assert asked["status"] == "question" and "Vous confirmez" in asked["message"]
    assert "préparer la déclaration de TVA de la dernière période, chaque mois, le 5 à 8 h" in asked["message"]
    assert "dossier(s) `ALPHA`, `BETA`, rapport à `chef@cabinet.ma`" in asked["message"]
    with cabinet_db() as db:
        assert db.execute("select count(*) from tarik_routines").fetchone() == (0,)
    done = make(True)
    assert done["status"] == "ok" and done["message"].startswith("Routine n° ")
    listed = routines.manage_routine("list")
    assert "1 routine(s)" in listed["message"] and "prochaine le" in listed["message"]


@pytest.mark.security
def test_a_report_address_the_client_did_not_write_or_the_cabinet_does_not_allow_is_asked(chat):
    asked = make(True, email="autre@ailleurs.ma")
    assert asked["message"] == "À quelle adresse e-mail envoyer ses rapports ?"
    chat["words"] += " ou moi@gmail.com"
    assert "pas dans les adresses autorisées" in make(True, email="moi@gmail.com")["message"]


@pytest.mark.functionality
def test_a_declaration_routine_at_midnight_is_asked_another_hour(chat):
    assert "de 1 à 23" in make(True, hour=0)["message"]  # midnight in Morocco: the day before on the server


@pytest.mark.security
def test_a_manager_makes_sees_and_manages_only_their_own_routines_on_their_own_dossiers(chat, monkeypatch):
    make(True)  # made in the chat
    reach = {"id": 7, "name": "Chef", "company_ids": [5, 6], "rank": 2}
    monkeypatch.setattr(routines, "sender_reach", lambda session_id: reach)
    monkeypatch.setattr(routines, "sender_of", lambda session_id: "chef@cabinet.ma")
    assert routines.manage_routine("list", session_id="s")["routine"]  # not theirs: neither seen nor managed
    with cabinet_db() as db:
        (other,) = db.execute("select id from tarik_routines").fetchone()
    assert "Quelle routine" in routines.manage_routine("pause", routine_id=other, session_id="s")["message"]
    assert make(True)["status"] == "ok"  # their own, its reports to them
    assert "1 routine(s)" in routines.manage_routine("list", session_id="s")["message"]
    reach["company_ids"] = [5]  # BETA taken from them: neither made nor seen
    assert "ne vous sont pas attribués dans Experio : `BETA`" in make(True)["message"]
    assert routines.manage_routine("list", session_id="s")["routine"]


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


def due_routine(kind="progress", hours_ago=1):
    """Make a routine due ``hours_ago`` hours ago on dossiers 5 and 6; return its id."""
    with cabinet_db() as db:
        return db.execute(
            "insert into tarik_routines (kind, dossier_ids, every, day, hour, notify, created_by, next_run_at) "
            "values (%s, '{5,6}', 'day', 0, 8, 'chef@cabinet.ma', 'chat', %s) returning id",
            (kind, datetime.now(ZONE) - timedelta(hours=hours_ago)),
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
def test_a_routines_report_is_sent_to_its_maker_and_kept(db_cabinet, monkeypatch):
    routine_id = due_routine("tva")
    (row,) = clock.claimed()
    calls = []

    async def call(cabinet, tool, **arguments):
        calls.append((tool, arguments))
        return {"status": "ok"}

    async def start(name, payload):
        given = payload["input"]["call"]
        dossier_id = given["dossier_id"]
        assert name == "declarations" and given["mode"] == "tva" and given["routine"] is True
        done = asyncio.get_running_loop().create_future()
        done.set_result(({"message": f"TVA du dossier {dossier_id} préparée."}, None))
        return "run", done

    monkeypatch.setattr(clock, "call", call)
    monkeypatch.setattr(clock.runs, "start", start)
    monkeypatch.setattr(clock, "allowed_dossiers", lambda notify, ids: [5])  # 6 taken from them meanwhile
    report = asyncio.run(clock.ran(db_cabinet, row))
    left = "1 dossier(s) laissé(s) de côté : `chef@cabinet.ma` ne les a plus dans Experio."
    assert report == f"TVA du dossier 5 préparée.\n\n{left}"
    (tool, sent) = calls[-1]
    assert tool == "send_email" and sent["to"] == "chef@cabinet.ma" and sent["body"] == report
    assert sent["attach_session"].startswith(f"routine-{routine_id}-")  # the XML the runs made
    assert sent["subject"].startswith(f"Routine n° {routine_id} : préparer la déclaration de TVA")
    with cabinet_db() as db:
        assert db.execute("select report from tarik_routine_runs").fetchone() == (report,)


@pytest.mark.security
def test_a_routine_whose_address_has_no_dossier_left_is_paused_and_sends_nothing(db_cabinet, monkeypatch):
    routine_id = due_routine()
    (row,) = clock.claimed()
    monkeypatch.setattr(clock, "allowed_dossiers", lambda notify, ids: [])

    async def call(cabinet, tool, **arguments):
        raise AssertionError("nothing read nor sent")

    monkeypatch.setattr(clock, "call", call)
    assert "Routine en pause" in asyncio.run(clock.ran(db_cabinet, row))
    with cabinet_db() as db:
        assert db.execute("select enabled from tarik_routines where id = %s", (routine_id,)).fetchone() == (False,)


@pytest.mark.communication
def test_a_failing_dossier_reads_as_tarik_says_never_as_the_error(db_cabinet, monkeypatch):
    due_routine("tva")
    (row,) = clock.claimed()
    monkeypatch.setattr(clock, "allowed_dossiers", lambda notify, ids: ids)

    async def start(name, payload):
        raise ConnectionError("postgres://tarik:secret@db/tarik refused")

    async def call(cabinet, tool, **arguments):
        return {"status": "ok"}

    monkeypatch.setattr(clock.runs, "start", start)
    monkeypatch.setattr(clock, "call", call)
    report = asyncio.run(clock.ran(db_cabinet, row))
    assert "secret" not in report and report.startswith("Dossier n° 5 : echec technique")


@pytest.mark.functionality
def test_the_address_may_still_see_only_the_dossiers_experio_gives_it(monkeypatch):
    people = {
        "chef@cabinet.ma": {"employee_type": "employee", "company_ids": [5]},
        "jane@experio.ma": {"employee_type": "internal", "company_ids": []},
    }
    monkeypatch.setattr(routines, "collaborator", people.get)
    assert routines.allowed_dossiers("chef@cabinet.ma", [5, 6]) == [5]
    assert routines.allowed_dossiers("jane@experio.ma", [5, 6]) == [5, 6]
    assert routines.allowed_dossiers("parti@cabinet.ma", [5, 6]) is None  # gone: nothing, the routine paused


@pytest.mark.functionality
def test_an_instruction_routine_needs_its_words_and_no_dossier(chat):
    assert "quelle consigne" in make(True, dossiers=(), kind="instruction")["message"]
    asked = make(False, dossiers=(), kind="instruction", prompt="Dis-moi les factures en brouillon.")
    assert "suivre sa consigne « Dis-moi les factures en brouillon. »" in asked["message"]
    assert make(True, dossiers=(), kind="instruction", prompt="Dis-moi les factures en brouillon.")["status"] == "ok"
    with cabinet_db() as db:
        assert db.execute("select kind, prompt from tarik_routines").fetchone() == (
            "instruction",
            "Dis-moi les factures en brouillon.",
        )


@pytest.mark.functionality
def test_an_instruction_is_handed_to_the_agent_in_the_routines_own_session(db_cabinet, monkeypatch):
    with cabinet_db() as db:
        routine_id = db.execute(
            "insert into tarik_routines (kind, dossier_ids, every, day, hour, notify, created_by, next_run_at, prompt) "
            "values ('instruction', '{5}', 'day', 0, 8, 'chef@cabinet.ma', 'chat', %s, 'Liste les brouillons.') "
            "returning id",
            (datetime.now(ZONE) - timedelta(hours=1),),
        ).fetchone()[0]
    (row,) = clock.claimed()
    started = []

    async def start(name, payload):
        started.append((name, payload))
        done = asyncio.get_running_loop().create_future()
        done.set_result(({"reply": "3 brouillons."}, None))
        return "run", done

    async def call(cabinet, tool, **arguments):
        return {"status": "ok"}

    monkeypatch.setattr(clock, "allowed_dossiers", lambda notify, ids: ids)
    monkeypatch.setattr(clock, "dossier_names", lambda ids: {5: "ALPHA"})
    monkeypatch.setattr(clock.runs, "start", start)
    monkeypatch.setattr(clock, "call", call)
    assert asyncio.run(clock.ran(db_cabinet, row)) == "3 brouillons."
    ((name, payload),) = started
    assert name == "discussion" and payload["input"]["message"] == "Liste les brouillons.\n\nDossiers : ALPHA"
    assert payload["session_id"].startswith(f"routine-{routine_id}-")


@pytest.mark.security
def test_a_routines_run_has_its_address_as_sender_and_a_routine_gone_none(db_cabinet):
    from engine.tools.dossiers import people

    routine_id = due_routine()
    assert people.sender_of(f"routine-{routine_id}-2026102708") == "chef@cabinet.ma"
    assert people.sender_of("routine-999-2026102708") == ""  # an address nobody has: nothing allowed
    assert people.sender_of("routine-x") == ""


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
