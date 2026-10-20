"""What the client wrote with their invoices: taken only as they wrote it, then each step takes it instead of asking."""

import pytest

from tarik.core.conversation import instructed
from tarik.invoices import accounts, correct


@pytest.mark.security
def test_an_instruction_counts_only_as_the_client_wrote_it():
    message = "Voici les factures d'achat, compte 6125, payees par cheque. Fichiers joints : f.pdf"
    assert instructed("6125", message) == "6125" and instructed("cheque", message) == "cheque"
    assert instructed("Chèque", message) == "Chèque"  # accents and case aside
    assert instructed("6126", message) == "" and instructed("virement", message) == ""  # the agent's own idea
    assert instructed("612", message) == "" and instructed("", message) == ""  # a part of a number is not it


@pytest.mark.functionality
def test_the_account_the_client_named_goes_on_the_invoice_first(monkeypatch):
    applied = []
    monkeypatch.setattr(accounts, "set_account", lambda invoice_id, answer: applied.append(answer) or {"status": "ok"})
    monkeypatch.setattr(
        accounts, "require_draft_invoice", lambda *a, **k: pytest.fail("not asked without an instruction")
    )
    assert accounts.assign_account(3, "6125", "compte 6125 pour celle-ci")["status"] == "ok"
    assert applied == ["6125"]
    monkeypatch.setattr(accounts, "require_draft_invoice", lambda *a, **k: {"account_id": [1, "6111 Achats"]})
    assert "deja renseigne" in accounts.assign_account(3, "6125", "comptabilisez-la")["message"]  # the usual path
    assert applied == ["6125"]


@pytest.mark.functionality
def test_the_payment_method_the_client_gave_wins_over_the_scan(monkeypatch):
    seen = []
    monkeypatch.setattr(correct, "require_draft_invoice", lambda *a, **k: {"refund": False, "payment_method_id": False})
    monkeypatch.setattr(correct, "stored_check", lambda invoice_id: (True, {"payment_method": "especes"}, []))
    monkeypatch.setattr(correct, "_currency", lambda code, changes, notes: None)
    monkeypatch.setattr(correct, "_payment_method", lambda name, invoice, changes: seen.append(name))
    correct.correct_invoice(3, "virement", "reglee par virement")
    correct.correct_invoice(3, "virement", "reglee en especes")  # the agent's word is not the client's: the scan's
    assert seen == ["virement", "especes"]
