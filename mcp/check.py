"""Phase 3: does what Experio (Bluejay) read match the scan? (tarik-master agents/ocr.py)"""

import json
import re
from datetime import date

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from app import server
from documents import scan_pages
from dossiers import MIN_SIMILARITY, normalized, similarity
from experio import POSTGRES, odoo_call, require_invoice
from model import ask_model


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
invoice_date_language is required: always pick one."""

CHECKED_FIELDS = [
    "invoice_number", "invoice_date", "amount_untaxed", "amount_tva", "amount_stamp", "amount_disbursement",
    "amount_guarantee_holdback", "amount_prorata", "amount_insurance", "amount_ts", "amount_tsp",
    "advance_amount", "amount_total", "tier", "ice",
]

def resolve_date(raw, language):
    """A date copied as printed, plus the invoice's language, as YYYY-MM-DD. Day/month
    order is decided here from the language, never by the model (ocr.py's rule)."""
    match = re.search(r"(\d{1,4})\D(\d{1,4})\D(\d{1,4})", raw or "")
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

def resolve_report(report, stored):
    """The model's report, with the date resolved in code and compared here: the
    model only ever sees the raw date, never the resolved one."""
    if not isinstance(report.get("readable"), bool) or not isinstance(report.get("extracted"), dict) \
            or not isinstance(report["extracted"].get("invoice_date_language"), str) \
            or not isinstance(report.get("bad_fields", []), list):
        raise ValueError(f"report does not match the expected shape: {str(report)[:200]}")
    extracted = report["extracted"]
    resolved = resolve_date(extracted.get("invoice_date_raw"), extracted["invoice_date_language"])
    extracted["invoice_date"] = resolved
    bad = [{key: str(b.get(key)).replace("Odoo", "Experio") for key in ("field", "stored", "extracted", "problem")}
           # Only the compared fields: the prompt forbids reporting invoice_date_raw and co., the model sometimes does.
           for b in report.get("bad_fields", []) if b.get("field") in CHECKED_FIELDS and b.get("field") != "invoice_date"]
    if resolved and str(stored.get("invoice_date")) != resolved:
        raw = extracted.get("invoice_date_raw")
        bad.append({"field": "invoice_date", "stored": str(stored.get("invoice_date")),
                    "extracted": f"{resolved} ({raw})" if raw else resolved,
                    "problem": f"La date capturee ({stored.get('invoice_date')}) differe de celle lue sur le scan ({raw} -> {resolved})."})
    return {"readable": report["readable"], "extracted": extracted, "bad_fields": drop_false_alarms(bad, stored, extracted)}

AMOUNT_FIELDS = {"amount_untaxed", "amount_tva", "amount_stamp", "amount_disbursement", "amount_ts", "amount_tsp", "amount_total"}

def drop_false_alarms(bad, stored, extracted):
    """The model's differences, minus the ones measured as false on real invoices:
    equal values, the same supplier written "NAME - IF", and amounts the model
    itself read inconsistently (its HT + TVA + taxes not adding up to its TTC)."""
    def plain(value):
        return re.sub(r"[^0-9a-z]", "", normalized(str(value or "")))
    def amount(name):
        return extracted.get(name) or 0

    parts = sum(amount(name) for name in ("amount_untaxed", "amount_tva", "amount_stamp", "amount_disbursement", "amount_ts", "amount_tsp"))
    self_consistent = extracted.get("amount_total") is None or abs(parts - amount("amount_total")) <= 1
    kept = []
    for b in bad:
        field, value = b["field"], stored.get(b["field"])
        read = extracted.get(field)
        if value not in (None, False, "") and read is not None and plain(value) == plain(read):
            continue
        if field == "tier" and value and read and similarity(re.sub(r"\s+-\s+\d+\s*$", "", value), read) >= MIN_SIMILARITY:
            continue
        if field in AMOUNT_FIELDS and not self_consistent:
            print(f"check: amounts read do not add up ({parts} vs {amount('amount_total')}), {field} flag dropped", flush=True)
            continue
        kept.append(b)
    return kept

@server.tool()
def check_invoice(invoice_id: int) -> str:
    """Check a draft invoice once Experio has read it: compares what Experio
    captured with what the scan shows, and lists every difference. Changes
    nothing. invoice_id is the draft invoice number given at its creation."""
    invoice = require_invoice(invoice_id, ["bluejay_state", "document_pdf", "file_type", *CHECKED_FIELDS[:-2], "partner_id"])
    if invoice["bluejay_state"] in ("pending", "sent"):
        return f"Facture {invoice_id} : Experio est encore en train de la lire, reessayez dans une minute."
    if invoice["bluejay_state"] != "done":
        return f"Facture {invoice_id} : Experio n'a pas lu cette facture automatiquement, rien a comparer."
    partner = invoice["partner_id"]
    stored = {name: invoice.get(name) for name in CHECKED_FIELDS[:-2]}
    stored["tier"] = partner[1] if partner else None
    stored["ice"] = (odoo_call("exp.partner", "read", [partner[0]], fields=["ice"])[0]["ice"] or None) if partner else None
    if not invoice["document_pdf"] or invoice["file_type"] not in (False, None, "pdf"):
        return f"Facture {invoice_id} : pas de scan PDF a verifier."
    pages = scan_pages(invoice["document_pdf"])
    # The model is told "Experio", never "Odoo": its explanations reach the client.
    # Asked twice when it says "unreadable": MiniMax-M3 sometimes does on a page it reads fine the next time.
    for attempt in range(2):
        answer = ask_model(f"{OCR_PROMPT.replace('Odoo', 'Experio')}\nToday's date is {date.today()}.", pages,
                           "Values captured in Experio:\n" + json.dumps(stored, default=str, ensure_ascii=False, indent=2)
                           + "\n\n" + OCR_SHAPE, "Verification de la facture")
        try:
            report = resolve_report(answer, stored)
        except ValueError as error:
            print(f"check_invoice {invoice_id}: {error}", flush=True)
            raise ToolError("Verification de la facture : echec, reessayez plus tard.") from None
        if report["readable"]:
            break
        print(f"check_invoice {invoice_id}: model said unreadable (attempt {attempt + 1})", flush=True)
    with psycopg.connect(**POSTGRES) as db:
        db.execute("insert into tarik_checks (invoice_id, readable, extracted, bad_fields) values (%s, %s, %s, %s) "
                   "on conflict (invoice_id) do update set readable = excluded.readable, extracted = excluded.extracted, "
                   "bad_fields = excluded.bad_fields, checked_at = now()",
                   (invoice_id, report["readable"], json.dumps(report["extracted"]), json.dumps(report["bad_fields"])))
    if not report["readable"]:
        return f"Facture {invoice_id} : scan illisible."
    if not report["bad_fields"]:
        return f"Facture {invoice_id} : conforme, ce qu'Experio a lu correspond au scan."
    return f"Facture {invoice_id} : {len(report['bad_fields'])} ecart(s) entre Experio et le scan :\n" + "\n".join(
        f"- {b['field']} : {b['problem']}" for b in report["bad_fields"])
