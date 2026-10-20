"""TVA: the question on the usual non-recoverable accounts, the client's answer, the cabinet's prorata."""

import pytest

from tarik.core import conversation
from tarik.invoices import tva


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
            "account_id": [1, "61251000 Carburant"],
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
    monkeypatch.setattr(tva, "experio_notes", lambda invoice_id, context: "")
    monkeypatch.setattr(tva, "test_mode_note", lambda: "")
    monkeypatch.setattr(tva, "_tax_lines", lambda invoice_id, context: ["500.0 HT a 20.0 % (TVA 20%)"])
    monkeypatch.setattr(tva, "odoo_call", lambda *a, **k: [{"use_prorata": state["prorata"], "prorata_rate": 80}])
    kept = {}  # the answers kept per invoice and step (tarik_answers)
    monkeypatch.setattr(conversation, "kept_answer", lambda invoice_id, step: kept.get((invoice_id, step), ""))
    monkeypatch.setattr(
        conversation, "keep_answer", lambda invoice_id, step, answer: kept.__setitem__((invoice_id, step), answer)
    )
    return state


@pytest.mark.functionality
def test_fuel_asks_then_the_answer_writes(experio):
    assert tva.settle_tva(3)["status"] == "question"  # 6125: usually non-recoverable, the client decides
    assert tva.settle_tva(3, "peut-etre")["message"].startswith("Facture 3 : reponse non comprise")
    assert tva.settle_tva(3, "oui")["message"].startswith("Facture 3 : TVA non recuperable")
    assert experio["written"] == [{"amount_tax_non_recoverable": True, "multi_tva": False}]
    assert tva.settle_tva(3, "non")["message"].startswith("Facture 3 : TVA recuperable. Lignes : 500.0")
    assert len(experio["written"]) == 1  # recoverable: nothing to write


@pytest.mark.functionality
def test_the_cabinets_prorata_applies_to_recoverable_tva(experio):
    experio["invoice"]["account_id"] = [2, "61110000 Marchandises"]
    experio["prorata"] = True
    result = tva.settle_tva(3)
    assert result["message"].startswith("Facture 3 : TVA recuperable, prorata de TVA 80 %.")
    assert experio["written"] == [{"tax_prorata": True}]
    experio["invoice"]["type"] = "sale"
    assert tva.settle_tva(3)["routine"]  # a sale: nothing to settle, nothing to tell


@pytest.mark.functionality
def test_an_answer_is_kept_for_the_invoice_taken_up_again(experio):
    """A "non" changes nothing in Experio: kept, so the invoice resumed later is not asked again."""
    assert tva.settle_tva(3)["status"] == "question"
    assert tva.settle_tva(3, "non")["message"].startswith("Facture 3 : TVA recuperable")
    assert tva.settle_tva(3)["message"].startswith("Facture 3 : TVA recuperable")  # taken up again: no question
