"""Does what Experio read match the scan: dates, the model's report, false alarms."""

from pathlib import Path

import pytest

from engine.tools.core.model import json_answer
from engine.tools.core.words import readings
from engine.tools.invoices.check import REPORT_KEYS, as_number, drop_false_alarms, resolve_date, resolve_report
from tests.fakes import flag


@pytest.mark.functionality
def test_dates_follow_the_invoice_language():  # decided in code, not by the model
    assert resolve_date("07/08/2025", "fr") == "2025-08-07"
    assert resolve_date("07/08/2025", "en") == "2025-07-08"
    assert resolve_date("LE : 03.09.26", "fr") == "2026-09-03"
    assert resolve_date("2025-08-07", "fr") == "2025-08-07"
    assert (
        resolve_date("31/02/2025", "fr") is None and resolve_date("", "fr") is None and resolve_date(None, "fr") is None
    )


@pytest.mark.functionality
def test_the_report_keeps_real_differences_only():
    stored = {"invoice_date": "2025-08-07", "amount_total": 100.0}
    report = resolve_report(
        {
            "readable": True,
            "extracted": {"invoice_date_raw": "08/07/2025", "invoice_date_language": "fr"},
            "bad_fields": [
                {"field": "invoice_date", "stored": "x", "extracted": "y", "problem": "z"},
                {"field": "amount_total", "stored": "100", "extracted": "120", "problem": "Odoo a 100"},
                {"field": "invoice_date_raw", "stored": "x", "extracted": "y", "problem": "never compared"},
            ],
        },
        stored,
    )
    assert "Odoo" not in str(report["bad_fields"])  # explanations reach the client
    assert [b["field"] for b in report["bad_fields"]] == [
        "amount_total",
        "invoice_date",
    ]  # the model's own date entry is replaced
    assert report["extracted"]["invoice_date"] == "2025-07-08"
    same = resolve_report(
        {"readable": True, "extracted": {"invoice_date_raw": "07/08/2025", "invoice_date_language": "fr"}}, stored
    )
    assert same["bad_fields"] == []
    with pytest.raises(ValueError):  # no language: refused, the check's prompt requires one
        resolve_report({"readable": True, "extracted": {}}, stored)


@pytest.mark.functionality
def test_false_alarms_are_dropped():  # measured on real invoices: a real Bluejay error, then model noise
    real = drop_false_alarms(
        [flag("amount_untaxed", 5000, 4545.45), flag("amount_tva", 0, 454.55)],
        {"amount_untaxed": 5000.0, "amount_tva": 0.0},
        {"amount_untaxed": 4545.45, "amount_tva": 454.55, "amount_total": 5000.0},
    )
    assert [b["field"] for b in real] == ["amount_untaxed", "amount_tva"]  # its own figures add up: kept
    noise = drop_false_alarms(
        [flag("amount_untaxed", 1970.81, 1884.73)],
        {"amount_untaxed": 1970.81},
        {"amount_untaxed": 1884.73, "amount_tva": 394.17, "amount_stamp": 5.91, "amount_total": 2370.89},
    )
    assert noise == []  # 1884.73 + 394.17 + 5.91 != 2370.89: the model misread
    # The HU bill (electricity and water, their other taxes in no field): its parts read add up to 2459.71, not 2545.79.
    hu = {"amount_untaxed": 2044.29, "amount_tva": 409.07, "amount_stamp": 6.35, "amount_total": 2545.79}
    assert len(drop_false_alarms([flag("amount_total", 2645.79, 2545.79)], {"amount_total": 2645.79}, hu)) == 1
    assert drop_false_alarms([flag("amount_total", 2459.71, 2545.79)], {"amount_total": 2459.71}, hu) == []
    assert (
        drop_false_alarms(
            [flag("ice", "001234567000089", "001234567000089")], {"ice": "001234567000089"}, {"ice": "001234567000089"}
        )
        == []
    )
    assert (
        drop_false_alarms(
            [flag("tier", "Société Générale Services Atlas Nord - 12345678", "x")],
            {"tier": "Société Générale Services Atlas Nord - 12345678"},
            {"tier": "Société Générale Services Atlas-Nord SA"},
        )
        == []
    )
    assert (
        len(drop_false_alarms([flag("tier", None, "ALPHA BUREAU")], {"tier": None}, {"tier": "ALPHA BUREAU"})) == 1
    )  # missing: kept
    assert (
        len(
            drop_false_alarms(
                [flag("tier", "BETA TRAVAUX - 123", "ALPHA BUREAU")],
                {"tier": "BETA TRAVAUX - 123"},
                {"tier": "ALPHA BUREAU"},
            )
        )
        == 1
    )


