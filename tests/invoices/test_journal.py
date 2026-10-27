"""The journal step: Experio's own rule, a missing setting set (the journal made when the dossier has none) and told,
one Experio would not set named, and a refresh that must land there."""

import contextlib

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.config import TEST_MODE
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
    """A fake Experio: the dossier's local purchase journal, and the one the invoice holds after a refresh. Setting a
    journal (made, or Experio's auto-detection) makes the settings right unless ``stuck``."""
    state = {"setting": [9, "Achats"], "after": [9, "Achats"], "invoice": dict(INVOICE), "refreshed": 0}
    state.update(misplaced={}, stuck=False, same=[{"name": "Achats"}], written=[])
    monkeypatch.setattr(journal, "require_invoice", lambda invoice_id, fields: state["invoice"])
    monkeypatch.setattr(journal, "dry_run", lambda: False)
    monkeypatch.setattr(journal, "current", lambda: "cabinet")
    monkeypatch.setattr(journal, "dossier_lock", lambda dossier_id: contextlib.nullcontext())
    monkeypatch.setattr(journal, "would", lambda done: f"serait {done}" if journal.dry_run() else done)
    monkeypatch.setattr(journal, "free_code", lambda dossier_id, prefix, context: f"{prefix}1")  # ACH already used
    monkeypatch.setattr(journal, "refresh", lambda invoice_id, context: state.update(refreshed=state["refreshed"] + 1))

    def setting_made(*call):
        state["written"].append(call)
        if not state["stuck"]:
            state["misplaced"] = {}

    def odoo_call(model, method, *args, fields=(), **kw):
        if model == "res.company":
            return [{fields[0]: state["setting"]}]
        if model == "exp.journal" and method == "search_read":
            return state["same"] if args[0][-1][0] == "type" else [{"code": "ACH"}]
        if method == "create":
            return setting_made(model, method, *args)
        return [{"journal_id": state["after"]}]

    monkeypatch.setattr(journal, "odoo_call", odoo_call)
    monkeypatch.setattr(journal, "odoo_json", lambda model, method, *args, **kw: setting_made(model, method, *args))
    monkeypatch.setattr(journal, "misplaced_journals", lambda company_id, fields: state["misplaced"])
    return state


@pytest.mark.functionality
def test_the_invoice_sits_in_the_dossiers_journal(experio):
    assert journal.check_journal(3)["routine"] and experio["refreshed"] == 0  # already there: nothing done
    experio["invoice"]["journal_id"] = [2, "Autre"]
    assert journal.check_journal(3)["message"] == "Facture 3 : journal `Achats`." and experio["refreshed"] == 1
    experio["after"] = [2, "Autre"]  # Experio kept its own
    with pytest.raises(ToolError, match="gardé le journal `Autre` au lieu de `Achats`"):
        journal.check_journal(3)


@pytest.mark.communication
def test_a_missing_journal_is_made_set_and_told_once(experio):
    experio.update(misplaced={"foreign_purchase_journal_id": None}, same=[])  # no purchase journal at all
    experio["invoice"]["is_local_invoice"] = False
    result = journal.check_journal(3)
    made = {"name": "Achats", "code": "ACH1", "type": "purchase", "company_id": 5}  # ACH already used
    assert experio["written"] == [("exp.journal", "create", made)]
    assert result["status"] == "ok" and not result.get("routine")
    assert "Fait : journal `Achats` (code `ACH1`) créé et mis dans la configuration du dossier" in result["message"]
    assert journal.check_journal(3)["routine"]  # told once
    experio["invoice"].update(enable_multi_journal=True, journal_id=[4, "Choisi"])
    assert journal.check_journal(3)["message"] == "Facture 3 : journal `Choisi` (choix propre au dossier)."


@pytest.mark.communication
def test_another_dossiers_journal_is_replaced_by_experios_auto_detection(experio):
    """Experio's default for a new dossier is its first dossier's journals: the account step could only fail on them."""
    experio["misplaced"] = {"local_purchase_journal_id": "ACHAT"}
    assert journal.journal_stop(3, INVOICE) is None
    assert experio["written"] == [("res.company", "auto_detect_settings", [5])]
    experio.update(misplaced={"local_purchase_journal_id": "ACHAT"}, stuck=True)  # Experio could not: named
    result = journal.journal_stop(3, INVOICE)
    assert result["status"] == "stop"
    assert "journal d'achats local du dossier (`ACHAT`) est celui d'un autre dossier" in result["message"]
    assert "Configuration > Comptabilite > Journaux" in result["message"] and "carte Achat local" in result["message"]
    experio["misplaced"] = {}
    assert journal.journal_stop(3, INVOICE) is None
    experio["misplaced"] = {"local_purchase_journal_id": "ACHAT"}
    assert journal.journal_stop(3, {**INVOICE, "folder_id": [1, "Douane"]}) is None  # the dossier's own choice


