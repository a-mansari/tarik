"""The client's answers to what a conversation waits on, on the real Postgres (db_cabinet)."""

import pytest

from tarik.core.app import ok, stop
from tarik.core.cabinets import cabinet_db
from tarik.invoices import exercice, pipeline


@pytest.fixture
def answers(db_cabinet, monkeypatch):
    """Experio's invoices 11 (ATLAS) and 12 (NOVA); every answer applied is recorded, the sender is allowed."""
    applied = []
    partners = {11: "ATLAS TRANSPORT - 1234", 12: "NOVA TELECOM", 20: "ALPHA", 35: "BETA"}

    def odoo_call(model, method, query, **kw):
        if method == "search":  # the drafts among the ids asked: 99 was deleted in Experio
            return [i for i in query[0][2] if i in partners]
        return [{"id": i, "invoice_number": f"FA-{i}", "partner_id": [i, partners[i]]} for i in query]

    monkeypatch.setattr(pipeline, "odoo_call", odoo_call)
    monkeypatch.setattr(pipeline, "check_sender", lambda session_id, dossier_id=0, invoice_id=0: ok("ok"))

    def record(invoice_id, answer, *asked):
        applied.append((invoice_id, answer))
        return ok("applique")

    monkeypatch.setitem(pipeline.ANSWERS, "assign_account", (record, "settle_tva"))
    monkeypatch.setitem(pipeline.ANSWERS, "settle_tva", (record, "check_journal"))
    return applied


def waits(invoice_id, step, text, session="s"):
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_pending (invoice_id, session_id, step, question) values (%s, %s, %s, %s)",
            (invoice_id, session, step, text),
        )


@pytest.mark.functionality
def test_the_only_question_gets_the_answer_and_the_sequence_resumes(answers):
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?")
    result = pipeline.answer_pending("s", "le 6125\n\nFichiers joints : ICE 001122334455667.pdf")
    assert answers == [(11, "le 6125")] and result["next_step"] == "settle_tva" and result["invoice_id"] == 11


@pytest.mark.functionality
def test_with_several_questions_the_answer_names_its_invoice(answers):
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?")
    waits(12, "settle_tva", "Facture de NOVA TELECOM : la TVA est-elle non recuperable ? oui / non")
    assert (
        pipeline.answer_pending("s", "oui")["status"] == "question" and answers == []
    )  # which one: asked, nothing applied
    assert pipeline.answer_pending("s", "oui pour Nova Telecom")["invoice_id"] == 12 and answers == [
        (12, "oui pour Nova Telecom")
    ]


@pytest.mark.security
def test_another_conversations_question_is_never_answered(answers):
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?", session="other")
    assert pipeline.answer_pending("s", "6125")["next_step"] == "waiting_drafts" and answers == []


@pytest.mark.security
def test_a_sender_without_the_dossier_changes_nothing_and_is_recorded_as_refused(answers, monkeypatch):
    monkeypatch.setattr(pipeline, "check_sender", lambda session_id, dossier_id=0, invoice_id=0: stop("pas ce dossier"))
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?")
    refused = pipeline.answer_pending("s", "6125")
    assert (
        refused["message"] == "pas ce dossier" and refused["step"] == "check_sender" and answers == []
    )  # the manager is told


@pytest.mark.security
def test_a_bare_number_names_no_invoice(answers):
    waits(20, "settle_tva", "Facture FA-20 : la TVA est-elle non recuperable ? oui / non")
    waits(35, "settle_tva", "Facture FA-35 : la TVA est-elle non recuperable ? oui / non")
    assert pipeline.answer_pending("s", "oui, TVA 20 %")["status"] == "question" and answers == []
    assert pipeline.answer_pending("s", "oui pour la facture 35")["invoice_id"] == 35


@pytest.mark.functionality
def test_a_question_on_an_invoice_deleted_since_is_dropped(answers):
    waits(99, "assign_account", "Facture 99 : quel compte utiliser ?")
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?")
    assert pipeline.answer_pending("s", "6125")["invoice_id"] == 11  # the only one left
    with cabinet_db() as db:
        assert db.execute("select invoice_id from tarik_pending").fetchall() == [(11,)]


