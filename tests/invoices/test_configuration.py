"""The dossier's settings before posting: Experio's own tax creation and auto-detection, then what still blocks."""

import pytest

from engine.tools.core import experio as experio_calls
from engine.tools.invoices import configuration


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: what its check says before and after its own detection; every call made."""
    state = {
        "before": [("cgnc", "<p>The tax with the 20 rate does not exist</p>")],
        "after": [],
        "calls": [],
        "taxes": 2,
    }
    invoice = {"state": "draft", "company_id": [5, "D"], "exercice_id": False, "type": "purchase"}
    invoice["is_local_invoice"] = True
    state["invoice"] = {**invoice, **dict.fromkeys(configuration.AMOUNTS, 0)}
    state["company"] = {}  # the dossier's switches and accounts: empty unless set
    monkeypatch.setattr(configuration, "require_invoice", lambda i, f: state["invoice"])
    monkeypatch.setattr(configuration, "assets_without_category", lambda i, context: state.get("assets", []))
    monkeypatch.setattr(configuration, "settle_folder_number", lambda i, context: state.get("numbered", ""))
    monkeypatch.setattr(configuration, "dry_run", lambda: False)

    def odoo_json(model, method, ids, **kw):
        state["calls"].append((model, method))
        if method == "collect_validation_messages":
            return state["after"] if "auto_detect_settings" in [c[1] for c in state["calls"]] else state["before"]
        if method == "create_tax_ids":
            state["taxes"] += 3

    def odoo_call(model, method, *a, **k):
        state["calls"].append((model, method))
        if method == "search_count":
            return state["taxes"]
        if model == "res.company":  # read: what the dossier holds
            return [{f: state["company"].get(f, False) for f in k["fields"]}]
        if model == "res.config.settings":  # the Configuration form, written as the dossier's
            state["company"].update(a[0])
            return 9
        if method == "search_read":  # an account by its code prefix: only 4413 exists
            return [{"id": 413}] if a[0][1][2] == "4413%" else []
        return [31, 32]

    monkeypatch.setattr(configuration, "odoo_json", odoo_json)
    monkeypatch.setattr(experio_calls, "odoo_json", odoo_json)  # its check, shared with posting
    monkeypatch.setattr(configuration, "odoo_call", odoo_call)
    return state


@pytest.mark.functionality
def test_experios_own_detection_runs_only_when_its_check_blocks(experio):
    result = configuration.configure_cabinet(3)
    assert [c[1] for c in experio["calls"]].count("create_tax_ids") == 1
    assert ("res.company", "auto_detect_settings") in experio["calls"]
    assert result["message"] == (
        "Facture 3 : détection automatique des paramètres du dossier lancée, 3 taxe(s) créée(s) : plus rien ne bloque "
        "la comptabilisation."
    )
    experio["before"], experio["calls"] = [], []
    assert configuration.configure_cabinet(3)["routine"] and len(experio["calls"]) == 1  # nothing missing: one read


@pytest.mark.communication
def test_what_still_blocks_stops_the_invoice_the_dossier_s_settings_apart_from_the_invoice_s_own(experio):
    experio["after"] = [
        ("cgnc", "<p>The configuration of <b>local supplier journal</b> is not defined.</p>"),
        ("error", "Le montant TTC de la facture doit être strictement positif."),
    ]
    result = configuration.configure_cabinet(3)
    assert result["status"] == "stop"
    assert result["message"].endswith(
        "Pas comptabilisable en l'état. À régler par le cabinet dans la Configuration du dossier : The configuration "
        "of local supplier journal is not defined. Sur la facture elle-même, Experio signale : Le montant TTC de la "
        "facture doit être strictement positif."
    )


@pytest.mark.functionality
def test_a_warning_is_no_setting_and_blocks_nothing_here(experio):
    # Experio posts despite its warnings (action_post raises on settings and errors only): settle_warnings' own.
    experio["after"] = [("warning", "<p>Date d'echeance depassee</p>")]
    result = configuration.configure_cabinet(3)
    assert result["status"] == "ok" and result["message"].endswith("plus rien ne bloque la comptabilisation.")
    experio["before"], experio["calls"] = [("warning", "<p>Prorata</p>")], []
    assert configuration.configure_cabinet(3)["routine"]  # nothing blocks: no detection run at all
    assert ("res.company", "auto_detect_settings") not in experio["calls"]


@pytest.mark.functionality
def test_an_amount_the_invoice_carries_turns_its_switch_on_with_its_account(experio):
    experio["invoice"].update(amount_guarantee_holdback=500, amount_prorata=20, amount_stamp=1)
    result = configuration.configure_cabinet(3)
    assert experio["company"] == {
        "use_supplier_guarantee_holdback": True,
        "supplier_guarantee_holdback_account_id": 413,
    }
    assert "retenue de garantie active(s) dans le dossier" in result["message"]
    # no 6149 account for the prorata, no 6167 for the stamp: the cabinet's, named
    assert result["message"].endswith(
        "À régler par le cabinet dans la Configuration du dossier : prorata : compte à choisir ; "
        "timbre : compte à choisir."
    )
    experio["calls"].clear()
    assert "active(s)" not in configuration.configure_cabinet(3)["message"]  # set now: nothing written again
    assert ("res.config.settings", "create") not in experio["calls"]


@pytest.mark.functionality
def test_experio_ignores_a_foreign_purchases_deductions_so_nothing_is_set(experio):
    experio["invoice"].update(amount_guarantee_holdback=500, amount_stamp=1, is_local_invoice=False)
    configuration.configure_cabinet(3)
    assert experio["company"] == {}


@pytest.mark.security
def test_a_setting_the_cabinets_rights_refuse_is_reported_not_claimed(experio, monkeypatch):
    experio["invoice"].update(amount_guarantee_holdback=500)
    monkeypatch.setattr(configuration, "save_settings", lambda *a: False)
    result = configuration.configure_cabinet(3)
    assert "active(s)" not in result["message"] and "retenue de garantie : compte à choisir" in result["message"]


@pytest.mark.functionality
def test_an_immobilisation_without_its_category_is_the_cabinets_setting(experio):
    experio["before"], experio["assets"] = [], ["23550000 Matériel informatique"]
    result = configuration.configure_cabinet(3)
    assert (
        result["status"] == "stop" and "catégorie d'immobilisation à créer pour le compte 23550000" in result["message"]
    )


@pytest.mark.functionality
def test_asset_lines_are_read_only_where_experio_has_its_immobilisations(monkeypatch):
    calls = []

    def odoo_call(model, method, *args, **kw):
        calls.append((model, method))
        if method == "fields_get":
            return {}  # no immobilisations module in this cabinet
        raise AssertionError("nothing else read")

    monkeypatch.setattr(configuration, "odoo_call", odoo_call)
    assert configuration.assets_without_category(3, {}) == [] and calls == [("exp.invoice", "fields_get")]
