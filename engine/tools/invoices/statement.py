"""A bank statement after its import: Experio's reading waited for and checked, its operations pointed by Experio's own
« Auto Point », posted when the client asks -- as the cabinet does on Experio's statement (module bank).

- Reading: until Experio has read it (is_detected; its bluejay_state stays « pending » for good); then Experio's
  own checks: its lines add up to the ending
  balance read (is_complete), its starting balance follows the journal's previous statement (is_valid).
- Pointing: « Auto Point » (action_refresh, detect_bank_statement_line_info) finds each line's operation, payment
  method and tiers from past posted lines and an invoice of exactly that tiers and amount, and shows them to confirm;
  Tarik confirms them (the window's action_apply). A line the client ties to an invoice by its number gets it when
  the amount owed is exactly the line's.
- Posting: Experio's own validation of the statement (action_post): its ready lines only; a window it opens instead
  (duplicate, out of the period, exchange rate) stops for the cabinet.
"""

import io
import time

import openpyxl

from engine.tools.core.app import ok, routine, step_tool, stop, wait
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import (
    dry_run,
    experio_refusal,
    in_dossier,
    odoo_call,
    odoo_json,
    plain,
    refused,
)
from engine.tools.core.queries import Q
from engine.tools.core.words import as_number, normalized
from engine.tools.invoices.check import resolve_date

STATEMENT = "exp.bank.statement"
LINE = "exp.bank.statement.line"
# Experio's Excel statement template (bank/static/xls/canvas_releves_bancaire.xlsx), its header row normalized.
CANVAS = ["date", "libelle", "type d operation", "tiers", "methode de paiement", "debit", "credit"]
FIELDS = ["state", "bluejay_state", "company_id", "journal_id", "currency_id", "balance_start", "balance_end"]
FIELDS += ["balance_end_real", "is_complete", "is_valid", "line_ids", "date", "is_detected"]
POSTED = ("post", "export")


def is_canvas(name, data):
    """True when spreadsheet ``data`` is Experio's Excel statement template: its first row starts with the template's
    columns (Date, Libellé, Type d'opération, Tiers, Méthode de paiement, Débit, Crédit)."""
    if not name.lower().endswith((".xlsx", ".xlsm")):
        return False
    try:
        sheet = openpyxl.load_workbook(io.BytesIO(bytes(data)), read_only=True, data_only=True).worksheets[0]
        first = next(sheet.iter_rows(max_row=1, values_only=True), ())
    except Exception:
        return False
    return [normalized(str(cell or "")) for cell in first[: len(CANVAS)]] == CANVAS


def import_canvas(name, data, dossier_id, journal_id):
    """Experio's Excel statement template imported as its « Load EXCEL » does, into a new statement of the chosen
    journal: each row a line created with Experio's import context, which appends it to the statement given
    (from_xlsx_import, default_statement_id: handle_imported_records) and finds its tiers and operation by their names
    itself (partner_value, imp_operation_id); payment method and currency by their exact name, as Odoo's importer does.
    The statement's id; RuntimeError naming what Experio refused. (Odoo's importer itself takes the file as raw bytes,
    from the browser's upload only.)"""
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
    """The template's filled rows, as {column (normalized header): value}."""
    sheet = openpyxl.load_workbook(io.BytesIO(bytes(data)), read_only=True, data_only=True).worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    headers = [normalized(str(cell or "")) for cell in rows[0]]
    return [dict(zip(headers, row, strict=False)) for row in rows[1:] if any(cell not in (None, "") for cell in row)]


def line_values(row, context):
    """One statement line's values from a template row."""
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
    with cabinet_db() as db:
        row = db.execute(Q.statement_of_file, (file_id,)).fetchone()
    return row[0] if row else None


def read_statement(statement_id):
    return odoo_call(STATEMENT, "read", [statement_id], fields=FIELDS)[0]


def auto_point(statement, context):
    """Experio's « Auto Point » on the statement, its detections confirmed as the window's « Apply » does: how many
    lines it filled."""
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
    """The lines still without invoice that the client ties to one, by its number in their own ``words``: given it
    when that posted invoice of the dossier owes exactly the line's amount (and is the line's tiers', when it has one).
    How many lines got theirs."""
    words = normalized(words or "")
    if not words:
        return 0
    fields = ["state", "invoice_ids", "partner_id", "debit", "credit", "operation_type", "payment_ref"]
    lines = odoo_call(LINE, "read", statement["line_ids"], fields=fields, context=context)
    given = 0
    for line in lines:
        if line["state"] in POSTED or line["invoice_ids"] or line.get("operation_type") not in ("purchase", "sale"):
            continue
        amount = round(abs((line["credit"] or 0) - (line["debit"] or 0)), 2)
        domain = [("company_id", "=", statement["company_id"][0]), ("state", "in", list(POSTED))]
        domain += [("amount_residual", "=", amount)]
        if line["partner_id"]:
            domain.append(("partner_id", "=", line["partner_id"][0]))
        candidates = odoo_call("exp.invoice", "search_read", domain, fields=["invoice_number"], context=context)
        named = [c for c in candidates if c["invoice_number"] and normalized(c["invoice_number"]) in words]
        if len(named) == 1:
            if not dry_run():
                odoo_call(LINE, "write", [line["id"]], {"invoice_ids": [(4, named[0]["id"])]}, context=context)
            given += 1
    return given