@pytest.mark.functionality
def test_amounts_are_compared_as_numbers():
    moved = drop_false_alarms(
        [flag("amount_untaxed", 100.5, 1005)], {"amount_untaxed": 100.5}, {"amount_untaxed": 1005.0}
    )
    assert len(moved) == 1  # a misplaced decimal point: "1005" both as text, a real difference
    assert (
        drop_false_alarms([flag("amount_total", 5000.0, 5000)], {"amount_total": 5000.0}, {"amount_total": 5000.0})
        == []
    )
    assert (
        drop_false_alarms([flag("amount_tva", 454.55, 454.554)], {"amount_tva": 454.55}, {"amount_tva": 454.554}) == []
    )


@pytest.mark.technical
def test_the_models_numbers_are_read_as_numbers_or_refused():
    assert [as_number(v) for v in ("454,55", "1 005,50", "1.005,50", "1,005.50", "20 %", "120 DH", "12.500.000")] == [
        454.55,
        1005.5,
        1005.5,
        1005.5,
        20.0,
        120.0,
        12500000.0,
    ]
    assert as_number(None) is None and as_number(7) == 7
    for bad in ("abc", "", "12.5.6", True):
        with pytest.raises(ValueError):
            as_number(bad)
    report = resolve_report(
        {
            "readable": True,
            "bad_fields": [],
            "extracted": {
                "invoice_date_language": "fr",
                "amount_tva": "454,55",
                "tva_lines": [{"rate": "20%", "amount_ht": "2 272,75", "amount_tva": "454,55"}],
            },
        },
        {},
    )
    assert report["extracted"]["amount_tva"] == 454.55 and report["extracted"]["tva_lines"][0]["rate"] == 20.0
    with pytest.raises(ValueError):  # asked again, never guessed
        resolve_report(
            {"readable": True, "extracted": {"invoice_date_language": "fr", "amount_total": "illisible"}}, {}
        )


@pytest.mark.functionality
def test_a_number_with_two_readings_is_never_guessed():
    assert readings("1,005") == readings("1.005") == [1.005, 1005.0]  # French decimals or American thousands
    assert readings("1,005.50") == readings("1.005,50") == readings("1 005,50") == [1005.5]
    with pytest.raises(ValueError, match="'1,005' reads as 1.005 or 1005"):
        as_number("1,005")
    with pytest.raises(ValueError):  # the scan's model is asked again, told which value
        resolve_report({"readable": True, "extracted": {"invoice_date_language": "fr", "amount_total": "1,005"}}, {})


REPLIES = Path(__file__).parent / "replies"  # MiniMax-M3's real replies the first-to-last brace cut failed on


@pytest.mark.parametrize(
    "reply, flagged",
    [
        ("notes_first_total", ["amount_total", "amount_untaxed", "amount_tva", "invoice_number", "invoice_date"]),
        (
            "two_objects_first_incomplete",
            ["invoice_number", "invoice_date", "amount_untaxed", "amount_tva", "amount_stamp"],
        ),
        ("notes_first_orange", ["amount_total"]),
        ("notes_first_orange_again", ["amount_total"]),
        ("notes_with_json_fragment", ["amount_total"]),
    ],
)
@pytest.mark.functionality
def test_the_whole_answer_is_found_among_the_models_notes(reply, flagged):
    report = json_answer((REPLIES / f"{reply}.txt").read_text(), REPORT_KEYS)
    assert report["readable"] is True and [b["field"] for b in report["bad_fields"]] == flagged


@pytest.mark.functionality
def test_no_single_whole_answer_is_never_guessed():
    whole = '{"readable": true, "extracted": {}, "bad_fields": []}'
    other = '{"readable": true, "extracted": {}, "bad_fields": [{"field": "amount_total"}]}'
    assert json_answer(f"{whole}\nThe same again:\n{whole}", REPORT_KEYS)["bad_fields"] == []
    for text in (f"{whole}\n{other}", '{"readable": true, "extracted": {}}', "no JSON at all", "{rate: 10}"):
        with pytest.raises(ValueError):
            json_answer(text, REPORT_KEYS)
    assert json_answer('Sure: {"code": "6145"}') == {"code": "6145"}  # no keys asked: the one object
