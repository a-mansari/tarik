"""A spreadsheet of invoices: recognised by its headers, imported as drafts through Experio's invoice import."""

import io

import openpyxl
import pytest

from engine.tools.invoices import sheet
from engine.tools.invoices.sheet_headers import is_invoice_sheet
from tests.fakes import FakeDb

CANVAS = ["Nom", "Partenaire", "Numéro de facture", "Date de facturation", "Montant HT", "Montant TVA", "Montant TTC"]
FREE = ["Fournisseur", "N° facture", "Date", "HT", "TTC"]


def workbook(headers):
    book = openpyxl.Workbook()
    book.active.append(headers)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.mark.functionality
def test_an_invoice_table_is_recognised_by_its_headers_never_a_statement_or_a_fec():
    assert is_invoice_sheet("factures.xlsx", workbook(CANVAS))
    assert is_invoice_sheet("achats.xlsx", workbook(FREE))
    assert is_invoice_sheet("achats.csv", "Fournisseur;N° facture;Date;HT;TTC\n".encode())
    assert not is_invoice_sheet("releve.xlsx", workbook(["Date", "Libellé", "Débit", "Crédit"]))
    assert not is_invoice_sheet("fec.xlsx", workbook(["JournalCode", "EcritureDate", "CompteNum", "Debit", "Credit"]))
    assert not is_invoice_sheet("facture.pdf", b"%PDF")


@pytest.mark.functionality
def test_a_free_form_header_experio_does_not_suggest_is_placed_by_its_usual_name():
    preview = {"headers": FREE, "matches": {"1": ["invoice_number"]}}  # the screen only knew one column
    assert sheet.mapped_fields(preview, {}) == [
        "partner_id",
        "invoice_number",
        "invoice_date",
        "amount_untaxed",
        "amount_total",
    ]


@pytest.mark.functionality
def test_the_clients_choice_wins_and_a_field_is_given_to_one_column_only():
    preview = {"headers": ["Tiers", "Autre tiers", "Montant"], "matches": {}}
    fields = sheet.mapped_fields(preview, sheet.parsed_columns("Autre tiers = ignorer; Montant = amount_total"))
    assert fields == ["partner_id", False, "amount_total"]
    assert sheet.mapped_fields({"headers": ["Tiers", "Fournisseur"], "matches": {}}, {}) == ["partner_id", False]


@pytest.mark.functionality
def test_what_an_invoice_still_needs_is_listed():
    assert sheet.missing(["partner_id", "invoice_number", "invoice_date", "amount_total"]) == []
    lacks = sheet.missing(["invoice_number", "amount_tva"])
    assert len(lacks) == 3 and any("TTC" in lack for lack in lacks)


PREVIEW = {"headers": FREE, "matches": {}, "options": {"separator": ";"}}
TEXT = "Fournisseur;N° facture;Date;HT;TTC\nATLAS;F1;2025-03-05;100;120\nATLAS;F2;2025-03-06;50;60\n"


@pytest.fixture
def experio(monkeypatch):
    state = {"held": [], "imported": [], "taken": [], "dry": False}
    monkeypatch.setattr(sheet, "cabinet_db", lambda: FakeDb())
    monkeypatch.setattr(sheet, "file_row", lambda db, file_id, *cols: ("achats.csv", "text/csv", TEXT.encode()))
    monkeypatch.setattr(sheet, "require_dossier", lambda dossier_id: "ATLAS")
    monkeypatch.setattr(sheet, "in_dossier", lambda dossier_id: {})
    monkeypatch.setattr(sheet, "table_text", lambda name, data: TEXT)
    monkeypatch.setattr(sheet, "previewed", lambda *a, **k: (7, PREVIEW))
    monkeypatch.setattr(sheet, "tested", lambda *a, **k: {"ids": [1, 2]})
    monkeypatch.setattr(sheet, "imported", lambda *a, **k: state["imported"].append(a) or {"ids": [11, 12]})
    monkeypatch.setattr(sheet, "taken", state["taken"].append)
    monkeypatch.setattr(sheet, "dry_run", lambda: state["dry"])
    monkeypatch.setattr(sheet, "odoo_call", lambda *a, **k: state["held"])
    return state


@pytest.mark.communication
def test_the_side_is_asked_when_the_client_did_not_say_it(experio):
    result = sheet.import_invoice_sheet(857, 1)
    assert result["status"] == "question" and "achat ou de vente" in result["message"] and not experio["imported"]


@pytest.mark.communication
def test_columns_that_cannot_be_placed_are_asked_with_what_is_missing(experio, monkeypatch):
    monkeypatch.setattr(sheet, "previewed", lambda *a, **k: (7, {"headers": ["Truc", "Machin"], "matches": {}}))
    result = sheet.import_invoice_sheet(857, 1, "achat")
    assert result["status"] == "question" and "`Truc`" in result["message"] and "numéro de facture" in result["message"]
    assert "partner_id" in result["choices"] and not experio["imported"]


@pytest.mark.functionality
def test_drafts_are_imported_on_the_side_given_and_the_columns_used_are_told(experio):
    result = sheet.import_invoice_sheet(857, 1, "vente")
    assert result["status"] == "ok" and result["invoice_ids"] == [11, 12]
    assert "2 brouillons créés" in result["message"] and "`HT` → le montant HT" in result["message"]
    assert experio["taken"] == [1]


@pytest.mark.security
def test_a_duplicate_is_refused_the_first_time_and_imported_when_the_client_insists(experio):
    experio["held"] = [{"invoice_number": "F1", "partner_id": [5, "ATLAS"]}]
    first = sheet.import_invoice_sheet(857, 1, "achat")
    assert first["status"] == "question" and "n° F1 (ATLAS)" in first["message"] and not experio["imported"]
    again = sheet.import_invoice_sheet(857, 1, "achat", keep_duplicates=True)
    assert again["status"] == "ok" and "n° F1 (ATLAS)" in again["message"] and experio["imported"]


@pytest.mark.security
def test_the_same_invoice_twice_in_the_sheet_is_a_duplicate(experio, monkeypatch):
    monkeypatch.setattr(sheet, "table_text", lambda name, data: TEXT + "ATLAS;F2;2025-03-07;10;12\n")
    assert sheet.import_invoice_sheet(857, 1, "achat")["status"] == "question"


@pytest.mark.security
def test_test_mode_runs_experios_test_and_imports_nothing(experio):
    experio["dry"] = True
    result = sheet.import_invoice_sheet(857, 1, "achat")
    assert result["status"] == "ok" and "n'ai rien importé" in result["message"] and not experio["imported"]


@pytest.mark.security
def test_a_row_experio_refuses_stops_the_import_and_takes_the_file(experio, monkeypatch):
    def refuse(*a, **k):
        raise sheet.Refused("Tableur : Experio a refusé le fichier")

    monkeypatch.setattr(sheet, "tested", refuse)
    result = sheet.import_invoice_sheet(857, 1, "achat")
    assert result["status"] == "stop" and experio["taken"] == [1] and not experio["imported"]
