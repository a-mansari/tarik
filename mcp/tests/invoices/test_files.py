"""Which files of a conversation are still to process, on the real Postgres (db_cabinet)."""

import pytest

from tarik.core.cabinets import cabinet_db
from tarik.dossiers import setup
from tarik.invoices import intake
from tarik.invoices.pipeline import files_to_process


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
def test_an_entries_file_experio_cannot_read_is_not_taken_again(db_cabinet, monkeypatch):
    monkeypatch.setattr(setup, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(setup, "odoo_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Experio refuse")))
    broken = add("fec.csv", data=b"JournalCode;Debit\nAC;10")
    assert setup.import_entries(557, broken)["status"] == "stop"
    assert files_to_process("s", entries=True)["files"] == []
