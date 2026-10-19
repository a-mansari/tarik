"""Posting: only when Experio's own check is clear, and the state read back decides."""

import pytest

from tarik.invoices import posting


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: what its check says blocks the invoice, the state after action_post, every call made."""
    state = {"blocking": [], "after": "post", "state": "draft", "calls": []}
    monkeypatch.setattr(
        posting, "require_invoice", lambda i, f: {"state": state["state"], "company_id": [5, "D"], "exercice_id": False}
    )
    monkeypatch.setattr(posting, "dry_run", lambda: False)
    monkeypatch.setattr(posting, "refresh", lambda invoice_id, context: None)
    monkeypatch.setattr(posting.time, "sleep", lambda seconds: None)

    def odoo_json(model, method, ids, **kw):
        state["calls"].append(method)
        if method == "collect_validation_messages":
            return [("error", text) for text in state["blocking"]]
        if method == "action_post" and state["after"] == "refused":
            raise RuntimeError("Traceback\nValidationError: Tiers sans compte")

    monkeypatch.setattr(posting, "odoo_json", odoo_json)
    monkeypatch.setattr(posting, "odoo_call", lambda *a, **k: [{"state": state["after"], "error_msg": "<p>Bloque</p>"}])
    return state


@pytest.mark.functionality
def test_posted_only_when_nothing_blocks(experio):
    assert posting.post_invoice(3)["message"] == "Facture 3 : comptabilisee."
    assert experio["calls"].count("action_post") == 1
    experio["blocking"] = ["<b>Compte</b> manquant"]
    result = posting.post_invoice(3)
    assert result["status"] == "stop" and result["message"].endswith("Experio signale : Compte manquant")
    assert (
        experio["calls"].count("collect_validation_messages") == 1 + posting.RETRIES + 1
    )  # asked again, then given up
    assert experio["calls"].count("action_post") == 1  # not posted


@pytest.mark.functionality
def test_experios_refusal_and_state_decide(experio):
    experio["after"] = "refused"
    result = posting.post_invoice(3)
    assert result["status"] == "stop" and result["message"].endswith("ValidationError: Tiers sans compte")
    experio["after"] = "draft"  # action_post went through, the invoice stayed a draft
    assert posting.post_invoice(3)["message"] == "Facture 3 : pas comptabilisee (Bloque)."
    experio["state"] = "post"
    assert posting.post_invoice(3)["routine"]
