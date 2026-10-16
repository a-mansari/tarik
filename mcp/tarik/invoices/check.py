"""Phase 3: does what Experio (Bluejay) read match the scan? (tarik-master agents/ocr.py)"""

import json
import re
from datetime import date

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import log, ok, server, stop, with_ids
from tarik.core.cabinets import cabinet_db
from tarik.core.config import MIN_SIMILARITY
from tarik.core.documents import scan_pages
from tarik.core.experio import has_scan, odoo_call, require_invoice, still_reading
from tarik.core.model import ask_model
from tarik.core.numbers import as_number
from tarik.core.text import normalized, similarity, without_if


# tarik-master/tarik/agents/ocr.py, verbatim.
OCR_PROMPT = """You verify invoices captured in a Moroccan (CGNC) accounting app.

You are given the scan of a supplier invoice, and the values a clerk captured
from it into Odoo. Do two things, in this order:

1. READ THE SCAN. Extract the eight fields yourself, from the image only. Never
   look at the captured values while reading -- they are often wrong, and copying
   them defeats the whole check.
2. COMPARE. Report every field where what you read differs from what was captured.

The fields, and where they are on a Moroccan invoice:

- invoice_number: the "Facture N°" reference. Copy it exactly, including the
  year and any separator. A digit added or dropped is the most common capture error.
- invoice_date_raw: the date of the invoice ("LE :", "Date"), copied exactly as
  printed, digits in the same order, whatever separator is used (e.g. "07/08/2025"
  or "08.07.2025"). Do not interpret it, do not reorder it, do not convert it --
  a plain copy of the three numbers as they appear. Read the year carefully: a
  2024 invoice copied as 2025 is a real error.
- invoice_date_language: "fr" if the invoice's own text (labels, wording) is in
  French, "en" if it is in English. This alone decides day/month order later --
  you do not need to resolve the date yourself.
- amount_untaxed: the H.T total. amount_tva: the TVA line. amount_total: the TTC.
  amount_stamp: "timbre" -- absent on most invoices, which means 0.
  amount_disbursement: "debours" (costs paid on the client's behalf, no TVA)
  -- absent on most invoices, which means 0.
  amount_guarantee_holdback: "retenue de garantie" ("RG"), on BTP invoices --
  withheld from what is paid, NOT part of the TTC. Absent means 0.
  amount_prorata: "prorata" (the share of the site's common costs charged to
  this supplier, BTP) -- deducted like the retenue, NOT part of the TTC.
  Absent means 0.
  amount_ts: "taxe de sejour" (hotels) -- part of the TTC. Absent means 0.
  amount_tsp: "taxe de promotion touristique" ("TPT", "TSP") -- part of the
  TTC. Absent means 0.
  advance_amount: "avance" / "acompte" already paid and deducted on this
  invoice -- NOT part of the TTC. Absent means 0.
  amount_insurance: "assurance" / "assurance BTP" (the site insurance charged
  to this supplier, BTP) -- deducted like the retenue, NOT part of the TTC.
  Absent means 0.
  Moroccan invoices write 83 333,33 for 83333.33: the space groups thousands and
  the comma is the decimal point.
- tier: the SUPPLIER, the company that issued the invoice -- its letterhead is at
  the top and its legal details in the footer. It is NOT the party addressed in
  the box ("client", "facturé à"): that one is the cabinet's own client.
- ice: the supplier's ICE, 15 digits, usually in the footer next to RC / IF /
  CNSS. Take the ICE of the supplier, never the one printed in the client box.
- vat: the supplier's IF (identifiant fiscal), digits only, next to the ICE in
  the footer ("IF", "I.F.", "Id. Fiscal"). Same rule: the supplier's, never
  the client's. Never compared to Odoo -- it helps identify the tiers.
- designation: what was actually billed, copied from the lines of the invoice
  ("MAIN D'OEUVRE", "location de materiel", "honoraires", "carburant"...). Copy
  the wording, do not interpret it. Several lines: join them with ", ". This is
  never compared to Odoo -- it is what lets the expense be classified.
- is_credit_note: true when the document is an "Avoir" / "Facture d'avoir" /
  credit note, false for a normal invoice. Amounts stay positive either way.
- currency: the ISO code of the invoice's currency ("MAD", "EUR", "USD"...).
  "DH", "Dhs" and "dirhams" are MAD.
- country: the ISO 3166 alpha-2 code of the tiers' own address ("MA", "FR",
  "IE", "US"...) -- the supplier for an achat, the customer for a vente.
- payment_method: how it is paid, when printed ("Mode de reglement",
  "Paiement") -- one of especes, cheque, virement, effet, prelevement,
  compensation, or null when not printed. Never compared to Odoo.
- period_start, period_end: the period billed, as YYYY-MM-DD, only when the
  invoice states one ("periode du ... au ...", "abonnement annuel", "loyer de
  janvier a decembre"), else null. Never compared to Odoo.
- tva_lines: one entry per TVA rate in the invoice's tax summary, with its
  rate (e.g. 20), its HT base and its TVA amount. A single-rate invoice gives
  one entry. Never compared to Odoo.

Rules for the comparison:

- Report a field only when it is genuinely different. Formatting is not a
  difference: "83 333,33" and 83333.33 are the same amount, and a supplier name
  captured with its IF number appended is still the same supplier.
- Never report invoice_date_raw or invoice_date_language as a mismatch: they
  are not compared to Odoo at all, only used afterwards to resolve the real
  date in code.
- A field you cannot read on the scan is not a mismatch: leave it out, and set
  its extracted value to null.
- Also report a captured field that is empty while the scan plainly shows a value.
- Beyond the comparison, report an internal inconsistency in what the scan itself
  shows: TTC must equal HT + TVA + stamp + debours + taxe de sejour + TPT, within
  one dirham.
- Give ``problem`` as one French sentence, quoting both values.
- Everything matching: return an empty list.
"""

