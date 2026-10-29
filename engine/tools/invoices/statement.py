"""A bank statement after its import, handled as the cabinet does on Experio's statement screen (module bank).

- Reading: Tarik waits until Experio has read the statement (is_detected, set by bluejay set_bank_statement; its
  bluejay_state stays « pending » for good). Then Experio's own checks: its lines add up to the ending balance read
  (is_complete), and its starting balance follows the journal's previous statement (is_valid).
- Pointing: « Auto Point » (action_refresh, detect_bank_statement_line_info) finds each line's operation, payment
  method and tiers from past posted lines, and an open posted invoice of exactly that tiers and amount, and shows them
  in a window to confirm; Tarik confirms them (the window's action_apply). A line the client ties to an invoice by its
  number gets that invoice when the amount owed is exactly the line's.
- Posting: Experio's own validation of the statement (action_post), which posts its ready lines only. A window it
  opens instead (duplicate, out of the period, exchange rate) stops for the cabinet.
"""

import ast
import io
import re
import time
from collections import Counter, defaultdict
from datetime import date, timedelta

import openpyxl

from engine.tools.core.app import CRASHED, ToolError, log, ok, routine, step_tool, stop, wait
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import (
    CHECK_REFUSALS,
    ExperioRefusal,
    dry_run,
    experio_refusal,
    in_dossier,
    is_refusal,
    odoo_call,
    odoo_json,
    plain,
    refused,
    type_in,
)
from engine.tools.core.model import ask_model, tool_prompt
from engine.tools.core.queries import Q
from engine.tools.core.words import as_number, counted, normalized, said, tiers_key
from engine.tools.core.words import reasons as reason_lines
from engine.tools.invoices import tpe
from engine.tools.invoices.check import resolve_date
from engine.tools.invoices.exercice import with_fiscal_year
from engine.tools.invoices.tiers import SIDE, series_account

STATEMENT = "exp.bank.statement"
LINE = "exp.bank.statement.line"
# Experio's Excel statement template (bank/static/xls/canvas_releves_bancaire.xlsx), its header row normalized.
CANVAS = ["date", "libelle", "type d operation", "tiers", "methode de paiement", "debit", "credit"]
FIELDS = ["state", "bluejay_state", "company_id", "journal_id", "currency_id", "balance_start", "balance_end"]
FIELDS += ["balance_end_real", "is_complete", "is_valid", "line_ids", "date", "is_detected"]
POSTED = ("post", "export")


def is_canvas(name, data):
    """True when spreadsheet ``data`` (file ``name``) is Experio's Excel statement template.

    That is: an .xlsx/.xlsm whose first row starts with the template's columns (Date, Libellé, Type d'opération, Tiers,
    Méthode de paiement, Débit, Crédit). False for an unreadable file.
    """
    if not name.lower().endswith((".xlsx", ".xlsm")):
        return False
    try:
        sheet = openpyxl.load_workbook(io.BytesIO(bytes(data)), read_only=True, data_only=True).worksheets[0]
        first = next(sheet.iter_rows(max_row=1, values_only=True), ())
    except Exception:
        return False
    return [normalized(str(cell or "")) for cell in first[: len(CANVAS)]] == CANVAS


def import_canvas(name, data, dossier_id, journal_id):
    """Import Experio's Excel statement template into a new statement of journal ``journal_id``, as « Load EXCEL » does.

    Args:
        name: the file name; without its extension, it names the statement.
        data: the spreadsheet's bytes.
        dossier_id: the dossier (res.company id).
        journal_id: the bank journal chosen for the statement.

    Each row becomes a line created with Experio's import context: Experio appends it to the given statement
    (from_xlsx_import with default_statement_id, handle_imported_records) and finds its tiers and operation by name
    itself (partner_value, imp_operation_id, in exp.bank.statement.line create). Payment method and currency are
    matched here by their exact name, as Odoo's importer does. Odoo's importer itself is not called: it takes the file
    only from the browser's upload.

    Returns the new statement's id.

    Raises:
        RuntimeError: the template has no filled row.
        Exception: Experio's refusal of a line; the empty statement made for it is deleted first.
    """
    rows = canvas_rows(data)
    if not rows:
        raise RuntimeError("aucune ligne dans le canevas")
    context = in_dossier(dossier_id)
    statement_id = odoo_call(
        STATEMENT, "create", {"journal_id": journal_id, "name": name.rsplit(".", 1)[0]}, context=context
    )
    try:
        values = [line_values(row, context) for row in rows]
        importing = {**context, "from_xlsx_import": True, "default_statement_id": statement_id}
        odoo_call(LINE, "create", values, context=importing)
    except Exception:
        odoo_call(STATEMENT, "unlink", [statement_id], context=context)  # the empty statement made for it, nothing else
        raise
    return statement_id


def canvas_rows(data):
    """Return the template's filled rows, each as {normalized header: cell value}."""
    sheet = openpyxl.load_workbook(io.BytesIO(bytes(data)), read_only=True, data_only=True).worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    headers = [normalized(str(cell or "")) for cell in rows[0]]
    return [dict(zip(headers, row, strict=False)) for row in rows[1:] if any(cell not in (None, "") for cell in row)]


def line_values(row, context):
    """Return one statement line's create values from a template ``row`` (dict from canvas_rows).

    The payment method and currency are set only when exactly one record of the dossier has that exact name.
    """
    day = row.get("date")
    day = day.date().isoformat() if hasattr(day, "date") else resolve_date(str(day or ""), "fr")
    values = {
        "date": day,
        "payment_ref": str(row.get("libelle") or "").strip(),
        "imp_operation_id": str(row.get("type d operation") or "").strip(),
        "partner_value": str(row.get("tiers") or "").strip(),
        "debit": as_number(row.get("debit")) or 0.0,
        "credit": as_number(row.get("credit")) or 0.0,
        "notes": str(row.get("remarques") or "").strip(),
    }
    rate = as_number(row.get("taux de change"))
    if rate:
        values["exchange_rate"] = rate
    for column, model, field in (
        ("methode de paiement", "exp.payment.method", "exp_payment_method_id"),
        ("devise", "res.currency", "currency_id"),
    ):
        text = str(row.get(column) or "").strip()
        if text:
            found = odoo_call(model, "search", [("name", "=", text)], limit=2, context=context)
            if len(found) == 1:
                values[field] = found[0]
    return values


def statement_of(file_id):
    """Return (statement_id, network) for the statement imported from file ``file_id``.

    network is the TPE network key for a TPE statement, None for a bank statement. Returns (None, None) when no
    statement was imported from the file.
    """
    with cabinet_db() as db:
        row = db.execute(Q.statement_of_file, (file_id,)).fetchone()
    return tuple(row) if row else (None, None)


def read_statement(statement_id):
    """Return bank statement ``statement_id`` read with FIELDS (dict)."""
    return odoo_call(STATEMENT, "read", [statement_id], fields=FIELDS)[0]


def auto_point(statement, context):
    """Run Experio's « Auto Point » on ``statement`` and confirm its detections as the window's « Apply » does.

    Returns how many lines the detection filled (0 when Experio found nothing). In test mode nothing is confirmed.
    """
    action = odoo_json(STATEMENT, "action_refresh", [statement["id"]], context=context)
    if not isinstance(action, dict) or action.get("res_model") != "bank.statement.line.detect.wizard":
        return 0  # « No detection results »: nothing to fill
    found = odoo_call(
        "bank.statement.line.detect.wizard", "read", [action["res_id"]], fields=["line_ids"], context=context
    )
    if not dry_run():
        odoo_json("bank.statement.line.detect.wizard", "action_apply", [action["res_id"]], context=context)
    return len(found[0]["line_ids"]) if found else 0