@pytest.mark.functionality
def test_a_statement_gets_the_bank_account_the_client_names(answers, monkeypatch):
    with cabinet_db() as db:
        file_id = db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, kind) values "
            "('s', 'releve mars.pdf', 'application/pdf', '', 'statement') returning id"
        ).fetchone()[0]
        db.execute(
            "insert into tarik_results (run_id, session_id, file_id, dossier_id, step, status, message) values "
            "('r', 's', %s, 557, 'import_bank_statement', 'question', 'releve mars.pdf : lequel ? BMCE - BMCE "
            "BANK (journal_id 4)')",
            (file_id,),
        )
    monkeypatch.setattr(pipeline, "journal_meant", lambda dossier_id, answer: 4 if "bmce" in answer.lower() else 0)
    monkeypatch.setattr(
        pipeline,
        "import_bank_statement",
        lambda file_id, dossier_id, journal_id: ok(f"{file_id} {dossier_id} {journal_id}"),
    )
    assert pipeline.answer_pending("s", "Le compte BMCE")["message"] == f"{file_id} 557 4"
    pipeline.record_step(
        "r2",
        "s",
        "Reprendre ou ?",
        {"status": "ok", "message": "MODE TEST : importe", "file_id": file_id, "step": "import_bank_statement"},
    )  # test mode: no statement saved
    assert pipeline.waiting_on("s") == []  # answered: it waits no more


@pytest.mark.functionality
def test_the_date_the_client_gives_is_the_invoices(monkeypatch):
    written = []
    monkeypatch.setattr(
        exercice,
        "require_invoice",
        lambda invoice_id, fields: {
            "state": "draft",
            "company_id": [1, "X"],
            "exercice_id": False,
            "invoice_date": False,
            "is_old_invoice": False,
        },
    )
    monkeypatch.setattr(
        exercice,
        "odoo_call",
        lambda *a, **k: [{"id": 5, "name": "Exercice 2026", "date_start": "2026-01-01", "date_end": "2026-12-31"}],
    )
    monkeypatch.setattr(exercice, "fill_invoice", lambda invoice_id, values, context: written.append(values))
    monkeypatch.setattr(exercice, "test_mode_note", lambda: "")
    assert exercice.settle_exercice(7, "")["status"] == "question"
    assert exercice.settle_exercice(7, "du 15/03/2026")["message"].endswith("rattachee a l'exercice Exercice 2026.")
    assert written == [{"invoice_date": "2026-03-15"}, {"exercice_id": 5}]


@pytest.mark.functionality
def test_a_draft_that_failed_waited_or_was_cut_short_is_taken_up_again(db_cabinet, monkeypatch):
    monkeypatch.setattr(pipeline, "odoo_call", lambda model, method, domain, **kw: domain[0][2])  # all still drafts
    for run, invoice_id, step, status in [
        ("r", 1, "check_invoice", "error"),
        ("r", 1, "wait_until_read", "ok"),
        ("r", 2, "wait_until_read", "wait"),
        ("r", 3, "settle_tva", "question"),
        ("r", 4, "check_invoice", "error"),
        ("r2", 4, "post_invoice", "ok"),
    ]:
        pipeline.record_step(run, "s", f"x ({step})", {"status": status, "message": "m", "invoice_id": invoice_id})
    with cabinet_db() as db:  # 5: its run was stopped before recording anything; 4's file is not resumed twice
        for invoice_id in (4, 5):
            db.execute(
                "insert into tarik_files (session_id, file_name, mime_type, data, invoice_id) "
                "values ('s', 'f.pdf', 'application/pdf', '', %s)",
                (invoice_id,),
            )
    assert pipeline.waiting_drafts("s")["invoices"] == [1, 2, 5]  # 3 waits on the client, 4 went through since


@pytest.mark.functionality
def test_a_run_waits_on_experio_reading_once_for_all_its_drafts(monkeypatch):
    clock, states = [0.0], {1: "pending", 2: "pending", 3: "done"}
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(pipeline, "require_invoice", lambda invoice_id, fields: {"bluejay_state": states[invoice_id]})
    monkeypatch.setattr(pipeline, "READ_DEADLINES", {})
    assert pipeline.wait_until_read(1, "run")["status"] == "wait"
    waited = clock[0]
    assert pipeline.READ_WAIT <= waited < pipeline.READ_WAIT + 15  # n8n stops the run at 300s
    assert pipeline.wait_until_read(2, "run")["status"] == "wait" and clock[0] == waited  # the deadline is passed
    assert pipeline.wait_until_read(3, "run")["status"] == "ok"
    assert pipeline.wait_until_read(2, "next run")["status"] == "wait" and clock[0] > waited  # its own deadline
