"""The client's answers to what a conversation waits on, on the real Postgres (db_cabinet)."""

import pytest

from tarik.core.app import ok, stop
from tarik.core.cabinets import cabinet_db
from tarik.invoices import exercice, pipeline


@pytest.fixture
def answers(db_cabinet, monkeypatch):
    """Experio's invoices 11 (ATLAS) and 12 (NOVA); every answer applied is recorded, the sender is allowed."""
    applied = []
    monkeypatch.setattr(pipeline, "odoo_call", lambda model, method, ids, **kw: [
        {"id": i, "invoice_number": f"FA-{i}", "partner_id": [i, {11: "ATLAS TRANSPORT - 1234", 12: "NOVA TELECOM"}[i]]} for i in ids])
    monkeypatch.setattr(pipeline, "check_sender", lambda session_id, dossier_id=0, invoice_id=0: ok("ok"))
    record = lambda invoice_id, answer, *asked: applied.append((invoice_id, answer)) or ok("applique")
    monkeypatch.setitem(pipeline.ANSWERS, "assign_account", (record, "settle_tva"))
    monkeypatch.setitem(pipeline.ANSWERS, "settle_tva", (record, "check_journal"))
    return applied


def waits(invoice_id, step, text, session="s"):
    with cabinet_db() as db:
        db.execute("insert into tarik_pending (invoice_id, session_id, step, question) values (%s, %s, %s, %s)",
                   (invoice_id, session, step, text))


@pytest.mark.functionality
def test_the_only_question_gets_the_answer_and_the_sequence_resumes(answers):
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?")
    result = pipeline.answer_pending("s", "le 6125\n\nFichiers joints : ICE 001122334455667.pdf")
    assert answers == [(11, "le 6125")] and result["next_step"] == "settle_tva" and result["invoice_id"] == 11


@pytest.mark.functionality
def test_with_several_questions_the_answer_names_its_invoice(answers):
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?")
    waits(12, "settle_tva", "Facture de NOVA TELECOM : la TVA est-elle non recuperable ? oui / non")
    assert pipeline.answer_pending("s", "oui")["status"] == "question" and answers == []  # which one: asked, nothing applied
    assert pipeline.answer_pending("s", "oui pour Nova Telecom")["invoice_id"] == 12 and answers == [(12, "oui pour Nova Telecom")]


@pytest.mark.security
def test_another_conversations_question_is_never_answered(answers):
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?", session="other")
    assert pipeline.answer_pending("s", "6125")["next_step"] == "waiting_drafts" and answers == []


@pytest.mark.security
def test_a_sender_without_the_dossier_changes_nothing(answers, monkeypatch):
    monkeypatch.setattr(pipeline, "check_sender", lambda session_id, dossier_id=0, invoice_id=0: stop("pas ce dossier"))
    waits(11, "assign_account", "Facture FA-11 : quel compte utiliser ?")
    assert pipeline.answer_pending("s", "6125")["message"] == "pas ce dossier" and answers == []


@pytest.mark.functionality
def test_a_statement_gets_the_bank_account_the_client_names(answers, monkeypatch):
    with cabinet_db() as db:
        file_id = db.execute("insert into tarik_files (session_id, file_name, mime_type, data, kind) values "
                             "('s', 'releve mars.pdf', 'application/pdf', '', 'statement') returning id").fetchone()[0]
        db.execute("insert into tarik_results (run_id, session_id, file_id, dossier_id, step, status, message) values "
                   "('r', 's', %s, 557, 'import_bank_statement', 'question', 'releve mars.pdf : lequel ? BMCE - BMCE BANK (journal_id 4)')",
                   (file_id,))
    monkeypatch.setattr(pipeline, "journal_meant", lambda dossier_id, answer: 4 if "bmce" in answer.lower() else 0)
    monkeypatch.setattr(pipeline, "import_bank_statement", lambda file_id, dossier_id, journal_id: ok(f"{file_id} {dossier_id} {journal_id}"))
    assert pipeline.answer_pending("s", "Le compte BMCE")["message"] == f"{file_id} 557 4"


@pytest.mark.functionality
def test_the_date_the_client_gives_is_the_invoices(monkeypatch):
    written = []
    monkeypatch.setattr(exercice, "require_invoice", lambda invoice_id, fields: {
        "state": "draft", "company_id": [1, "X"], "exercice_id": False, "invoice_date": False, "is_old_invoice": False})
    monkeypatch.setattr(exercice, "odoo_call", lambda *a, **k: [{"id": 5, "name": "Exercice 2026", "date_start": "2026-01-01",
                                                                 "date_end": "2026-12-31"}])
    monkeypatch.setattr(exercice, "fill_invoice", lambda invoice_id, values, context: written.append(values))
    monkeypatch.setattr(exercice, "test_mode_note", lambda: "")
    assert exercice.settle_exercice(7, "")["status"] == "question"
    assert exercice.settle_exercice(7, "du 15/03/2026")["message"].endswith("rattachee a l'exercice Exercice 2026.")
    assert written == [{"invoice_date": "2026-03-15"}, {"exercice_id": 5}]
