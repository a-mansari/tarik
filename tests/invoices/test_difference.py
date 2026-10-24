"""A payment difference written off through Experio's own wizard, after the client confirmed it."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.invoices import difference


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: the dossier's posted invoices, the wizard's proposal, every call made."""
    state = {
        "invoices": [
            {"id": 81, "invoice_number": "FV 12", "partner_id": [9, "CLIENT SA - 3421C001"], "state": "post"},
        ],
        "wizard": {"difference_amount": 3.0, "difference_account_id": [6, "65850000 Charges"], "journal_id": [4, "OD"]},
        "calls": [],
        "paid": "partial",
    }
    state["invoices"][0].update(amount_residual=3.0, payment_state="partial", company_id=[5, "D"])
    monkeypatch.setattr(difference, "require_dossier", lambda dossier_id: "GECAFISC")
    monkeypatch.setattr(difference, "dry_run", lambda: False)

    def odoo_call(model, method, *args, **kw):
        if model == difference.WIZARD:
            return [state["wizard"]]
        if method == "read":
            return [{"payment_state": state["paid"]}]
        domain = args[0]
        if ("id", "=", 81) in domain or any(c[0] == "invoice_number" for c in domain):
            return state["invoices"]
        return []

    def odoo_json(model, method, *args, **kw):
        state["calls"].append((method, kw["context"]["active_ids"]))
        if method == "action_apply":
            state["paid"] = "paid"
        return {"res_id": 70}

    monkeypatch.setattr(difference, "odoo_call", odoo_call)
    monkeypatch.setattr(difference, "odoo_json", odoo_json)
    return state


@pytest.mark.security
def test_the_difference_is_written_off_only_once_the_client_confirmed(experio):
    asked = difference.settle_difference(5, "FV12")  # spaces aside, as the client writes it
    assert asked["status"] == "question" and "il reste 3.00 DH dû" in asked["message"]
    assert "65850000 Charges" in asked["message"] and ("action_apply", [81]) not in experio["calls"]
    done = difference.settle_difference(5, "FV 12", client_confirmed=True)
    assert done["status"] == "ok" and done["message"].endswith("facture soldée.")
    assert experio["calls"][-1] == ("action_apply", [81])  # Experio's own wizard, on that invoice alone


@pytest.mark.functionality
def test_nothing_owed_no_account_or_no_invoice_is_said(experio):
    experio["invoices"][0]["amount_residual"] = 0.0
    assert difference.settle_difference(5, "81")["routine"]
    experio["invoices"][0]["amount_residual"] = 3.0
    experio["wizard"]["difference_account_id"] = False  # the dossier has no 6585 account: the cabinet's
    assert difference.settle_difference(5, "81", client_confirmed=True)["status"] == "stop"
    with pytest.raises(ToolError):
        difference.settle_difference(5, "FV 99")
