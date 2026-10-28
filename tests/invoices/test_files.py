"""Which files of a conversation are still to process, on the real Postgres (db_cabinet)."""

import pytest

from engine.tools.core.cabinets import cabinet_db
from engine.tools.dossiers import setup
from engine.tools.invoices import intake
from engine.tools.invoices.pipeline import files_to_process


def add(name, kind=None, parent=None, invoice_id=None, statement_id=None, data=b"%PDF"):
    with cabinet_db() as db:
        return db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, kind, parent_id, invoice_id, "
            "statement_id) "
            "values ('s', %s, 'application/pdf', %s, %s, %s, %s, %s) returning id",
            (name, data, kind, parent, invoice_id, statement_id),
        ).fetchone()[0]


@pytest.mark.functionality
def test_whatever_has_no_draft_or_statement_yet_is_picked_again(db_cabinet):
    new = add("new.pdf")
    uncut = add("uncut.pdf", "invoice")  # its side wasn't given yet
    cut = add("cut.pdf", "invoice")
    done_part = add("cut - page 1.pdf", "invoice", parent=cut, invoice_id=11)
    failed_part = add("cut - page 2.pdf", "invoice", parent=cut)  # its draft failed
    waiting_statement = add("releve.pdf", "statement")  # waiting for its bank account
    add("imported.pdf", "statement", statement_id=5)
    add("drafted.pdf", "invoice", invoice_id=12)
    assert files_to_process("s")["files"] == [new, uncut, failed_part, waiting_statement]
    assert done_part not in files_to_process("s")["files"]


@pytest.mark.functionality
def test_an_invoice_cut_out_of_a_pdf_is_never_cut_again(db_cabinet):
    part = add("cut - pages 1-2.pdf", "invoice", parent=add("cut.pdf", "invoice"))
    assert intake.split_invoices(part, "achat", "Voici les factures d'achat.")["files"] == [part]


@pytest.mark.functionality
def test_an_entries_file_imported_or_refused_is_not_taken_again(db_cabinet):
    refused, corrected = add("fec.csv"), add("fec corrige.csv")
    assert files_to_process("s", entries=True)["files"] == [refused, corrected]
    setup.taken(refused)
    assert files_to_process("s", entries=True)["files"] == [corrected]


