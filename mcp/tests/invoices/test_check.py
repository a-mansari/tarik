"""Does what Experio read match the scan: dates, the model's report, false alarms."""

import pytest

from tarik.invoices.check import drop_false_alarms, resolve_date, resolve_report


def flag(field, stored_value, read_value):
    return {"field": field, "stored": str(stored_value), "extracted": str(read_value), "problem": "x"}


def test_dates_follow_the_invoice_language():  # decided in code (tarik-master ocr.py)
    assert resolve_date("07/08/2025", "fr") == "2025-08-07"
    assert resolve_date("07/08/2025", "en") == "2025-07-08"
    assert resolve_date("LE : 03.09.26", "fr") == "2026-09-03"
    assert resolve_date("2025-08-07", "fr") == "2025-08-07"
    assert resolve_date("31/02/2025", "fr") is None and resolve_date("", "fr") is None and resolve_date(None, "fr") is None


def test_the_report_keeps_real_differences_only():
    stored = {"invoice_date": "2025-08-07", "amount_total": 100.0}
    report = resolve_report({"readable": True, "extracted": {"invoice_date_raw": "08/07/2025", "invoice_date_language": "fr"},
                             "bad_fields": [{"field": "invoice_date", "stored": "x", "extracted": "y", "problem": "z"},
                                            {"field": "amount_total", "stored": "100", "extracted": "120", "problem": "Odoo a 100"},
                                            {"field": "invoice_date_raw", "stored": "x", "extracted": "y", "problem": "never compared"}]}, stored)
    assert "Odoo" not in str(report["bad_fields"])  # explanations reach the client
    assert [b["field"] for b in report["bad_fields"]] == ["amount_total", "invoice_date"]  # the model's own date entry is replaced
    assert report["extracted"]["invoice_date"] == "2025-07-08"
    same = resolve_report({"readable": True, "extracted": {"invoice_date_raw": "07/08/2025", "invoice_date_language": "fr"}}, stored)
    assert same["bad_fields"] == []
    with pytest.raises(ValueError):  # no language: refused, as ocr.py's schema does
        resolve_report({"readable": True, "extracted": {}}, stored)


def test_false_alarms_are_dropped():  # measured on real invoices: a real Bluejay error, then model noise
    real = drop_false_alarms([flag("amount_untaxed", 5000, 4545.45), flag("amount_tva", 0, 454.55)],
                             {"amount_untaxed": 5000.0, "amount_tva": 0.0},
                             {"amount_untaxed": 4545.45, "amount_tva": 454.55, "amount_total": 5000.0})
    assert [b["field"] for b in real] == ["amount_untaxed", "amount_tva"]  # its own figures add up: kept
    noise = drop_false_alarms([flag("amount_untaxed", 1970.81, 1884.73)], {"amount_untaxed": 1970.81},
                              {"amount_untaxed": 1884.73, "amount_tva": 394.17, "amount_stamp": 5.91, "amount_total": 2370.89})
    assert noise == []  # 1884.73 + 394.17 + 5.91 != 2370.89: the model misread
    assert drop_false_alarms([flag("ice", "001234567000089", "001234567000089")], {"ice": "001234567000089"}, {"ice": "001234567000089"}) == []
    assert drop_false_alarms([flag("tier", "Société Générale Services Atlas Nord - 12345678", "x")],
                             {"tier": "Société Générale Services Atlas Nord - 12345678"},
                             {"tier": "Société Générale Services Atlas-Nord SA"}) == []
    assert len(drop_false_alarms([flag("tier", None, "ALPHA BUREAU")], {"tier": None}, {"tier": "ALPHA BUREAU"})) == 1  # missing: kept
    assert len(drop_false_alarms([flag("tier", "BETA TRAVAUX - 123", "ALPHA BUREAU")], {"tier": "BETA TRAVAUX - 123"},
                                 {"tier": "ALPHA BUREAU"})) == 1
