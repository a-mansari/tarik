"""The duplicate check: against posted invoices and the dossier's other drafts, never a cancelled one."""

import pytest

from tarik.invoices import duplicates

INVOICE = {
    "id": 30,
    "company_id": [5, "GECAFISC"],
    "exercice_id": [1, "Exercice 2026"],
    "invoice_number": "F 2026-001",
    "partner_id": [7, "ATLAS"],
    "type": "purchase",
}


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: the drafts search_read answers with, and every call made."""
    calls, drafts = [], []
    monkeypatch.setattr(duplicates, "require_invoice", lambda invoice_id, fields: dict(INVOICE))

    def odoo_call(model, method, *args, **kw):
        calls.append((model, method, args))
        return drafts

    monkeypatch.setattr(duplicates, "odoo_call", odoo_call)
    monkeypatch.setattr(
        duplicates, "odoo_json", lambda model, method, *args, **kw: calls.append((model, method, args)) or []
    )
    return calls, drafts


@pytest.mark.functionality
def test_only_drafts_are_compared_with_the_invoice(experio):
    """A cancelled invoice with the same number is no duplicate: Experio counts every id it is given."""
    calls, _ = experio
    assert duplicates.check_duplicate(30)["status"] == "ok"
    domain = next(args[0] for model, method, args in calls if method == "search_read")
    assert ("state", "in", ["pending", "draft"]) in domain
    assert ("id", "!=", 30) in domain


@pytest.mark.functionality
def test_the_oldest_draft_is_the_original(experio, monkeypatch):
    calls, drafts = experio
    drafts.append({"id": 12, "invoice_number": "F2026-001"})  # the same number, spaces aside
    monkeypatch.setattr(duplicates, "odoo_json", lambda model, method, ids, **kw: [30] if ids == [12, 30] else [])
    result = duplicates.check_duplicate(30)
    assert result["status"] == "stop" and "brouillon 12" in result["message"]
