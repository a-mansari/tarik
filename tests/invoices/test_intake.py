"""What the client sends becomes a draft: a dossier first, the file identified, invoices cut apart."""

import asyncio
import io

import pypdfium2
import pytest
from PIL import Image

from engine.tools import ToolFailure, call
from tarik.core.app import ToolError
from tarik.invoices.intake import require_kind, split_pdf
from tests.fakes import FakeDb


@pytest.mark.security
@pytest.mark.parametrize("args", [{"file_id": 1}, {"file_id": 1, "dossier_id": None}, {"file_id": 1, "dossier_id": 0}])
def test_a_draft_needs_a_dossier_before_anything_is_touched(args, db_cabinet):
    with pytest.raises(ToolFailure, match="dossier|informations manquent"):  # refused, before anything is touched
        asyncio.run(call(db_cabinet, "create_draft_invoice", **args))


@pytest.mark.technical
def test_a_pdf_is_cut_where_its_invoices_start():
    buffer = io.BytesIO()
    pages = [Image.new("RGB", (200, 280), color) for color in ("white", "gray", "white", "gray")]
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    four = buffer.getvalue()
    parts = split_pdf(four, [1, 3])
    assert [(first, last) for first, last, _ in parts] == [(1, 2), (3, 4)]
    assert [len(pypdfium2.PdfDocument(raw)) for _, _, raw in parts] == [2, 2]
    assert [(first, last) for first, last, _ in split_pdf(four, [3, 0, 9, 3])] == [
        (1, 2),
        (3, 4),
    ]  # page 1 added, out of range dropped
    assert [(first, last) for first, last, _ in split_pdf(four, [])] == [(1, 4)]
    assert len(split_pdf(four, [1, 2, 3, 4])) == 4


@pytest.mark.functionality
def test_a_file_is_identified_before_anything_is_made_from_it():
    require_kind("f.pdf", "invoice", "invoice")
    for kind, wanted, hint in (
        (None, "invoice", "identify_document"),
        ("statement", "invoice", "import_bank_statement"),
        ("invoice", "statement", "split_invoices"),
    ):
        with pytest.raises(ToolError, match=hint):
            require_kind("f.pdf", kind, wanted)


@pytest.mark.security
def test_test_mode_creates_no_draft_and_imports_no_statement(monkeypatch):
    from tarik.invoices import intake

    def odoo_call(model, method, *args, **kw):
        if method == "search_read":  # the dossier's bank journals
            return [{"id": 5, "name": "BMCE", "bank_id": [1, "BMCE"]}]
        raise AssertionError(f"test mode wrote to Experio: {model}.{method}")

    monkeypatch.setattr(intake, "odoo_call", odoo_call)
    monkeypatch.setattr(intake, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(intake, "dry_run", lambda: True)

    monkeypatch.setattr(
        intake, "cabinet_db", lambda: FakeDb(("s", "f.pdf", "application/pdf", b"%PDF", "invoice", None, None, None))
    )
    draft = intake.create_draft_invoice(1, 557)
    assert draft["status"] == "stop" and draft["message"].startswith("MODE TEST") and "invoice_id" not in draft

    monkeypatch.setattr(
        intake, "cabinet_db", lambda: FakeDb(("s", "r.pdf", "application/pdf", b"%PDF", "statement", None, None, None))
    )
    statement = intake.import_bank_statement(2, 557)
    assert (
        statement["status"] == "ok" and statement["message"].startswith("MODE TEST") and "BMCE" in statement["message"]
    )


def pdf(count):
    """A PDF of ``count`` blank pages."""
    buffer = io.BytesIO()
    pages = [Image.new("RGB", (200, 280), "white") for _ in range(count)]
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    return buffer.getvalue()


@pytest.mark.functionality
@pytest.mark.parametrize(
    "said, choice",
    [
        ("Une facture par page svp", "by_page"),
        ("Découpez page par page", "by_page"),
        ("par document", "by_document"),
        ("C'est une seule facture", "by_document"),
        ("Voici les factures d'achat", None),  # nothing said: asked
        ("par page ou par document, je ne sais pas", None),  # both said: never guessed
    ],
)
def test_how_a_pdf_is_cut_is_the_client_s_word(said, choice):
    from tarik.invoices.intake import split_choice

    assert split_choice(["Voici les factures d'achat de TARIK TEST", said]) == choice


def saved(db_cabinet, data, session="s-split"):
    from tarik.core.cabinets import cabinet_db

    with cabinet_db() as db:
        (file_id,) = db.execute(
            "insert into tarik_files (session_id, file_name, mime_type, data, kind) "
            "values (%s, 'lot.pdf', 'application/pdf', %s, 'invoice') returning id",
            (session, data),
        ).fetchone()
    return file_id


@pytest.mark.functionality
def test_a_pdf_of_several_pages_waits_for_the_client_to_say_how_to_cut_it(db_cabinet):
    from tarik.invoices.intake import split_invoices

    file_id = saved(db_cabinet, pdf(3))
    asked = split_invoices(file_id=file_id, invoice_type="achat", client_message="Voici les factures d'achat")
    assert asked["status"] == "question" and "par page" in asked["message"] and "par document" in asked["message"]
    whole = split_invoices(file_id=file_id, invoice_type="achat", client_message="par document, achat")
    assert whole["status"] == "ok" and whole["files"] == [file_id]


@pytest.mark.functionality
def test_a_pdf_cut_per_page_is_one_invoice_per_page_and_one_page_never_asked(db_cabinet):
    from tarik.invoices.intake import split_invoices

    file_id = saved(db_cabinet, pdf(3), "s-pages")
    cut = split_invoices(file_id=file_id, invoice_type="achat", client_message="achat, une facture par page")
    assert cut["status"] == "ok" and len(cut["files"]) == 3 and file_id not in cut["files"]
    one = saved(db_cabinet, pdf(1), "s-one")
    assert split_invoices(file_id=one, invoice_type="achat", client_message="achat")["files"] == [one]
