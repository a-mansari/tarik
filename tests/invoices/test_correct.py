"""Corrections planned from a check (the HT/TVA case is the real Bluejay error seen on a fuel invoice)."""

import pytest

from engine.tools.invoices.check import CAPTURED
from engine.tools.invoices.correct import planned_changes
from tests.fakes import flag


@pytest.mark.functionality
def test_only_what_the_scan_shows_is_written():
    bad = [
        flag("amount_untaxed", 5000, 4545.45),
        flag("amount_tva", 0, 454.55),
        flag("tier", None, "STATION"),
        flag("ice", None, "1"),
    ]
    ex = {
        "amount_untaxed": 4545.45,
        "amount_tva": 454.55,
        "tva_lines": [{"rate": 10, "amount_ht": 4545.45, "amount_tva": 454.55}],
    }
    assert planned_changes(ex, bad, {}) == {
        "amount_untaxed": 4545.45,
        "amount_tva": 454.55,
    }  # tiers and ICE are a later step
    assert (
        planned_changes({"amount_total": None}, [flag("amount_total", 1, None)], {}) == {}
    )  # nothing read, nothing written


@pytest.mark.functionality
def test_credit_notes():
    assert planned_changes({"is_credit_note": True}, [], {})["refund"] is True
    assert "refund" not in planned_changes({"is_credit_note": "false"}, [], {})  # the model's text is not true
    assert "refund" not in planned_changes({"is_credit_note": True}, [], {"refund": True})


@pytest.mark.functionality
def test_several_tva_rates():
    multi = planned_changes(
        {
            "amount_tva": 25,
            "tva_lines": [
                {"rate": 20, "amount_ht": 100, "amount_tva": 20},
                {"rate": 10, "amount_ht": 50, "amount_tva": 5},
            ],
        },
        [],
        {},
    )
    assert multi == {"multi_tva": True, "amount_tva_20": 20, "amount_tva_10": 5, "amount_tva": 25}
    assert "multi_tva" not in planned_changes(
        {
            "tva_lines": [
                {"rate": 20, "amount_ht": 100, "amount_tva": 20},
                {"rate": 19, "amount_ht": 50, "amount_tva": 9.5},
            ]
        },
        [],
        {},
    )  # no amount_tva_19 field
    assert "multi_tva" not in planned_changes(
        {
            "tva_lines": [
                {"rate": 20, "amount_ht": 100, "amount_tva": 20},
                {"rate": 10, "amount_ht": 50, "amount_tva": 5},
            ]
        },
        [],
        {"amount_tax_non_recoverable": True},
    )


@pytest.mark.functionality
def test_lines_at_one_rate_add_up():
    lines = [
        {"rate": 20, "amount_ht": 1000, "amount_tva": 200},
        {"rate": 20, "amount_ht": 500, "amount_tva": 100},
        {"rate": 10, "amount_ht": 100, "amount_tva": 10},
    ]
    assert planned_changes({"amount_tva": 310, "tva_lines": lines}, [], {}) == {
        "multi_tva": True,
        "amount_tva_20": 300,
        "amount_tva_10": 10,
        "amount_tva": 310,
    }
    one_rate = [{"rate": 20, "amount_ht": 1000, "amount_tva": 200}, {"rate": 20, "amount_ht": 500, "amount_tva": 100}]
    assert "multi_tva" not in planned_changes({"tva_lines": one_rate}, [], {})  # one rate: no split


@pytest.mark.functionality
def test_lines_not_read_in_full_or_not_adding_up_are_never_split():
    unread = [{"rate": 20, "amount_ht": 1000, "amount_tva": None}, {"rate": 10, "amount_ht": 500, "amount_tva": 50}]
    assert "multi_tva" not in planned_changes({"amount_tva": 250, "tva_lines": unread}, [], {})
    twice = [{"rate": 20, "amount_ht": 1000, "amount_tva": 200}] * 2 + [
        {"rate": 10, "amount_ht": 500, "amount_tva": 50}
    ]
    assert "multi_tva" not in planned_changes(
        {"amount_tva": 250, "tva_lines": twice}, [], {}
    )  # 450 is not the 250 read
    assert "multi_tva" not in planned_changes({"tva_lines": twice[1:]}, [], {})  # no total read: nothing to check it by


