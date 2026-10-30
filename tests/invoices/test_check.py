"""Does what Experio read match the scan: dates, the model's report, false alarms."""

import json
from pathlib import Path

import pytest

from engine.tools.core.app import ToolError
from engine.tools.core.model import json_answer
from engine.tools.core.words import readings
from engine.tools.invoices.check import (
    CAPTURED,
    REPORT_KEYS,
    as_number,
    drop_false_alarms,
    resolve_date,
    resolve_report,
)
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


SCANNED = {"id": 3, "bluejay_state": "done", "document_pdf": "JVBER", "file_type": "pdf", "partner_id": [40, "ALPHA"]}
SCANNED.update(dict.fromkeys(CAPTURED), token=False, utility_type=False, type="purchase", invoice_number="F-9")
WHOLE = {"readable": True, "extracted": {"invoice_date_language": "fr"}, "bad_fields": []}


@pytest.fixture
def checking(monkeypatch):
    """check_invoice on SCANNED draft 3: Experio's answers by (model, method), the model's answers in turn."""
    from engine.tools.invoices import check

    state = {"invoice": dict(SCANNED), "calls": [], "answers": [WHOLE], "prompts": []}
    state["experio"] = {("exp.partner", "read"): [{"ice": "001234567000089"}], ("exp.invoice", "search_read"): []}

    def odoo_call(model, method, *args, **kw):
        state["calls"].append((model, method, args, kw))
        return state["experio"][(model, method)]

    def ask_model(system, pages, prompt, what, **kw):
        state["prompts"].append(prompt)
        answer = state["answers"].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return json.loads(json.dumps(answer))

    monkeypatch.setattr(check, "require_invoice", lambda invoice_id, fields: state["invoice"])
    monkeypatch.setattr(check, "odoo_call", odoo_call)
    monkeypatch.setattr(check, "ask_model", ask_model)
    monkeypatch.setattr(check, "tool_prompt", lambda name: name)
    monkeypatch.setattr(check, "scan_pages", lambda pdf: [pdf])
    return state


@pytest.mark.functionality
def test_what_experio_captured_includes_the_tiers_ice_read_from_experio(checking):
    from engine.tools.invoices.check import _captured

    stored = _captured(checking["invoice"])
    assert stored["tier"] == "ALPHA" and stored["ice"] == "001234567000089"
    assert checking["calls"] == [("exp.partner", "read", ([40],), {"fields": ["ice"]})]
    checking["experio"][("exp.partner", "read")] = [{"ice": False}]
    assert _captured(checking["invoice"])["ice"] is None  # no ICE on the tiers sheet
    checking["calls"].clear()
    assert _captured({**checking["invoice"], "partner_id": False})["tier"] is None and checking["calls"] == []


@pytest.mark.functionality
def test_a_draft_alone_of_its_document_is_checked_against_its_own_scan(checking):
    from engine.tools.invoices.check import split_scan

    assert split_scan(checking["invoice"]) == (None, "") and checking["calls"] == []  # no token: nothing searched
    checking["invoice"]["token"] = "abc"
    assert split_scan(checking["invoice"]) == (None, "")
    assert checking["calls"][0][2] == ([("token", "=", "abc"), ("id", "!=", 3)],)


@pytest.mark.functionality
def test_one_bill_of_a_document_experio_split_is_checked_on_the_scan_another_draft_holds(checking):
    from engine.tools.invoices.check import split_scan

    holder = {"id": 4, "document_pdf": "SCAN", "file_type": "pdf"}
    checking["invoice"].update(token="abc", document_pdf=False, utility_type="water")
    checking["experio"][("exp.invoice", "search_read")] = [holder]
    scanned, part = split_scan(checking["invoice"])
    assert scanned == holder and "holds 2 bills" in part and "only the water bill numbered F-9" in part


@pytest.mark.functionality
def test_a_conforming_check_is_stored_and_routine(checking, db_cabinet):
    from engine.tools.invoices import check

    result = check.check_invoice(3)
    assert result["routine"] and "conforme" in result["message"]
    assert check.stored_check(3) == (
        True,
        {"invoice_date_language": "fr", "tva_lines": [], "charge_lines": [], "invoice_date": None},
        [],
    )
    assert check.stored_check(4) is None


@pytest.mark.functionality
def test_the_differences_found_are_listed_and_replace_the_earlier_check(checking, db_cabinet):
    from engine.tools.invoices import check

    check.check_invoice(3)
    checking["invoice"]["amount_total"] = 100.0
    read = {"invoice_date_language": "fr", "amount_untaxed": 120, "amount_total": 120}
    checking["answers"] = [{**WHOLE, "extracted": read, "bad_fields": [flag("amount_total", 100, 120)]}]
    result = check.check_invoice(3)
    assert result["message"].startswith("Facture 3 : 1 écart entre Experio et le scan :\n- amount_total : x")
    assert [b["field"] for b in check.stored_check(3)[2]] == ["amount_total"]


@pytest.mark.functionality
def test_a_scan_said_unreadable_twice_stops_and_is_stored(checking, db_cabinet):
    from engine.tools.invoices import check

    checking["answers"] = [{**WHOLE, "readable": False}] * 2
    result = check.check_invoice(3)
    assert result["status"] == "stop" and "je n'y lis pas de facture" in result["message"]
    assert check.stored_check(3)[0] is False


