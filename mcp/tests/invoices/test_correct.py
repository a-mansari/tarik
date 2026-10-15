"""Corrections planned from a check (the HT/TVA case is the real Bluejay error seen on a fuel invoice)."""

from tarik.invoices.correct import planned_changes


def flag(field, stored_value, read_value):
    return {"field": field, "stored": str(stored_value), "extracted": str(read_value), "problem": "x"}


def test_only_what_the_scan_shows_is_written():
    bad = [flag("amount_untaxed", 5000, 4545.45), flag("amount_tva", 0, 454.55), flag("tier", None, "STATION"), flag("ice", None, "1")]
    ex = {"amount_untaxed": 4545.45, "amount_tva": 454.55, "tva_lines": [{"rate": 10, "amount_ht": 4545.45, "amount_tva": 454.55}]}
    assert planned_changes(ex, bad, {}) == {"amount_untaxed": 4545.45, "amount_tva": 454.55}  # tiers and ICE are a later step
    assert planned_changes({"amount_total": None}, [flag("amount_total", 1, None)], {}) == {}  # nothing read, nothing written


def test_credit_notes():
    assert planned_changes({"is_credit_note": True}, [], {})["refund"] is True
    assert "refund" not in planned_changes({"is_credit_note": True}, [], {"refund": True})


def test_several_tva_rates():
    multi = planned_changes({"tva_lines": [{"rate": 20, "amount_ht": 100, "amount_tva": 20}, {"rate": 10, "amount_ht": 50, "amount_tva": 5}]}, [], {})
    assert multi == {"multi_tva": True, "amount_tva_20": 20, "amount_tva_10": 5, "amount_tva": 25}
    assert "multi_tva" not in planned_changes({"tva_lines": [{"rate": 20, "amount_ht": 100, "amount_tva": 20},
                                                             {"rate": 19, "amount_ht": 50, "amount_tva": 9.5}]}, [], {})  # no amount_tva_19 field
    assert "multi_tva" not in planned_changes({"tva_lines": [{"rate": 20, "amount_ht": 100, "amount_tva": 20},
                                                             {"rate": 10, "amount_ht": 50, "amount_tva": 5}]}, [], {"amount_tax_non_recoverable": True})