@pytest.mark.communication
def test_a_scan_owing_nothing_keeps_what_experio_captured(monkeypatch):
    from engine.tools.invoices import correct

    written = []
    monkeypatch.setattr(correct, "fill_invoice", lambda invoice_id, changes, context: written.append(changes) or {})
    monkeypatch.setattr(correct, "invoice_context", lambda invoice: {})
    for total in (0.0, -600.0):  # a hotel's folio once paid: Total 0,00, Paiement -600, Solde -600
        check = (True, {"amount_total": total, "amount_untaxed": 0.0, "is_credit_note": True}, [])
        bad = [flag("amount_untaxed", 600, 0.0), flag("amount_total", 600, total)]
        monkeypatch.setattr(correct, "stored_check", lambda invoice_id, c=(*check[:2], bad): c)
        for captured in (0.0, False):  # Experio has no amount either: nothing to import
            monkeypatch.setattr(correct, "require_draft_invoice", lambda *a, c=captured, **k: {"amount_total": c})
            result = correct.correct_invoice(3)
            assert result["status"] == "stop" and "justificatif de paiement, pas une facture" in result["message"]
        monkeypatch.setattr(correct, "require_draft_invoice", lambda *a, **k: {"amount_total": 600.0, "refund": False})
        result = correct.correct_invoice(3)
        assert result["status"] != "stop" and "montants gardés tels que saisis par Experio" in result["message"]
    assert not written  # no amount, no credit note written over Experio's 600


@pytest.mark.functionality
def test_a_draft_stopped_for_no_amount_is_taken_up_again_when_asked():
    from engine.tools.invoices.pipeline import resumable

    assert resumable("correct_invoice", "stop", stopped=True)
    assert not resumable("correct_invoice", "stop", stopped=False)  # an automatic follow-up never retries it


@pytest.mark.functionality
def test_a_hotel_bills_parts_are_written_where_the_dossier_uses_them_the_ht_keeping_the_rest():
    from engine.tools.invoices.correct import hotel_parts

    invoice = {"amount_untaxed": 1000.0, "amount_ht_restauration": 0.0, "amount_ht_breakfast": 0.0}
    invoice.update(amount_ht_blanchisserie=0.0, should_use_ht_restauration=True, should_use_ht_breakfast=False)
    invoice["should_use_ht_blanchisserie"] = True
    read = {"amount_untaxed": 1000.0, "amount_ht_restauration": 300.0, "amount_ht_breakfast": 50.0}
    read["amount_ht_blanchisserie"] = 20.0
    changes = {}
    hotel_parts(read, invoice, changes)  # breakfast: not used by the dossier, stays in the HT
    assert changes == {"amount_ht_restauration": 300.0, "amount_ht_blanchisserie": 20.0, "amount_untaxed": 680.0}
    changes = {}
    hotel_parts({**read, "amount_ht_restauration": 1200.0}, invoice, changes)  # parts over the HT: misread, nothing
    assert changes == {}
    changes = {}
    hotel_parts(read, {**invoice, "amount_ht_restauration": 300.0}, changes)  # already split: left alone
    assert changes == {}


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("title", "unticked"),
    [("FACTURE N° 2026-14", True), ("Facture", True), ("Facture d'avoir", False), ("AVOIR", False), (None, False)],
)
def test_avoir_is_unticked_only_when_the_printed_title_says_facture(title, unticked):
    from engine.tools.invoices.correct import planned_changes

    read = {"is_credit_note": False, "document_title": title}
    changes = planned_changes(read, [], {"refund": True, "amount_tax_non_recoverable": False})
    assert (changes.get("refund") is False) == unticked
    assert "refund" not in planned_changes(read, [], {"refund": False, "amount_tax_non_recoverable": False})


DRAFT = {"id": 3, "state": "draft", "company_id": [5, "D"], "exercice_id": [2, "2026"], "refund": False}
DRAFT.update(amount_tax_non_recoverable=0.0, payment_method_id=False, currency_id=[1, "MAD"])
DRAFT.update(amount_total=1200.0, amount_untaxed=1000.0)
CONTEXT = {"allowed_company_ids": [5], "cron_fiscal_entry_id": 2}


