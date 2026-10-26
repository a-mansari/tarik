"""A posted credit note applied to the invoice it cancels, through Experio's « Crédits en attente »."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.config import TEST_MODE
from engine.tools.invoices import credit_note

CANDIDATES = [  # what Experio lists on credit note 262740 (cabinet 8), its opening balance among them
    {"journal_name": "2505571", "amount": 28459.54, "id": 380619, "move_id": 146528},
    {"journal_name": "2505792", "amount": 1140.0, "id": 520208, "move_id": 200105},
    {"journal_name": "2025-01-01", "amount": 63666.17, "id": 560106, "move_id": 222570},
    {"journal_name": "2603495", "amount": 28459.54, "id": 1613490, "move_id": 666495},
]


@pytest.fixture
def experio(monkeypatch):
    state = {"refund": True, "state": "post", "residual": 1140.0, "named": None, "applied": [], "after": 0.0}
    invoices = {146528, 200105, 666495}  # the tiers' invoices; 222570 is an opening balance

    def require_invoice(invoice_id, fields):
        return {
            "id": invoice_id,
            "state": state["state"],
            "refund": state["refund"],
            "company_id": [5, "D"],
            "partner_id": [9, "IPSEN LOGISTICS"],
            "move_id": [666496, "2600081"],
            "amount_residual": state["residual"],
            "invoice_number": "2600081",
        }

    def odoo_call(model, method, *args, **kw):
        if method == "search_read":
            return [{"move_id": [m, ""]} for m in args[0][0][2] if m in invoices]
        if kw.get("fields") == [credit_note.WIDGET]:
            return [{credit_note.WIDGET: {"content": CANDIDATES}}]
        return [{"amount_residual": state["after"]}]

    def odoo_json(model, method, ids, line_id, context):
        state["applied"].append((model, method, ids, line_id))

    monkeypatch.setattr(credit_note, "require_invoice", require_invoice)
    monkeypatch.setattr(credit_note, "odoo_call", odoo_call)
    monkeypatch.setattr(credit_note, "odoo_json", odoo_json)
    monkeypatch.setattr(credit_note, "dry_run", lambda: False)
    monkeypatch.setattr(credit_note, "stored_check", lambda i: (True, {"credit_note_of": state["named"]}, []))
    return state


@pytest.mark.functionality
def test_the_invoice_the_credit_note_names_gets_it(experio):
    experio["named"], experio["residual"], experio["after"] = "2603 495", 900.0, 0.0
    result = credit_note.apply_credit_note(7)
    assert experio["applied"] == [("exp.move", "js_assign_outstanding_line", [666496], 1613490)]
    assert "la facture 2603495 qu'il cite" in result["message"]


@pytest.mark.functionality
def test_else_the_one_invoice_owing_exactly_its_amount(experio):
    result = credit_note.apply_credit_note(7)  # 1140: only 2505792 owes it
    assert experio["applied"][0][3] == 520208 and "même montant (1140.00)" in result["message"]


@pytest.mark.security
def test_no_plain_choice_applies_nothing_and_lists_the_invoices(experio):
    experio["residual"] = 28459.54  # two invoices owe that much: not certain
    result = credit_note.apply_credit_note(7)
    assert experio["applied"] == [] and "à imputer par le cabinet" in result["message"]
    assert "2025-01-01" not in result["message"]  # an opening balance is no invoice to propose


@pytest.mark.functionality
def test_an_invoice_a_draft_or_a_used_credit_note_is_left_alone(experio):
    experio["refund"] = False
    assert credit_note.apply_credit_note(7)["routine"]
    experio["refund"], experio["state"] = True, "draft"
    assert credit_note.apply_credit_note(7)["routine"]
    experio["state"], experio["residual"] = "post", 0.0
    assert credit_note.apply_credit_note(7)["routine"] and experio["applied"] == []


@pytest.mark.functionality
def test_a_credit_note_without_its_tiers_is_left_alone(experio, monkeypatch):
    fake = credit_note.require_invoice
    monkeypatch.setattr(credit_note, "require_invoice", lambda i, f: {**fake(i, f), "partner_id": False})
    assert credit_note.apply_credit_note(7)["routine"] and experio["applied"] == []


@pytest.mark.functionality
@pytest.mark.parametrize("widget", [False, {"content": []}, {"content": [CANDIDATES[2]]}])
def test_no_invoice_of_the_tiers_to_apply_it_to_leaves_it_as_it_is(experio, monkeypatch, widget):
    """Experio lists nothing, or only the tiers' opening balance: nothing to apply the credit note to."""
    fake = credit_note.odoo_call
    monkeypatch.setattr(
        credit_note,
        "odoo_call",
        lambda model, method, *a, **kw: (
            [{credit_note.WIDGET: widget}] if method == "read" else fake(model, method, *a, **kw)
        ),
    )
    result = credit_note.apply_credit_note(7)
    assert result["routine"] and "avoir sans facture de ce tiers" in result["message"] and experio["applied"] == []


@pytest.mark.functionality
def test_in_test_mode_the_credit_note_is_only_said(experio, monkeypatch):
    monkeypatch.setattr(credit_note, "dry_run", lambda: True)
    result = credit_note.apply_credit_note(7)
    assert result["message"].startswith(f"{TEST_MODE}Facture 7 : l'avoir serait imputé sur la seule facture")
    assert experio["applied"] == []


@pytest.mark.communication
def test_what_is_left_of_the_credit_note_after_experio_applied_it_is_said(experio):
    experio["after"] = 140.0
    assert ", reste 140.00 à imputer (annulable" in credit_note.apply_credit_note(7)["message"]


@pytest.mark.functionality
def test_experio_refusing_to_apply_it_is_a_tool_error(experio, monkeypatch):
    def odoo_json(model, method, ids, line_id, context):
        raise RuntimeError("Traceback\nUserError: Lignes déjà lettrées")

    monkeypatch.setattr(credit_note, "odoo_json", odoo_json)
    with pytest.raises(ToolError, match="Experio a refusé l'imputation de l'avoir"):
        credit_note.apply_credit_note(7)
