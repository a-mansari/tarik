"""python -m pytest-free check of the dossier matching: python test_server.py (inside the tarik-mcp container)."""

import asyncio

from mcp.server.mcpserver.exceptions import ToolError

import io

import pypdfium2
from PIL import Image

from check import drop_false_alarms, resolve_date, resolve_report
from correct import planned_changes
from dossiers import MIN_SIMILARITY, similarity
from experio import exact_cabinet, expected_uid, require_dossier, require_invoice
from intake import require_kind, split_pdf
from server import server
from tiers import TIERS_MIN_SIMILARITY, set_tiers
from accounts import resolve_account
from tva import vat_answer

DOSSIERS = ["STE ALLIANCE GENIE CIVIL SARL", "Alliance Transport", "Boulangerie Atlas", "ATLAS IMMOBILIER", "Cabinet Benali"]


def best(query):
    return max(DOSSIERS, key=lambda name: similarity(query, name))


assert best("alliance genie civil") == "STE ALLIANCE GENIE CIVIL SARL"
assert best("Alliance Génie-Civil") == "STE ALLIANCE GENIE CIVIL SARL"  # accents, hyphen
assert best("boulangeri atlas") == "Boulangerie Atlas"  # typo
assert best("immobilier atlas") == "ATLAS IMMOBILIER"  # word order
assert best("benali") == "Cabinet Benali"  # part of the name
assert similarity("benali", "Cabinet Benali") >= 0.9
assert max(similarity("xyz", name) for name in DOSSIERS) < MIN_SIMILARITY  # nothing alike
assert similarity("zzqx boulangerie inexistante", "FinStrat Consulting") < MIN_SIMILARITY  # was a 51 % false match
assert similarity("", "Atlas") == 0.0

# A dossier tool fails without a real dossier id, before touching Experio or the file.
for bad in (None, 0, -5, "778", True):
    try:
        require_dossier(bad)
    except ToolError as error:
        assert "find_dossier" in str(error)
    else:
        raise AssertionError(f"require_dossier accepted {bad!r}")
for args in ({"file_id": 1}, {"file_id": 1, "dossier_id": None}, {"file_id": 1, "dossier_id": 0}):
    try:
        asyncio.run(server.call_tool("create_draft_invoice", args))
    except ToolError as error:  # a ToolError reaches the agent with its message
        assert "dossier_id" in str(error), error
    else:
        raise AssertionError(f"create_draft_invoice accepted {args}")

# The cabinet's database name is used exactly, or Tarik doesn't start.
assert exact_cabinet("experio_cabinet_8") == "experio_cabinet_8"
for bad in (None, "", " experio_cabinet_8", "experio_cabinet_8 ", "experio cabinet 8", "experio_cabinet_8\n"):
    try:
        exact_cabinet(bad)
    except SystemExit:
        continue
    raise AssertionError(f"exact_cabinet accepted {bad!r}")
assert expected_uid("430") == 430
for bad in (None, "", "0", "-1", "abc", " 430"):
    try:
        expected_uid(bad)
    except SystemExit:
        continue
    raise AssertionError(f"expected_uid accepted {bad!r}")

# Cutting a 4-page PDF where invoices start on pages 1 and 3.
buffer = io.BytesIO()
pages = [Image.new("RGB", (200, 280), color) for color in ("white", "gray", "white", "gray")]
pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
four = buffer.getvalue()
parts = split_pdf(four, [1, 3])
assert [(first, last) for first, last, _ in parts] == [(1, 2), (3, 4)]
assert [len(pypdfium2.PdfDocument(raw)) for _, _, raw in parts] == [2, 2]
assert [(f, l) for f, l, _ in split_pdf(four, [3, 0, 9, 3])] == [(1, 2), (3, 4)]  # page 1 added, out of range dropped
assert [(f, l) for f, l, _ in split_pdf(four, [])] == [(1, 4)]
assert len(split_pdf(four, [1, 2, 3, 4])) == 4

# Dates: order from the language, decided in code (tarik-master ocr.py).
assert resolve_date("07/08/2025", "fr") == "2025-08-07"
assert resolve_date("07/08/2025", "en") == "2025-07-08"
assert resolve_date("LE : 03.09.26", "fr") == "2026-09-03"
assert resolve_date("2025-08-07", "fr") == "2025-08-07"
assert resolve_date("31/02/2025", "fr") is None and resolve_date("", "fr") is None and resolve_date(None, "fr") is None

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
try:
    resolve_report({"readable": True, "extracted": {}}, stored)  # no language: refused, as ocr.py's schema does
except ValueError:
    pass
else:
    raise AssertionError("resolve_report accepted a report without invoice_date_language")

for bad in (None, 0, -1, "264417", True):
    try:
        require_invoice(bad, ["id"])
    except ToolError as error:
        assert "invoice_id" in str(error)
    else:
        raise AssertionError(f"require_invoice accepted {bad!r}")