@pytest.fixture
def correcting(monkeypatch):
    """correct_invoice on DRAFT 3, checked with ``read`` differing in ``bad``: what Experio is asked and writes."""
    from engine.tools.invoices import correct

    state = {"invoice": dict(DRAFT), "read": {"amount_untaxed": 1000.0}, "bad": [], "readable": True}
    state.update(
        calls=[], filled=[], refreshed=[], found={"res.currency": [{"id": 9}], "exp.payment.method": [{"id": 4}]}
    )
    state["changed"] = None  # what Experio says changed: the changes themselves unless set

    def odoo_call(model, method, domain, **kw):
        state["calls"].append((model, method, domain, kw))
        return state["found"][model]

    def fill_invoice(invoice_id, changes, context):
        state["filled"].append((invoice_id, changes, context))
        return dict(changes) if state["changed"] is None else state["changed"]

    monkeypatch.setattr(correct, "require_draft_invoice", lambda *a: state["invoice"])
    monkeypatch.setattr(correct, "stored_check", lambda i: (state["readable"], state["read"], state["bad"]))
    monkeypatch.setattr(correct, "odoo_call", odoo_call)
    monkeypatch.setattr(correct, "fill_invoice", fill_invoice)
    monkeypatch.setattr(
        correct, "refresh", lambda invoice_id, context: state["refreshed"].append((invoice_id, context))
    )
    monkeypatch.setattr(correct, "dry_run", lambda: False)
    return state


@pytest.mark.functionality
def test_the_differences_are_written_through_experios_form_then_its_refresh_rebuilds_the_lines(correcting):
    from engine.tools.invoices import correct

    correcting["read"] = {"amount_untaxed": 900.0, "invoice_number": "F-12"}
    correcting["bad"] = [flag("amount_untaxed", 1000, 900), flag("invoice_number", "F-1", "F-12")]
    result = correct.correct_invoice(3)
    assert correcting["filled"] == [(3, {"amount_untaxed": 900.0, "invoice_number": "F-12"}, CONTEXT)]
    assert correcting["refreshed"] == [(3, CONTEXT)] and correcting["calls"] == []  # MAD: no currency searched
    assert result == {
        "status": "ok",
        "message": "Facture 3 : corrigée (montant HT 900.0, numéro F-12).",
        "invoice_id": 3,
    }


@pytest.mark.security
def test_test_mode_says_what_the_correction_would_be(correcting, monkeypatch):
    from engine.tools.invoices import correct

    monkeypatch.setattr(correct, "dry_run", lambda: True)
    correcting["read"], correcting["bad"] = {"amount_untaxed": 900.0}, [flag("amount_untaxed", 1000, 900)]
    assert correct.correct_invoice(3)["message"] == (
        f"{correct.TEST_MODE}Facture 3, correction prévue : montant HT 900.0."
    )


@pytest.mark.functionality
def test_a_foreign_currency_experio_has_is_set_and_one_it_lacks_is_noted(correcting):
    from engine.tools.invoices import correct

    correcting["read"] = {"currency": "eur"}
    assert correct.correct_invoice(3)["message"] == "Facture 3 : corrigée (devise renseigné(e))."
    assert correcting["calls"] == [
        ("res.currency", "search_read", [("name", "=", "EUR")], {"fields": ["id"], "limit": 1})
    ]
    assert correcting["filled"][0][1] == {"currency_id": 9}
    correcting["found"]["res.currency"], correcting["filled"][:] = [], []
    result = correct.correct_invoice(3)
    assert result["routine"] and result["message"] == "Facture 3 : rien à corriger. (devise EUR inconnue dans Experio)"
    assert correcting["filled"] == [] and correcting["refreshed"] == [(3, CONTEXT)]  # refreshed once, by the first run