@pytest.mark.technical
def test_an_answer_unreadable_or_unusable_once_is_asked_again(checking, db_cabinet):
    from engine.tools.invoices import check

    for first in ({**WHOLE, "readable": False}, ToolError("no single answer")):
        checking["answers"] = [first, WHOLE]
        assert check.check_invoice(3)["routine"] and not checking["answers"]
    checking["answers"], checking["prompts"] = [{"readable": True, "extracted": {}}, WHOLE], []
    check.check_invoice(3)
    assert "Your last answer was unusable" in checking["prompts"][1]  # told what was wrong


@pytest.mark.technical
@pytest.mark.parametrize(
    ("answers", "error"),
    [
        ([ToolError("cut"), ToolError("cut")], "cut"),
        ([{"readable": True, "extracted": {}}] * 2, "Vérification de la facture : échec, réessayez plus tard."),
    ],
)
def test_an_answer_unusable_twice_is_refused_and_nothing_stored(checking, db_cabinet, answers, error):
    from engine.tools.invoices import check

    checking["answers"] = list(answers)
    with pytest.raises(ToolError, match=error):
        check.check_invoice(3)
    assert check.stored_check(3) is None


@pytest.mark.functionality
@pytest.mark.parametrize(
    ("change", "status", "said"),
    [
        ({"bluejay_state": "pending"}, "wait", "encore en train de la lire"),
        ({"bluejay_state": "failed"}, "ok", "n'a pas lu cette facture automatiquement"),
        ({"document_pdf": False}, "ok", "pas de scan PDF"),
        ({"file_type": "excel"}, "ok", "pas de scan PDF"),  # Experio's only non-PDF type
    ],
)
def test_a_draft_experio_has_not_read_or_without_a_scan_is_not_checked(checking, change, status, said):
    from engine.tools.invoices import check

    checking["invoice"].update(change)
    result = check.check_invoice(3)
    assert result["status"] == status and said in result["message"] and checking["prompts"] == []


@pytest.mark.functionality
def test_a_sale_is_checked_against_its_customer(checking, db_cabinet):
    from engine.tools.invoices import check

    checking["invoice"]["type"] = "sale"
    check.check_invoice(3)
    assert check.SALE_NOTE in checking["prompts"][0]


@pytest.mark.technical
def test_a_date_or_tva_lines_out_of_shape_are_never_guessed():
    assert resolve_date("07/08/202", "fr") is None  # a three-digit year
    with pytest.raises(ValueError, match="tva_lines is not a list"):
        resolve_report({"readable": True, "extracted": {"invoice_date_language": "fr", "tva_lines": "20 %"}}, {})


@pytest.mark.functionality
def test_a_pdf_experio_stored_without_its_file_type_is_a_scan():
    """Experio leaves file_type "none" on the drafts it splits off a scan and on Facturation's: still PDFs."""
    from engine.tools.core.experio import has_scan

    assert has_scan({"document_pdf": "JVBER", "file_type": "none"})
    assert not has_scan({"document_pdf": "UEsDB", "file_type": "excel"})
    assert not has_scan({"document_pdf": False, "file_type": "pdf"})


@pytest.mark.functionality
def test_a_purchase_s_regional_tax_is_read_from_its_own_experio_field(checking):
    from engine.tools.invoices.check import _captured

    invoice = {**checking["invoice"], "amount_tva_3": 30.0, "amount_regional_tax": 0.0, "amount_tsc": 12.0}
    assert _captured(invoice)["amount_regional_tax"] == 30.0 and _captured(invoice)["amount_tsc"] == 12.0
    assert _captured({**invoice, "type": "sale", "amount_regional_tax": 25.0})["amount_regional_tax"] == 25.0


@pytest.mark.functionality
def test_the_tsc_and_the_regional_tax_count_in_the_ttc_the_scan_adds_up_to():
    scan = {"amount_untaxed": 100, "amount_tva": 20, "amount_tsc": 5, "amount_regional_tax": 3, "amount_total": 128}
    assert [b["field"] for b in drop_false_alarms([flag("amount_tsc", 0, 5)], {"amount_tsc": 0}, scan)] == [
        "amount_tsc"
    ]
    # without them the parts (120) miss the TTC (128): the amount flag would be dropped as the model's misreading
    bare = {**scan, "amount_tsc": 0, "amount_regional_tax": 0}
    assert drop_false_alarms([flag("amount_untaxed", 90, 100)], {"amount_untaxed": 90}, bare) == []


@pytest.mark.functionality
def test_the_scan_s_charge_lines_are_read_as_numbers_or_refused():
    report = {
        "readable": True,
        "extracted": {
            "invoice_date_language": "fr",
            "charge_lines": [{"designation": "loyer", "amount_ht": "1 200,50", "rate": "20"}, "bruit"],
        },
    }
    assert resolve_report(report, {})["extracted"]["charge_lines"] == [
        {"designation": "loyer", "amount_ht": 1200.5, "rate": 20}
    ]
    report["extracted"]["charge_lines"] = "loyer"
    with pytest.raises(ValueError, match="charge_lines is not a list"):
        resolve_report(report, {})
