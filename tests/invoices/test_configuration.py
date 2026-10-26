"""The dossier's settings before posting: Experio's own tax creation and auto-detection, then what still blocks."""

import pytest

from engine.tools.core import experio as experio_calls
from engine.tools.core.app import ToolError
from engine.tools.core.config import TEST_MODE
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


@pytest.mark.functionality
def test_asset_lines_without_their_category_are_named_by_their_account(monkeypatch):
    lines = [
        {"account_id": [1, "23550000 Matériel informatique"], "asset_category_id": False},
        {"account_id": [2, "23400000 Matériel de transport"], "asset_category_id": [3, "Véhicules"]},
        {"account_id": False, "asset_category_id": False},
    ]
    state = {"ids": [11, 12, 13], "read": []}

    def odoo_call(model, method, *args, **kw):
        state["read"].append(model)
        if method == "fields_get":
            return {"invoice_asset_line_ids": {"type": "one2many"}}
        if model == "exp.invoice":
            return [{"invoice_asset_line_ids": state["ids"]}]
        return lines

    monkeypatch.setattr(configuration, "odoo_call", odoo_call)
    assert configuration.assets_without_category(3, {}) == ["23550000 Matériel informatique"]
    state.update(ids=[], read=[])  # no asset line: nothing more read
    assert configuration.assets_without_category(3, {}) == [] and "exp.invoice.asset.line" not in state["read"]


@pytest.mark.functionality
def test_a_posted_invoice_has_nothing_to_set(experio):
    experio["invoice"]["state"] = "post"
    assert configuration.configure_cabinet(3)["routine"] and experio["calls"] == []


@pytest.mark.communication
def test_a_folder_number_given_is_said_when_nothing_blocks(experio):
    experio["before"], experio["numbered"] = [], "numéro du dossier d'import : DUM-12"
    result = configuration.configure_cabinet(3)
    assert result["status"] == "ok" and result["message"] == "Facture 3 : numéro du dossier d'import : DUM-12."


@pytest.mark.functionality
def test_in_test_mode_the_detection_is_only_said(experio, monkeypatch):
    monkeypatch.setattr(configuration, "dry_run", lambda: True)
    result = configuration.configure_cabinet(3)
    assert result["status"] == "ok" and result["message"].startswith(TEST_MODE)
    assert "The tax with the 20 rate does not exist" in result["message"]
    assert [call[1] for call in experio["calls"]] == ["collect_validation_messages"]


@pytest.mark.functionality
def test_an_immobilisation_still_without_its_category_after_the_detection_stops(experio):
    experio["assets"] = ["23550000 Matériel informatique"]
    result = configuration.configure_cabinet(3)
    assert ("res.company", "auto_detect_settings") in experio["calls"]
    assert (
        result["status"] == "stop" and "catégorie d'immobilisation à créer pour le compte 23550000" in result["message"]
    )


@pytest.mark.functionality
@pytest.mark.parametrize(
    "failing, message",
    [("collect_validation_messages", "n'a pas pu vérifier la facture"), ("auto_detect_settings", "a refusé le paramé")],
)
def test_experio_failing_its_check_or_its_detection_is_a_tool_error(experio, monkeypatch, failing, message):
    fake = configuration.odoo_json

    def odoo_json(model, method, ids, **kw):
        if method == failing:
            raise RuntimeError("Traceback\nAccessError: Droits insuffisants")
        return fake(model, method, ids, **kw)

    monkeypatch.setattr(experio_calls, "odoo_json", odoo_json)
    monkeypatch.setattr(configuration, "odoo_json", odoo_json)
    with pytest.raises(ToolError, match=message):
        configuration.configure_cabinet(3)


@pytest.fixture
def folder(monkeypatch):
    """A fake Experio: the invoice's import folder, the dossier's numbering rule, the folder's invoices, every write."""
    state = {"folder_id": [40, "DI-40"], "number": False, "rule": "commercial_invoice", "found": "FC-77", "writes": []}
    monkeypatch.setattr(configuration, "dry_run", lambda: False)

    def odoo_call(model, method, *args, **kw):
        if model == "exp.import.folder" and method == "write":
            state["writes"].append((args[0], args[1]))
            return True
        if model == "exp.import.folder":
            return [{"id": 40, "folder_number": state["number"], "folder_type": "import", "company_id": [5, "D"]}]
        if model == "res.company":
            return [{"import_folder_number": state["rule"]}]
        if method == "search_read":
            assert ("invoice_type", "=", "supplier") in args[0] and ("state", "!=", "cancel") in args[0]
            return [{"invoice_number": state["found"]}] if state["found"] is not None else []
        return [{"folder_id": state["folder_id"]}]

    monkeypatch.setattr(configuration, "odoo_call", odoo_call)
    return state


@pytest.mark.functionality
def test_a_folder_without_its_number_takes_the_one_experios_rule_gives(folder):
    assert configuration.settle_folder_number(3, {}) == "numéro du dossier d'import : FC-77"
    assert folder["writes"] == [([40], {"folder_number": "FC-77"})]


@pytest.mark.functionality
@pytest.mark.parametrize(
    "change",
    [
        {"folder_id": False},  # no import folder
        {"number": "DI-2026-1"},  # numbered already
        {"rule": "manual"},  # the dossier numbers its folders by hand
        {"found": None},  # no invoice of that kind in the folder
        {"found": False},  # its invoice has no number yet
    ],
)
def test_a_folder_number_is_written_only_when_experios_rule_gives_one(folder, change):
    folder.update(change)
    assert configuration.settle_folder_number(3, {}) == "" and folder["writes"] == []


@pytest.mark.functionality
def test_in_test_mode_no_folder_number_is_written(folder, monkeypatch):
    monkeypatch.setattr(configuration, "dry_run", lambda: True)
    assert configuration.settle_folder_number(3, {}) == "" and folder["writes"] == []