@pytest.mark.functionality
def test_in_test_mode_a_missing_journal_is_only_said(experio, monkeypatch):
    monkeypatch.setattr(journal, "dry_run", lambda: True)
    experio.update(misplaced={"local_purchase_journal_id": None}, same=[])
    result = journal.journal_stop(3, INVOICE)
    assert result["status"] == "stop" and "journal `Achats` (code `ACH1`) serait créé" in result["message"]
    assert experio["written"] == []


@pytest.mark.functionality
def test_an_fnp_invoice_is_checked_against_the_settings_journal_when_the_dossier_has_no_fnp_journal(experio):
    fnp = {**INVOICE, "is_fnp_invoice": True}
    experio["setting"] = [7, "FNP"]  # the dossier's FNP journal: Experio's own choice
    assert journal.chosen_by_dossier(fnp)
    experio["setting"] = False  # none: Experio takes the local purchase journal, which must be the dossier's own
    assert not journal.chosen_by_dossier(fnp)
    experio.update(misplaced={"local_purchase_journal_id": "ACHAT"}, stuck=True)
    assert journal.journal_stop(3, fnp)["status"] == "stop"


@pytest.mark.functionality
def test_no_account_is_looked_up_without_the_dossiers_journal(experio, monkeypatch):
    from engine.tools.invoices import accounts

    draft = {"account_id": False, "type": "purchase"}
    monkeypatch.setattr(accounts, "require_draft_invoice", lambda *a, **k: draft)
    monkeypatch.setattr(accounts, "require_invoice", lambda invoice_id, fields: INVOICE)
    experio.update(misplaced={"local_purchase_journal_id": "ACHAT"}, stuck=True)
    monkeypatch.setattr(accounts, "side_of", lambda invoice: pytest.fail("Experio is not asked for an account"))
    assert accounts.assign_account(3)["status"] == "stop"


@pytest.mark.functionality
def test_an_invoice_made_before_the_dossiers_journals_is_moved_to_them_first(experio, monkeypatch):
    from engine.tools.invoices import accounts

    moved = []
    monkeypatch.setattr(accounts, "require_draft_invoice", lambda *a, **k: {"account_id": False, "type": "purchase"})
    monkeypatch.setattr(accounts, "require_invoice", lambda invoice_id, fields: INVOICE)
    monkeypatch.setattr(accounts, "check_journal", lambda invoice_id, tell: moved.append((invoice_id, tell)))
    monkeypatch.setattr(accounts, "side_of", lambda invoice: pytest.fail("stops after the journal, before any account"))
    with pytest.raises(pytest.fail.Exception):
        accounts.assign_account(3)
    assert moved == [(3, False)]  # what Tarik set is told at the journal step, not lost here


@pytest.mark.functionality
def test_in_test_mode_the_journal_is_only_said(experio, monkeypatch):
    monkeypatch.setattr(journal, "dry_run", lambda: True)
    experio["invoice"]["journal_id"] = [2, "Autre"]
    result = journal.check_journal(3)
    assert (
        result["message"] == f"{TEST_MODE}Facture 3 : elle passerait au journal `Achats`." and experio["refreshed"] == 0
    )


@pytest.mark.security
def test_a_posted_invoice_never_changes_journal(experio):
    experio["invoice"].update(journal_id=[2, "Autre"], state="post")
    with pytest.raises(ToolError, match="seule une facture brouillon peut changer de journal"):
        journal.check_journal(3)
    assert experio["refreshed"] == 0


@pytest.mark.functionality
def test_experio_refusing_its_refresh_or_leaving_no_journal_is_a_tool_error(experio, monkeypatch):
    experio["invoice"]["journal_id"] = False
    experio["after"] = False
    with pytest.raises(ToolError, match="gardé le journal vide au lieu de `Achats`"):
        journal.check_journal(3)

    def refresh(invoice_id, context):
        raise RuntimeError("Traceback\nUserError: Journal introuvable")

    monkeypatch.setattr(journal, "refresh", refresh)
    with pytest.raises(ToolError, match="Experio n'a pas pu mettre a jour le journal"):
        journal.check_journal(3)
