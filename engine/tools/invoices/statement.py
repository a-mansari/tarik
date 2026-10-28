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

import openpyxl

from engine.tools.core.app import ToolError, log, ok, routine, step_tool, stop, wait
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import (
    CHECK_REFUSALS,
    ExperioRefusal,
    dry_run,
    experio_refusal,
    in_dossier,
    odoo_call,
    odoo_json,
    plain,
    refused,
    type_in,
)
from engine.tools.core.model import ask_model, tool_prompt
from engine.tools.core.queries import Q
from engine.tools.core.words import as_number, normalized, said, without_if
from engine.tools.invoices import tpe
from engine.tools.invoices.check import resolve_date

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
    sentence for the reply naming each such invoice no line got, and why ("" when there is none).
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
    why = {i: "déjà sur une opération d'un autre relevé" for i in ids if i in elsewhere}  # {invoice: why not given}
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
    left = " ; ".join(
        f"{c['invoice_number']} : {why[c['id']]}" for c in named if c["id"] in why and c["id"] not in given
    )
    return len(given), left and f" Facture(s) citée(s) non rattachée(s) : {left}."


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
LEGAL_FORMS = {"ste", "societe", "sarl", "sarlau", "sa", "sas", "au", "ets", "etablissements", "cie"}


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
    fields = ["name", "type", "payment_type", "account_id", "error_msg"]
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


def chosen_operations(statement, lines, context):
    """Give lines without an operation the dossier's operation the model reads in their label, and purchase lines
    without a payment method the method it reads (mandatory for a purchase, bank/models/bank_statement_line.py:
    4069-4075). The model picks among the dossier's operations (operations_of) of the line's way only, shown with
    the labels they were used for (past_labels), and answers null when the label does not say. It is asked twice:
    only a line it answers the same both times is written, a label it reads two ways stays for the cabinet.

    The operation is typed in the line's form first: its onchange (_onchange_operation_type, :2095-2116) gives the
    tiers the operation carries; an RPC write runs no onchange, so that tiers is written with it.

    Returns how many lines got an operation or a payment method (counted also in test mode, where nothing is
    written).
    """
    asked = [
        line
        for line in lines
        if line["payment_ref"]
        and (not line["operation_id"] or line["operation_type"] == "purchase" and not line["exp_payment_method_id"])
    ]
    if not asked:
        return 0
    dossier_id = statement["company_id"][0]
    operations = operations_of(dossier_id, context)
    methods = {m["id"]: m["name"] for m in odoo_call("exp.payment.method", "search_read", [("payment_id", "!=", 1)])}
    used = past_labels(dossier_id, context)
    listed = "\n".join(
        f"{i} | {o['name']} | {o['type'] or 'autre'} | {o['payment_type'] or 'both'} | "
        f"{o['account_id'][1] if o['account_id'] else ''} | {' ; '.join(used.get(i, []))}"
        for i, o in operations.items()
    )
    shown = "\n".join(
        f"{line['id']} | {line['date']} | {(line['credit'] or 0) - (line['debit'] or 0):.2f} | {side(line)} | "
        f"{line['operation_id'][1] if line['operation_id'] else '?'} | {line['payment_ref']}"
        for line in asked
    )
    instruction = (
        "Operations (id | name | type | way | account | labels it was used for):\n"
        f"{listed}\n\nPayment methods: {', '.join(f'{i} {name}' for i, name in methods.items())}\n\n"
        f"Lines (id | date | amount | way | operation, ? when none | label):\n{shown}\n\n"
        'Answer with only a JSON object: {"lines": [{"id": <line id>, "operation": <operation id> or null, '
        '"payment_method": <payment method id> or null}]}'
    )
    # shortcut: one model call per statement, every line in it; split it if statements of hundreds of lines come
    try:
        answers = [
            ask_model(tool_prompt("point-bank-lines"), [], instruction, "Pointage du relevé", keys=("lines",))["lines"]
            for _ in range(2)
        ]
    except ToolError as failure:  # no model now: the lines stay for the cabinet, the rest of the pointing goes on
        log(f"chosen_operations {statement['id']}: {failure}")
        return 0
    given = 0
    by_id = {line["id"]: line for line in asked}
    first, second = (found if isinstance(found, list) else [] for found in answers)
    for choice in [choice for choice in first if choice in second]:  # the same answer twice
        line = by_id.pop(choice.get("id"), None) if isinstance(choice, dict) else None
        if not line:
            continue
        values = {}
        operation = operations.get(choice.get("operation")) if not line["operation_id"] else None
        if operation and operation["payment_type"] in (side(line), False):
            form = {"operation_id": operation["id"]}
            type_in(LINE, line["id"], form, "operation_id", ("partner_id",), context)
            values = {
                "operation_id": operation["id"],
                **({"partner_id": form["partner_id"]} if form.get("partner_id") else {}),
            }
        kind = operation["type"] if values else line["operation_type"]
        if (
            kind == "purchase"
            and choice.get("payment_method") in methods
            and (values or not line["exp_payment_method_id"])
        ):
            values["exp_payment_method_id"] = choice["payment_method"]
        if values:
            if not dry_run():
                odoo_call(LINE, "write", [line["id"]], values, context=context)
            given += 1
    return given


