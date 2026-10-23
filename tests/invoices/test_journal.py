"""The journal step: Experio's own rule, the missing setting named, and a refresh that must land there."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.invoices import journal

INVOICE = {
    "state": "draft",
    "type": "purchase",
    "is_local_invoice": True,
    "company_id": [5, "D"],
    "exercice_id": [1, "E"],
    "journal_id": [9, "Achats"],
    "enable_multi_journal": False,
    "folder_id": False,
    "is_fnp_invoice": False,
}


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: the dossier's local purchase journal, and the one the invoice holds after a refresh."""
    state = {"setting": [9, "Achats"], "after": [9, "Achats"], "invoice": dict(INVOICE), "refreshed": 0}
    monkeypatch.setattr(journal, "require_invoice", lambda invoice_id, fields: state["invoice"])
    monkeypatch.setattr(journal, "dry_run", lambda: False)
    monkeypatch.setattr(journal, "refresh", lambda invoice_id, context: state.update(refreshed=state["refreshed"] + 1))

    def odoo_call(model, method, ids, fields, **kw):
        if model == "res.company":
            return [{fields[0]: state["setting"]}]
        return [{"journal_id": state["after"]}]

    monkeypatch.setattr(journal, "odoo_call", odoo_call)
    state["misplaced"] = {}
    monkeypatch.setattr(journal, "misplaced_journals", lambda company_id, fields: state["misplaced"])
    return state


@pytest.mark.functionality
def test_the_invoice_sits_in_the_dossiers_journal(experio):
    assert journal.check_journal(3)["routine"] and experio["refreshed"] == 0  # already there: nothing done
    experio["invoice"]["journal_id"] = [2, "Autre"]
    assert journal.check_journal(3)["message"] == "Facture 3 : journal Achats." and experio["refreshed"] == 1
    experio["after"] = [2, "Autre"]  # Experio kept its own
    with pytest.raises(ToolError, match="gardé le journal Autre au lieu de Achats"):
        journal.check_journal(3)


@pytest.mark.communication
def test_the_missing_setting_is_named(experio):
    experio["misplaced"] = {"foreign_purchase_journal_id": None}
    experio["invoice"]["is_local_invoice"] = False
    result = journal.check_journal(3)
    assert result["status"] == "stop" and "journal d'achats etranger configuré" in result["message"]
    experio["invoice"].update(enable_multi_journal=True, journal_id=[4, "Choisi"])
    assert journal.check_journal(3)["message"] == "Facture 3 : journal Choisi (choix propre au dossier)."


@pytest.mark.communication
def test_another_dossiers_journal_stops_the_invoice(experio):
    """Experio's default for a new dossier is its first dossier's journals: the account step could only fail on them."""
    experio["misplaced"] = {"local_purchase_journal_id": "ACHAT"}
    result = journal.journal_stop(3, INVOICE)
    assert result["status"] == "stop"
    assert "journal d'achats local du dossier (ACHAT) est celui d'un autre dossier" in result["message"]
    assert "Configuration > Comptabilite > Journaux" in result["message"] and "carte Achat local" in result["message"]
    experio["misplaced"] = {}
    assert journal.journal_stop(3, INVOICE) is None
    experio["misplaced"] = {"local_purchase_journal_id": "ACHAT"}
    assert journal.journal_stop(3, {**INVOICE, "folder_id": [1, "Douane"]}) is None  # the dossier's own choice


@pytest.mark.functionality
def test_no_account_is_looked_up_without_the_dossiers_journal(experio, monkeypatch):
    from engine.tools.invoices import accounts

    draft = {"account_id": False, "type": "purchase"}
    monkeypatch.setattr(accounts, "require_draft_invoice", lambda *a, **k: draft)
    monkeypatch.setattr(accounts, "require_invoice", lambda invoice_id, fields: INVOICE)
    experio["misplaced"] = {"local_purchase_journal_id": "ACHAT"}
    monkeypatch.setattr(accounts, "side_of", lambda invoice: pytest.fail("Experio is not asked for an account"))
    assert accounts.assign_account(3)["status"] == "stop"


@pytest.mark.functionality
def test_an_invoice_made_before_the_dossiers_journals_is_moved_to_them_first(experio, monkeypatch):
    from engine.tools.invoices import accounts

    moved = []
    monkeypatch.setattr(accounts, "require_draft_invoice", lambda *a, **k: {"account_id": False, "type": "purchase"})
    monkeypatch.setattr(accounts, "require_invoice", lambda invoice_id, fields: INVOICE)
    monkeypatch.setattr(accounts, "check_journal", lambda invoice_id: moved.append(invoice_id))
    monkeypatch.setattr(accounts, "side_of", lambda invoice: pytest.fail("stops after the journal, before any account"))
    with pytest.raises(pytest.fail.Exception):
        accounts.assign_account(3)
    assert moved == [3]