@pytest.mark.functionality
def test_the_payment_method_the_client_wrote_is_taken_over_the_scans(correcting):
    from engine.tools.invoices import correct

    correcting["read"] = {"payment_method": "especes"}
    correct.correct_invoice(3, payment_method="cheque", client_message="Payée par cheque")
    assert correcting["calls"][0][2] == [("name", "=ilike", "Ch%")] and correcting["filled"][0][1] == {
        "payment_method_id": 4
    }
    correcting["calls"][:] = []
    correct.correct_invoice(3, payment_method="cheque", client_message="Voici la facture")  # not written: the scan's
    assert correcting["calls"][0][2] == [("name", "=ilike", "Esp%")]


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("scan", "invoice_has", "found", "searched"),
    [("especes", [2, "Esp"], [{"id": 4}], False), ("bitcoin", False, [{"id": 4}], False), ("cheque", False, [], True)],
)
def test_no_payment_method_is_written_over_one_set_unknown_or_missing_in_experio(
    correcting, scan, invoice_has, found, searched
):
    from engine.tools.invoices import correct

    correcting["read"], correcting["invoice"]["payment_method_id"] = {"payment_method": scan}, invoice_has
    correcting["found"]["exp.payment.method"] = found
    assert correct.correct_invoice(3)["message"] == "Facture 3 : rien à corriger."
    assert bool(correcting["calls"]) is searched and correcting["filled"] == []


@pytest.mark.functionality
def test_a_correction_experio_already_holds_changes_nothing(correcting):
    from engine.tools.invoices import correct

    correcting["read"], correcting["bad"] = {"amount_untaxed": 900.0}, [flag("amount_untaxed", 1000, 900)]
    correcting["changed"] = {}
    assert correct.correct_invoice(3)["message"] == "Facture 3 : déjà a jour, rien à changer."
    assert correcting["refreshed"] == []


@pytest.mark.communication
def test_what_experio_recomputed_by_itself_is_logged_not_told(correcting, capsys):
    from engine.tools.invoices import correct

    correcting["read"] = {"currency": "USD"}
    correcting["changed"] = {"currency_id": 9, "exchange_rate": 10.1}
    assert correct.correct_invoice(3)["message"] == "Facture 3 : corrigée (devise renseigné(e))."
    assert "Experio also updated exchange_rate" in capsys.readouterr().out


@pytest.mark.communication
@pytest.mark.parametrize("failing", ["fill_invoice", "refresh", "odoo_call"])
def test_a_correction_experio_refuses_is_said_refused(correcting, monkeypatch, failing):
    from engine.tools.core.app import ToolError
    from engine.tools.invoices import correct

    def refuse(*args, **kwargs):
        raise RuntimeError("ValidationError: exercice clôturé")

    monkeypatch.setattr(correct, failing, refuse)
    correcting["read"] = {"amount_untaxed": 900.0, "currency": "EUR"}
    correcting["bad"] = [flag("amount_untaxed", 1000, 900)]
    with pytest.raises(ToolError, match="Facture 3 : Experio a refusé la correction."):
        correct.correct_invoice(3)


@pytest.mark.functionality
def test_an_unchecked_or_unreadable_invoice_is_not_corrected(correcting, monkeypatch):
    from engine.tools.core.app import ToolError
    from engine.tools.invoices import correct

    correcting["readable"] = False
    assert correct.correct_invoice(3)["message"] == "Facture 3 : scan illisible, rien à corriger."
    monkeypatch.setattr(correct, "stored_check", lambda i: None)
    with pytest.raises(ToolError, match="appelez d'abord check_invoice"):
        correct.correct_invoice(3)
    assert correcting["filled"] == [] and correcting["calls"] == []


@pytest.mark.communication
def test_each_corrected_field_is_said_as_the_client_reads_it():
    from engine.tools.invoices.correct import change_text

    assert change_text("multi_tva", True) is None
    assert change_text("refund", True) == "avoir" and change_text("refund", False) == "facture, pas un avoir"
    assert change_text("amount_tva_20", 40.0) == "TVA 20 % 40.0"
    assert change_text("payment_method_id", 4) == "mode de paiement renseigné(e)"


@pytest.mark.communication
def test_every_header_amount_correct_invoice_writes_is_said_in_french():
    from engine.tools.invoices.correct import change_text

    assert [field for field in CAPTURED if field in change_text(field, 2.0)] == []  # amount_stamp, amount_ts...
