"""Experio's warnings, each handled on its own: fixed, waited out, dismissed or asked (docs/experio-warnings.md)."""

import pytest

from engine.tools.invoices import warnings


@pytest.fixture
def experio(monkeypatch):
    """A fake Experio: which of its checks warn (they stop once fixed), the invoice, the dossier, every write."""
    state = {"warn": {}, "written": [], "dismissed": [], "company": {"name": "D", "ice": "", "vat": "", "id": 5}}
    state["invoice"] = {
        "id": 3,
        "state": "draft",
        "type": "purchase",
        "company_id": [5, "D"],
        "exercice_id": [9, "2026"],
        "partner_id": [7, "T"],
        "invoice_date": "2026-10-15",
        "is_local_invoice": True,
        "amount_tax_non_recoverable": False,
        "amount_untaxed": 100.0,
        "amount_tva": 20.0,
        "retenue": True,
        "local_withholding_percentage": False,
        "tax_withholding_percentage": "75",
        "detected_supplier_ice": False,
        "detected_supplier_if": False,
        "detected_customer_ice": False,
        "detected_customer_if": False,
        "dismissed_warning_codes": False,
    }
    state["company"]["prorata_rate"] = 40
    monkeypatch.setattr(warnings, "require_invoice", lambda i, f: dict(state["invoice"]))
    monkeypatch.setattr(warnings, "warns", lambda i, check, context: state["warn"].get(check))
    monkeypatch.setattr(warnings, "dry_run", lambda: False)
    monkeypatch.setattr(warnings, "refresh", lambda i, context: None)
    monkeypatch.setattr(warnings, "kept_yes_no", lambda i, step, answer: (answer, state.get("kept")))

    def fill_invoice(invoice_id, changes, context):
        state["written"].append(changes)
        if "retenue" in changes:  # Experio's onchange sets the rate its rules give
            state["warn"].pop("validate_warning_withholding", None)
            return {"tax_withholding_percentage": "100"}
        if "tax_prorata" in changes:
            state["warn"].pop("validate_warning_use_prorata", None)
        return dict(changes)

    def odoo_call(model, method, ids, *args, **kw):
        if model == "res.company" and method == "read":
            return [state["company"]]
        if model == "res.company":
            return state.get("others", [])
        if model == "exp.partner":
            return [{"type": state.get("tiers_type", "company")}]
        if method == "write":
            state["invoice"]["warning_code"] = args[0]["warning_code"]
        return True

    def odoo_json(model, method, ids, context):
        code = state["invoice"].pop("warning_code")
        state["dismissed"].append(code)
        state["invoice"]["dismissed_warning_codes"] = ",".join(state["dismissed"])

    monkeypatch.setattr(warnings, "fill_invoice", fill_invoice)
    monkeypatch.setattr(warnings, "odoo_call", odoo_call)
    monkeypatch.setattr(warnings, "odoo_json", odoo_json)
    return state


@pytest.mark.functionality
def test_no_warning_is_routine(experio):
    assert warnings.settle_warnings(3)["routine"]


@pytest.mark.functionality
def test_the_ras_tva_rate_is_set_by_experios_own_rules_never_typed(experio):
    experio["warn"]["validate_warning_withholding"] = "Pensez à retenir 100 % de la RAS TVA."
    result = warnings.settle_warnings(3)
    assert result["status"] == "ok" and "RAS TVA mise à 100 %" in result["message"]
    assert experio["written"] == [{"retenue": True}]  # typed as on screen: Experio's onchange decides the rate


@pytest.mark.functionality
def test_prorata_is_ticked_where_the_form_has_it_and_dismissed_where_it_has_none(experio):
    experio["warn"]["validate_warning_use_prorata"] = "Le document utilise un prorata (40 %)."
    assert "prorata de TVA du dossier appliqué (40 %)" in warnings.settle_warnings(3)["message"]
    assert experio["written"] == [{"tax_prorata": True}] and experio["dismissed"] == []
    # Non-recoverable TVA: the form shows no Prorata box (views 786-789), the warning cannot apply.
    experio["written"].clear()
    experio["invoice"]["amount_tax_non_recoverable"] = True
    experio["warn"]["validate_warning_use_prorata"] = "Le document utilise un prorata (40 %)."
    result = warnings.settle_warnings(3)
    assert "avertissement de prorata levé" in result["message"] and experio["written"] == []
    assert experio["dismissed"] == ["prorata_vat_rate_not_applied_on_invoice"]
    experio["written"].clear()  # dismissed on this invoice: never handled again, though Experio's check still says it
    assert warnings.settle_warnings(3)["routine"] and experio["written"] == []