def named_invoices(statement, words, context):
    """Give unposted purchase/sale lines without an invoice the invoice the client names by its number in ``words``.

    Args:
        statement: the statement read with FIELDS (dict).
        words: the client's words.
        context: the dossier's context.

    A line gets an invoice only when exactly one of the invoices named in ``words`` that owe its amount (named_in) is
    offered by its form's picker (picked), is on no statement line yet (detection's already_in_use,
    bank/models/bank_statement_line.py:2441-2453; reconcile_invoice_ids refuses an invoice paid already, :4372-4376)
    and is accepted by the line's form (typed_in), which also gives the tiers it fills. A named invoice that owes no
    open line's amount is not looked at (a partial payment included): only invoices of a line's amount are named back.

    Returns (given, note): how many lines got one (counted also in test mode, where nothing is written), and a
    sentence per line for the reply naming each such invoice no line got, and why ("" when there is none).
    """
    words = normalized(words or "")
    fields = ["state", "invoice_ids", "partner_id", "debit", "credit", "operation_type", "invoice_ids_domain"]
    fields += ["payment_ref", "date"]
    lines = odoo_call(LINE, "read", statement["line_ids"], fields=fields, context=context) if words else []
    lines = [line for line in lines if amount_of(line)]
    named = named_in(words, statement, sorted({amount_of(line) for line in lines}), context)
    if not named:
        return 0, ""
    ids = [c["id"] for c in named]
    used = odoo_call(LINE, "search_read", [("invoice_ids", "in", ids)], fields=["invoice_ids"], context=context)
    # shortcut: test mode does not apply Auto Point's detections, so its invoices are not seen as taken and the lines
    # keep their tiers and picker from before it: the count may promise a link the real run will not make
    taken = {i for row in used for i in row["invoice_ids"]}  # invoices on a line, this run's added below
    elsewhere = {i for row in used if row["id"] not in statement["line_ids"] for i in row["invoice_ids"]}
    why = {
        i: "elle est déjà sur une opération d'un autre relevé" for i in ids if i in elsewhere
    }  # {invoice: why not given}
    given, fits = set(), set()
    for line in lines:
        same = [c for c in named if c["id"] not in taken and round(c["amount_residual"], 2) == amount_of(line)]
        fits.update(c["id"] for c in same)
        offered = picked(line, same, context)
        accepted = []
        for c in offered:
            if len(accepted) > 1:
                break  # two the form accepts: the line gets none, no need to try more
            values, reason = typed_in(line, c["id"], context)
            if values:
                accepted.append((c["id"], values))
            else:
                why.setdefault(c["id"], f"Experio la refuse sur l'opération « {label(line)} » ({reason})")
        if len(accepted) == 1:
            invoice_id, values = accepted[0]
            if not dry_run():
                odoo_call(LINE, "write", [line["id"]], values, context=context)
            taken.add(invoice_id)
            given.add(invoice_id)
        elif accepted:  # the line gets none; those tried and accepted, and those not tried, say why
            tried = {i for i, _ in accepted}
            for c in offered:
                if c["id"] in tried or c["id"] not in why:
                    why[c["id"]] = f"une autre facture citée convient aussi à l'opération « {label(line)} »"
    for i in fits:
        why.setdefault(i, "l'opération de son montant ne la propose pas (autre tiers ou autre sens)")
    left = "\n".join(
        f"Je n'ai pas rattaché la facture {c['invoice_number']} que vous avez citée : {why[c['id']]}."
        for c in named
        if c["id"] in why and c["id"] not in given
    )
    return len(given), left


# A number a bank label carries (cheque, invoice...): 4 digits or more, dates (« 13/02/25 », « 13 02 25 ») left out.
DATES = re.compile(r"\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\b|\b\d{1,2}\s\d{2}\s\d{2,4}\b")
NUMBER = re.compile(r"[A-Za-z]*\d[\dA-Za-z\-/]*")
LABEL_TOKENS = 3  # numbers of a label looked up: more is a reference list, not an invoice


def label_numbers(label):
    """Return the numbers ``label`` carries, as written (« F25-111001 », « 0902108 »), at most LABEL_TOKENS."""
    found = [n for n in NUMBER.findall(DATES.sub(" ", label or "")) if len(re.sub(r"\D", "", n)) >= 4]
    return found[:LABEL_TOKENS]


def number_key(text):
    """Return ``text`` as invoice numbers compare: letters and digits only, lower case, no leading zeros."""
    return re.sub(r"[^a-z0-9]", "", normalized(text or "")).lstrip("0")


def label_invoices(statement, lines, context):
    """Give purchase and sale lines without an invoice the one open invoice whose number their label carries.

    Many labels end with the invoice they pay (« CHQ N° 0902108_PAP BAT_F25-111001 », « VIREMENT_MEFELEC_29457 »): the
    cabinets' own dossiers show it on 28% of their invoiced lines, 60 to 90% in some. A number counts only when it is
    the invoice's number or reference exactly (invoice_number, invoice_ref; case, signs and leading zeros aside), the
    invoice is among those the line's picker offers (a partner already on the line narrows it), on no statement line
    yet, and the only one: on 250 such lines of the cabinets' dossiers the one invoice found was always the right one;
    a number that is not an invoice's found one by mistake on 1% of invoiced lines. The amount is not asked: 14% of
    them are paid less or more than the total. The line's form must accept it (typed_in), which also gives its tiers.

    Returns how many lines got one (counted also in test mode, where nothing is written).
    """
    given, taken = 0, set()
    for line in lines:
        numbers = label_numbers(line["payment_ref"]) if amount_of(line) else []
        if not numbers:
            continue
        base, found = ast.literal_eval(line["invoice_ids_domain"]), set()
        for number in numbers:
            for field in ("invoice_number", "invoice_ref"):
                offered = odoo_call(
                    "exp.invoice",
                    "search_read",
                    [*base, (field, "ilike", number)],
                    fields=[field],
                    limit=10,
                    context=context,
                )
                found |= {row["id"] for row in offered if number_key(row[field]) == number_key(number)}
        found -= taken
        if not found:
            continue
        used = odoo_call(
            LINE, "search_read", [("invoice_ids", "in", list(found))], fields=["invoice_ids"], context=context
        )
        found -= {i for row in used for i in row["invoice_ids"]}
        if len(found) != 1:
            continue
        invoice_id = found.pop()
        values, _ = typed_in(line, invoice_id, context)
        if values:
            if not dry_run():
                odoo_call(LINE, "write", [line["id"]], values, context=context)
            taken.add(invoice_id)
            given += 1
    return given


def amount_of(line):
    """Return the amount statement ``line`` (dict) may get an invoice for, 0 when it may get none.

    0 for a posted line, one with an invoice, one neither purchase nor sale, one without picker domain, or of no
    amount (verify_selected_invoices refuses it, bank/models/bank_statement_line.py:4548-4562). The amount is
    abs(credit - debit), in the line's currency (its currency_id, :720-729), matched as a bare number against the
    invoice's amount_residual, in the invoice's (exp_account/models/exp_move.py:680-686), as Experio's detection
    matches them (:2456-2471, :617-623): no currency is compared, so a line and an invoice in two currencies match
    only when their numbers are equal, and then wrongly.
    """
    if line["state"] in POSTED or line["invoice_ids"] or line.get("operation_type") not in ("purchase", "sale"):
        return 0
    return round(abs((line["credit"] or 0) - (line["debit"] or 0)), 2) if line["invoice_ids_domain"] else 0


def named_in(words, statement, amounts, context):
    """Return the dossier's posted invoices owing one of ``amounts`` ({id, invoice_number, amount_residual}) whose
    number the client's normalized ``words`` cite (cited). One search for the whole statement; none without amounts."""
    if not words or not amounts:
        return []
    domain = [("company_id", "=", statement["company_id"][0]), ("state", "in", list(POSTED))]
    domain += [("invoice_number", "!=", False), ("amount_residual", "in", amounts)]
    fields = ["invoice_number", "amount_residual"]
    invoices = odoo_call("exp.invoice", "search_read", domain, fields=fields, context=context)
    return [c for c in invoices if cited(c["invoice_number"], words)]


def cited(number, words):
    """True when invoice ``number`` is in the client's normalized ``words`` as whole words, its spaces aside as Experio
    compares invoice numbers (invoice/models/exp_invoice.py:1550-1557, :8795-8825): « FAC 123 » is « FAC123 », « FA-1 »
    is not in « FA-12 ». A number without a digit (« Facture », « FNP ») or of three characters or fewer (« 15 »,
    « A15 ») never counts: it would be any word, date or amount. One of digits only is cited whole, its digits
    together or spaced as stored, so « 1510 » is not in « 15/10 »."""
    compact = normalized(number).replace(" ", "")
    if not re.search(r"\d", compact) or len(compact) <= 3:
        return False
    if compact.isdigit():
        pattern = "|".join(map(re.escape, {compact, normalized(number)}))
    else:
        pattern = " ?".join(map(re.escape, compact))  # any space in the words, or none, between its characters
    pattern = f"(?:{pattern})"
    return re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", words) is not None


def picked(line, invoices, context):
    """Return those of ``invoices`` statement ``line``'s form's invoice picker offers: invoice_ids' domain, the line's
    stored invoice_ids_domain (bank/views/bank_statement_views.xml:89; get_invoice_ids_domain,
    bank/models/bank_statement_line.py:4236-4291: the dossier's posted invoices still owed, of the line's tiers and
    of its operation's way)."""
    if not invoices:
        return []
    domain = ast.literal_eval(line["invoice_ids_domain"]) + [("id", "in", [c["id"] for c in invoices])]
    offered = set(odoo_call("exp.invoice", "search", domain, context=context))
    return [c for c in invoices if c["id"] in offered]


def label(line):
    """Return statement ``line``'s label for the reply: its bank label, else its date and amount."""
    return line["payment_ref"] or f"{line['date']} {abs((line['credit'] or 0) - (line['debit'] or 0)):.2f}"