@pytest.mark.security
def test_a_draft_another_run_made_is_left_to_it(db_cabinet, monkeypatch):
    monkeypatch.setattr(intake, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(intake, "odoo_call", lambda *a, **k: pytest.fail("a second draft"))
    answer = intake.create_draft_invoice(add("f.pdf", "invoice", invoice_id=42), 557)
    assert answer["status"] == "stop" and answer["invoice_id"] == 42


@pytest.mark.functionality
def test_a_request_to_post_is_kept_with_the_draft_so_a_question_on_the_way_does_not_lose_it(db_cabinet, monkeypatch):
    from engine.tools.invoices import pipeline

    monkeypatch.setattr(intake, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(intake, "dry_run", lambda: False)
    monkeypatch.setattr(intake, "as_pdf", lambda mime, data: data)
    monkeypatch.setattr(intake, "import_pdf", lambda *a, **k: 77)
    file_id = add("f.pdf", "invoice")
    assert intake.create_draft_invoice(file_id, 557, "achat", "oui")["invoice_id"] == 77
    # the run stopped at a TVA question; the run resumed by the answer carries no request, yet the draft is released
    assert pipeline.hold_posting(77, "s")["status"] == "ok"


@pytest.mark.functionality
def test_an_entries_file_experio_cannot_read_is_not_taken_again(db_cabinet, monkeypatch):
    monkeypatch.setattr(setup, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(setup, "odoo_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Experio refuse")))
    broken = add("fec.csv", data=b"JournalCode;Debit\nAC;10")
    assert setup.import_entries(557, broken)["status"] == "stop"
    assert files_to_process("s", entries=True)["files"] == []


@pytest.mark.communication
def test_a_message_asking_for_work_without_a_file_is_told_its_attachment_is_missing(db_cabinet):
    from engine.tools.invoices.pipeline import NO_FILE

    assert files_to_process("s", asked=True)["message"] == NO_FILE
    assert files_to_process("s")["message"] == "0 fichier(s) a traiter."  # a follow-up says nothing


@pytest.mark.technical
def test_a_draft_another_run_is_working_on_is_never_taken_up_by_a_second_one(db_cabinet, monkeypatch):
    from engine.tools.invoices import pipeline

    monkeypatch.setattr(pipeline, "odoo_call", lambda model, method, domain: domain[0][2])  # every one still a draft
    pipeline.record_step("A", "s", "wait_until_read", {"status": "wait", "message": "lit encore", "invoice_id": 7})
    # Run A (conversation s) is still at it: run B, of another conversation asking to post the dossier's drafts, and
    # a second run of s, both find draft 7 waiting, and leave it.
    assert pipeline.waiting_drafts("s", run_id="B")["invoices"] == []
    pipeline.run_summary("A")  # A is over: 7 is free
    assert pipeline.waiting_drafts("s", run_id="B")["invoices"] == [7]
    assert pipeline.waiting_drafts("s", run_id="C")["invoices"] == []  # now B's


@pytest.mark.communication
@pytest.mark.parametrize(
    "message, told",
    [
        ("Objet : Facture\n\nBonjour, voici la facture de juillet.", True),
        ("Objet : Facture\n\nCi-joint le relevé.", True),
        ("Objet : Re: Facture\n\nOui, comptabilisez-la.", False),  # an answer: no file meant
        ("Objet : Zip\n\nVoici le zip.\n\nFichiers laissés de côté : lot.zip : archive vide", False),  # told already
    ],
)
def test_a_missing_attachment_is_told_only_when_the_client_said_they_attached_one(message, told):
    from engine.tools.core.conversation import mentions_attachment

    assert mentions_attachment(message) is told


@pytest.mark.functionality
def test_a_draft_stopped_as_no_invoice_is_classified_again_once_the_client_gives_its_side(db_cabinet, monkeypatch):
    from engine.tools.core.conversation import remember_client_message
    from engine.tools.invoices import pipeline

    monkeypatch.setattr(pipeline, "odoo_call", lambda model, method, domain: domain[0][2])
    stopped = {"status": "stop", "message": "Facture 9 : ce n'est pas une facture.", "invoice_id": 9}
    pipeline.record_step("A", "s", "classify_invoice", stopped)
    pipeline.run_summary("A")
    assert pipeline.waiting_drafts("s", stopped=True, run_id="B", invoice_type="achat")["invoices"] == []  # not said
    remember_client_message("s", "C'est bien une facture d'achat, des droits de douane.")
    assert pipeline.waiting_drafts("s", stopped=True, run_id="C", invoice_type="achat")["invoices"] == [9]


@pytest.mark.functionality
def test_the_same_invoice_file_sent_again_makes_no_second_draft(db_cabinet, monkeypatch):
    monkeypatch.setattr(intake, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(intake, "dry_run", lambda: False)
    alive = {42}  # the drafts still in Experio, in this dossier, not cancelled
    monkeypatch.setattr(intake, "odoo_call", lambda model, method, domain, **kw: sorted(alive & set(domain[0][2])))
    monkeypatch.setattr(intake, "import_pdf", lambda *a, **k: 43)
    add("facture.pdf", "invoice", invoice_id=42, data=b"%PDF same")
    again = add("facture (1).pdf", "invoice", data=b"%PDF same")
    answer = intake.create_draft_invoice(again, 557)
    assert answer["status"] == "stop" and answer["duplicate"] and "facture n° 42" in answer["message"]
    assert again not in files_to_process("s")["files"]  # never picked again
    alive.clear()  # the first draft was deleted in Experio: the file makes its draft
    other = add("facture (2).pdf", "invoice", data=b"%PDF same")
    assert intake.create_draft_invoice(other, 557)["invoice_id"] == 43