OCR_SHAPE = """Answer with only one JSON object, of this shape:
{"readable": bool, "extracted": {"invoice_number": str|null, "invoice_date_raw": str|null,
 "invoice_date_language": "fr"|"en", "amount_untaxed": num|null, "amount_tva": num|null,
 "amount_stamp": num|null, "amount_disbursement": num|null, "amount_guarantee_holdback": num|null,
 "amount_prorata": num|null, "amount_insurance": num|null, "amount_ts": num|null, "amount_tsp": num|null,
 "advance_amount": num|null, "amount_total": num|null, "tier": str|null, "designation": str|null,
 "ice": str|null, "vat": str|null, "is_credit_note": bool, "currency": str|null, "payment_method": str|null,
 "period_start": str|null, "period_end": str|null, "country": str|null,
 "tva_lines": [{"rate": num, "amount_ht": num, "amount_tva": num}]},
 "bad_fields": [{"field": str, "stored": str, "extracted": str, "problem": str}]}
invoice_date_language is required: always pick one. Every num is a JSON number, with a decimal point and no
thousands separator (1005.5, never "1 005,50" nor "1,005")."""

# The invoice header fields Experio captured (correct_invoice may write them back), then the tiers' name and ICE.
CAPTURED = (
    "invoice_number invoice_date amount_untaxed amount_tva amount_stamp amount_disbursement amount_guarantee_holdback "
    "amount_prorata amount_insurance amount_ts amount_tsp advance_amount amount_total"
).split()
CHECKED_FIELDS = [*CAPTURED, "tier", "ice"]
NUMBERS = set(CAPTURED) - {"invoice_number", "invoice_date"}  # compared as numbers, never as text
LINE_NUMBERS = ("rate", "amount_ht", "amount_tva")
# The amounts that make the TTC: HT + TVA + stamp + debours + taxe de sejour + TPT.
TTC_PARTS = {"amount_untaxed", "amount_tva", "amount_stamp", "amount_disbursement", "amount_ts", "amount_tsp"}
AMOUNT_FIELDS = TTC_PARTS | {"amount_total"}