def typed_in(line, invoice_id, context):
    """Choose invoice ``invoice_id`` in statement ``line``'s form; saves nothing.

    Experio's onchange on invoice_ids (_onchange_invoice_ids, bank/models/bank_statement_line.py:2138-2160) runs
    verify_selected_invoices (:4478-4596: the invoice's account matches the operation's, it is posted, it goes the
    line's way) and fills the line's tiers when it has none (:2145-2146). On screen that tiers then empties
    invoice_ids (_onchange_partner_id, :2118-2127) and the cabinet picks the invoice again: Tarik writes both at once.

    Returns (values, reason): the values to write (invoice_ids, and the tiers the form fills: an RPC write runs no
    onchange, and a purchase/sale line without tiers stays draft) and "", or None and Experio's reason when its form
    refuses the invoice. Raises Experio's other errors (ExperioRefusal), which are not a refusal of the invoice.
    """
    form = {"invoice_ids": [(6, 0, [invoice_id])]}
    try:
        type_in(LINE, line["id"], form, "invoice_ids", ("invoice_ids", "partner_id"), context)
    except ExperioRefusal as refusal:
        if refusal.name not in CHECK_REFUSALS:
            raise
        return None, experio_refusal(refusal)
    values = {"invoice_ids": [(4, invoice_id)]}
    if not line["partner_id"] and form.get("partner_id"):
        values["partner_id"] = form["partner_id"]
    return values, ""


# What Auto Point leaves, given as the cabinet gives it on the statement's « Operations » list
# (button_open_statement_lines, bank/views/bank_statement_views.xml:713): only sure values are written, the rest stay
# for the cabinet with Experio's own reason (blocked_reasons).
LINE_FIELDS = ["state", "operation_id", "operation_type", "partner_id", "partner_id_domain", "exp_payment_method_id"]
LINE_FIELDS += ["payment_ref", "date", "debit", "credit", "invoice_ids", "invoice_ids_domain"]


def open_lines(statement, context):
    """Return the statement's lines not posted yet, read with LINE_FIELDS."""
    lines = odoo_call(LINE, "read", statement["line_ids"], fields=LINE_FIELDS, context=context)
    return [line for line in lines if line["state"] not in POSTED]


def side(line):
    """Return the operations' payment_type statement ``line`` (dict) takes: inbound for money received, outbound for
    money paid, as the line's operation picker (bank/views/bank_statement_views.xml:339, amount = credit - debit)."""
    return "inbound" if (line["credit"] or 0) - (line["debit"] or 0) > 0 else "outbound"


def operations_of(dossier_id, context):
    """Return the operations a line of dossier ``dossier_id`` may get: its own and the shared ones (operation_id's
    check_company, bank/models/bank_statement_line.py:177-181), without an error of their own (a line on one stays
    draft, :4031-4035; validate_operation_fields, bank/models/bank_operation_type.py:176-216)."""
    domain = ["|", ("company_id", "=", dossier_id), ("company_id", "=", False)]
    fields = ["name", "type", "payment_type", "account_id", "error_msg", "bank_type"]
    found = odoo_call("bank.operation.type", "search_read", domain, fields=fields, context=context)
    return {o["id"]: o for o in found if not o["error_msg"]}


def past_labels(dossier_id, context, each=3):
    """Return {operation id: up to ``each`` labels of the dossier's latest posted lines on it}: how the dossier was
    pointed before."""
    domain = [("company_id", "=", dossier_id), ("state", "in", list(POSTED)), ("operation_id", "!=", False)]
    rows = odoo_call(
        LINE,
        "search_read",
        domain,
        fields=["payment_ref", "operation_id"],
        order="date desc",
        limit=500,
        context=context,
    )
    labels = {}
    for row in rows:
        kept = labels.setdefault(row["operation_id"][0], [])
        if row["payment_ref"] and len(kept) < each and row["payment_ref"] not in kept:
            kept.append(row["payment_ref"])
    return labels


# The words of a label that say how it was paid, per payment method (normalized name): VIR, CHQ, PRLV...
METHOD_WORDS = {
    "virement": ("vir", "virement"),
    "cheque": ("chq", "cheque"),
    "prelevement": ("prlv", "prelevement"),
    "effet": ("effet", "lcn"),
    "espece": ("espece", "especes"),
    "carte": ("carte", "cb"),
    # HB: the bank's home banking, which the cabinets pointed « Autres » on 240 of 404 purchase lines (Prélèvement 78,
    # Chèque 47, Virement 39) in 11 dossiers: no method of its own
    "autres": ("hb",),
}


def method_in_label(label, methods):
    """Return the id of the payment method (``methods``: {id: name}) the label's words say, None when it says none
    or two: the model's null is not the end of it when the label reads « VIR » or « CHQ »."""
    words = normalized(label)
    found = {
        i
        for i, name in methods.items()
        for kind, keys in METHOD_WORDS.items()
        if kind in normalized(name) and any(said(key, words) for key in keys)
    }
    return found.pop() if len(found) == 1 else None


def operation_on(operations, account, **where):
    """Return the first of ``operations`` ({id: operation}) whose account code starts with ``account`` and whose fields
    are ``where``, None when none."""
    for operation in operations.values():
        code = operation["account_id"][1] if operation["account_id"] else ""
        if code.startswith(account) and all(operation[key] == value for key, value in where.items()):
            return operation
    return None


def label_operation(line, operations, context):
    """Return (operation, tiers id or None) the line's label settles, (None, None) when it settles none.

    « CD AP » credits (the bank's card remittances) are sales on the dossier's generic customer « CLIENTS DIVERS »
    (3421): Vente Local, 79 of the 101 such lines in the cabinets' 159 dossiers, the others in suspense; only when the
    dossier has that one customer tiers.

    « TIMBRE » (« DROIT DE TIMBRE SUR VERSEMENT », 1 MAD on each cash deposit) is stamp duty: the dossier's
    stamp-duty operation (account 6167). The cabinets' dossiers do it on 90% of such lines (a pharmacy's 233 of 233);
    a model reading it as a bank tax was the commonest error of a test copy.

    « SECURICARTE » is the bank's card insurance: bank charges (61473), without TVA, as insurance is exempt. « DGI »
    on money paid is the TVA due when a TVA declaration of the dossier owes exactly that amount (its tva_due_unsigned,
    exp.deduction.table), to the TVA due account (4456); any other tax payment is for the cabinet.
    """
    words = normalized(line["payment_ref"])
    company = [("company_id", "=", context["allowed_company_ids"][0])]
    if side(line) == "inbound":
        if not words.startswith("cd ap "):
            return None, None
        generic = [*company, ("is_customer", "=", True), ("name", "=ilike", "CLIENTS DIVERS%")]
        found = odoo_call("exp.partner", "search", generic, context=context)
        return (operation_on(operations, "3421", type="sale"), found[0]) if len(found) == 1 else (None, None)
    if said("timbre", words):  # the stamp duty on a deposit: the stamp-duty operation (6167), not a bank tax
        return operation_on(operations, "6167", type=False), None
    if said("securicarte", words):
        return operation_on(operations, "61473", type="bank_fee", bank_type="frais_without_tax"), None
    amount = round(abs((line["credit"] or 0) - (line["debit"] or 0)), 2)
    if said("dgi", words):
        owed = [("tva_due_unsigned", ">=", amount - 0.005), ("tva_due_unsigned", "<=", amount + 0.005)]
        if odoo_call("exp.deduction.table", "search_count", [*company, *owed], context=context):
            return operation_on(operations, "4456"), None
    return None, None


def is_suspense(operation):
    """True for a « compte transitoire ou d'attente » operation (account 3497 or 4497, no type of its own)."""
    code = operation["account_id"][1] if operation["account_id"] else ""
    return not operation["type"] and code.startswith(("3497", "4497"))


def suspense_operation(operations, line):
    """Return the dossier's « compte transitoire ou d'attente » operation for the line's way: débiteurs (3497) for money
    paid, créditeurs (4497) for money received; None when the dossier has none."""
    way = side(line)
    found = operation_on(operations, "3497" if way == "outbound" else "4497", type=False)
    return found if found and found["payment_type"] in (way, False) else None


MEMORY_LINES = 3000  # the dossier's latest posted lines the label memory reads


def label_key(label):
    """Return a bank label as the dossier's history compares it: lower case, numbers and dates out, words of 2 letters
    or more (« PAIEMENT CHEQUE N 0373242 » and « ... 0373241 » are one label)."""
    words = re.sub(r"[^a-z ]", " ", re.sub(r"\d+([/.\-]\d+)*", " ", normalized(label or "")))
    return " ".join(word for word in words.split() if len(word) > 1)


def label_memory(dossier_id, context):
    """Return how the dossier's cabinet pointed each label before: {label key: {"operation" | "partner" | "method":
    Counter of the ids used}}, from its MEMORY_LINES latest posted lines."""
    found = odoo_call(
        LINE,
        "search_read",
        [("company_id", "=", dossier_id), ("state", "in", list(POSTED))],
        fields=["payment_ref", "operation_id", "partner_id", "exp_payment_method_id"],
        order="date desc, id desc",
        limit=MEMORY_LINES,
        context=context,
    )
    memory = defaultdict(lambda: {"operation": Counter(), "partner": Counter(), "method": Counter()})
    for row in found:
        key = label_key(row["payment_ref"])
        for field, name in (
            ("operation_id", "operation"),
            ("partner_id", "partner"),
            ("exp_payment_method_id", "method"),
        ):
            if key and row.get(field):
                memory[key][name][row[field][0]] += 1
    return memory


