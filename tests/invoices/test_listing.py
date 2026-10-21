"""Invoice list filters, as the agent chooses them from the client's words."""

import pytest

from tarik.core.app import ToolError
from tarik.invoices import listing
from tarik.invoices.listing import label


@pytest.mark.functionality
@pytest.mark.parametrize("status", ["", "en cours", "brouillon", "comptabilisee", "payee", "exportee"])
def test_a_status_the_agent_chooses_filters_the_list(monkeypatch, status):
    asked = []
    monkeypatch.setattr(listing, "require_dossier", lambda dossier_id: "ALPHA")
    monkeypatch.setattr(listing, "odoo_call", lambda model, method, domain, **kw: asked.append(domain) or 0)
    listing.list_invoices(5, status)
    assert asked[0] == [("company_id", "=", 5), ("state", "!=", "cancel"), *listing.STATUSES.get(status, [])]


@pytest.mark.security
@pytest.mark.parametrize("status", ["impayees", "toutes", "brouillon ou payee", "annulee"])
def test_any_other_status_is_refused_never_guessed(monkeypatch, status):
    monkeypatch.setattr(listing, "require_dossier", lambda dossier_id: "ALPHA")
    with pytest.raises(ToolError, match="inconnu"):
        listing.list_invoices(5, status)


ATLAS = {"id": 12, "invoice_number": "F-1", "invoice_date": "2026-03-15", "amount_total": 10, "state": "draft"}


@pytest.mark.functionality
def test_each_invoice_has_one_status():
    assert listing.line({**ATLAS, "partner_id": [3, "ATLAS - ROULEMENT - 12345678"]}).count("ATLAS - ROULEMENT,") == 1
    assert label({"state": "post", "payment_state": "paid"}) == "payee"
    assert label({"state": "export", "payment_state": "paid"}) == "exportee"
    assert label({"state": "post", "payment_state": "partial"}) == "comptabilisee"
    assert label({"state": "pending", "payment_state": "unpaid"}) == "en cours"


@pytest.mark.communication
def test_amounts_have_two_decimals():
    from tarik.invoices.listing import amount

    assert amount(999.9960000000001) == "1 000.00"
    assert amount(12000) == "12 000.00" and amount(85.72) == "85.72" and amount(None) == "0.00"
