"""Invoice list filters, as the agent chooses them from the client's words."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.invoices import listing
from engine.tools.invoices.listing import label


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


@pytest.mark.functionality
def test_a_side_filters_the_list_and_any_other_is_refused(monkeypatch):
    asked = []
    monkeypatch.setattr(listing, "require_dossier", lambda dossier_id: "ALPHA")
    monkeypatch.setattr(listing, "odoo_call", lambda model, method, domain, **kw: asked.append(domain) or 0)
    assert listing.list_invoices(5, "exportee", side="achat")["message"] == (
        "Aucune facture d'achat exportee dans le dossier ALPHA."
    )
    assert asked[0][-1] == ("type", "=", "purchase")
    with pytest.raises(ToolError, match="inconnu"):
        listing.list_invoices(5, side="mixte")


ATLAS = {"id": 12, "invoice_number": "F-1", "invoice_date": "2026-03-15", "amount_total": 10, "state": "draft"}


@pytest.mark.functionality
def test_each_invoice_has_one_status():
    assert listing.line({**ATLAS, "partner_id": [3, "ATLAS - ROULEMENT - 12345678"]}).count("ATLAS - ROULEMENT,") == 1
    assert label({"state": "post", "payment_state": "paid"}) == "payee"
    assert label({"state": "export", "payment_state": "paid"}) == "exportee"
    assert label({"state": "post", "payment_state": "partial"}) == "comptabilisee (payée en partie)"
    assert label({"state": "pending", "payment_state": "unpaid"}) == "en cours"


@pytest.mark.communication
def test_amounts_have_two_decimals():
    from engine.tools.invoices.listing import amount

    assert amount(999.9960000000001) == "1 000.00"
    assert amount(12000) == "12 000.00" and amount(85.72) == "85.72" and amount(None) == "0.00"


@pytest.mark.communication
def test_a_credit_note_is_listed_as_one_its_amount_as_experio_shows_it():
    from engine.tools.invoices.listing import line

    note = {"id": 7, "invoice_number": "AV-1", "partner_id": [9, "ATLAS"], "invoice_date": "2026-04-01"}
    note.update(amount_total=1140.0, state="post", payment_state="not_paid", refund=True)
    assert line(note) == "- avoir 7 : AV-1, ATLAS, 2026-04-01, -1 140.00 DH, comptabilisee"


@pytest.mark.communication
def test_the_list_says_how_many_and_shows_only_the_most_recent(monkeypatch):
    asked = []
    found = [{**ATLAS, "partner_id": [3, "ATLAS"], "payment_state": "not_paid"}]

    def odoo_call(model, method, domain, **kw):
        asked.append(kw.get("limit"))
        return 30 if method == "search_count" else found

    monkeypatch.setattr(listing, "require_dossier", lambda dossier_id: "ALPHA")
    monkeypatch.setattr(listing, "odoo_call", odoo_call)
    assert listing.list_invoices(5, "brouillon", limit=1)["message"] == (
        "30 facture(s) brouillon dans le dossier ALPHA (les 1 plus recentes) :\n"
        "- facture 12 : F-1, ATLAS, 2026-03-15, 10.00 DH, brouillon"
    )
    assert listing.list_invoices(5, limit=0)["message"].startswith("30 facture(s) dans le dossier ALPHA (les 1 ")
    listing.list_invoices(5, limit=500)
    listing.list_invoices(5, limit=-3)
    assert asked == [None, 1, None, 20, None, 100, None, 1]  # 20 by default, 100 at most, 1 at least


@pytest.mark.functionality
def test_experio_failing_to_list_is_a_tool_error(monkeypatch):
    def odoo_call(model, method, domain, **kw):
        raise RuntimeError("Traceback\nConnectionError: Experio injoignable")

    monkeypatch.setattr(listing, "require_dossier", lambda dossier_id: "ALPHA")
    monkeypatch.setattr(listing, "odoo_call", odoo_call)
    with pytest.raises(ToolError, match="Experio n'a pas pu donner la liste des factures"):
        listing.list_invoices(5)
