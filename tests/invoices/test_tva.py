"""TVA: the question on the usual non-recoverable accounts, the client's answer, the cabinet's prorata."""

import pytest

from engine.tools.core import conversation
from engine.tools.core.app import ToolError
from engine.tools.core.config import TEST_MODE
from engine.tools.invoices import tva


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: the invoice, the dossier's prorata, what was written through the form."""
    state = {
        "invoice": {
            "id": 3,
            "state": "draft",
            "type": "purchase",
            "company_id": [5, "D"],
            "exercice_id": False,
            "partner_id": [7, "SHELL"],
            "account_id": [1, "61223000 Achats de combustibles"],
            "amount_tva": 100.0,
            "amount_tax_non_recoverable": False,
            "tax_prorata": False,
            "tva_line_ids": [],
        },
        "prorata": False,
        "written": [],
    }
    monkeypatch.setattr(tva, "require_draft_invoice", lambda invoice_id, fields: state["invoice"])
    monkeypatch.setattr(tva, "side_of", lambda invoice: invoice["type"])
    monkeypatch.setattr(tva, "fill_invoice", lambda invoice_id, changes, context: state["written"].append(changes))
    monkeypatch.setattr(tva, "refresh", lambda invoice_id, context: None)
    monkeypatch.setattr(tva, "add_missing_taxes", lambda invoice_id, context: [])
    monkeypatch.setattr(tva, "test_mode_note", lambda: "")
    monkeypatch.setattr(tva, "_tax_lines", lambda invoice_id, context: ["500.0 HT à 20.0 % (TVA 20%)"])
    monkeypatch.setattr(tva, "odoo_call", lambda *a, **k: [{"use_prorata": state["prorata"], "prorata_rate": 80}])
    kept = {}  # the answers kept per invoice and step (tarik_answers)
    monkeypatch.setattr(conversation, "kept_answer", lambda invoice_id, step: kept.get((invoice_id, step), ""))
    monkeypatch.setattr(
        conversation, "keep_answer", lambda invoice_id, step, answer: kept.__setitem__((invoice_id, step), answer)
    )
    return state


@pytest.mark.functionality
def test_fuel_asks_then_the_answer_writes(experio):
    assert tva.settle_tva(3)["status"] == "question"  # fuel (61223): usually non-recoverable, the client decides
    assert tva.settle_tva(3, "peut-être")["message"].startswith("Facture 3 : réponse non comprise")
    assert tva.settle_tva(3, "oui")["message"].startswith("Facture 3 : TVA non récupérable")
    assert experio["written"] == [{"amount_tax_non_recoverable": True, "multi_tva": False}]
    assert tva.settle_tva(3, "non")["message"].startswith("Facture 3 : TVA récupérable. Lignes : 500.0")
    assert len(experio["written"]) == 1  # recoverable: nothing to write


@pytest.mark.functionality
def test_water_and_electricity_are_never_asked(experio):
    experio["invoice"]["account_id"] = [2, "61251000 Achats de fournitures non stockables (eau, électricité,,,)"]
    assert tva.settle_tva(3)["status"] == "ok"


@pytest.mark.functionality
def test_the_cabinets_prorata_applies_to_recoverable_tva(experio):
    experio["invoice"]["account_id"] = [2, "61110000 Marchandises"]
    experio["prorata"] = True
    result = tva.settle_tva(3)
    assert result["message"].startswith("Facture 3 : TVA récupérable, prorata de TVA 80 %.")
    assert experio["written"] == [{"tax_prorata": True}]
    experio["invoice"]["type"] = "sale"
    assert tva.settle_tva(3)["routine"]  # a sale: nothing to settle, nothing to tell


@pytest.mark.functionality
def test_an_answer_is_kept_for_the_invoice_taken_up_again(experio):
    """A "non" changes nothing in Experio: kept, so the invoice resumed later is not asked again."""
    assert tva.settle_tva(3)["status"] == "question"
    assert tva.settle_tva(3, "non")["message"].startswith("Facture 3 : TVA récupérable")
    assert tva.settle_tva(3)["message"].startswith("Facture 3 : TVA récupérable")  # taken up again: no question