# False alarms measured on real invoices (264420 is a real Bluejay error, 264423 was model noise).
def flag(field, stored_value, read_value):
    return {"field": field, "stored": str(stored_value), "extracted": str(read_value), "problem": "x"}
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
assert len(drop_false_alarms([flag("tier", "BETA TRAVAUX - 123", "ALPHA BUREAU")], {"tier": "BETA TRAVAUX - 123"}, {"tier": "ALPHA BUREAU"})) == 1

# Corrections planned from a check (the HT/TVA case is the real Bluejay error seen on a fuel invoice).
bad = [flag("amount_untaxed", 5000, 4545.45), flag("amount_tva", 0, 454.55), flag("tier", None, "STATION"), flag("ice", None, "1")]
ex = {"amount_untaxed": 4545.45, "amount_tva": 454.55, "tva_lines": [{"rate": 10, "amount_ht": 4545.45, "amount_tva": 454.55}]}
assert planned_changes(ex, bad, {}) == {"amount_untaxed": 4545.45, "amount_tva": 454.55}  # tiers and ICE are a later step
assert planned_changes({"amount_total": None}, [flag("amount_total", 1, None)], {}) == {}  # nothing read, nothing written
assert planned_changes({"is_credit_note": True}, [], {})["refund"] is True
assert "refund" not in planned_changes({"is_credit_note": True}, [], {"refund": True})
multi = planned_changes({"tva_lines": [{"rate": 20, "amount_ht": 100, "amount_tva": 20}, {"rate": 10, "amount_ht": 50, "amount_tva": 5}]}, [], {})
assert multi == {"multi_tva": True, "amount_tva_20": 20, "amount_tva_10": 5, "amount_tva": 25}
assert "multi_tva" not in planned_changes({"tva_lines": [{"rate": 20, "amount_ht": 100, "amount_tva": 20},
                                                         {"rate": 19, "amount_ht": 50, "amount_tva": 9.5}]}, [], {})  # no amount_tva_19 field
assert "multi_tva" not in planned_changes({"tva_lines": multi and [{"rate": 20, "amount_ht": 100, "amount_tva": 20},
                                                                  {"rate": 10, "amount_ht": 50, "amount_tva": 5}]}, [], {"amount_tax_non_recoverable": True})

# A file is identified before anything is made from it.
require_kind("f.pdf", "invoice", "invoice")
for kind, wanted, hint in ((None, "invoice", "identify_document"), ("statement", "invoice", "import_bank_statement"),
                           ("invoice", "statement", "split_invoices")):
    try:
        require_kind("f.pdf", kind, wanted)
    except ToolError as error:
        assert hint in str(error), error
    else:
        raise AssertionError(f"require_kind accepted {kind} as {wanted}")

for bad in (None, 0, -3, True):
    try:
        set_tiers(1, bad)
    except ToolError as error:
        assert "partner_id" in str(error)
    else:
        raise AssertionError(f"set_tiers accepted {bad!r}")
assert similarity("GAMMA DISTRIBUTION SARL", "DELTA FAIR DISTRIBUTION") < TIERS_MIN_SIMILARITY  # shared generic words only
assert similarity("atlas roulement", "ATLAS DE ROULEMENT") >= TIERS_MIN_SIMILARITY
assert similarity("Nova Telecom SA", "NOVA TELECOM") >= TIERS_MIN_SIMILARITY > similarity("Nova Telecom SA", "ORBIT TELECOM")
# The client's words -> one account (fictional chart, same shapes as a real one).
chart = [{"id": 1, "code": "61110000", "name": "Achats de marchandises"}, {"id": 2, "code": "61120000", "name": "Achats de marchandises"},
         {"id": 3, "code": "61190000", "name": "Rabais, remises et ristournes obtenus sur achats de marchandises"},
         {"id": 4, "code": "61251000", "name": "Achats de fournitures non stockables"}, {"id": 5, "code": "61455000", "name": "Frais de téléphone"}]
assert resolve_account(chart, "achats de marchandises")["id"] == 1  # same label twice: lowest code
assert resolve_account(chart, "6111")["id"] == 1 and resolve_account(chart, "61455000")["id"] == 5
assert resolve_account(chart, "téléphone")["id"] == 5
assert resolve_account(chart, "marchandises") is None  # three different labels: ask
assert resolve_account(chart, "611") is None and resolve_account(chart, "carburant") is None
assert vat_answer("Oui") is True and vat_answer("non recuperable") is True
assert vat_answer("non") is False and vat_answer("recuperable") is False
assert vat_answer("je ne sais pas") is None and vat_answer("") is None
print("similarity, dossier, cabinet, split, check, false-alarm, correction, kind, tiers, account and tva checks ok")
