"""A dossier whose onboarding waits on Experio (on the real Postgres, db_cabinet): finished in the run when its
time allows, else left waiting for a follow-up; a refused import never leaves it waiting."""

import pytest

from engine.tools.core.cabinets import cabinet_db
from engine.tools.dossiers import setup


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
    monkeypatch.setattr(setup, "misplaced_journals", lambda dossier_id, fields: {})
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
    assert experio == [] and setup.waiting_setups("s")["dossiers"] == [557]  # left waiting for a later run
    assert setup.finish_setup(557, seconds_left=90)["status"] == "ok"
    assert experio == [("sleep", 60), "stepper"]
    assert setup.waiting_setups("s")["dossiers"] == [557]  # until its lines are processed (setup_status)


@pytest.mark.functionality
def test_a_follow_up_finishes_it_whatever_the_time(experio):
    assert setup.finish_setup(557)["status"] == "ok"
    again = setup.finish_setup(557)
    assert again["message"].endswith("Experio traite les ecritures des leur import.") and again.get("routine")


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


@pytest.mark.communication
def test_a_setup_ends_telling_the_journals_still_to_create(experio, monkeypatch):
    monkeypatch.setattr(setup, "misplaced_journals", lambda dossier_id, fields: {"local_purchase_journal_id": "ACHAT"})
    message = setup.finish_setup(557, seconds_left=90)["message"]
    assert "journal d'achats sont ceux d'un autre dossier" in message


@pytest.mark.functionality
def test_the_models_advance_accounts_are_made_reconcilable_before_its_copy(monkeypatch):
    """Experio refuses to copy an advance operation whose account is not reconcilable, and creates them so."""
    calls = []

    def odoo_call(model, method, *args, **kw):
        calls.append((model, method, args))
        found = {"search_read": [{"account_id": [7, "4421"]}, {"account_id": False}], "search": [7], "write": True}
        return found[method]

    monkeypatch.setattr(setup, "odoo_call", odoo_call)
    assert setup.make_advance_accounts_reconcilable(890) == 1
    assert calls[-1] == ("exp.account", "write", ([7], {"reconcile": True}))


@pytest.mark.functionality
def test_the_state_waits_for_experio_to_process_the_lines(monkeypatch):
    pending = [2, 1, 0]  # lines still processing at each look
    slept = []
    monkeypatch.setattr(setup, "odoo_call", lambda *a, **k: pending.pop(0))
    monkeypatch.setattr(setup.time, "sleep", slept.append)
    setup.processed(557, {})
    assert pending == [] and slept == [10, 10]


def statused(monkeypatch, states, onboarding=False, stale=()):
    """setup_status on dossier 557 with its lines in ``states`` ({state: count}) and ``stale`` lines stuck: what
    was sent again, and its answer."""
    resent = []
    monkeypatch.setattr(setup, "processed", lambda dossier_id, context: None)
    monkeypatch.setattr(
        setup,
        "setup_facts",
        lambda dossier_id, context: (
            {"show_onboarding": onboarding, **dict.fromkeys(setup.POSTING_SETTINGS, [1, "x"])},
            states,
            [],
            [],
            dict.fromkeys(("exp.account", "exp.journal", "exp.tax", "exp.partner"), 1),
        ),
    )

    def odoo_call(model, method, *args, **kw):
        if method == "read":
            return [{"show_onboarding": onboarding}]
        return list(stale) if method == "search" else 0

    monkeypatch.setattr(setup, "odoo_call", odoo_call)
    monkeypatch.setattr(setup, "odoo_json", lambda model, method, ids, **kw: resent.append((method, ids)))
    return resent, setup.setup_status(557)


@pytest.mark.functionality
def test_a_dossier_waits_until_experio_processed_its_lines(experio, monkeypatch):
    _, answer = statused(monkeypatch, {"done": 90, "pending": 10})
    assert "10 ligne(s) encore en traitement" in answer["message"] and setup.waiting_setups("s")["dossiers"] == [557]
    statused(monkeypatch, {"done": 100})
    assert setup.waiting_setups("s")["dossiers"] == []  # every line processed: the follow-up has said it all


@pytest.mark.functionality
def test_lines_stuck_after_the_onboarding_are_sent_again(experio, monkeypatch):
    resent, answer = statused(monkeypatch, {"new": 3}, stale=[11, 12, 13])
    assert resent == [("action_resend", [11, 12, 13])]  # Experio's own « Re-send »
    assert "3 ligne(s) bloquee(s) renvoyee(s)" in answer["message"]
    resent, _ = statused(monkeypatch, {"new": 3}, onboarding=True, stale=[11])
    assert resent == []  # still onboarding: its lines wait on purpose


@pytest.mark.functionality
def test_the_same_entries_file_sent_again_is_not_imported_twice(experio, monkeypatch):
    from engine.tools.invoices.pipeline import record_step

    with cabinet_db() as db:
        first, second = (
            db.execute(
                "insert into tarik_files (session_id, file_name, mime_type, data) "
                f"values ('s', '{name}', 'text/csv', 'JournalCode;Debit\nAC;10') returning id"
            ).fetchone()[0]
            for name in ("fec.csv", "fec (1).csv")
        )
    record_step(
        "r", "s", "import_entries", {"status": "ok", "message": "FEC importe", "file_id": first, "dossier_id": 557}
    )
    monkeypatch.setattr(setup, "odoo_json", lambda *a, **k: pytest.fail("imported a second time"))
    answer = setup.import_entries(557, second)
    assert answer["status"] == "stop" and "deja importe" in answer["message"] and "`fec.csv`" in answer["message"]


@pytest.mark.communication
def test_what_experios_detection_created_is_said():
    before = {"exp.account": 10, "exp.journal": 2, "exp.partner": 0, "exp.fiscal.entries": 1}
    after = {"exp.account": 52, "exp.journal": 6, "exp.partner": 14, "exp.fiscal.entries": 1}
    assert setup.created_note(before, after) == " Experio a cree : 42 compte(s), 4 journal(aux), 14 tiers."
    assert setup.created_note(before, before) == " Experio n'a rien eu a creer."