def remembered(memory, line, name, times=1):
    """Return the id the dossier always used for ``line``'s label (``name``: operation, partner or method), None when
    it never did, used two, or used it fewer than ``times``. Measured on the cabinets' 38k posted lines, each against
    the earlier ones of its dossier: the operation is right 97% of the time (59% of lines have one), the method 97%
    (57%), the tiers 98% when seen twice (37%) and 95% when seen once."""
    counts = memory.get(label_key(line["payment_ref"]), {}).get(name)
    if counts and len(counts) == 1:
        ((value, seen),) = counts.items()
        return value if seen >= times else None
    return None


def chosen_operations(statement, lines, context):
    """Give lines without an operation the dossier's operation, and purchase lines without a payment method the method
    (mandatory for a purchase, bank/models/bank_statement_line.py:4069-4075): first as the dossier always pointed that
    label before (label_memory), then as the model reads it in the label. The model picks among the dossier's
    operations (operations_of) of the line's way only, shown with the labels they were used for (past_labels), and
    answers null when the label does not say; a purchase line's payment method is then read from the label's own words
    (method_in_label) when it gave none. It is asked twice: only a line it answers the same both times is written, a
    label it reads two ways stays for the cabinet. Lines the memory settles are not put to it.

    The operation is typed in the line's form first: its onchange (_onchange_operation_type, :2095-2116) gives the
    tiers the operation carries; an RPC write runs no onchange, so that tiers is written with it.

    A line the model leaves is read by the label rules (label_operation); one still without an operation, the model
    having answered, goes to the dossier's suspense operation for its way (suspense_operation): the cabinet reclassifies
    it, and the client is told.

    Returns (how many lines got an operation or a payment method, how many went to suspense), counted also in test
    mode, where nothing is written.
    """
    asked = [
        line
        for line in lines
        if line["payment_ref"]
        and (not line["operation_id"] or line["operation_type"] == "purchase" and not line["exp_payment_method_id"])
    ]
    if not asked:
        return 0, 0
    dossier_id = statement["company_id"][0]
    operations = operations_of(dossier_id, context)
    methods = {m["id"]: m["name"] for m in odoo_call("exp.payment.method", "search_read", [("payment_id", "!=", 1)])}
    used = past_labels(dossier_id, context)
    memory, memorized = label_memory(dossier_id, context), {}
    waiting = {i for i, o in operations.items() if is_suspense(o)}  # Tarik's own parking is never the dossier's habit
    for line in asked:  # what the dossier always did with this label: no need to ask the model
        operation = None if line["operation_id"] else remembered(memory, line, "operation")
        operation = None if operation in waiting else operation
        method = remembered(memory, line, "method")
        memorized[line["id"]] = {
            "operation": operation if operation in operations else None,
            "payment_method": method if method in methods else None,
        }
    ruled = {}  # what the label's own words settle (label_operation), before the model is asked
    for line in asked:
        if not line["operation_id"] and not memorized[line["id"]]["operation"]:
            found, tiers = label_operation(line, operations, context)
            if found:
                ruled[line["id"]] = (found, tiers)
    to_model = [
        line
        for line in asked
        if line["id"] not in ruled
        and (not line["operation_id"] and not memorized[line["id"]]["operation"])
        or (
            line["operation_type"] == "purchase"
            and not line["exp_payment_method_id"]
            and not memorized[line["id"]]["payment_method"]
        )
    ]
    listed = "\n".join(
        f"{i} | {o['name']} | {o['type'] or 'autre'} | {o['payment_type'] or 'both'} | "
        f"{o['account_id'][1] if o['account_id'] else ''} | {' ; '.join(used.get(i, []))}"
        for i, o in operations.items()
    )
    shown = "\n".join(
        f"{line['id']} | {line['date']} | {(line['credit'] or 0) - (line['debit'] or 0):.2f} | {side(line)} | "
        f"{line['operation_id'][1] if line['operation_id'] else '?'} | {line['payment_ref']}"
        for line in to_model
    )
    instruction = (
        "Operations (id | name | type | way | account | labels it was used for):\n"
        f"{listed}\n\nPayment methods: {', '.join(f'{i} {name}' for i, name in methods.items())}\n\n"
        f"Lines (id | date | amount | way | operation, ? when none | label):\n{shown}\n\n"
        'Answer with only a JSON object: {"lines": [{"id": <line id>, "operation": <operation id> or null, '
        '"payment_method": <payment method id> or null}]}'
    )
    # shortcut: one model call per statement, every line in it; split it if statements of hundreds of lines come
    answered, agreed = True, {}
    try:
        for _ in range(2 if to_model else 0):  # the model agreeing with itself on under a quarter of the lines failed
            agreed = agreed_lines(tool_prompt("point-bank-lines"), instruction, "Pointage du relevé")
            if len(agreed) * 4 >= len(to_model):
                break
        else:
            if to_model:
                log(f"chosen_operations {statement['id']}: the model agreed on {len(agreed)} of {len(to_model)} lines")
                answered = False  # not « nothing says »: no line goes to suspense because of it
    except (ToolError, LookupError) as failure:  # no model now: the lines stay for the cabinet, the pointing goes on
        log(f"chosen_operations {statement['id']}: {failure}")
        agreed, answered = {}, False
    given = parked = 0
    for line in asked:
        remembered_here = {key: value for key, value in memorized[line["id"]].items() if value}
        choice = {**agreed.get(line["id"], {}), **remembered_here}  # the dossier's habit first, then the model
        values = {}
        operation = operations.get(choice.get("operation")) if choice and not line["operation_id"] else None
        label_tiers = None
        if not operation and not line["operation_id"]:
            operation, label_tiers = ruled.get(line["id"], (None, None))
        suspended = False
        if not operation and not line["operation_id"] and answered:  # nothing says what it is: suspense, told
            operation = suspense_operation(operations, line)
            suspended = operation is not None
        if operation and operation["payment_type"] in (side(line), False):
            form = {"operation_id": operation["id"]}
            type_in(LINE, line["id"], form, "operation_id", ("partner_id",), context)
            values = {
                "operation_id": operation["id"],
                **({"partner_id": form["partner_id"]} if form.get("partner_id") else {}),
            }
            if label_tiers and "partner_id" not in values:
                values["partner_id"] = label_tiers
        kind = operation["type"] if values else line["operation_type"]
        if kind == "purchase" and (values or not line["exp_payment_method_id"]):
            method = choice.get("payment_method")
            if not (type(method) is int and method in methods):
                method = method_in_label(line["payment_ref"], methods)
            if method:
                values["exp_payment_method_id"] = method
        if values:
            if not dry_run():
                odoo_call(LINE, "write", [line["id"]], values, context=context)
            parked, given = (parked + 1, given) if suspended else (parked, given + 1)
    return given, parked


def agreed_lines(system, instruction, what):
    """Ask the model ``instruction`` twice (with the ``system`` prompt) and return {line id: choice} for the
    choices it gave identically both times, one per line: a line listed twice, or with an id that is not a number, is
    a reading two ways and stays for the cabinet. Raises ToolError."""
    answers = [ask_model(system, [], instruction, what, keys=("lines",))["lines"] for _ in range(2)]
    first, second = (found if isinstance(found, list) else [] for found in answers)
    ids = [choice.get("id") for choice in first if isinstance(choice, dict)]
    return {
        choice["id"]: choice
        for choice in first
        if isinstance(choice, dict)
        and choice in second
        and type(choice.get("id")) is int
        and ids.count(choice["id"]) == 1
    }


