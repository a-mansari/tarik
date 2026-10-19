"""The journal step: Experio's own rule, the missing setting named, and a refresh that must land there."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tarik.invoices import journal

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
    return state


@pytest.mark.functionality
def test_the_invoice_sits_in_the_dossiers_journal(experio):
    assert journal.check_journal(3)["routine"] and experio["refreshed"] == 0  # already there: nothing done
    experio["invoice"]["journal_id"] = [2, "Autre"]
    assert journal.check_journal(3)["message"] == "Facture 3 : journal Achats." and experio["refreshed"] == 1
    experio["after"] = [2, "Autre"]  # Experio kept its own
    with pytest.raises(ToolError, match="garde le journal Autre au lieu de Achats"):
        journal.check_journal(3)


@pytest.mark.communication
def test_the_missing_setting_is_named(experio):
    experio["setting"] = False
    experio["invoice"]["is_local_invoice"] = False
    assert "journal d'achats etranger" in journal.check_journal(3)["message"]
    experio["invoice"].update(enable_multi_journal=True, journal_id=[4, "Choisi"])
    assert journal.check_journal(3)["message"] == "Facture 3 : journal Choisi (choix propre au dossier)."
