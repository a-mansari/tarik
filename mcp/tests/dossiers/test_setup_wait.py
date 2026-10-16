"""A dossier whose onboarding waits on Experio (on the real Postgres, db_cabinet): finished in the run when its
time allows, else left waiting for a follow-up; a refused import never leaves it waiting."""

import pytest

from tarik.core.cabinets import cabinet_db
from tarik.dossiers import setup


@pytest.fixture
def experio(db_cabinet, monkeypatch):
    """Dossier 557 still onboarding, its newest imported line 60 s from being handed over; what was done."""
    done = []
    monkeypatch.setattr(setup, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(setup, "in_dossier", lambda dossier_id: {})
    monkeypatch.setattr(setup, "dry_run", lambda: False)
    monkeypatch.setattr(setup, "exercice_note", lambda dossier_id, dossier: None)
    monkeypatch.setattr(setup, "lines_ready_in", lambda dossier_id, context: 60)
    monkeypatch.setattr(setup.time, "sleep", lambda seconds: done.append(("sleep", seconds)))
    monkeypatch.setattr(setup, "complete_stepper", lambda company, context: done.append("stepper"))

    def odoo_call(model, method, *args, **kw):
        if method == "read":
            return [{"show_onboarding": "stepper" not in done}]
        return 7  # lines handed to Experio

    monkeypatch.setattr(setup, "odoo_call", odoo_call)
    with cabinet_db() as db:
        file_id = db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data) "
            "values ('s', 'fec.txt', 'text/plain', '') returning id"
        ).fetchone()[0]
    setup.setup_waits(557, file_id)
    return done


@pytest.mark.functionality
def test_a_dossier_waits_when_the_run_has_no_time_left_for_experio(experio):
    assert setup.finish_setup(557, seconds_left=40)["status"] == "wait"
    assert experio == [] and setup.waiting_setups("s")["dossiers"] == [557]  # n8n would stop the run at 120 s
    assert setup.finish_setup(557, seconds_left=90)["status"] == "ok"
    assert experio == [("sleep", 60), "stepper"] and setup.waiting_setups("s")["dossiers"] == []


@pytest.mark.functionality
def test_a_follow_up_finishes_it_whatever_the_time(experio):
    assert setup.finish_setup(557)["status"] == "ok" and setup.waiting_setups("s")["dossiers"] == []
    assert setup.finish_setup(557)["message"].endswith("Experio traite les ecritures des leur import.")


@pytest.mark.functionality
def test_a_refused_import_never_leaves_the_dossier_waiting(experio, monkeypatch):
    monkeypatch.setattr(setup, "odoo_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Experio refuse")))
    with cabinet_db() as db:
        broken = db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data) "
            "values ('s', 'fec.csv', 'text/csv', 'JournalCode;Debit\nAC;10') returning id"
        ).fetchone()[0]
    assert setup.import_entries(557, broken)["status"] == "stop"
    assert setup.waiting_setups("s")["dossiers"] == []  # the client sends a corrected file first