def named_tiers(statement, lines, context):
    """Give lines of an operation needing a tiers (purchase, sale, other third party: :4184-4196) the one tiers their
    label names: its ICE (15 digits, as Experio keys tiers, exp_account/models/exp_partner.py:530-560) or its name
    as whole words (tiers_key), among those the line's tiers picker offers (its stored partner_id_domain, :46-76).
    A label naming two tiers or none gets none, unless the model reads one in it (read_tiers).

    Returns how many lines got one (counted also in test mode, where nothing is written).
    """
    needing = [
        line
        for line in lines
        if line["payment_ref"]
        and not line["partner_id"]
        and line["operation_type"] in ("purchase", "sale", "other_third_party")
        and line["partner_id_domain"]
    ]
    offered = {}  # domain: the tiers it offers, searched once
    given, left = 0, []
    for line in needing:
        domain = line["partner_id_domain"]
        if domain not in offered:
            found = odoo_call(
                "exp.partner", "search_read", ast.literal_eval(domain), fields=["name", "ice"], context=context
            )
            offered[domain] = [
                (p["id"], tiers_key(p["name"]), (p["ice"] or "").replace(" ", "").zfill(15), p["name"]) for p in found
            ]
        words = normalized(line["payment_ref"])
        ices = set(re.findall(r"(?<!\d)\d{15}(?!\d)", line["payment_ref"].replace(" ", "")))
        fits = {
            i for i, key, ice, _ in offered[domain] if (ice.strip("0") and ice in ices) or (key and said(key, words))
        }
        if len(fits) == 1:
            if not dry_run():
                odoo_call(LINE, "write", [line["id"]], {"partner_id": fits.pop()}, context=context)
            given += 1
        elif not fits:  # a label naming two tiers stays for the cabinet
            left.append(line)
    memory = label_memory(statement["company_id"][0], context) if left else {}
    for line in list(left):  # what the dossier always did with this label (seen twice), if the line accepts it
        partner_id = remembered(memory, line, "partner", times=2)
        if partner_id and partner_id in {i for i, *_ in offered[line["partner_id_domain"]]}:
            if not dry_run():
                odoo_call(LINE, "write", [line["id"]], {"partner_id": partner_id}, context=context)
            given += 1
            left.remove(line)
    chosen, names = read_tiers(left, offered)
    for line_id, partner_id in chosen.items():
        if not dry_run():
            odoo_call(LINE, "write", [line_id], {"partner_id": partner_id}, context=context)
        given += 1
    created, by_name, width = [], {}, cut_width(left, names)
    for line in left:  # a counterparty the dossier does not know yet: created, once per name, and told
        name = names.get(line["id"])
        if not name or line["operation_type"] not in ("purchase", "sale"):
            continue
        cut = name.rstrip().endswith("-") or (  # « SOLAR-CLIM- », or the bank cut the label and the name with it
            width
            and len(line["payment_ref"].strip()) == width
            and normalized(line["payment_ref"]).endswith(normalized(name))
        )
        made = {tiers_key(k[1]) or k[1]: i for k, i in by_name.items() if k[0] == line["operation_type"]}
        known = {tiers_key(n): i for i, _, _, n in offered[line["partner_id_domain"]]} | made
        partner_id = twin(tiers_key(name) or normalized(name), known, cut)  # « STE X SARL » is « x », as tiers keys
        key = (line["operation_type"], normalized(name))
        if partner_id is None and key not in by_name:
            if cut:  # a cut name is not a tiers' whole name: the line stays for the cabinet
                continue
            by_name[key] = (
                -1 - len(created) if dry_run() else created_tiers(line, name, context)
            )  # test mode: a stand-in
            created.append(name)
        if not dry_run():
            odoo_call(LINE, "write", [line["id"]], {"partner_id": partner_id or by_name[key]}, context=context)
        given += 1
    return given, created


def cut_width(lines, names):
    """Return the length at which the bank cut its labels, None when it cut none: the length (50 or more) at least 3
    different names share exactly, each ending its label (« …EN FAVEUR DE PAPETERIE-BENMI », 60 characters); natural
    labels ending with a name vary in length. A name repeated, or misread (« AIS INEC », « AIS INFC »), counts once."""
    ending = {}
    for line in lines:
        name = normalized(names.get(line["id"]) or "")
        if name and normalized(line["payment_ref"]).endswith(name):
            ending.setdefault(len(line["payment_ref"].strip()), set()).add((tiers_key(name) or name)[:6])
    return max((length for length, found in ending.items() if len(found) >= 3 and length >= 50), default=None)


# Characters a scan of a bank statement mistakes for one another (« AIS INEC » for « AIS INFC », « AGRL » for « AGRI »)
CONFUSED = [set("ef"), set("il1"), set("oc0q"), set("s5"), set("b8"), set("g6"), set("z2")]


def misread(a, b):
    """True when ``a`` and ``b`` are one name read twice: same length, one character apart, a pair a scan confuses
    (never two digits: « alpha 1 » and « alpha 2 » are two companies)."""
    apart = [(x, y) for x, y in zip(a, b, strict=True) if x != y]
    return len(apart) == 1 and any(set(apart[0]) <= pair for pair in CONFUSED)


def twin(name, known, cut):
    """Return the id of the one tiers in ``known`` ({key: id}, keys normalized) that ``name`` (normalized) is a
    misreading of (misread) or, when ``cut``, the start of; None when there is none or more than one."""
    found = {
        i
        for key, i in known.items()
        if key
        and len(name) >= 6
        and ((len(key) == len(name) and misread(key, name)) or (cut and key.startswith(name) and key != name))
    }
    return found.pop() if len(found) == 1 else None


LINES_PER_ASK = 50  # lines read by the model at once, so a long statement fits one answer
TIERS_LISTED = 400  # shortcut: a picker offering more tiers is not shown to the model, its lines stay for the cabinet


def read_tiers(lines, offered):
    """Return {line id: tiers id} for the ``lines`` whose label the model reads a tiers in, among the tiers the
    line's picker offers (``offered``: {domain: [(id, key, ice, name)]}): the tiers a label names by an
    abbreviation, a brand or a first name, which no whole-name match finds. Asked twice like chosen_operations (see
    agreed_lines), LINES_PER_ASK lines at a time; an id outside the line's own list never counts."""
    lines = [line for line in lines if len(offered[line["partner_id_domain"]]) <= TIERS_LISTED]
    chosen, names = {}, {}
    for start in range(0, len(lines), LINES_PER_ASK):
        group = lines[start : start + LINES_PER_ASK]
        domains = sorted({line["partner_id_domain"] for line in group})
        lists = "\n\n".join(
            f"List {n}:\n" + "\n".join(f"{i} | {name}" for i, _, _, name in offered[domain])
            for n, domain in enumerate(domains)
        )
        shown = "\n".join(
            f"{line['id']} | list {domains.index(line['partner_id_domain'])} | {line['date']} | "
            f"{(line['credit'] or 0) - (line['debit'] or 0):.2f} | {line['payment_ref']}"
            for line in group
        )
        instruction = (
            f"Tiers (id | name):\n{lists}\n\nLines (id | its tiers list | date | amount | label):\n{shown}\n\n"
            'Answer with only a JSON object: {"lines": [{"id": <line id>, "tiers": <tiers id> or null, '
            '"name": <the counterparty\'s name as written in the label> or null}]}'
        )
        try:
            agreed = agreed_lines(tool_prompt("point-line-tiers"), instruction, "Tiers des lignes")
        except (ToolError, LookupError) as failure:  # no model now: the lines stay for the cabinet
            log(f"read_tiers: {failure}")
            return chosen, names
        for line in group:
            choice = agreed.get(line["id"], {})
            tiers = choice.get("tiers")
            if type(tiers) is int and tiers in {i for i, *_ in offered[line["partner_id_domain"]]}:
                chosen[line["id"]] = tiers
            elif tiers is None and written_name(choice.get("name"), line["payment_ref"]):
                names[line["id"]] = " ".join(choice["name"].split())
    return chosen, names


def written_name(name, label):
    """True when ``name`` is a counterparty's name the label really carries: 4 to 60 characters, letters in it, every
    word of it in the label (a name the model made up or completed is not one)."""
    if not isinstance(name, str) or not 4 <= len(name.strip()) <= 60 or not re.search(r"[A-Za-z]{3}", name):
        return False
    return all(said(word, normalized(label)) for word in normalized(name).split())


def created_tiers(line, name, context):
    """Create the tiers ``name`` for ``line`` (a customer for a sale, a supplier for a purchase) in its dossier, with
    the Compte Tiers its dossier's series gives when Experio's own numbering is off; returns its id.

    Experio's partner create runs its own checks and numbering (exp.partner, autofill); a name only, as Tarik creates a
    tiers for an invoice when the scan prints no ICE."""
    kind = "sale" if line["operation_type"] == "sale" else "purchase"
    side = SIDE[kind]
    company_id = context["allowed_company_ids"][0]
    values = {"name": name, "type": "company", "company_id": company_id, side["side"]: True}
    partner_id = odoo_call("exp.partner", "create", values, context=context)
    made = odoo_call("exp.partner", "read", [partner_id], fields=[side["account"]], context=context)[0]
    if not made[side["account"]]:  # numbering off: the next code of the dossier's own series, told with the tiers
        code = series_account({"ice": False, "vat": False}, kind, context)
        if code:
            odoo_call("exp.partner", "write", [partner_id], {side["account"]: code}, context=context)
    return partner_id


