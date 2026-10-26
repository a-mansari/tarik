"""What the processing sequence asks of Experio: drafts it split off, a reading sent again, notes, banks, answers."""

import pytest

from engine.tools.core.cabinets import cabinet_db
from engine.tools.invoices import pipeline


def made_by_tarik(invoice_id):
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, kind, invoice_id) "
            "values ('s', 'f.pdf', 'application/pdf', '', 'invoice', %s)",
            (invoice_id,),
        )


@pytest.mark.functionality
def test_the_drafts_experio_split_off_tarik_draft_are_those_no_step_took(db_cabinet, monkeypatch):
    made_by_tarik(40)
    pipeline.record_step("r", "s", "check_tiers", {"status": "ok", "message": "m", "invoice_id": 42})
    asked = []
    monkeypatch.setattr(pipeline, "require_invoice", lambda invoice_id, fields: {"token": "job-1"})
    monkeypatch.setattr(pipeline, "odoo_call", lambda model, method, domain, order: asked.append(domain) or [41, 42])
    assert pipeline.split_off(40) == [41]
    assert asked == [[("token", "=", "job-1"), ("id", "!=", 40), ("state", "in", ["pending", "draft"])]]


@pytest.mark.functionality
def test_a_draft_tarik_did_not_make_or_without_reading_job_has_no_split_off(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "odoo_call", lambda *a, **k: pytest.fail("nothing to search"))
    monkeypatch.setattr(pipeline, "require_invoice", lambda invoice_id, fields: {"token": False})
    assert pipeline.split_off(41) == []  # one Experio split off: never reports the others again
    made_by_tarik(40)
    assert pipeline.split_off(40) == []


@pytest.mark.communication
def test_a_draft_read_with_others_from_its_document_says_them(monkeypatch):
    monkeypatch.setattr(pipeline, "require_invoice", lambda invoice_id, fields: {"bluejay_state": "done"})
    monkeypatch.setattr(pipeline, "split_off", lambda invoice_id: [41, 42])
    monkeypatch.setattr(pipeline, "READ_DEADLINES", {"old run": 0.0})
    result = pipeline.wait_until_read(40, "r")
    assert result["status"] == "ok" and result["split_off"] == [41, 42] and "n° 41, n° 42" in result["message"]
    assert "old run" not in pipeline.READ_DEADLINES  # a run gone over an hour is forgotten


def stuck(monkeypatch, test_mode, refusal=None):
    old = (pipeline.datetime.now(pipeline.UTC) - pipeline.timedelta(hours=2)).replace(tzinfo=None).isoformat()
    draft = {"state": "pending", "company_id": [5, "D"], "create_date": old, "write_date": old}
    calls = []

    def odoo_json(model, method, ids, context):
        calls.append(method)
        if refusal:
            raise refusal

    monkeypatch.setattr(pipeline, "require_invoice", lambda invoice_id, fields: draft)
    monkeypatch.setattr(pipeline, "dry_run", lambda: test_mode)
    monkeypatch.setattr(pipeline, "odoo_json", odoo_json)
    return calls


@pytest.mark.communication
def test_a_resend_experio_refuses_tells_the_cabinet_can_do_it(monkeypatch):
    calls = stuck(monkeypatch, False, PermissionError("access denied"))
    assert pipeline.resend_if_stuck(4) == " depuis plus d'une heure (le cabinet peut la relancer dans Experio)"
    assert calls == ["action_resend"]


@pytest.mark.security
def test_test_mode_sends_no_reading_again(monkeypatch):
    calls = stuck(monkeypatch, True)
    assert pipeline.resend_if_stuck(4) == "" and calls == []


@pytest.mark.functionality
def test_a_bank_journal_question_is_dropped_once_the_journal_has_its_bank(monkeypatch):
    names = {3: "BQ ATLAS", 4: None}

    def odoo_call(model, method, domain, fields):
        assert ("bank_id", "=", False) in domain
        found = names[domain[0][2]]
        return [{"name": found}] if found else []

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    items = pipeline.bank_questions([(857, 3, "Quelle banque ?", None), (857, 4, "Quelle banque ?", None)])
    assert [(item["journal_id"], item["label"]) for item in items] == [(3, "journal BQ ATLAS")]


@pytest.mark.communication
def test_a_note_experio_refuses_never_stops_the_others_nor_the_summary(db_cabinet, monkeypatch):
    noted = []

    def odoo_json(model, method, ids, **kw):
        if ids == [7]:
            raise PermissionError("no access")
        noted.append(ids)

    monkeypatch.setattr(pipeline, "dry_run", lambda: False)
    monkeypatch.setattr(pipeline, "odoo_json", odoo_json)
    for invoice_id in (7, 9):
        message = f"Facture {invoice_id} : comptabilisée."
        pipeline.record_step("n", "s", "post_invoice", {"status": "ok", "message": message, "invoice_id": invoice_id})
    pipeline.record_step("n", "s", "identify_document", {"status": "ok", "message": "`f.pdf` : facture", "file_id": 3})
    assert pipeline.run_summary("n")["status"] == "ok" and noted == [[9]]  # a file's outcome is no invoice's note


@pytest.mark.security
def test_a_run_in_test_mode_writes_no_note(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "dry_run", lambda: True)
    monkeypatch.setattr(pipeline, "odoo_json", lambda *a, **k: pytest.fail("test mode noted"))
    pipeline.record_step("t", "s", "post_invoice", {"status": "ok", "message": "Facture 7 : ok.", "invoice_id": 7})
    pipeline.run_summary("t")


@pytest.mark.functionality
def test_a_run_without_outcome_says_nothing_was_done(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "odoo_json", lambda *a, **k: pytest.fail("nothing to note"))
    assert pipeline.run_summary("empty")["message"] == pipeline.NOTHING


@pytest.fixture
def imports(monkeypatch):
    calls = []

    def import_bank_statement(file_id, dossier_id, journal_id, network=""):
        calls.append((journal_id, network))
        return {"status": "ok", "message": "importé"}

    monkeypatch.setattr(pipeline, "import_bank_statement", import_bank_statement)
    return calls


def network_item():
    asked = pipeline.tpe.network_question("tpe.pdf")
    choices = {str(k): v for k, v in asked["choices"].items()}
    return {
        "file_id": 3,
        "dossier_id": 857,
        "file_name": "tpe.pdf",
        "choices": choices,
        "step": "import_bank_statement",
    }


@pytest.mark.functionality
def test_the_network_the_client_chose_imports_the_tpe_statement(imports):
    result = pipeline.apply_answer(network_item(), "NAPS", "2")
    assert result["step"] == "import_bank_statement" and imports == [(0, "naps")]


@pytest.mark.functionality
def test_a_network_not_understood_is_asked_again(imports):
    pipeline.apply_answer(network_item(), "je ne sais pas", "")
    assert imports == [(0, "")]


@pytest.mark.functionality
@pytest.mark.parametrize(("status", "resumes"), [("ok", True), ("question", False)])
def test_a_tpe_statement_client_answer_resumes_its_pointing_once_given(monkeypatch, status, resumes):
    monkeypatch.setattr(pipeline.tpe, "answer_client", lambda file_id, answer, decision: {"status": status})
    item = {"file_id": 3, "dossier_id": 857, "step": "settle_statement"}
    result = pipeline.apply_answer(item, "clients divers", "clients divers")
    assert result["step"] == "settle_statement" and result["file_id"] == 3
    assert (result.get("next_step") == "settle_statement") == resumes
