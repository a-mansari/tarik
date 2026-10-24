"""Posting: only when Experio's own check is clear, and the state read back decides."""

import pytest

from engine.tools.core import experio as experio_calls
from engine.tools.invoices import posting


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: what its check says blocks the invoice, the state after action_post, every call made."""
    state = {"blocking": [], "after": "post", "state": "draft", "calls": [], "lines": []}
    invoice = {"company_id": [5, "D"], "exercice_id": False}
    monkeypatch.setattr(
        posting, "require_invoice", lambda i, f: {**invoice, "state": state["state"], "line_ids": state["lines"]}
    )
    monkeypatch.setattr(posting, "dry_run", lambda: False)
    monkeypatch.setattr(posting, "missing_compte_tiers", lambda invoice: None)
    monkeypatch.setattr(posting, "auto_merge", lambda invoice: None)
    monkeypatch.setattr(posting, "disbursements_first", lambda invoice: None)
    monkeypatch.setattr(posting, "assets_without_category", lambda invoice_id, context: state.get("assets", []))
    monkeypatch.setattr(posting, "refresh", lambda invoice_id, context: state["calls"].append("refresh"))
    monkeypatch.setattr(posting.time, "sleep", lambda seconds: None)

    def odoo_json(model, method, ids, **kw):
        state["calls"].append(method)
        if method == "collect_validation_messages":
            return [("error", text) for text in state["blocking"]]
        if method == "action_post" and state["after"] == "refused":
            raise RuntimeError("Traceback\nValidationError: Tiers sans compte")
        if method == "action_post":
            return state.get("action")

    monkeypatch.setattr(posting, "odoo_json", odoo_json)
    monkeypatch.setattr(experio_calls, "odoo_json", odoo_json)  # its check, shared with configuration
    monkeypatch.setattr(posting, "odoo_call", lambda *a, **k: [{"state": state["after"], "error_msg": "<p>Bloque</p>"}])
    return state


@pytest.mark.functionality
def test_posted_only_when_nothing_blocks(experio):
    assert posting.post_invoice(3)["message"] == "Facture 3 : comptabilisée."
    assert experio["calls"].count("action_post") == 1
    experio["blocking"] = ["<b>Compte</b> manquant"]
    result = posting.post_invoice(3)
    assert result["status"] == "stop" and result["message"].endswith("Experio signale : Compte manquant")
    assert (
        experio["calls"].count("collect_validation_messages") == 1 + posting.RETRIES + 1
    )  # asked again, then given up
    assert experio["calls"].count("action_post") == 1  # not posted


@pytest.mark.functionality
def test_writing_lines_fixed_by_hand_are_not_rebuilt(experio):
    posting.post_invoice(3)
    assert experio["calls"].count("refresh") == 1  # no writing lines yet: Experio builds them
    experio["lines"] = [41, 42]  # the cabinet may have fixed them in Experio: a refresh would rebuild them
    posting.post_invoice(3)
    assert experio["calls"].count("refresh") == 1 and experio["calls"].count("action_post") == 2


@pytest.mark.functionality
def test_experios_refusal_and_state_decide(experio):
    experio["after"] = "refused"
    result = posting.post_invoice(3)
    assert result["status"] == "stop" and result["message"].endswith("ValidationError: Tiers sans compte")
    experio["after"] = "draft"  # action_post went through, the invoice stayed a draft
    assert posting.post_invoice(3)["message"] == "Facture 3 : pas comptabilisée (Bloque)."
    experio["state"] = "post"
    assert posting.post_invoice(3)["routine"]


@pytest.mark.functionality
def test_an_invoice_whose_tiers_has_no_compte_tiers_is_not_posted(experio, monkeypatch):
    monkeypatch.undo()  # the fixture's fakes go; this test brings its own
    posted = []
    accounts = {"third_party_supplier_account": False, "third_party_customer_account": "3421"}
    company = {"ignore_third_party_account": False}

    def odoo_call(model, method, ids, fields):
        return [company if model == "res.company" else dict(accounts)]

    monkeypatch.setattr(posting, "odoo_call", odoo_call)
    monkeypatch.setattr(posting, "dry_run", lambda: False)
    monkeypatch.setattr(posting, "auto_merge", lambda invoice: None)
    monkeypatch.setattr(posting, "disbursements_first", lambda invoice: None)
    purchase = {"state": "draft", "company_id": [5, "D"], "exercice_id": False, "partner_id": [9, "T"]}
    purchase["type"] = "purchase"
    monkeypatch.setattr(posting, "require_invoice", lambda i, f: purchase)
    monkeypatch.setattr(posting, "refresh", lambda invoice_id, context: posted.append("refresh"))
    result = posting.post_invoice(3)
    assert result["status"] == "stop" and "Compte Tiers fournisseur" in result["message"] and posted == []
    assert posting.missing_compte_tiers({**purchase, "type": "sale"}) is None  # its customer account is there
    company["ignore_third_party_account"] = True  # the dossier does without them
    assert posting.missing_compte_tiers(purchase) is None


@pytest.mark.functionality
def test_a_window_experio_opens_instead_of_posting_is_said(experio):
    """action_post may answer with a window (a closed earlier exercice) and post nothing: over RPC nobody sees it."""
    experio["after"] = "draft"
    experio["action"] = {"type": "ir.actions.act_window", "res_model": "exp.fiscal.year.confirmation.wizard"}
    experio["action"]["name"] = "Facture antérieure"
    result = posting.post_invoice(3)
    assert result["status"] == "stop" and "« Facture antérieure »" in result["message"]


@pytest.mark.security
def test_posting_never_merges_two_tiers_for_good(monkeypatch):
    """A dossier that merges the tiers Experio read into the one chosen when they differ deletes one at posting."""
    company, invoice = {"auto_merge_detected_partner": True}, {"has_wrong_partner": True}
    monkeypatch.setattr(
        posting, "odoo_call", lambda model, method, ids, fields: [company if model == "res.company" else invoice]
    )
    draft = {"id": 3, "company_id": [5, "D"]}
    assert posting.auto_merge(draft)["status"] == "stop"
    invoice["has_wrong_partner"] = False  # the tiers chosen is the one read: nothing merged
    assert posting.auto_merge(draft) is None
    company["auto_merge_detected_partner"], invoice["has_wrong_partner"] = False, True  # the dossier never merges
    assert posting.auto_merge(draft) is None


@pytest.mark.functionality
def test_an_immobilisation_without_its_category_is_not_posted(experio):
    """Without a category for its class 2 account, Experio posts the invoice and makes no asset, silently."""
    experio["assets"] = ["23550000 Matériel informatique"]
    result = posting.post_invoice(3)
    assert result["status"] == "stop" and "Configuration > Immobilisations > Catégories" in result["message"]
    assert "action_post" not in experio["calls"]
