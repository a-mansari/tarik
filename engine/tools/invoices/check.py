"""Check that what Experio (Bluejay) read on an invoice matches its scan, and store the result for correct_invoice."""

import json
import re
from datetime import date
from typing import Final

from engine.tools.core.app import ToolError, log, ok, routine, step_tool, stop
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import MIN_SIMILARITY
from engine.tools.core.experio import has_scan, odoo_call, require_invoice, still_reading
from engine.tools.core.model import ask_model, scan_pages, tool_prompt
from engine.tools.core.queries import Q
from engine.tools.core.words import as_number, counted, normalized, similarity, without_if

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
# A hotel bill's HT parts, read on the scan, never captured by Experio's reading (nor compared): correct_invoice writes
# them where the dossier uses them (should_use_ht_*).
HOTEL: Final = ("amount_ht_restauration", "amount_ht_breakfast", "amount_ht_blanchisserie")
# On a sale, the tiers to read is the customer: the scan's issuer is the cabinet's own client (check-invoice.md says
# supplier, the purchase's case).
SALE_NOTE: Final = (
    "\nThis invoice is a SALE (vente), issued by the cabinet's client: tier, ice and vat are the CUSTOMER's, the "
    'party addressed in the client box ("client", "facturé à"), never the issuer at the top.'
)
REPORT_KEYS: Final = ("readable", "extracted", "bad_fields")  # a reply without bad_fields is not "nothing differs"
NUMBERS = set(CAPTURED) - {"invoice_number", "invoice_date"}  # compared as numbers, never as text
LINE_NUMBERS = ("rate", "amount_ht", "amount_tva")
# The amounts that make the TTC: HT + TVA + stamp + debours + taxe de sejour + TPT.
TTC_PARTS = {"amount_untaxed", "amount_tva", "amount_stamp", "amount_disbursement", "amount_ts", "amount_tsp"}
AMOUNT_FIELDS = TTC_PARTS | {"amount_total"}
UTILITIES = {"water": "water ", "electricity": "electricity "}  # Experio's utility_type, as the model reads it


def resolve_date(raw, language):
    """Return the date ``raw`` (copied as printed on the scan) as YYYY-MM-DD; None when it is no valid date.

    ``language``: the invoice's language; English puts the month first, any other the day first. Decided here, never
    by the model. A 2-digit year is read as 20xx."""
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
    """Raise ValueError unless the model's ``report`` (dict) has the shape check-invoice-answer.md asks for."""
    extracted = report.get("extracted")
    if (
        not isinstance(report.get("readable"), bool)
        or not isinstance(extracted, dict)
        or not isinstance(extracted.get("invoice_date_language"), str)
        or not isinstance(report.get("bad_fields", []), list)
    ):
        raise ValueError(f"report does not match the expected shape: {str(report)[:200]}")


def _as_numbers(extracted):
    """Turn the amounts the model read into numbers, in place in ``extracted``, its TVA lines' amounts too.

    Raises ValueError when an amount is not one number (as_number) or tva_lines is not a list."""
    for field in (NUMBERS | set(HOTEL)) & set(extracted):
        extracted[field] = as_number(extracted[field])
    if not isinstance(extracted.get("tva_lines") or [], list):
        raise ValueError(f"tva_lines is not a list: {extracted['tva_lines']!r}")
    extracted["tva_lines"] = [
        {**line, **{key: as_number(line.get(key)) for key in LINE_NUMBERS}}
        for line in extracted.get("tva_lines") or []
        if isinstance(line, dict)
    ]


def _date_difference(stored, extracted, resolved):
    """Return the bad field entry for invoice_date when ``resolved`` (the scan's date, YYYY-MM-DD) differs from the
    date Experio ``stored``; else None."""
    if not resolved or str(stored.get("invoice_date")) == resolved:
        return None
    raw = extracted.get("invoice_date_raw")
    return {
        "field": "invoice_date",
        "stored": str(stored.get("invoice_date")),
        "extracted": f"{resolved} ({raw})" if raw else resolved,
        "problem": (
            f"La date lue par Experio ({stored.get('invoice_date')}) diffère de celle du scan ({raw}, soit {resolved})."
        ),
    }


def resolve_report(report, stored):
    """Return the model's ``report`` cleaned: {"readable", "extracted", "bad_fields"}.

    Amounts become numbers, the date is resolved and compared in code (the model only reports the raw date), bad
    fields outside CHECKED_FIELDS and false alarms are dropped. ``stored``: what Experio captured (_captured).
    Raises ValueError when the report is out of shape or an amount is not one number."""
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
    """Return ``value`` as text with only its lowercase unaccented letters and digits: "Sté A-1" -> "stea1"."""
    return normalized(str(value or "")).replace(" ", "")


def _equal(field, stored, read):
    """True when the stored and read values of ``field`` are the same: amounts within a centime, texts as words."""
    if field in NUMBERS:
        return stored is not None and read is not None and abs(float(stored) - read) < 0.01
    return stored not in (None, False, "") and read is not None and _letters(stored) == _letters(read)