@pytest.mark.functionality
def test_an_exchange_rate_not_final_waits(experio):
    experio["warn"]["validate_end_of_month_exchange_rate"] = "Le taux moyen pourrait changer."
    result = warnings.settle_warnings(3)
    assert result["status"] == "wait" and "après le 31/10/2026" in result["message"]
    experio["warn"] = {"validate_warning_today_exchange_rate": "Le taux du jour n'est pas disponible."}
    assert warnings.settle_warnings(3)["status"] == "wait"


@pytest.mark.functionality
def test_a_sale_read_as_a_purchase_stops_and_another_buyer_is_asked_once(experio):
    experio["company"].update(ice="001111", vat="22")
    experio["warn"]["validate_warning_invalid_ice_and_if"] = "Facture de vente importée comme achat ?"
    experio["invoice"]["detected_supplier_ice"] = "001111"  # the issuer read is the dossier itself
    result = warnings.settle_warnings(3)
    assert result["status"] == "stop" and "facture de vente" in result["message"]
    experio["invoice"].update(detected_supplier_ice=False, detected_customer_ice="009999")  # the buyer: another one
    experio["others"] = [{"name": "AUTRE SARL"}]
    asked = warnings.settle_warnings(3)
    assert asked["status"] == "question" and "C'est l'ICE du dossier AUTRE SARL." in asked["message"]
    experio["kept"] = True  # the client said it is for this dossier: dismissed, never asked again
    assert "gardée dans ce dossier" in warnings.settle_warnings(3)["message"]
    assert experio["dismissed"] == ["detected_ice_mismatch_with_company_record"]
    experio["kept"] = False
    experio["invoice"]["dismissed_warning_codes"] = False
    assert warnings.settle_warnings(3)["status"] == "stop"


@pytest.mark.functionality
def test_an_is_ir_rate_experio_refuses_for_the_tiers_type_is_asked_and_answered(experio, monkeypatch):
    experio["invoice"]["local_withholding_percentage"] = "15"
    experio["warn"]["validate_warning_local_withholding"] = "Personne morale : 5, 10, 20, 30 %."
    asked = warnings.settle_warnings(3)
    assert asked["status"] == "question" and "5, 10, 20, 30 %" in asked["message"]
    assert warnings.answer_warnings(3, "mettez 25")["message"].startswith("Réponse non comprise")
    experio["written"].clear()

    def fixed(invoice_id, changes, context):
        experio["written"].append(changes)
        experio["warn"].clear()
        return changes

    monkeypatch.setattr(warnings, "fill_invoice", fixed)
    assert warnings.answer_warnings(3, "10 %")["routine"]
    assert experio["written"] == [{"local_withholding_percentage": "10"}]


@pytest.mark.functionality
def test_a_warning_still_there_after_its_fix_stops_with_experios_words(experio, monkeypatch):
    experio["warn"]["validate_warning_withholding"] = "Montant TVA manquant."
    monkeypatch.setattr(warnings, "fill_invoice", lambda i, changes, context: {})  # the fix changed nothing
    result = warnings.settle_warnings(3)
    assert result["status"] == "stop" and result["message"].endswith("Experio signale toujours : Montant TVA manquant.")


@pytest.mark.functionality
def test_what_was_fixed_is_told_before_what_stops(experio):
    experio["warn"]["validate_warning_withholding"] = "RAS TVA."
    experio["warn"]["validate_folder_dum_detection"] = "Taux de la DUM introuvable."
    result = warnings.settle_warnings(3)
    assert result["status"] == "stop"
    assert result["message"].startswith("Facture 3 : RAS TVA mise à 100 %")
    assert "À régler par le cabinet (dossier d'import)." in result["message"]


@pytest.mark.functionality
def test_after_relinking_its_exercice_the_invoice_is_checked_against_the_new_one(experio, monkeypatch):
    """Experio checks the date against the exercice the context selects: the invoice's own, once relinked."""
    experio["invoice"]["exercice_id"] = False

    def warns(invoice_id, check, context):
        wrong = context.get("cron_fiscal_entry_id") != 778
        return "Date hors exercice." if check == "validate_warning_previous_charges" and wrong else None

    def relinked(invoice_id, answer="", decision=""):
        experio["invoice"]["exercice_id"] = [778, "Exercice 2025"]
        return {"status": "ok", "message": "Facture 3 : rattachée à l'exercice Exercice 2025."}

    monkeypatch.setattr(warnings, "warns", warns)
    monkeypatch.setattr(warnings, "settle_exercice", relinked)
    monkeypatch.setattr(
        warnings, "invoice_context", lambda invoice: {"cron_fiscal_entry_id": (invoice["exercice_id"] or [0])[0]}
    )
    result = warnings.settle_warnings(3)
    assert result["status"] == "ok" and "rattachée à l'exercice Exercice 2025" in result["message"]