@pytest.mark.functionality
def test_a_sale_billing_no_tva_is_ticked_tva_non_facturee_without_a_question(experio):
    experio["invoice"].update(type="sale", amount_tva=0.0, amount_total=1200.0, account_id=[1, "71240000 Services"])
    result = tva.settle_tva(3)
    assert result["status"] == "ok" and result["message"].startswith("Facture 3 : TVA non facturée")
    assert experio["written"] == [{"amount_tax_non_recoverable": True, "multi_tva": False}]
    experio["written"].clear()
    experio["invoice"]["amount_tva"] = 240.0  # TVA billed: nothing ticked, nothing asked
    assert tva.settle_tva(3)["message"].startswith("Facture 3 : TVA facturée") and experio["written"] == []


@pytest.mark.communication
def test_the_tax_lines_experio_rebuilt_are_read_back_untaxed_ones_only(monkeypatch):
    lines = [
        {"line_type": "untaxed", "tax_id": [4, "TVA 20% ACHATS"], "tax_rate": 20.0, "amount_ht": 500.0},
        {"line_type": "untaxed", "tax_id": False, "tax_rate": 0.0, "amount_ht": 30.0},
        {"line_type": "tva", "tax_id": [4, "TVA 20% ACHATS"], "tax_rate": 20.0, "amount_ht": 0.0},
    ]
    calls = []

    def odoo_call(model, method, ids, fields, context):
        calls.append(model)
        return [{"tva_line_ids": state["ids"]}] if model == "exp.invoice" else lines

    state = {"ids": [41, 42, 43]}
    monkeypatch.setattr(tva, "odoo_call", odoo_call)
    assert tva._tax_lines(3, {}) == ["500.0 HT à 20.0 % (`TVA 20% ACHATS`)", "30.0 HT à 0.0 % (sans taxe)"]
    calls.clear()
    state["ids"] = []  # no TVA line yet: nothing more read
    assert tva._tax_lines(3, {}) == [] and calls == ["exp.invoice"]


@pytest.mark.functionality
def test_without_its_account_the_tva_waits_in_test_mode_and_is_refused_otherwise(experio, monkeypatch):
    experio["invoice"]["account_id"] = False
    monkeypatch.setattr(tva, "dry_run", lambda: True)  # the account step only simulated its write
    result = tva.settle_tva(3)
    assert result["status"] == "ok" and result["message"].startswith(TEST_MODE)
    monkeypatch.setattr(tva, "dry_run", lambda: False)
    with pytest.raises(ToolError, match="appelez d'abord assign_account"):
        tva.settle_tva(3)
    assert experio["written"] == []


@pytest.mark.communication
def test_no_tax_line_yet_and_the_dossiers_taxes_made_are_said(experio, monkeypatch):
    experio["invoice"]["account_id"] = [2, "61110000 Marchandises"]
    monkeypatch.setattr(tva, "_tax_lines", lambda invoice_id, context: [])
    assert tva.settle_tva(3)["message"] == "Facture 3 : TVA récupérable. Pas encore de ligne de TVA."
    made = ["taxe TVA 10% ACHATS créée (compte 34550000)"]
    monkeypatch.setattr(tva, "add_missing_taxes", lambda invoice_id, context: made)
    result = tva.settle_tva(3)
    assert result["status"] == "ok" and "routine" not in result
    assert result["message"].endswith("Taxes du dossier : taxe TVA 10% ACHATS créée (compte 34550000).")


@pytest.mark.functionality
def test_experio_refusing_its_refresh_is_a_tool_error(experio, monkeypatch):
    experio["invoice"]["account_id"] = [2, "61110000 Marchandises"]

    def refresh(invoice_id, context):
        raise RuntimeError("Traceback\nUserError: La taxe avec le taux 10 n'existe pas")

    monkeypatch.setattr(tva, "refresh", refresh)
    with pytest.raises(ToolError, match="Experio a refusé la mise à jour de la TVA"):
        tva.settle_tva(3)
