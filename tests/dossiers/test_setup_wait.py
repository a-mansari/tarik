"""A dossier whose onboarding waits on Experio (on the real Postgres, db_cabinet): finished in the run when its
time allows, else left waiting for a follow-up; a refused import never leaves it waiting."""

from datetime import UTC, date, datetime, timedelta

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import TEST_MODE
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


@pytest.fixture
def stepper(monkeypatch):
    """Dossier 557 still onboarding, its 40 lines imported, its exercice open; Experio's calls."""
    calls = []
    monkeypatch.setattr(setup, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(setup, "in_dossier", lambda dossier_id: {"allowed_company_ids": [557]})
    monkeypatch.setattr(setup, "dry_run", lambda: False)
    monkeypatch.setattr(setup, "lines_ready_in", lambda dossier_id, context: 0)
    monkeypatch.setattr(setup.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(setup, "misplaced_journals", lambda dossier_id, fields: {})
    monkeypatch.setattr(setup, "open_exercice", lambda dossier_id, year: calls.append(("open_exercice", year)))

    def odoo_call(model, method, *args, **kw):
        if method == "read":
            company = {"show_onboarding": True, **dict.fromkeys(setup.STEPPER_JOURNALS, False)}
            return [{**company, **dict.fromkeys(setup.STEPPER_PREFIXES, False)}]
        return {"experio.history": 40, "exp.fiscal.entries": 1}[model]

    monkeypatch.setattr(setup, "odoo_call", odoo_call)
    monkeypatch.setattr(setup, "odoo_json", lambda model, method, *args, **kw: calls.append((method, args, kw)))
    return calls


@pytest.mark.functionality
def test_the_stepper_is_walked_to_its_end_with_the_dossiers_journals_and_prefixes_saved_first(stepper):
    company = {**dict.fromkeys(setup.STEPPER_JOURNALS, False), **dict.fromkeys(setup.STEPPER_PREFIXES, False)}
    company.update(local_purchase_journal_id=[4, "ACHATS"], third_party_supplier_account="4411000")
    setup.complete_stepper(company, {"allowed_company_ids": [557]})
    phases = [args[0] for method, args, kw in stepper if method == "set_configuration_stepper_state"]
    assert len(phases) == 8 and phases[0]["current_phase"] == 2 and phases[0]["completed_phases"] == [1]
    assert phases[-1] == {
        "current_phase": 8,
        "completed_phases": list(range(1, 9)),
        "visited_phases": list(range(1, 9)),
    }
    (method, (config,), kw), last = stepper[0], stepper[9]  # saved before the 8th phase hands the lines over
    assert method == "save_validation_config" and kw == {"context": {"allowed_company_ids": [557]}}
    assert config["local_purchase_journal_id"] == 4 and config["local_sale_journal_id"] is False
    assert config["third_party_supplier_account"] == "4411000" and config["third_party_customer_account"] == "3421"
    assert last[0] == "complete_onboarding" and len(stepper) == 10


@pytest.mark.functionality
def test_a_finished_setup_says_how_many_lines_went_to_experio(stepper):
    answer = setup.finish_setup(557)
    assert answer["message"] == (
        "Dossier `Gecafisc` : integration terminee (40 ligne(s) d'ecritures transmises au traitement d'Experio)."
    )
    assert stepper[-1][0] == "complete_onboarding"


@pytest.mark.communication
def test_experio_refusing_to_finish_the_onboarding_gives_its_reason(stepper, monkeypatch):
    monkeypatch.setattr(setup, "odoo_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("journal requis")))
    with pytest.raises(ToolError, match=r"`Gecafisc` : Experio refuse de terminer l'integration \(journal requis\)"):
        setup.finish_setup(557)


@pytest.mark.security
def test_in_test_mode_the_onboarding_is_only_said_finished(stepper, monkeypatch):
    monkeypatch.setattr(setup, "dry_run", lambda: True)
    monkeypatch.setattr(setup, "exercice_note", lambda dossier_id, dossier: "un exercice 2026 serait ouvert")
    answer = setup.finish_setup(557)
    assert answer["message"] == (
        f"{TEST_MODE}Dossier `Gecafisc` : integration terminee (un exercice 2026 serait ouvert, stepper termine)."
    )
    assert stepper == []


@pytest.mark.functionality
def test_the_current_exercice_is_opened_only_when_the_dossier_has_none(stepper, monkeypatch):
    year = date.today().year
    assert setup.exercice_note(557, "Gecafisc") is None and stepper == []
    monkeypatch.setattr(setup, "odoo_call", lambda model, method, domain: 0)
    assert setup.exercice_note(557, "Gecafisc") == f"exercice {year} ouvert" and stepper == [("open_exercice", year)]
    monkeypatch.setattr(setup, "dry_run", lambda: True)
    assert setup.exercice_note(557, "Gecafisc") == f"un exercice {year} serait ouvert" and len(stepper) == 1


@pytest.mark.communication
def test_experio_refusing_the_exercice_gives_its_reason(stepper, monkeypatch):
    monkeypatch.setattr(setup, "odoo_call", lambda model, method, domain: 0)
    monkeypatch.setattr(setup, "open_exercice", lambda *a: (_ for _ in ()).throw(RuntimeError("dates en conflit")))
    with pytest.raises(ToolError, match=r"Experio refuse d'ouvrir l'exercice \d+ \(dates en conflit\)"):
        setup.exercice_note(557, "Gecafisc")


@pytest.mark.functionality
@pytest.mark.parametrize("ago, ready", [(None, 0.0), (False, 0.0), (10, 55), (3600, 0.0)])
def test_the_lines_are_ready_a_minute_after_the_newest_was_imported(monkeypatch, ago, ready):
    sent = ago and (datetime.now(UTC) - timedelta(seconds=ago)).strftime("%Y-%m-%d %H:%M:%S")  # Experio stores UTC
    newest = [] if ago is None else [{"send_date": sent}]
    asked = []
    monkeypatch.setattr(setup, "odoo_call", lambda model, method, domain, **kw: asked.append(domain) or newest)
    assert setup.lines_ready_in(557, {}) == pytest.approx(ready, abs=2)
    assert asked == [[("company_id", "=", 557), ("state", "=", "new")]]


@pytest.mark.functionality
def test_the_setup_facts_are_read_in_the_dossier(monkeypatch):
    seen = []

    def odoo_call(model, method, domain, *args, **kw):
        seen.append(kw.get("context"))
        if method == "read":
            return [{"show_onboarding": False}]
        if method == "read_group":
            return [{"state": "done", "state_count": 9}] if args[0] == ["state"] else [{"error_type": "x"}]
        if method == "search_read":
            return [{"name": "2025", "state": "open", "selected": True}]
        return {"exp.account": 50, "exp.journal": 6, "exp.tax": 12, "exp.partner": 3}[model]

    monkeypatch.setattr(setup, "odoo_call", odoo_call)
    company, states, reasons, exercices, counts = setup.setup_facts(557, {"allowed_company_ids": [557]})
    assert company == {"show_onboarding": False} and states == {"done": 9} and reasons == [{"error_type": "x"}]
    assert exercices[0]["name"] == "2025" and counts == {
        "exp.account": 50,
        "exp.journal": 6,
        "exp.tax": 12,
        "exp.partner": 3,
    }
    assert seen[1:] == [{"allowed_company_ids": [557]}] * (len(seen) - 1)  # all but the company itself


@pytest.mark.communication
def test_experios_error_reasons_are_merged_and_the_commonest_told():
    reasons = [
        {"error_type": "3 comptes tiers introuvables", "error_type_count": 3},
        {"error_type": "4 comptes tiers introuvables", "error_type_count": 4},
        {"error_type": None, "error_type_count": 1},
        {"error_type": "Journal inconnu", "error_type_count": 2},
        {"error_type": "Date hors exercice", "error_type_count": 1},
    ]
    assert setup.error_reasons(reasons) == (
        "comptes tiers introuvables (7 lignes) | Journal inconnu (2 lignes) | sans motif (1 lignes)"
    )


@pytest.mark.communication
def test_the_state_tells_experios_errors_the_missing_settings_and_another_dossiers_journals(monkeypatch):
    company = {
        "show_onboarding": True,
        **dict.fromkeys(setup.POSTING_SETTINGS, [1, "x"]),
        "local_sale_journal_id": False,
    }
    facts = (
        company,
        {"done": 5, "error": 2},
        [{"error_type": "Compte inconnu", "error_type_count": 2}],
        [{"name": "2024", "state": "closed", "selected": False}, {"name": "2025", "state": "open", "selected": True}],
        dict.fromkeys(("exp.account", "exp.journal", "exp.tax", "exp.partner"), 4),
    )
    monkeypatch.setattr(setup, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(setup, "in_dossier", lambda dossier_id: {})
    monkeypatch.setattr(setup, "resend_stale", lambda dossier_id, context: 0)
    monkeypatch.setattr(setup, "processed", lambda dossier_id, context: None)
    monkeypatch.setattr(setup, "setup_facts", lambda dossier_id, context: facts)
    monkeypatch.setattr(setup, "misplaced_journals", lambda dossier_id, fields: {"local_purchase_journal_id": "ACH"})
    assert setup.setup_status(557)["message"] == (
        "Dossier `Gecafisc` : 4 comptes, 4 journaux, 4 taxes, 4 tiers ; "
        "ecritures importees : 5 traitees, 2 en erreur ; "
        "erreurs signalees par Experio, a corriger dans le fichier puis a renvoyer : Compte inconnu (2 lignes) ; "
        "exercices ouverts : `2025` (en cours) ; a parametrer avant de comptabiliser : journal de ventes ; "
        "reglages sur les journaux d'un autre dossier (a creer ou importer) : journal d'achats ; "
        "integration pas encore terminee."
    )


@pytest.mark.communication
def test_experio_not_giving_the_state_is_said(monkeypatch):
    monkeypatch.setattr(setup, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(setup, "in_dossier", lambda dossier_id: {})
    monkeypatch.setattr(setup, "odoo_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout")))
    with pytest.raises(ToolError, match="`Gecafisc` : Experio n'a pas pu donner l'etat du parametrage"):
        setup.setup_status(557)


@pytest.mark.security
def test_stuck_lines_are_not_sent_again_in_test_mode_nor_when_none_is_stuck(monkeypatch):
    stuck = [[11]]
    monkeypatch.setattr(
        setup,
        "odoo_call",
        lambda model, method, *a, **k: [{"show_onboarding": False}] if method == "read" else stuck[0],
    )
    monkeypatch.setattr(setup, "odoo_json", lambda *a, **k: pytest.fail("sent again"))
    monkeypatch.setattr(setup, "dry_run", lambda: True)
    assert setup.resend_stale(557, {}) == 0
    stuck[0] = []
    monkeypatch.setattr(setup, "dry_run", lambda: False)
    assert setup.resend_stale(557, {}) == 0
