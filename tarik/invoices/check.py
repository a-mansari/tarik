"""Does what Experio (Bluejay) read match the scan?"""

import json
import re
from datetime import date
from typing import Final

from tarik.core.app import ToolError, log, ok, routine, step_tool, stop
from tarik.core.cabinets import cabinet_db
from tarik.core.config import MIN_SIMILARITY
from tarik.core.experio import has_scan, odoo_call, require_invoice, still_reading
from tarik.core.model import ask_model, scan_pages, tool_prompt
from tarik.core.queries import Q
from tarik.core.words import as_number, normalized, similarity, without_if

# The invoice header fields Experio captured (correct_invoice may write them back), then the tiers' name and ICE.
CAPTURED: Final = (
    "invoice_number",
    "invoice_date",
    "amount_untaxed",
    "amount_tva",
    "amount_stamp",
    "amount_disbursement",
    "amount_guarantee_holdback",
    "amount_prorata",
    "amount_insurance",
    "amount_ts",
    "amount_tsp",
    "advance_amount",
    "amount_total",
)
CHECKED_FIELDS = [*CAPTURED, "tier", "ice"]
REPORT_KEYS: Final = ("readable", "extracted", "bad_fields")  # a reply without bad_fields is not "nothing differs"
NUMBERS = set(CAPTURED) - {"invoice_number", "invoice_date"}  # compared as numbers, never as text
LINE_NUMBERS = ("rate", "amount_ht", "amount_tva")
# The amounts that make the TTC: HT + TVA + stamp + debours + taxe de sejour + TPT.
TTC_PARTS = {"amount_untaxed", "amount_tva", "amount_stamp", "amount_disbursement", "amount_ts", "amount_tsp"}
AMOUNT_FIELDS = TTC_PARTS | {"amount_total"}


def resolve_date(raw, language):
    """A date copied as printed, plus the invoice's language, as YYYY-MM-DD (None when it is no date). Day/month
    order is decided here from the language, never by the model."""
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
    """ValueError unless the model's ``report`` (dict) has the shape check-invoice-answer.md asks for."""
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
    ValueError when an amount is not one number (as_number) or tva_lines is not a list."""
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
    raw date, never the resolved one. ``stored``: what Experio captured. ValueError when out of shape or an
    amount is not one number."""
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
    return normalized(str(value or "")).replace(" ", "")


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
        # A TTC captured far from both the TTC read and the parts read is wrong whichever the model misread
        # (a bill holding two bills, its other taxes in no field: the HU invoice).
        wrong_either_way = (
            field == "amount_total" and value is not None and min(abs(value - total), abs(value - parts)) > 1
        )
        if field in AMOUNT_FIELDS and not self_consistent and not wrong_either_way:
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
    """The model's resolved report on the scan's ``pages`` against ``stored``. Asked twice when it says "unreadable",
    answers out of shape or gives no single whole answer: MiniMax-M3 sometimes does on a page it reads fine the
    next time."""
    system = tool_prompt("check-invoice") + f"\nToday's date is {date.today()}."
    captured = "Values captured in Experio:\n" + json.dumps(stored, default=str, ensure_ascii=False, indent=2)
    unclear = ""
    for attempt in range(2):
        # No thinking: measured on real scans, it read no better and took up to 170 s instead of 11.
        prompt = captured + "\n\n" + tool_prompt("check-invoice-answer") + unclear
        try:  # a reply with no single whole answer in it (notes around it, two answers) is asked again
            answer = ask_model(system, pages, prompt, "Verification de la facture", think=False, keys=REPORT_KEYS)
        except ToolError:
            if attempt:
                raise
            continue
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


def stored_check(invoice_id):
    """The invoice's check as _save_check stored it: (readable, extracted, bad_fields), None when not checked yet."""
    with cabinet_db() as db:
        return db.execute(Q.stored_check_of_invoice, (invoice_id,)).fetchone()


def _save_check(invoice_id, report):
    """Store ``report`` as the invoice's check in tarik_checks, replacing any earlier one."""
    with cabinet_db() as db:
        db.execute(
            Q.upsert_check,
            (invoice_id, report["readable"], json.dumps(report["extracted"]), json.dumps(report["bad_fields"])),
        )


def _check_result(invoice_id, report):
    """What check_invoice returns for ``report``: unreadable (stop), conforming, or its differences listed."""
    if not report["readable"]:
        return stop(f"Facture {invoice_id} : scan illisible, demandez au client de la renvoyer plus nette.")
    if not report["bad_fields"]:
        return routine(f"Facture {invoice_id} : conforme, ce qu'Experio a lu correspond au scan.")
    differences = "\n".join(f"- {b['field']} : {b['problem']}" for b in report["bad_fields"])
    found = f"{len(report['bad_fields'])} ecart(s) entre Experio et le scan"
    return routine(f"Facture {invoice_id} : {found} :\n{differences}")  # correct_invoice says what it changed


@step_tool
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
        return routine(f"Facture {invoice_id} : pas de scan PDF a verifier.")
    report = _read_scan(invoice_id, scan_pages(invoice["document_pdf"]), stored)
    _save_check(invoice_id, report)
    return _check_result(invoice_id, report)
