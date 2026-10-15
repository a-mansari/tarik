"""Invoice list filters, as the client words them."""

from tarik.invoices.listing import label, status_of


def test_filters():
    assert status_of("") == "" and status_of("toutes") == ""
    assert status_of("en cours") == "en cours" and status_of("Brouillons") == "brouillon"
    assert status_of("Comptabilisées") == "comptabilisee" and status_of("payées") == "payee"
    assert status_of("annulee") is None and status_of("brouillon ou payee") is None


def test_each_invoice_has_one_status():
    assert label({"state": "post", "payment_state": "paid"}) == "payee"
    assert label({"state": "export", "payment_state": "partial"}) == "comptabilisee"
    assert label({"state": "pending", "payment_state": "unpaid"}) == "en cours"
