"""A payment difference written off through Experio's own wizard, after the client confirmed it."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.config import TEST_MODE
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


@pytest.mark.functionality
def test_no_invoice_named_is_refused_before_asking_experio(experio, monkeypatch):
    monkeypatch.setattr(difference, "odoo_call", lambda *a, **kw: pytest.fail("Experio is not asked"))
    with pytest.raises(ToolError, match="Quelle facture solder"):
        difference.settle_difference(5, "  ")


@pytest.mark.communication
def test_several_posted_invoices_with_that_number_are_listed_not_guessed(experio):
    experio["invoices"].append({**experio["invoices"][0], "id": 82})
    with pytest.raises(
        ToolError, match=r"plusieurs factures comptabilisées portent ce numéro : n° 81 \(FV 12\), n° 82"
    ):
        difference.settle_difference(5, "FV 12", client_confirmed=True)
    assert experio["calls"] == []


@pytest.mark.communication
def test_a_dossier_without_its_general_journal_is_the_cabinets(experio):
    experio["wizard"]["journal_id"] = False
    result = difference.settle_difference(5, "81", client_confirmed=True)
    assert result["status"] == "stop" and "n'a pas de journal des opérations diverses" in result["message"]
    assert ("action_apply", [81]) not in experio["calls"]


@pytest.mark.functionality
def test_in_test_mode_the_difference_is_only_said(experio, monkeypatch):
    monkeypatch.setattr(difference, "dry_run", lambda: True)
    result = difference.settle_difference(5, "81", client_confirmed=True)
    assert (
        result["message"]
        == f"{TEST_MODE}Facture 81 (FV 12) : l'écart de 3.00 DH serait passé en compte `65850000 Charges`."
    )
    assert ("action_apply", [81]) not in experio["calls"]


@pytest.mark.communication
def test_an_invoice_left_unpaid_after_the_entry_says_its_payment_state(experio, monkeypatch):
    monkeypatch.setattr(difference, "odoo_json", lambda model, method, *a, **kw: {"res_id": 70})  # nothing paid
    result = difference.settle_difference(5, "81", client_confirmed=True)
    assert result["status"] == "ok" and result["message"].endswith("facture état de paiement : partial.")


@pytest.mark.functionality
@pytest.mark.parametrize(
    "failing, message",
    [("action_open_difference_change_wizard", "n'a pas pu préparer"), ("action_apply", "a refusé l'écriture")],
)
def test_experio_refusing_its_wizard_is_a_tool_error(experio, monkeypatch, failing, message):
    fake = difference.odoo_json

    def odoo_json(model, method, *args, **kw):
        if method == failing:
            raise RuntimeError("Traceback\nUserError: Exercice clôturé")
        return fake(model, method, *args, **kw)

    monkeypatch.setattr(difference, "odoo_json", odoo_json)
    with pytest.raises(ToolError, match=message):
        difference.settle_difference(5, "81", client_confirmed=True)
