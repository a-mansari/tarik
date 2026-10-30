"""What a spreadsheet of invoices looks like, from its header row alone (no Experio call): Experio's own template
(canvas_factures.xlsx, mapped by Experio itself) or a client's free-form table."""

import csv
import io

import openpyxl

from engine.tools.core.words import normalized

TABLE_FILES = (".xlsx", ".xlsm", ".csv", ".txt")

# Experio's field of each header a client's free-form table commonly has (normalized), for the columns Experio's
# import screen has no suggestion for. Experio's template headers are not here: its screen maps them itself
# (addons/invoice/models/base_import.py INVOICE_COLUMN_MAPPING).
# shortcut: only the usual French headers; a client's other header is asked (missing_fields), add it here if it recurs.
FREE_FORM = {
    **dict.fromkeys(("fournisseur", "client", "tiers", "raison sociale", "nom du tiers", "nom tiers"), "partner_id"),
    **dict.fromkeys(
        ("n facture", "n° facture", "num facture", "numero facture", "facture", "n de facture", "ref facture"),
        "invoice_number",
    ),
    **dict.fromkeys(("date", "date facture", "date de facture", "date piece"), "invoice_date"),
    **dict.fromkeys(("ht", "total ht", "montant h t"), "amount_untaxed"),
    **dict.fromkeys(("tva", "total tva"), "amount_tva"),
    **dict.fromkeys(("ttc", "total ttc", "montant t t c"), "amount_total"),
    **dict.fromkeys(("compte charge", "compte de charge", "code compte"), "account_code"),
}
# The fields a client can map a column to, as the question names them to the client.
FIELD_LABELS = {
    "partner_id": "le tiers (fournisseur ou client)",
    "invoice_number": "le numéro de facture",
    "invoice_date": "la date de la facture",
    "amount_untaxed": "le montant HT",
    "amount_tva": "le montant de TVA",
    "amount_total": "le montant TTC",
    "account_code": "le compte comptable",
    "currency_id": "la devise",
    "tax_rate": "le taux de TVA",
    "amount_stamp": "les droits de timbre",
    "amount_disbursement": "les débours",
    "tax_withholding_percentage": "la RAS TVA",
    "local_withholding_percentage": "la RAS IR/IS",
    "notes": "les notes internes",
    "invoice_ref": "le nom (référence interne)",
}
# What an invoice needs at least: the tiers, its number, its date, and one total.
REQUIRED = ("partner_id", "invoice_number", "invoice_date")
TOTALS = ("amount_untaxed", "amount_total")

# Header words that make a table one of invoices. A bank statement has dates and amounts too (débit, crédit), but
# never an HT, TTC or TVA column.
AMOUNT_WORDS = {"ht", "ttc", "tva"}
TIERS_WORDS = ("fournisseur", "client", "tiers", "partenaire")


def header_row(name, data):
    """Return the normalized headers (first row) of table file ``name`` with bytes ``data``; [] when unreadable."""
    lower = name.lower()
    try:
        if lower.endswith((".xlsx", ".xlsm")):
            sheet = openpyxl.load_workbook(io.BytesIO(bytes(data)), read_only=True, data_only=True).worksheets[0]
            first = next(sheet.iter_rows(max_row=1, values_only=True), ())
        elif lower.endswith((".csv", ".txt")):
            line = bytes(data).decode("utf-8-sig", errors="replace").split("\n", 1)[0]
            separator = max(";,\t|", key=line.count)
            first = next(csv.reader([line], delimiter=separator), [])
        else:
            return []
    except Exception:
        return []
    return [normalized(str(cell or "")) for cell in first]


def is_invoice_sheet(name, data):
    """True when table ``name`` (bytes ``data``) has the headers of an invoice list: a date, an HT, TTC or TVA
    amount, and a number or a tiers. Experio's bank statement template and a statement's export never do."""
    headers = header_row(name, data)
    words = {word for header in headers for word in header.replace(".", " ").split()}
    has_amount = bool(words & AMOUNT_WORDS) or any("montant ht" in h or "montant ttc" in h for h in headers)
    has_date = any("date" in h for h in headers)
    has_who = any("facture" in h or any(t in h for t in TIERS_WORDS) for h in headers)
    return has_amount and has_date and has_who