def resolve_date(raw, language):
    """A date copied as printed, plus the invoice's language, as YYYY-MM-DD (None when it is no date). Day/month
    order is decided here from the language, never by the model (ocr.py's rule)."""
    match = re.search(r"(\d{1,4})\D(\d{1,4})\D(\d{1,4})", raw or "")  # three numbers, one separator between each
    if not match:
        return None
    a, b, c = match.groups()
    if len(a) == 4:
        year, month, day = a, b, c
    elif len(c) in (2, 4):
        year = c if len(c) == 4 else f"20{c}"
        month, day = (a, b) if (language or "").lower().startswith("en") else (b, a)
    else:
        return None
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _check_shape(report):
    """ValueError unless the model's ``report`` (dict) has the shape OCR_SHAPE asks for."""
    extracted = report.get("extracted")
    if (
        not isinstance(report.get("readable"), bool)
        or not isinstance(extracted, dict)
        or not isinstance(extracted.get("invoice_date_language"), str)
        or not isinstance(report.get("bad_fields", []), list)
    ):
        raise ValueError(f"report does not match the expected shape: {str(report)[:200]}")


def _as_numbers(extracted):
    """Turn the amounts the model read (in ``extracted``, changed in place) into numbers, its TVA lines' too.
    ValueError when tva_lines is not a list."""
    for field in NUMBERS & set(extracted):
        extracted[field] = as_number(extracted[field])
    if not isinstance(extracted.get("tva_lines") or [], list):
        raise ValueError(f"tva_lines is not a list: {extracted['tva_lines']!r}")
    extracted["tva_lines"] = [
        {**line, **{key: as_number(line.get(key)) for key in LINE_NUMBERS}}
        for line in extracted.get("tva_lines") or []
        if isinstance(line, dict)
    ]


def _date_difference(stored, extracted, resolved):
    """The bad field for the date, when the date ``resolved`` from the scan differs from the stored one; else None."""
    if not resolved or str(stored.get("invoice_date")) == resolved:
        return None
    raw = extracted.get("invoice_date_raw")
    return {
        "field": "invoice_date",
        "stored": str(stored.get("invoice_date")),
        "extracted": f"{resolved} ({raw})" if raw else resolved,
        "problem": (
            f"La date capturee ({stored.get('invoice_date')}) differe de celle lue sur le scan ({raw} -> {resolved})."
        ),
    }


def resolve_report(report, stored):
    """The model's report (dict), with the date resolved in code and compared here: the model only ever sees the
    raw date, never the resolved one. ``stored``: what Experio captured. ValueError when out of shape."""
    _check_shape(report)
    extracted = report["extracted"]
    _as_numbers(extracted)
    resolved = resolve_date(extracted.get("invoice_date_raw"), extracted["invoice_date_language"])
    extracted["invoice_date"] = resolved
    bad = [
        {key: str(b.get(key)).replace("Odoo", "Experio") for key in ("field", "stored", "extracted", "problem")}
        for b in report.get("bad_fields", [])
        # Only the compared fields: the prompt forbids reporting invoice_date_raw and co., the model sometimes does.
        if b.get("field") in CHECKED_FIELDS and b.get("field") != "invoice_date"
    ]
    date_difference = _date_difference(stored, extracted, resolved)
    if date_difference:
        bad.append(date_difference)
    kept = drop_false_alarms(bad, stored, extracted)
    return {"readable": report["readable"], "extracted": extracted, "bad_fields": kept}


def _letters(value):
    """``value`` as text, only its lowercase unaccented letters and digits: "Sté A-1" -> "stea1"."""
    return re.sub(r"[^0-9a-z]", "", normalized(str(value or "")))  # anything but a letter or digit


def _equal(field, stored, read):
    """True when the stored and read values of ``field`` are the same: amounts within a centime, texts as words."""
    if field in NUMBERS:
        return stored is not None and read is not None and abs(float(stored) - read) < 0.01
    return stored not in (None, False, "") and read is not None and _letters(stored) == _letters(read)


def drop_false_alarms(bad, stored, extracted):
    """The model's differences ``bad``, minus the ones measured as false on real invoices:
    equal values (amounts within a centime: 100.5 and 1005 differ), the same supplier written
    "NAME - IF", and amounts the model itself read inconsistently (HT + TVA + taxes not its TTC)."""
    total = extracted.get("amount_total") or 0
    parts = sum(extracted.get(name) or 0 for name in TTC_PARTS)
    self_consistent = extracted.get("amount_total") is None or abs(parts - total) <= 1
    kept = []
    for b in bad:
        field, value = b["field"], stored.get(b["field"])
        read = extracted.get(field)
        if _equal(field, value, read):
            continue
        if field == "tier" and value and read and similarity(without_if(value), read) >= MIN_SIMILARITY:
            continue
        if field in AMOUNT_FIELDS and not self_consistent:
            log(f"check: amounts read do not add up ({parts} vs {total}), {field} flag dropped")
            continue
        kept.append(b)
    return kept