def owed_invoices(statement, lines, context):
    """Give purchase and sale lines with no invoice the one open invoice owing exactly their amount among those their
    invoice picker offers (picked's domain), on no statement line yet: Experio's detection's own rule
    (bank/models/bank_statement_line.py:2440-2475), for the lines its detection did not fill. A line with no tiers yet
    is searched among every tiers' invoices, and the invoice gives it its tiers (a cheque line names none). Two such
    invoices: none (the detection takes the first; Tarik leaves it to the cabinet), except that a line with no tiers
    yet still gets the tiers when all those invoices are one tiers' (a cheque paying one of three equal invoices of
    the same supplier names the supplier). The line's form must accept it (typed_in).

    Returns (how many lines got an invoice, how many only a tiers), counted also in test mode, where nothing is
    written.
    """
    given, only_tiers, taken = 0, 0, set()
    for line in lines:
        amount = amount_of(line)
        if not amount:
            continue
        domain = ast.literal_eval(line["invoice_ids_domain"]) + [("amount_residual", "=", amount)]
        found = set(odoo_call("exp.invoice", "search", domain, context=context)) - taken
        used = odoo_call(
            LINE, "search_read", [("invoice_ids", "in", list(found))], fields=["invoice_ids"], context=context
        )
        found -= {i for row in used for i in row["invoice_ids"]}
        if len(found) > 1 and not line["partner_id"]:
            read = odoo_call("exp.invoice", "read", list(found), fields=["partner_id"])
            owners = {row["partner_id"] and row["partner_id"][0] for row in read}
            if len(owners) == 1 and False not in owners:
                if not dry_run():
                    odoo_call(LINE, "write", [line["id"]], {"partner_id": owners.pop()}, context=context)
                only_tiers += 1
        if len(found) != 1:
            continue
        invoice_id = found.pop()
        values, _ = typed_in(line, invoice_id, context)
        if values:
            if not dry_run():
                odoo_call(LINE, "write", [line["id"]], values, context=context)
            taken.add(invoice_id)
            given += 1
    return given, only_tiers


LEDGER_DAYS = 400  # how far back a supplier entry still explains a payment


def ledger_tiers(statement, lines, context):
    """Give purchase lines paid out with no tiers and no invoice the one supplier the dossier's ledger names for them.

    Two pieces of evidence, the first that answers: (1) another entry quoting the transfer or effet number in its
    label (the cabinet booked the payment when it issued it); (2) one unpaid supplier entry (credit on a 4411 account,
    not reconciled) of exactly the line's amount in the LEDGER_DAYS before its date. Only a single supplier, and one
    the line's tiers picker accepts. Measured on the cabinets' 159 dossiers (their own entries left out): (1) was right
    for 62 of 65 transfers and 13 of 17 effets, but 15 of 35 cheques: a cheque number counts only on an entry of the
    line's own amount; (2) found 32 and was right 27 times on cheque lines with no invoice. A tiers Tarik infers, said
    as such in the report for the cabinet to check.

    Returns how many lines got one (counted also in test mode, where nothing is written).
    """
    company, given = context["allowed_company_ids"][0], 0
    for line in lines:
        amount = amount_of(line)
        if (
            not amount
            or line["partner_id"]
            or line["operation_type"] != "purchase"
            or not (line["debit"] or 0) > (line["credit"] or 0)
            or not line["partner_id_domain"]
        ):
            continue
        own = ("move_id.bank_statement_line_id", "!=", line["id"])
        base = [("company_id", "=", company), ("partner_id", "!=", False), own]
        cheque = any(word in normalized(line["payment_ref"]) for word in ("cheque", "cheoue", "chq"))
        numbers = re.findall(r"\d{6,}", line["payment_ref"])[:1]
        wanted = [[*base, ("name", "ilike", number)] for number in numbers]  # (1) then (2)
        if line["date"]:  # undated (Experio asks for one): no window before it, so no evidence (2)
            since = (date.fromisoformat(line["date"]) - timedelta(days=LEDGER_DAYS)).isoformat()
            wanted.append(
                [
                    *base,
                    ("account_id.code", "=like", "4411%"),
                    ("credit", "=", amount),
                    ("reconciled", "=", False),
                    ("date", "<=", line["date"]),
                    ("date", ">=", since),
                ]
            )
        for n, domain in enumerate(wanted):
            fields = ["partner_id", "debit", "credit"]
            found = odoo_call("exp.move.line", "search_read", domain, fields=fields, limit=20, context=context)
            if n == 0 and numbers and cheque:  # a cheque's number alone is weak: its own amount too, dated or not
                found = [e for e in found if round(abs((e["debit"] or 0) - (e["credit"] or 0)), 2) == amount]
            suppliers = {entry["partner_id"][0] for entry in found}
            if not suppliers:
                continue
            accepted = len(suppliers) == 1 and odoo_call(
                "exp.partner",
                "search_count",
                [*ast.literal_eval(line["partner_id_domain"]), ("id", "in", list(suppliers))],
                context=context,
            )
            if accepted:
                if not dry_run():
                    odoo_call(LINE, "write", [line["id"]], {"partner_id": suppliers.pop()}, context=context)
                given += 1
            break  # the first evidence that answers decides, right or not
    return given


def blocked_reasons(statement, context):
    """Return (reasons, ready, posted) for the statement's lines.

    reasons: {Experio's reason: how many lines} for the draft and pending lines (Experio's own error_msg, the red
    icon's text, else tpe.NO_REASON). ready and posted: how many lines are ready, and posted or exported.
    """
    lines = odoo_call(LINE, "read", statement["line_ids"], fields=["state", "error_msg"], context=context)
    reasons = {}
    for line in lines:
        if line["state"] in ("draft", "pending"):
            reason = plain(line["error_msg"] or "") or tpe.NO_REASON
            reasons[reason] = reasons.get(reason, 0) + 1
    return reasons, sum(line["state"] == "ready" for line in lines), sum(line["state"] in POSTED for line in lines)


def opening_note(statement, context):
    """Compare the journal's first statement's starting balance with the bank account's à-nouveau of its exercice.

    Returns a note for the reply when they differ (in absolute value), else "" (also when the statement is not the
    journal's first, or no à-nouveau was posted). Experio checks nothing there: a journal's first statement is always
    « valid » (_get_statement_validity finds no previous statement).
    """
    domain = [("journal_id", "=", statement["journal_id"][0]), ("id", "<", statement["id"]), ("state", "!=", "cancel")]
    if odoo_call(STATEMENT, "search_count", domain, context=context):
        return ""
    journal = odoo_call(
        "exp.journal", "read", [statement["journal_id"][0]], fields=["default_account_id"], context=context
    )
    account = journal[0]["default_account_id"]
    if not account or not statement["date"]:
        return ""
    lines = odoo_call(
        "exp.move.line",
        "search_read",
        [
            ("account_id", "=", account[0]),
            ("move_id.move_category", "=", "carry_forward"),
            ("parent_state", "in", ["posted", "exported"]),
        ],
        fields=["balance", "date"],
        context=context,
    )
    year = statement["date"][:4]
    lines = [line for line in lines if (line["date"] or "")[:4] == year]  # only the statement's exercice's
    carried = round(sum(line["balance"] for line in lines), 2)
    start = round(statement["balance_start"] or 0, 2)
    if not lines or abs(abs(carried) - abs(start)) < 0.01:
        return ""
    return (
        f"C'est le premier relevé du journal, et son solde initial ({start:.2f}) diffère de l'à-nouveau du compte "
        f"({abs(carried):.2f})."
    )


READ_DEADLINES = {}


def being_read(statement):
    """True while Experio has not read the bank statement yet.

    Experio's reading (bluejay set_bank_statement) sets state draft and is_detected; bluejay_state stays « pending »,
    posted statements included, so it cannot be used.
    """
    return statement["state"] == "pending" and not statement["is_detected"]


@step_tool
def settle_statement(file_id: int, run_id: str = "") -> dict:
    """Take a bank or TPE statement up after its import, once Experio has read it.

    Args:
        file_id: the imported file.
        run_id: the current run; its wait for Experio's reading is capped at 10 minutes across calls.

    Waits for Experio's reading (a wait result past the deadline). A TPE statement is settled whole by
    tpe.settle_tpe. A bank statement gets a routine ok result with its statement_id and bank True: its pointing steps
    follow (detect_statement_lines to report_statement).
    """
    statement_id, network = statement_of(file_id)
    if not statement_id:
        return routine(f"Fichier n° {file_id} : pas de relevé importé.")
    deadline = READ_DEADLINES.setdefault(run_id, time.monotonic() + 600) if run_id else time.monotonic() + 600
    read, reading = (tpe.read_tpe, tpe.being_read) if network else (read_statement, being_read)
    statement = read(statement_id)
    while reading(statement):
        if time.monotonic() >= deadline:
            return wait(f"Relevé n° {statement_id} : Experio le lit encore, il sera repris à votre prochain message.")
        time.sleep(15)
        statement = read(statement_id)
    if network:
        with refused(
            f"settle_statement {statement_id}", f"Relevé TPE n° {statement_id} : Experio a refusé ({{reason}})."
        ):
            return tpe.settle_tpe(statement_id)
    with cabinet_db() as db:
        asked = bool(db.execute(Q.file_post_asked, (file_id,)).fetchone()[0])
    return routine(
        f"Relevé n° {statement_id} : lu par Experio.", statement_id=statement_id, bank=True, post_asked=asked
    )


# A bank statement's pointing, one step each, as the cabinet works the statement's « Operations » list: Experio's own
# detection, then what it leaves, then where the statement stands (report_statement). Each step says what it did in
# ``said`` (its sentences, one per line) and ``note`` (the same, for what is left), routine results the report gathers.