def drop_false_alarms(bad, stored, extracted):
    """Return the model's differences ``bad`` minus the ones measured as false on real invoices.

    Dropped: equal values (amounts within a centime: 100.5 and 1005 differ), the same supplier written
    "NAME - IF", and amount flags when the amounts the model read do not add up (HT + TVA + taxes not its TTC),
    except a captured TTC far from both the TTC read and the sum of the parts read."""
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
    """Return what Experio captured on ``invoice`` (dict): its CAPTURED fields, plus "tier" (the tiers' name) and
    "ice" (the tiers' ICE), None when no tiers is set."""
    partner = invoice["partner_id"]
    stored = {name: invoice.get(name) for name in CAPTURED}
    stored["tier"] = partner[1] if partner else None
    stored["ice"] = None
    if partner:
        stored["ice"] = odoo_call("exp.partner", "read", [partner[0]], fields=["ice"])[0]["ice"] or None
    return stored


def split_scan(invoice):
    """Return (scanned, part) for a draft that is one of several Experio made from one document (one per bill, e.g.
    water and electricity, sharing one token).

    scanned: the draft of that document holding the scan (this one or a sibling), None when none holds it.
    part: the note telling the model this draft is only one of the document's bills.
    Returns (None, "") for a draft that is alone from its document."""
    if not invoice["token"]:
        return None, ""
    others = odoo_call(
        "exp.invoice",
        "search_read",
        [("token", "=", invoice["token"]), ("id", "!=", invoice["id"])],
        fields=["document_pdf", "file_type"],
    )
    if not others:
        return None, ""
    scanned = next((other for other in [invoice, *others] if has_scan(other)), None)
    kind = UTILITIES.get(invoice["utility_type"], "")
    part = (
        f"\nThis document holds {len(others) + 1} bills; Experio made one draft per bill. This draft is only the "
        f"{kind}bill numbered {invoice['invoice_number']}: compare the captured values with that bill alone, never "
        "with another bill nor with the document's total of all its bills."
    )
    return scanned, part


def _read_scan(invoice_id, pages, stored, part=""):
    """Return the model's resolved report (resolve_report) on the scan's ``pages`` against ``stored``.

    ``part``: which bill of the document this draft is (split_scan), "" for a whole document. The model is asked a
    second time when it says "unreadable", answers out of shape or gives no single whole answer: MiniMax-M3
    sometimes does on a page it reads fine the next time. Raises ToolError when the second answer is still
    unusable."""
    system = tool_prompt("check-invoice") + f"\nToday's date is {date.today()}."
    captured = "Values captured in Experio:\n" + json.dumps(stored, default=str, ensure_ascii=False, indent=2) + part
    unclear = ""
    for attempt in range(2):
        # No thinking: measured on real scans, it read no better and took up to 170 s instead of 11.
        prompt = captured + "\n\n" + tool_prompt("check-invoice-answer") + unclear
        try:  # a reply with no single whole answer in it (notes around it, two answers) is asked again
            answer = ask_model(system, pages, prompt, "Vérification de la facture", think=False, keys=REPORT_KEYS)
        except ToolError:
            if attempt:
                raise
            continue
        try:
            report = resolve_report(answer, stored)
        except ValueError as error:
            log(f"check_invoice {invoice_id}: {error} (attempt {attempt + 1})")
            if attempt:
                raise ToolError("Vérification de la facture : échec, réessayez plus tard.") from None
            # The model, which sees the scan, says which reading it meant (a number written "1,005": 1.005 or 1005).
            unclear = f"\nYour last answer was unusable: {error}. Read that value on the scan again, as a JSON number."
            continue
        if report["readable"]:
            break
        log(f"check_invoice {invoice_id}: model said unreadable (attempt {attempt + 1})")
    return report


def stored_check(invoice_id):
    """Return the invoice's check as _save_check stored it: (readable, extracted, bad_fields); None when not checked
    yet."""
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
    """Return check_invoice's result for ``report``: a stop result when unreadable, else a routine result saying
    it conforms or listing its differences."""
    if not report["readable"]:
        return stop(
            f"Facture {invoice_id} : je n'y lis pas de facture (scan illisible, ou document qui n'en est pas une). "
            "Renvoyez la facture elle-même, plus nette si c'est un scan."
        )
    if not report["bad_fields"]:
        return routine(f"Facture {invoice_id} : conforme, ce qu'Experio a lu correspond au scan.")
    differences = "\n".join(f"- {b['field']} : {b['problem']}" for b in report["bad_fields"])
    found = counted(len(report["bad_fields"]), "{} écart", "{} écarts") + " entre Experio et le scan"
    return routine(f"Facture {invoice_id} : {found} :\n{differences}")  # correct_invoice says what it changed


@step_tool
def check_invoice(invoice_id: int) -> dict:
    """Check a draft invoice once Experio has read it: compare what Experio captured with what the scan shows,
    and list every difference. Changes nothing in Experio; the check is stored for correct_invoice.
    ``invoice_id``: the draft invoice number given at its creation."""
    fields = ["bluejay_state", "document_pdf", "file_type", *CAPTURED, "partner_id", "token", "utility_type", "type"]
    invoice = require_invoice(invoice_id, fields)
    reading = still_reading(invoice)
    if reading:
        return reading
    if invoice["bluejay_state"] != "done":
        return ok(f"Facture {invoice_id} : Experio n'a pas lu cette facture automatiquement, rien à comparer.")
    stored = _captured(invoice)
    scanned, part = split_scan(invoice)
    scanned = scanned or (invoice if has_scan(invoice) else None)
    if not scanned:
        return routine(f"Facture {invoice_id} : pas de scan PDF a vérifier.")
    part += SALE_NOTE if invoice["type"] == "sale" else ""
    report = _read_scan(invoice_id, scan_pages(scanned["document_pdf"]), stored, part)
    _save_check(invoice_id, report)
    return _check_result(invoice_id, report)
