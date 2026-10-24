"""Corrections planned from a check (the HT/TVA case is the real Bluejay error seen on a fuel invoice)."""

import pytest

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
