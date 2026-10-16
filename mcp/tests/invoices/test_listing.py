"""Invoice list filters, as the client words them."""

import pytest

from tarik.invoices.listing import label, status_of


@pytest.mark.functionality
def test_filters():
    assert status_of("") == "" and status_of("toutes") == ""
    assert status_of("en cours") == "en cours" and status_of("Brouillons") == "brouillon"
    assert status_of("Comptabilisées") == "comptabilisee" and status_of("payées") == "payee"
    assert status_of("annulee") is None and status_of("brouillon ou payee") is None


@pytest.mark.functionality
def test_each_invoice_has_one_status():
    assert label({"state": "post", "payment_state": "paid"}) == "payee"
    assert label({"state": "export", "payment_state": "partial"}) == "comptabilisee"
    assert label({"state": "pending", "payment_state": "unpaid"}) == "en cours"


@pytest.mark.communication
def test_amounts_have_two_decimals():
    from tarik.invoices.listing import amount

    assert amount(999.9960000000001) == "1 000.00"
    assert amount(12000) == "12 000.00" and amount(85.72) == "85.72" and amount(None) == "0.00"
