"""What the client wrote with their invoices: taken only as they wrote it, then each step takes it instead of asking."""

import pytest

from engine.tools.core.conversation import files_of, handed, instructed, own_words
from engine.tools.invoices import accounts, correct


@pytest.mark.security
def test_an_instruction_counts_only_as_the_client_wrote_it():
    message = "Voici les factures d'achat, compte 6125, payees par cheque. Fichiers joints : f.pdf"
    assert instructed("6125", message) == "6125" and instructed("cheque", message) == "cheque"
    assert instructed("Chèque", message) == "Chèque"  # accents and case aside
    assert instructed("6126", message) == "" and instructed("virement", message) == ""  # the agent's own idea
    assert instructed("612", message) == "" and instructed("", message) == ""  # a part of a number is not it


@pytest.mark.security
def test_the_files_names_handed_with_a_message_are_not_the_clients_words():
    text = handed("compte 6125", ["ICE 001122334455667.pdf", "b.png"])
    assert files_of(text) == ["ICE 001122334455667.pdf", "b.png"] and own_words(text) == "compte 6125"
    assert files_of(handed("", [])) == [] and own_words(handed("", [])) == ""
    left = ["lot.zip › compte 6126.docx : type non pris en charge"]  # what a zip left out: not their words either
    text = handed("compte 6125", ["lot.zip › f.pdf"], left)
    assert files_of(text) == ["lot.zip › f.pdf"] and own_words(text) == "compte 6125" and "6126" in text
    assert own_words(handed("", [], left)) == ""


@pytest.mark.functionality
def test_the_account_the_client_named_goes_on_the_invoice_first(monkeypatch):
    applied = []
    monkeypatch.setattr(
        accounts, "set_account", lambda invoice_id, answer, confirm=False: applied.append(answer) or {"status": "ok"}
    )
    monkeypatch.setattr(
        accounts, "require_draft_invoice", lambda *a, **k: pytest.fail("not asked without an instruction")
    )
    assert accounts.assign_account(3, "6125", "compte 6125 pour celle-ci")["status"] == "ok"
    assert applied == ["6125"]
    monkeypatch.setattr(accounts, "require_draft_invoice", lambda *a, **k: {"account_id": [1, "6111 Achats"]})
    monkeypatch.setattr(accounts, "account_label", lambda account_id: "61110000 Achats de marchandises")
    assert "déjà renseigné" in accounts.assign_account(3, "", "comptabilisez-la")["message"]  # the usual path
    # An account the client's words don't hold: asked about, never skipped (the invoice would be posted on another).
    asked = accounts.assign_account(3, "6125", "comptabilisez-la")
    assert asked["status"] == "question" and "« 6125 »" in asked["message"]
    assert applied == ["6125"]


@pytest.mark.functionality
def test_an_account_correction_made_in_an_earlier_message_is_applied(monkeypatch, db_cabinet):
    from engine.tools.core.conversation import remember_client_message

    applied = []
    monkeypatch.setattr(
        accounts, "set_account", lambda invoice_id, answer, confirm=False: applied.append(answer) or {"status": "ok"}
    )
    remember_client_message("s", "Nouveau. Et ce n'est pas du téléphone : mettez-la en frais de déplacement / péage.")
    later = "Oui, comptabilisez la facture 272391 maintenant."
    assert accounts.assign_account(3, "frais de déplacement / péage", later, "s")["status"] == "ok"
    assert applied == ["frais de déplacement / péage"]


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


@pytest.mark.security
def test_an_account_given_for_another_invoice_is_asked_to_be_confirmed_never_applied(monkeypatch):
    invoice = {"invoice_number": "CD241122", "company_id": [904, "D"]}
    monkeypatch.setattr(accounts, "require_draft_invoice", lambda *a, **k: invoice)
    monkeypatch.setattr(accounts, "chart", lambda company_id: [{"id": 9, "code": "61425000", "name": "Transport"}])
    monkeypatch.setattr(accounts, "apply_account", lambda *a: pytest.fail("never applied before the client confirms"))
    asked = accounts.set_account(272471, "61425000", confirm=True)
    assert asked["status"] == "question" and asked["choices"] == {9: "61425000 Transport"}
    assert "je la passe en compte 61425000 Transport" in asked["message"] and asked["message"].endswith("oui / non")