def _captured(invoice):
    """What Experio captured on ``invoice``: its CAPTURED fields, the tiers' name and the tiers' ICE (dict)."""
    partner = invoice["partner_id"]
    stored = {name: invoice.get(name) for name in CAPTURED}
    stored["tier"] = partner[1] if partner else None
    stored["ice"] = (
        (odoo_call("exp.partner", "read", [partner[0]], fields=["ice"])[0]["ice"] or None) if partner else None
    )
    return stored


def _read_scan(invoice_id, pages, stored):
    """The model's resolved report on the scan's ``pages`` against ``stored``. Asked twice when it says "unreadable"
    or answers out of shape: MiniMax-M3 sometimes does on a page it reads fine the next time."""
    # The model is told "Experio", never "Odoo": its explanations reach the client.
    system = f"{OCR_PROMPT.replace('Odoo', 'Experio')}\nToday's date is {date.today()}."
    captured = "Values captured in Experio:\n" + json.dumps(stored, default=str, ensure_ascii=False, indent=2)
    unclear = ""
    for attempt in range(2):
        answer = ask_model(system, pages, captured + "\n\n" + OCR_SHAPE + unclear, "Verification de la facture")
        try:
            report = resolve_report(answer, stored)
        except ValueError as error:
            log(f"check_invoice {invoice_id}: {error} (attempt {attempt + 1})")
            if attempt:
                raise ToolError("Verification de la facture : echec, reessayez plus tard.") from None
            # The model, which sees the scan, says which reading it meant (a number written "1,005": 1.005 or 1005).
            unclear = f"\nYour last answer was unusable: {error}. Read that value on the scan again, as a JSON number."
            continue
        if report["readable"]:
            break
        log(f"check_invoice {invoice_id}: model said unreadable (attempt {attempt + 1})")
    return report


def _save_check(invoice_id, report):
    """Store ``report`` as the invoice's check in tarik_checks, replacing any earlier one."""
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_checks (invoice_id, readable, extracted, bad_fields) values (%s, %s, %s, %s) "
            "on conflict (invoice_id) do update set readable = excluded.readable, extracted = excluded.extracted, "
            "bad_fields = excluded.bad_fields, checked_at = now()",
            (invoice_id, report["readable"], json.dumps(report["extracted"]), json.dumps(report["bad_fields"])),
        )


def _check_result(invoice_id, report):
    """What check_invoice returns for ``report``: unreadable (stop), conforming, or its differences listed."""
    if not report["readable"]:
        return stop(f"Facture {invoice_id} : scan illisible, demandez au client de la renvoyer plus nette.")
    if not report["bad_fields"]:
        return ok(f"Facture {invoice_id} : conforme, ce qu'Experio a lu correspond au scan.")
    differences = "\n".join(f"- {b['field']} : {b['problem']}" for b in report["bad_fields"])
    return ok(f"Facture {invoice_id} : {len(report['bad_fields'])} ecart(s) entre Experio et le scan :\n{differences}")


@server.tool()
@with_ids
def check_invoice(invoice_id: int) -> dict:
    """Check a draft invoice once Experio has read it: compares what Experio
    captured with what the scan shows, and lists every difference. Changes
    nothing. invoice_id is the draft invoice number given at its creation."""
    invoice = require_invoice(invoice_id, ["bluejay_state", "document_pdf", "file_type", *CAPTURED, "partner_id"])
    reading = still_reading(invoice)
    if reading:
        return reading
    if invoice["bluejay_state"] != "done":
        return ok(f"Facture {invoice_id} : Experio n'a pas lu cette facture automatiquement, rien a comparer.")
    stored = _captured(invoice)
    if not has_scan(invoice):
        return ok(f"Facture {invoice_id} : pas de scan PDF a verifier.")
    report = _read_scan(invoice_id, scan_pages(invoice["document_pdf"]), stored)
    _save_check(invoice_id, report)
    return _check_result(invoice_id, report)