def completing(statement_id, step, work):
    """Run ``work(statement, context)`` -> (said, note) on bank statement ``statement_id`` and return its routine
    result, ``said`` and ``note`` in it; nothing on a posted statement. Experio's refusal is a ToolError naming the
    statement."""
    statement = read_statement(statement_id)
    if statement["state"] in POSTED:
        return routine(f"Relevé n° {statement_id} : déjà comptabilisé.", statement_id=statement_id)
    context = in_dossier(statement["company_id"][0])
    with refused(f"{step} {statement_id}", f"Relevé n° {statement_id} : Experio a refusé ({{reason}})."):
        said, note = work(statement, context)
    done = " ".join(said.splitlines()) or "rien d'autre à faire."
    text = f"{TEST_MODE if dry_run() else ''}Relevé n° {statement_id} : {done}"
    return routine(text, statement_id=statement_id, said=said, note=note)


def sentence(count, one, many):
    """Return words.counted's sentence for ``count``, "" for none (nothing to say)."""
    return counted(count, one, many) if count else ""


@step_tool
def detect_statement_lines(statement_id: int) -> dict:
    """Run Experio's « Auto Point » on bank statement ``statement_id`` and confirm what it finds (auto_point)."""
    return completing(
        statement_id,
        "detect_statement_lines",
        lambda statement, context: (
            sentence(
                auto_point(statement, context),
                "{} opération a été pointée par la détection d'Experio.",
                "{} opérations ont été pointées par la détection d'Experio.",
            ),
            "",
        ),
    )


@step_tool
def choose_line_operations(statement_id: int) -> dict:
    """Give the statement's lines the operation and payment method the model reads in their label
    (chosen_operations)."""

    def work(statement, context):
        given, parked = chosen_operations(statement, open_lines(statement, context), context)
        dossier_id = statement["company_id"][0]
        note = ""
        lacking = any(not line["operation_id"] for line in open_lines(statement, context))  # as left, shared ones used
        domain = [("company_id", "=", dossier_id)]
        own = lacking and odoo_call("bank.operation.type", "search_count", domain, context=context)
        if lacking and not own:  # a dossier made without a model dossier
            note = (
                "Le dossier n'a aucune opération bancaire à lui : pour que je pointe ses relevés, dites-moi le "
                "dossier modèle dont reprendre le paramétrage.\n"
            )
        note += sentence(
            parked,
            "{} opération dont le libellé ne dit rien est en compte d'attente (3497 ou 4497) : le cabinet doit la "
            "reclasser.",
            "{} opérations dont le libellé ne dit rien sont en compte d'attente (3497 ou 4497) : le cabinet doit les "
            "reclasser.",
        )
        said = sentence(
            given,
            "{} opération a reçu son type d'opération ou son mode de paiement, que j'ai lu dans son libellé.",
            "{} opérations ont reçu leur type d'opération ou leur mode de paiement, que j'ai lu dans leur libellé.",
        )
        return said, note.strip()

    return completing(statement_id, "choose_line_operations", work)


@step_tool
def name_line_tiers(statement_id: int) -> dict:
    """Give the statement's lines the one tiers their label names, or one created from the name the label carries
    (named_tiers)."""

    def work(statement, context):
        given, created = named_tiers(statement, open_lines(statement, context), context)
        names = ", ".join(f"`{name}`" for name in created)
        note = counted(len(created), "J'ai créé le tiers", "J'ai créé les tiers") + f" {names} d'après le libellé."
        said = sentence(
            given,
            "{} opération a reçu le tiers que son libellé nomme.",
            "{} opérations ont reçu le tiers que leur libellé nomme.",
        )
        return said, note if created else ""

    return completing(
        statement_id,
        "name_line_tiers",
        work,
    )


@step_tool
def tie_cited_invoices(statement_id: int, client_message: str = "") -> dict:
    """Give the statement's lines the invoice the client names by its number in ``client_message`` (named_invoices),
    then the one whose number their own label carries (label_invoices)."""

    def work(statement, context):
        tied, unnamed = named_invoices(statement, client_message, context)
        labelled = label_invoices(statement, open_lines(statement, context), context)  # tied above: no longer open
        said = sentence(
            tied,
            "{} opération est rattachée à la facture que vous avez citée.",
            "{} opérations sont rattachées aux factures que vous avez citées.",
        )
        also = sentence(
            labelled,
            "{} opération est rattachée à la facture dont son libellé porte le numéro.",
            "{} opérations sont rattachées à la facture dont leur libellé porte le numéro.",
        )
        return "\n".join(filter(None, [said, also])), unnamed

    return completing(statement_id, "tie_cited_invoices", work)


@step_tool
def tie_owed_invoices(statement_id: int) -> dict:
    """Give the statement's lines the one open invoice owing their amount, or only its tiers (owed_invoices), then
    the supplier the ledger names for a payment still without one (ledger_tiers)."""

    def work(statement, context):
        tied, only_tiers = owed_invoices(statement, open_lines(statement, context), context)
        ledger = ledger_tiers(statement, open_lines(statement, context), context)
        said = sentence(
            tied,
            "{} opération a reçu la seule facture ouverte de son tiers et de son montant.",
            "{} opérations ont reçu la seule facture ouverte de leur tiers et de leur montant.",
        )
        also = sentence(
            only_tiers,
            "{} opération a reçu le tiers des factures ouvertes de son montant, qui sont toutes de lui.",
            "{} opérations ont reçu le tiers des factures ouvertes de leur montant, qui sont toutes de lui.",
        )
        found = sentence(
            ledger,
            "{} opération a reçu son tiers, retrouvé dans le grand livre.",
            "{} opérations ont reçu leur tiers, retrouvé dans le grand livre.",
        )
        note = sentence(
            ledger,
            "Ce tiers vient d'une écriture citant le numéro du chèque ou du virement, ou d'une facture fournisseur "
            "impayée de ce montant : le cabinet doit le vérifier.",
            "Ces tiers viennent d'une écriture citant le numéro du chèque ou du virement, ou d'une facture "
            "fournisseur impayée de ce montant : le cabinet doit les vérifier.",
        )
        return "\n".join(filter(None, [said, also, found])), note

    return completing(statement_id, "tie_owed_invoices", work)


@step_tool
def tie_tpe_remittances(statement_id: int) -> dict:
    """Tie the statement's TPE remittances to their TPE statement when Experio is sure (tie_tpe)."""

    def work(statement, context):
        tied, left = tie_tpe(statement, context) or (0, 0)
        note = sentence(
            left,
            "{} versement TPE n'a pas de relevé TPE certain : le cabinet doit le rapprocher.",
            "{} versements TPE n'ont pas de relevé TPE certain : le cabinet doit les rapprocher.",
        )
        said = sentence(
            tied,
            "{} versement TPE est rapproché de son relevé TPE.",
            "{} versements TPE sont rapprochés de leur relevé TPE.",
        )
        return said, note

    return completing(statement_id, "tie_tpe_remittances", work)


@step_tool
def report_statement(statement_id: int, said: list[str] | None = None, notes: list[str] | None = None) -> dict:
    """Say where bank statement ``statement_id`` stands after its pointing, for the client.

    Args:
        statement_id: the statement.
        said: what the pointing steps did, their sentences one per line (« 18 opérations ont été pointées par la
            détection d'Experio. »).
        notes: what they left, the same way.

    A headline with how many operations were read, ready and posted, then one « - » line per sentence of ``said``,
    per balance check (the statement's own, the journal's previous statement, the à-nouveau for the journal's first),
    per sentence of ``notes``, and per reason Experio gives for lines it still blocks."""
    statement = read_statement(statement_id)
    context = in_dossier(statement["company_id"][0])
    with refused(f"report_statement {statement_id}", f"Relevé n° {statement_id} : Experio a refusé ({{reason}})."):
        reasons, ready, posted = blocked_reasons(statement, context)
        opening = opening_note(statement, context)
    read = counted(len(statement["line_ids"]), "{} opération", "{} opérations")
    text = f"Relevé n° {statement_id} : j'ai lu {read}, " + (
        counted(ready, "{} est prête à comptabiliser", "{} sont prêtes à comptabiliser")
        if ready
        else "aucune n'est prête à comptabiliser"
    )
    text += counted(posted, " et {} est déjà comptabilisée", " et {} sont déjà comptabilisées") if posted else ""
    points = [line for done in said or [] for line in done.splitlines()]
    no_balances = not statement["balance_start"] and not statement["balance_end_real"]
    if no_balances:  # Excel's template has none; a scan Experio read no balance on
        points.append(
            "Le relevé n'indique pas ses soldes : saisissez-les dans Experio pour vérifier qu'il est complet."
        )
    elif not statement["is_complete"]:
        points.append(
            f"Le solde final calculé ({statement['balance_end']:.2f}) ne correspond pas au solde du relevé "
            f"({statement['balance_end_real']:.2f}) : une opération manque ou est mal lue."
        )
    if not statement["is_valid"]:
        points.append("Le solde initial du relevé ne suit pas le solde final du relevé précédent de ce journal.")
    points += [line for note in [opening, *(notes or [])] for line in note.splitlines()]
    points += reason_lines(reasons, "{} opération à compléter dans Experio", "{} opérations à compléter dans Experio")
    text += "." + "".join(f"\n- {point}" for point in points)
    return ok(f"{TEST_MODE if dry_run() else ''}{text}", statement_id=statement_id)