def blocked_reasons(statement, context):
    """{Experio's reason: how many lines} for the lines not ready (its own error_msg, the red icon's text)."""
    lines = odoo_call(LINE, "read", statement["line_ids"], fields=["state", "error_msg"], context=context)
    reasons = {}
    for line in lines:
        if line["state"] in ("draft", "pending"):
            reason = plain(line["error_msg"] or "") or line["state"]
            reasons[reason] = reasons.get(reason, 0) + 1
    return reasons, sum(line["state"] == "ready" for line in lines), sum(line["state"] in POSTED for line in lines)


def opening_note(statement, context):
    """For the journal's first statement, its starting balance against the bank account's à-nouveau of the exercice
    (Experio checks nothing there: its first statement is always « valid »): a note when they differ, else ""."""
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
    carried = round(sum(line["balance"] for line in lines if (line["date"] or "")[:4] == year), 2)
    start = round(statement["balance_start"] or 0, 2)
    if not lines or abs(abs(carried) - abs(start)) < 0.01:
        return ""
    return (
        f" Premier relevé du journal : son solde initial ({start:.2f}) diffère de l'à-nouveau du compte "
        f"({abs(carried):.2f})."
    )


READ_DEADLINES = {}


def being_read(statement):
    """True while Experio has not read the statement: its reading sets state draft and is_detected (bluejay
    set_bank_statement); its bluejay_state stays « pending », posted statements included."""
    return statement["state"] == "pending" and not statement["is_detected"]


@step_tool
def settle_statement(file_id: int, run_id: str = "", client_message: str = "") -> dict:
    """After a bank statement's import: wait for Experio's reading, check its balances, point its operations with
    Experio's own « Auto Point » (confirmed), give the lines the client ties to an invoice by its number that invoice,
    and say where it stands (ready, blocked and why)."""
    statement_id = statement_of(file_id)
    if not statement_id:
        return routine(f"Fichier n° {file_id} : pas de relevé importé.")
    deadline = READ_DEADLINES.setdefault(run_id, time.monotonic() + 600) if run_id else time.monotonic() + 600
    statement = read_statement(statement_id)
    while being_read(statement):
        if time.monotonic() >= deadline:
            return wait(f"Relevé n° {statement_id} : Experio le lit encore, il sera repris à votre prochain message.")
        time.sleep(15)
        statement = read_statement(statement_id)
    context = in_dossier(statement["company_id"][0])
    with refused(f"settle_statement {statement_id}", f"Relevé n° {statement_id} : Experio n'a pas pu le pointer."):
        pointed = auto_point(statement, context) if statement["state"] not in POSTED else 0
        tied = named_invoices(statement, client_message, context)
        statement = read_statement(statement_id)
        reasons, ready, posted = blocked_reasons(statement, context)
        opening = opening_note(statement, context)
    parts = [f"Relevé n° {statement_id} : {len(statement['line_ids'])} opération(s) lue(s)"]
    if pointed:
        parts.append(f"{pointed} pointée(s) par la détection d'Experio")
    if tied:
        parts.append(f"{tied} rattachée(s) à la facture que vous avez citée")
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
    text += opening
    if reasons:
        text += " À compléter dans Experio : " + " ; ".join(f"{why} ({n})" for why, n in reasons.items()) + "."
    return ok(f"{TEST_MODE if dry_run() else ''}{text}", statement_id=statement_id)


@step_tool
def post_statement(file_id: int) -> dict:
    """Post a bank statement's ready operations with Experio's own validation (action_post): a window Experio opens
    instead (a duplicate statement, operations out of its period, an exchange rate still to fetch) or its refusal
    stops it for the cabinet; the operations posted but still unpaid or partly paid (suspens) are listed."""
    statement_id = statement_of(file_id)
    if not statement_id:
        return routine(f"Fichier n° {file_id} : pas de relevé à comptabiliser.")
    statement = read_statement(statement_id)
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
    """The statement's posted purchase and sale operations still unpaid or partly paid (Experio's « Suspense »
    screen: payment_state unpaid / partial, not reconciled), as « libellé montant » listed."""
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
