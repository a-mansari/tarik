"""Prepaid: the period rule, the client's oui / non, and the spreading typed into Experio's form."""

import pytest

from engine.tools.core import conversation
from engine.tools.core.app import ToolError
from engine.tools.invoices import prepaid
from engine.tools.invoices.prepaid import prepaid_period


@pytest.mark.functionality
def test_the_billed_period():
    assert prepaid_period("2026-01-01", "2026-12-31") == {
        "prepaid_start_date": "2026-01-01",
        "prepaid_end_date": "2026-12-31",
        "prepaid_months": 12,
    }
    assert prepaid_period("2026-09-01", "2026-09-30") is None and prepaid_period(None, "2026-12-31") is None
    assert prepaid_period("2026-12-31", "2026-01-01") is None


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: the invoice, the period its check read, the answers kept, every write through the form."""
    state = {
        "invoice": {
            "state": "draft",
            "type": "purchase",
            "company_id": [5, "D"],
            "exercice_id": [9, "Exercice 2026"],
            "invoice_number": "F-12",
            "has_prepaid_expenses": False,
        },
        "check": (True, {"period_start": "2026-01-01", "period_end": "2026-12-31"}, []),
        "written": [],
        "refused": False,
    }
    monkeypatch.setattr(prepaid, "require_invoice", lambda invoice_id, fields: dict(state["invoice"]))
    monkeypatch.setattr(prepaid, "stored_check", lambda invoice_id: state["check"])
    monkeypatch.setattr(prepaid, "test_mode_note", lambda: "")

    def fill_invoice(invoice_id, changes, context):
        if state["refused"]:
            raise RuntimeError("Traceback\nValidationError: Période invalide")
        state["written"].append((changes, context))
        return changes

    monkeypatch.setattr(prepaid, "fill_invoice", fill_invoice)
    kept = {}
    monkeypatch.setattr(conversation, "kept_answer", lambda invoice_id, step: kept.get((invoice_id, step), ""))
    monkeypatch.setattr(
        conversation, "keep_answer", lambda invoice_id, step, answer: kept.__setitem__((invoice_id, step), answer)
    )
    return state


@pytest.mark.functionality
def test_nothing_to_spread_is_routine_and_never_asked(experio):
    experio["invoice"]["has_prepaid_expenses"] = True  # spread already
    assert prepaid.settle_prepaid(3)["routine"]
    experio["invoice"]["has_prepaid_expenses"] = False
    experio["check"] = None  # not checked yet
    assert prepaid.settle_prepaid(3)["routine"]
    experio["check"] = (True, {"period_start": "2026-03-01", "period_end": "2026-03-31"}, [])  # one month
    assert prepaid.settle_prepaid(3)["routine"] and experio["written"] == []


@pytest.mark.communication
def test_a_period_over_several_months_is_asked_and_an_unclear_answer_asked_again(experio):
    asked = prepaid.settle_prepaid(3)
    assert asked["status"] == "question" and asked["message"] == (
        "Facture F-12 : elle couvre 12 mois (2026-01-01 au 2026-12-31). "
        "La répartir en charges constates d'avance ? oui / non"
    )
    again = prepaid.settle_prepaid(3, "peut-être")
    assert again["status"] == "question" and again["message"].startswith("Réponse non comprise. Facture F-12")
    experio["invoice"]["type"] = "sale"
    assert "produits constates d'avance" in prepaid.settle_prepaid(3)["message"]
    assert experio["written"] == []


@pytest.mark.functionality
def test_non_writes_nothing_and_is_not_asked_again(experio):
    assert prepaid.settle_prepaid(3, "non")["routine"]
    assert prepaid.settle_prepaid(3)["routine"]  # taken up again: the kept answer, no question
    assert experio["written"] == []


@pytest.mark.functionality
def test_oui_ticks_the_spreading_then_the_period_in_experios_form(experio):
    result = prepaid.settle_prepaid(3, "oui")
    period = {"prepaid_start_date": "2026-01-01", "prepaid_end_date": "2026-12-31", "prepaid_months": 12}
    context = {"allowed_company_ids": [5], "cron_fiscal_entry_id": 9}
    assert experio["written"] == [({"has_prepaid_expenses": True, **period}, context)]
    assert result["status"] == "ok" and result["message"].startswith(
        "Facture 3 : charges constates d'avance sur 12 mois à partir du 2026-01-01"
    )


@pytest.mark.security
def test_a_posted_invoice_is_never_spread(experio):
    experio["invoice"]["state"] = "post"
    with pytest.raises(ToolError, match="seule une facture brouillon"):
        prepaid.settle_prepaid(3, "oui")
    assert experio["written"] == []


@pytest.mark.functionality
def test_experio_refusing_the_spreading_is_a_tool_error(experio):
    experio["refused"] = True
    with pytest.raises(ToolError, match="Experio a refusé la répartition"):
        prepaid.settle_prepaid(3, "oui")
