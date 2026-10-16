"""What the client sends becomes a draft: a dossier first, the file identified, invoices cut apart."""

import asyncio
import io

import pypdfium2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image

from server import server
from tarik.invoices.intake import require_kind, split_pdf
from tests.fakes import FakeDb


@pytest.mark.security
@pytest.mark.parametrize("args", [{"file_id": 1}, {"file_id": 1, "dossier_id": None}, {"file_id": 1, "dossier_id": 0}])
def test_a_draft_needs_a_dossier_before_anything_is_touched(args):
    with pytest.raises(ToolError, match="dossier_id"):  # a ToolError reaches the agent with its message
        asyncio.run(server.call_tool("create_draft_invoice", args))


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

    monkeypatch.setattr(intake, "cabinet_db", lambda: FakeDb(("f.pdf", "application/pdf", b"%PDF", None, "invoice")))
    draft = intake.create_draft_invoice(1, 557)
    assert draft["status"] == "stop" and draft["message"].startswith("MODE TEST") and "invoice_id" not in draft

    monkeypatch.setattr(intake, "cabinet_db", lambda: FakeDb(("r.pdf", "application/pdf", b"%PDF", "statement", None)))
    statement = intake.import_bank_statement(2, 557)
    assert (
        statement["status"] == "ok" and statement["message"].startswith("MODE TEST") and "BMCE" in statement["message"]
    )
