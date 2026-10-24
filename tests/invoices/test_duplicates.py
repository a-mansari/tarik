"""The duplicate check: against posted invoices and the dossier's other drafts, never a cancelled one."""

import pytest

from engine.tools.invoices import duplicates

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


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("own_tiers", "other", "caught"),
    [
        ([7, "ATLAS"], {"id": 12, "invoice_date": "2025-03-06", "amount_total": 20000.0}, True),  # 272389 and 272383
        (False, {"id": 12, "invoice_date": "2025-03-06", "amount_total": 20000.0}, True),  # still waiting on its tiers
        ([7, "ATLAS"], {"id": 12, "invoice_date": "2025-03-06", "amount_total": 2000.0}, False),  # another total
        ([7, "ATLAS"], {"id": 12, "invoice_date": "2025-03-07", "amount_total": 20000.0}, False),  # another date
    ],
)
def test_an_invoice_without_tiers_is_still_compared_by_number_date_and_total(monkeypatch, own_tiers, other, caught):
    invoice = {**INVOICE, "partner_id": own_tiers, "invoice_date": "2025-03-06", "amount_total": 20000.0}
    searched = []

    def odoo_call(model, method, domain, **kw):
        searched.append(domain)
        return [{"invoice_number": "F2026-001", **other}]

    monkeypatch.setattr(duplicates, "require_invoice", lambda invoice_id, fields: invoice)
    monkeypatch.setattr(duplicates, "odoo_call", odoo_call)
    monkeypatch.setattr(duplicates, "odoo_json", lambda *a, **kw: [])
    result = duplicates.check_duplicate(30)
    assert (result["status"] == "stop" and "facture 12" in result["message"]) == caught
    assert ("id", "<", 30) in searched[0] and ("state", "!=", "cancel") in searched[0]
    assert ("refund", "=", False) in searched[0]  # an invoice and its credit note are never twins
    assert (("partner_id", "=", False) in searched[0]) == bool(own_tiers)  # with a tiers: only the ones without


@pytest.mark.functionality
def test_a_sale_follows_experios_sale_rule_whatever_its_customer(monkeypatch):
    """Experio's sale key has no customer (get_duplicated_invoice_ids_impl): a sale is checked without one, and its
    drafts are compared by number alone."""
    sale = {**INVOICE, "type": "sale", "partner_id": False, "invoice_date": "2026-04-20", "amount_total": 100.0}
    calls = []

    def odoo_call(model, method, domain, **kw):
        calls.append(domain)
        return [{"id": 12, "invoice_number": "F2026-001"}]

    def odoo_json(model, method, ids, **kw):
        if method == "get_duplicated_invoice_msg":
            return "Duplicated customer reference detected."
        return [30] if ids == [30] else []  # Experio finds it against a posted sale of the same number

    monkeypatch.setattr(duplicates, "require_invoice", lambda invoice_id, fields: dict(sale))
    monkeypatch.setattr(duplicates, "odoo_call", odoo_call)
    monkeypatch.setattr(duplicates, "odoo_json", odoo_json)
    result = duplicates.check_duplicate(30)  # no customer yet: checked all the same
    assert result["status"] == "stop" and result["duplicate"] and "client non renseigné" in result["message"]
    monkeypatch.setattr(duplicates, "odoo_json", lambda model, method, ids, **kw: [30] if ids == [12, 30] else [])
    assert "brouillon 12" in duplicates.check_duplicate(30)["message"]
    assert all(clause[0] != "partner_id" for clause in calls[-1])  # the drafts: by number, not by customer