def tie_tpe(statement, context):
    """Tie the statement's TPE remittance lines to their TPE statement's net remise line, sure matches only.

    Uses Experio's own « Rapprochement automatique TPE » (module tpe_reconcile, action_reconcile_tpe_auto): a bank
    line of operation type « monetary_operations » matches a SOLDE NET REMISE (funds_transfer) TPE line of the same
    amount, dated on or before the bank line (find_matching_tpe_lines). Only « matched » lines (a single candidate) are
    written, as the wizard's « Reconcile » does.

    Returns (tied, left): lines tied, and remittance lines left without a sure match. Returns None when tpe_reconcile
    is not installed: nothing to tie, and Experio then neither requires nor letters it.
    """
    installed = odoo_call(
        "ir.module.module", "search_count", [("name", "=", "tpe_reconcile"), ("state", "=", "installed")]
    )
    if not installed:
        return None
    lines = odoo_call(LINE, "read", statement["line_ids"], fields=["operation_type", "tpe_line_id"], context=context)
    open_lines = [
        line["id"] for line in lines if line.get("operation_type") == "monetary_operations" and not line["tpe_line_id"]
    ]
    if not open_lines:
        return 0, 0
    action = odoo_json(LINE, "action_reconcile_tpe_auto", open_lines, context=context)
    if not isinstance(action, dict) or not action.get("res_id"):
        return 0, len(open_lines)
    wizard = odoo_call("tpe.auto.reconcile.wizard", "read", [action["res_id"]], fields=["line_ids"], context=context)[0]
    found = odoo_call(
        "tpe.auto.reconcile.wizard.line",
        "read",
        wizard["line_ids"],
        fields=["line_id", "tpe_line_id", "state"],
        context=context,
    )
    sure = [f for f in found if f["state"] == "matched" and f["tpe_line_id"]]
    if not dry_run():
        for match in sure:  # what the wizard's « Reconcile » writes, for its sure lines only
            odoo_call(LINE, "write", [match["line_id"][0]], {"tpe_line_id": match["tpe_line_id"][0]}, context=context)
    return len(sure), len(open_lines) - len(sure)


@step_tool
def post_statement(file_id: int) -> dict:
    """Post a bank or TPE statement's ready operations with Experio's own validation (action_post).

    A TPE statement goes to tpe.post_tpe. For a bank statement, a window Experio opens instead (a duplicate
    statement, operations out of its period, an exchange rate still to fetch) or Experio's refusal returns a stop
    result for the cabinet. Otherwise an ok result with how many operations were posted, how many are left to
    complete, and the posted ones still unpaid or partly paid (suspens), one « - » line each under a headline.
    """
    statement_id, network = statement_of(file_id)
    if not statement_id:
        return routine(f"Fichier n° {file_id} : pas de relevé à comptabiliser.")
    if network:
        with refused(
            f"post_statement {statement_id}", f"Relevé TPE n° {statement_id} : Experio a refusé ({{reason}})."
        ):
            return tpe.post_tpe(statement_id)
    return posting(read_statement(statement_id))


def posting(statement):
    """Post bank ``statement`` (read with FIELDS): post_statement's result."""
    statement_id = statement["id"]
    if statement["state"] in POSTED:
        return routine(f"Relevé n° {statement_id} : déjà comptabilisé.")
    context = in_dossier(statement["company_id"][0])
    if dry_run():
        return ok(f"{TEST_MODE}Relevé n° {statement_id} : ses opérations prêtes seraient comptabilisées.")
    try:
        action = with_fiscal_year(odoo_json(STATEMENT, "action_post", [statement_id], context=context), context)
    except Exception as error:  # Experio's refusal (complete validation required, exact duplicate): its own words
        log(f"post_statement {statement_id}: {error}")
        if not is_refusal(error):
            return stop(f"Relevé n° {statement_id} : {CRASHED}")
        return stop(f"Relevé n° {statement_id} : pas comptabilisé, Experio refuse : {experio_refusal(error)}")
    if isinstance(action, dict) and action.get("res_model"):
        window = f"la fenêtre « {action['name']} »" if action.get("name") else "une fenêtre de confirmation"
        return stop(
            f"Relevé n° {statement_id} : pas comptabilisé, Experio ouvre à l'écran {window} : à valider par le "
            "cabinet dans Experio."
        )
    statement = read_statement(statement_id)
    reasons, ready, posted = blocked_reasons(statement, context)
    pending = suspense(statement, context)
    text = f"Relevé n° {statement_id} : j'ai comptabilisé {counted(posted, '{} opération', '{} opérations')}."
    left = sum(reasons.values())
    if left:
        text += counted(
            left,
            "\n- {} opération reste à compléter dans Experio.",
            "\n- {} opérations restent à compléter dans Experio.",
        )
    if pending:
        text += f"\n- {pending}"
    return ok(text, statement_id=statement_id)


def suspense(statement, context):
    """Return the statement's posted operations still unpaid or partly paid, as a sentence for the reply ("" when
    none).

    Purchase, sale and their advance operations with payment_state unpaid or partial and not reconciled, as Experio's
    « Suspense » screen lists them: « libellé (montant) », the first 10 then how many others.
    """
    fields = ["state", "operation_type", "payment_state", "payment_ref", "debit", "credit", "is_reconciled"]
    lines = odoo_call(LINE, "read", statement["line_ids"], fields=fields, context=context)
    left = [
        f"{line['payment_ref']} ({abs((line['credit'] or 0) - (line['debit'] or 0)):.2f})"
        for line in lines
        if line["state"] in POSTED
        and line.get("operation_type") in ("purchase", "sale", "purchase_advance", "sale_advance")
        and line.get("payment_state") in ("unpaid", "partial")
        and not line.get("is_reconciled")
    ]
    if not left:
        return ""
    listed = ", ".join(left[:10]) + (counted(len(left) - 10, " et {} autre", " et {} autres") if len(left) > 10 else "")
    said = counted(
        len(left),
        "{} opération comptabilisée reste en suspens, payée sans facture rapprochée : ",
        "{} opérations comptabilisées restent en suspens, payées sans facture rapprochée : ",
    )
    return f"{said}{listed}."


# A dossier's statements already in Experio (imported by the cabinet, or by Tarik earlier): those not posted yet, read
# by Experio (pending is a statement still read, or a line's exchange rate still fetched: left for later).
OPEN_STATEMENTS = ("draft", "partial")


def open_statements(dossier_id):
    """Return dossier ``dossier_id``'s bank statements not posted yet, read with FIELDS, oldest first."""
    domain = [("company_id", "=", dossier_id), ("state", "in", list(OPEN_STATEMENTS))]
    found = odoo_call(STATEMENT, "search", domain, order="date asc, id asc", context=in_dossier(dossier_id))
    return odoo_call(STATEMENT, "read", found, fields=FIELDS, context=in_dossier(dossier_id)) if found else []


NO_STATEMENT = "Dossier n° {} : aucun relevé bancaire à pointer ou comptabiliser."


@step_tool
def list_open_statements(dossier_id: int) -> dict:
    """List dossier ``dossier_id``'s bank statements already in Experio and not posted yet (open_statements), each to be
    pointed step by step: a routine ok result with their ids (statement_ids), oldest first."""
    found = [statement["id"] for statement in open_statements(dossier_id)]
    if not found:
        return routine(NO_STATEMENT.format(dossier_id), statement_ids=[])
    numbers = ", ".join(f"n° {i}" for i in found)
    found_text = counted(len(found), "{} relevé à pointer", "{} relevés à pointer")
    return routine(f"Dossier n° {dossier_id} : j'ai trouvé {found_text} ({numbers}).", statement_ids=found)


def each_statement(dossier_id, step):
    """Run ``step`` (a statement read with FIELDS -> a step result) on each of the dossier's open statements and return
    one ok result, with their ids (statement_ids): a headline then a « - » item per statement, its own sub-lines
    indented under it; a routine result when there is none."""
    statements = open_statements(dossier_id)
    if not statements:
        return routine(NO_STATEMENT.format(dossier_id))
    results = []
    for statement in statements:
        try:
            results.append(step(statement))
        except ToolError as failure:  # one statement Experio refuses: said, the others still done
            results.append({"message": str(failure)})
    dossier = statements[0]["company_id"][1]  # read with FIELDS: the dossier's name, no Experio read for it
    text = f"Dossier `{dossier}`, " + counted(len(results), "{} relevé :", "{} relevés :")
    text += "".join("\n- " + result["message"].replace("\n", "\n  ") for result in results)
    return ok(text, statement_ids=[statement["id"] for statement in statements])


@step_tool
def post_dossier_statements(dossier_id: int) -> dict:
    """Post the ready operations of every bank statement of dossier ``dossier_id`` not posted yet, with Experio's own
    validation, as post_statement posts one (posting); each statement's outcome said."""
    return each_statement(dossier_id, posting)
