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
    monkeypatch.setattr(routines, "require_dossier", lambda dossier_id: {5: "ALPHA", 6: "BETA"}[dossier_id])
    monkeypatch.setattr(routines, "client_messages", lambda session_id, message: [said["words"]])
    monkeypatch.setattr(routines, "confirmed", lambda session_id, yes, *parts: yes)
    return said


def make(yes, email="chef@cabinet.ma", dossiers=(5, 6)):
    return routines.manage_routine("create", "tva", list(dossiers), "month", 5, 8, 0, email, yes, "", "s")


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
def test_a_report_address_the_client_did_not_write_is_asked(chat):
    asked = make(True, email="autre@ailleurs.ma")
    assert asked["message"] == "À quelle adresse e-mail envoyer ses rapports ?"


@pytest.mark.security
def test_a_manager_makes_and_sees_routines_on_their_own_dossiers_only(chat, monkeypatch):
    make(True)
    reach = {"id": 7, "name": "Chef", "company_ids": [5], "rank": 2}
    monkeypatch.setattr(routines, "sender_reach", lambda session_id: reach)
    monkeypatch.setattr(routines, "sender_of", lambda session_id: "chef@cabinet.ma")
    assert "ne vous sont pas attribués dans Experio : `BETA`" in make(True)["message"]
    assert routines.manage_routine("list")["message"] == "Aucune routine pour vos dossiers."  # its BETA is not theirs
    with cabinet_db() as db:
        (routine_id,) = db.execute("select id from tarik_routines").fetchone()
    assert "Quelle routine" in routines.manage_routine("pause", routine_id=routine_id, session_id="s")["message"]


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
            "values (%s, '{5,6}', 'day', 0, 8, 'chef@cabinet.ma', 'chef@cabinet.ma', %s) returning id",
            (kind, datetime.now(ZONE) - timedelta(hours=hours_ago)),
        ).fetchone()[0]


@pytest.mark.functionality
def test_a_due_routine_is_claimed_once_and_moved_to_its_next_time(db_cabinet):
    routine_id = due_routine()
    due = clock.claimed()
    assert [row[0] for row in due] == [routine_id] and clock.claimed() == []  # never twice
    with cabinet_db() as db:
        (upcoming,) = db.execute("select next_run_at from tarik_routines where id = %s", (routine_id,)).fetchone()
        db.execute("update tarik_routines set next_run_at = %s where id = %s", (due[0][7], routine_id))
    assert upcoming > datetime.now(ZONE) and upcoming.astimezone(ZONE).hour == 8
    assert clock.claimed() == []  # the same period again (a restart): recorded, not run again


@pytest.mark.functionality
def test_a_routines_report_is_sent_to_its_maker_and_kept(db_cabinet, monkeypatch):
    routine_id = due_routine("tva")
    (row,) = clock.claimed()
    calls = []

    async def call(cabinet, tool, **arguments):
        calls.append((tool, arguments))
        return {"status": "ok"}

    async def start(name, payload):
        dossier_id = payload["input"]["call"]["dossier_id"]
        assert name == "declarations" and payload["input"]["call"]["mode"] == "tva" and "declarer" not in payload
        done = asyncio.get_running_loop().create_future()
        done.set_result(({"message": f"TVA du dossier {dossier_id} préparée."}, None))
        return "run", done

    monkeypatch.setattr(clock, "call", call)
    monkeypatch.setattr(clock.runs, "start", start)
    report = asyncio.run(clock.ran(db_cabinet, row))
    assert report == "TVA du dossier 5 préparée.\n\nTVA du dossier 6 préparée."
    (tool, sent) = calls[-1]
    assert tool == "send_email" and sent["to"] == "chef@cabinet.ma" and sent["body"] == report
    assert sent["subject"].startswith(f"Routine n° {routine_id} : préparer la déclaration de TVA")
    with cabinet_db() as db:
        assert db.execute("select report from tarik_routine_runs").fetchone() == (report,)