def tiers_key(name):
    """Return a tiers' name as looked for in a bank label: normalized, without the « - <IF> » Experio appends nor its
    legal form (« STE ATLAS SARL » is « atlas »); "" when under 4 letters are left, too short to tell."""
    words = [word for word in normalized(without_if(name)).split() if word not in LEGAL_FORMS]
    key = " ".join(words)
    return key if len(key.replace(" ", "")) >= 4 else ""


def named_tiers(statement, lines, context):
    """Give lines of an operation needing a tiers (purchase, sale, other third party: :4184-4196) the one tiers their
    label names: its ICE (15 digits, as Experio keys tiers, exp_account/models/exp_partner.py:530-560) or its name
    as whole words (tiers_key), among those the line's tiers picker offers (its stored partner_id_domain, :46-76).
    A label naming two tiers or none gets none.

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
    given = 0
    for line in needing:
        domain = line["partner_id_domain"]
        if domain not in offered:
            found = odoo_call(
                "exp.partner", "search_read", ast.literal_eval(domain), fields=["name", "ice"], context=context
            )
            offered[domain] = [
                (p["id"], tiers_key(p["name"]), (p["ice"] or "").replace(" ", "").zfill(15)) for p in found
            ]
        words = normalized(line["payment_ref"])
        ices = set(re.findall(r"(?<!\d)\d{15}(?!\d)", line["payment_ref"].replace(" ", "")))
        fits = {i for i, key, ice in offered[domain] if (ice.strip("0") and ice in ices) or (key and said(key, words))}
        if len(fits) == 1:
            if not dry_run():
                odoo_call(LINE, "write", [line["id"]], {"partner_id": fits.pop()}, context=context)
            given += 1
    return given


def owed_invoices(statement, lines, context):
    """Give purchase and sale lines with a tiers and no invoice the one open invoice owing exactly their amount among
    those their invoice picker offers (picked's domain), on no statement line yet: Experio's detection's own rule
    (bank/models/bank_statement_line.py:2440-2475), for the lines its detection did not fill. Two such invoices: none
    (the detection takes the first; Tarik leaves it to the cabinet). The line's form must accept it (typed_in).

    Returns how many lines got one (counted also in test mode, where nothing is written).
    """
    given, taken = 0, set()
    for line in lines:
        amount = amount_of(line)
        if not amount or not line["partner_id"]:
            continue
        domain = ast.literal_eval(line["invoice_ids_domain"]) + [("amount_residual", "=", amount)]
        found = set(odoo_call("exp.invoice", "search", domain, context=context)) - taken
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


def blocked_reasons(statement, context):
    """Return (reasons, ready, posted) for the statement's lines.

    reasons: {Experio's reason: how many lines} for the draft and pending lines (Experio's own error_msg, the red
    icon's text, else the state). ready and posted: how many lines are ready, and posted or exported.
    """
    lines = odoo_call(LINE, "read", statement["line_ids"], fields=["state", "error_msg"], context=context)
    reasons = {}
    for line in lines:
        if line["state"] in ("draft", "pending"):
            reason = plain(line["error_msg"] or "") or line["state"]
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
        f" Premier relevé du journal : son solde initial ({start:.2f}) diffère de l'à-nouveau du compte "
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
    return routine(f"Relevé n° {statement_id} : lu par Experio.", statement_id=statement_id, bank=True)


# A bank statement's pointing, one step each, as the cabinet works the statement's « Operations » list: Experio's own
# detection, then what it leaves, then where the statement stands (report_statement). Each step says what it did in
# ``said`` (a part of the report's sentence) and ``note`` (a sentence after it), routine results the report gathers.


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
    text = f"{TEST_MODE if dry_run() else ''}Relevé n° {statement_id} : {said or 'rien de plus'}."
    return routine(text, statement_id=statement_id, said=said, note=note)


def counted(count, words):
    """Return « <count> <words> », "" for none."""
    return f"{count} {words}" if count else ""


@step_tool
def detect_statement_lines(statement_id: int) -> dict:
    """Run Experio's « Auto Point » on bank statement ``statement_id`` and confirm what it finds (auto_point)."""
    return completing(
        statement_id,
        "detect_statement_lines",
        lambda statement, context: (
            counted(auto_point(statement, context), "pointée(s) par la détection d'Experio"),
            "",
        ),
    )


@step_tool
def choose_line_operations(statement_id: int) -> dict:
    """Give the statement's lines the operation and payment method the model reads in their label
    (chosen_operations)."""
    phrase = "reçoivent de Tarik leur type d'opération ou mode de paiement, lu dans le libellé"
    return completing(
        statement_id,
        "choose_line_operations",
        lambda statement, context: (
            counted(chosen_operations(statement, open_lines(statement, context), context), phrase),
            "",
        ),
    )


@step_tool
def name_line_tiers(statement_id: int) -> dict:
    """Give the statement's lines the one tiers their label names (named_tiers)."""
    return completing(
        statement_id,
        "name_line_tiers",
        lambda statement, context: (
            counted(
                named_tiers(statement, open_lines(statement, context), context), "leur tiers, nommé dans le libellé"
            ),
            "",
        ),
    )


@step_tool
def tie_cited_invoices(statement_id: int, client_message: str = "") -> dict:
    """Give the statement's lines the invoice the client names by its number in ``client_message`` (named_invoices)."""

    def work(statement, context):
        tied, unnamed = named_invoices(statement, client_message, context)
        return counted(tied, "rattachée(s) à la facture que vous avez citée"), unnamed.strip()

    return completing(statement_id, "tie_cited_invoices", work)


@step_tool
def tie_owed_invoices(statement_id: int) -> dict:
    """Give the statement's lines with a tiers the one open invoice owing their amount (owed_invoices)."""
    phrase = "la seule facture ouverte de leur tiers et de leur montant"
    return completing(
        statement_id,
        "tie_owed_invoices",
        lambda statement, context: (
            counted(owed_invoices(statement, open_lines(statement, context), context), phrase),
            "",
        ),
    )


@step_tool
def tie_tpe_remittances(statement_id: int) -> dict:
    """Tie the statement's TPE remittances to their TPE statement when Experio is sure (tie_tpe)."""

    def work(statement, context):
        tied, left = tie_tpe(statement, context) or (0, 0)
        note = f"{left} versement(s) TPE sans relevé TPE certain : à rapprocher par le cabinet." if left else ""
        return counted(tied, "versement(s) TPE rapproché(s) de leur relevé TPE"), note

    return completing(statement_id, "tie_tpe_remittances", work)


@step_tool
def report_statement(statement_id: int, said: list[str] | None = None, notes: list[str] | None = None) -> dict:
    """Say where bank statement ``statement_id`` stands after its pointing, as one line for the client.

    Args:
        statement_id: the statement.
        said: what the pointing steps did, each a part of the sentence (« 18 pointée(s) par la détection d'Experio »).
        notes: their sentences to add after it.

    The line has how many operations were read, ``said``, how many are ready and posted, the balance checks (the
    statement's own, the journal's previous statement, the à-nouveau for the journal's first), ``notes``, and what
    Experio says still blocks lines."""
    statement = read_statement(statement_id)
    context = in_dossier(statement["company_id"][0])
    with refused(f"report_statement {statement_id}", f"Relevé n° {statement_id} : Experio a refusé ({{reason}})."):
        reasons, ready, posted = blocked_reasons(statement, context)
        opening = opening_note(statement, context)
    parts = [f"Relevé n° {statement_id} : {len(statement['line_ids'])} opération(s) lue(s)", *(said or [])]
    parts.append(f"{ready} prête(s) à comptabiliser" + (f", {posted} déjà comptabilisée(s)" if posted else ""))
    text = ", ".join(parts) + "."
    no_balances = not statement["balance_start"] and not statement["balance_end_real"]
    if no_balances:  # Excel's template has none; a scan Experio read no balance on
        text += " Le relevé ne donne pas ses soldes : à saisir dans Experio pour contrôler qu'il est complet."
    elif not statement["is_complete"]:
        text += (
            f" Le solde final calculé ({statement['balance_end']:.2f}) ne correspond pas au solde du relevé "
            f"({statement['balance_end_real']:.2f}) : une opération manque ou est mal lue."
        )
    if not statement["is_valid"]:
        text += " Son solde initial ne suit pas le solde final du relevé précédent de ce journal."
    text += opening + "".join(f" {note}" for note in notes or [])
    if reasons:
        text += " À compléter dans Experio : " + " ; ".join(f"{why} ({n})" for why, n in reasons.items()) + "."
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
    complete, and the posted ones still unpaid or partly paid (suspens).
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
        action = odoo_json(STATEMENT, "action_post", [statement_id], context=context)
    except Exception as error:  # Experio's refusal (complete validation required, exact duplicate): its own words
        return stop(f"Relevé n° {statement_id} : pas comptabilisé, Experio refuse : {experio_refusal(error)}")
    if isinstance(action, dict) and action.get("res_model"):
        return stop(
            f"Relevé n° {statement_id} : pas comptabilisé, Experio demande une confirmation à l'écran "
            f"(« {action.get('name') or action['res_model']} ») : à valider par le cabinet dans Experio."
        )
    statement = read_statement(statement_id)
    reasons, ready, posted = blocked_reasons(statement, context)
    pending = suspense(statement, context)
    text = f"Relevé n° {statement_id} : {posted} opération(s) comptabilisée(s)"
    text += f", {sum(reasons.values())} restent à compléter" if reasons else ""
    text += "."
    if pending:
        text += f" En suspens (payées sans facture rapprochée) : {pending}."
    return ok(text, statement_id=statement_id)


def suspense(statement, context):
    """Return the statement's posted operations still unpaid or partly paid, as text for the reply ("" when none).

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
    return ", ".join(left[:10]) + (f" et {len(left) - 10} autre(s)" if len(left) > 10 else "")


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
    return routine(f"Dossier n° {dossier_id} : {len(found)} relevé(s) à pointer ({numbers}).", statement_ids=found)


def each_statement(dossier_id, step):
    """Run ``step`` (a statement read with FIELDS -> a step result) on each of the dossier's open statements and return
    one ok result joining their messages, with their ids (statement_ids); a routine result when there is none."""
    statements = open_statements(dossier_id)
    if not statements:
        return routine(NO_STATEMENT.format(dossier_id))
    results = []
    for statement in statements:
        try:
            results.append(step(statement))
        except ToolError as failure:  # one statement Experio refuses: said, the others still done
            results.append({"message": str(failure)})
    text = "\n- ".join(result["message"] for result in results)  # one bullet each in the summary's « - » list
    return ok(text, statement_ids=[statement["id"] for statement in statements])


@step_tool
def post_dossier_statements(dossier_id: int) -> dict:
    """Post the ready operations of every bank statement of dossier ``dossier_id`` not posted yet, with Experio's own
    validation, as post_statement posts one (posting); each statement's outcome said."""
    return each_statement(dossier_id, posting)
