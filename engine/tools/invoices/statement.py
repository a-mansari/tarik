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

    A line gets an invoice only when exactly one posted invoice of the dossier owes exactly the line's amount (not 0,
    as detection's amount_residual != 0, bank/models/bank_statement_line.py:2451), is the line's tiers' (when the line
    has one), has its number in ``words``, goes the line's way (credit: sale or purchase refund, debit: purchase or
    sale refund; an FNP invoice either way, verify_selected_invoices' type check, bank_statement_line.py:4566-4596)
    and is on no statement line yet (detection's already_in_use, bank_statement_line.py:2441-2453;
    reconcile_invoice_ids refuses an invoice paid already, :4372-4376). A line without tiers gets the invoice's too, as
    _onchange_invoice_ids would (bank_statement_line.py:2145-2146): an RPC write runs no onchange, and a purchase/sale
    line without tiers stays draft. Not checked here: verify_selected_invoices' account match (the invoice's tiers
    account against the line's operation account, :4485-4531), which action_post still refuses; and the amount is the
    line's in the statement's currency, where Experio compares the company's (:4548-4554), so a foreign-currency
    line's invoice is not found.

    Returns how many lines got one (counted also in test mode, where nothing is written).
    """
    words = normalized(words or "")
    if not words:
        return 0
    fields = ["state", "invoice_ids", "partner_id", "debit", "credit", "operation_type", "payment_ref"]
    lines = odoo_call(LINE, "read", statement["line_ids"], fields=fields, context=context)
    choices = []  # (line, invoices it may get)
    for line in lines:
        if line["state"] in POSTED or line["invoice_ids"] or line.get("operation_type") not in ("purchase", "sale"):
            continue
        amount = round(abs((line["credit"] or 0) - (line["debit"] or 0)), 2)
        if not amount:
            continue
        domain = [("company_id", "=", statement["company_id"][0]), ("state", "in", list(POSTED))]
        domain += [("amount_residual", "=", amount)]
        if line["partner_id"]:
            domain.append(("partner_id", "=", line["partner_id"][0]))
        candidates = odoo_call(
            "exp.invoice",
            "search_read",
            domain,
            fields=["invoice_number", "partner_id", "type", "refund", "is_fnp_invoice"],
            context=context,
        )
        way = "sale" if (line["credit"] or 0) > 0 else "purchase"
        named = [
            c
            for c in candidates
            if c["invoice_number"]
            and normalized(c["invoice_number"]) in words
            and (c["is_fnp_invoice"] or (c["type"] == way) != bool(c["refund"]))
        ]
        if named:
            choices.append((line, named))
    ids = list({c["id"] for _, named in choices for c in named})
    used = ids and odoo_call(LINE, "search_read", [("invoice_ids", "in", ids)], fields=["invoice_ids"], context=context)
    # shortcut: in test mode, invoices Auto Point would have applied are not on their lines, so not seen as taken
    taken = {i for row in used for i in row["invoice_ids"]}  # invoices on a line, this run's added below
    given = 0
    for line, named in choices:
        named = [c for c in named if c["id"] not in taken]
        if len(named) == 1:
            values = {"invoice_ids": [(4, named[0]["id"])]}
            if not line["partner_id"] and named[0]["partner_id"]:
                values["partner_id"] = named[0]["partner_id"][0]
            if not dry_run():
                odoo_call(LINE, "write", [line["id"]], values, context=context)
            taken.add(named[0]["id"])
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
def settle_statement(file_id: int, run_id: str = "", client_message: str = "") -> dict:
    """Settle a bank or TPE statement after its import, and report where it stands.

    Args:
        file_id: the imported file.
        run_id: the current run; its wait for Experio's reading is capped at 10 minutes across calls.
        client_message: the client's words, where they may tie lines to invoices by number.

    Waits for Experio's reading (a wait result past the deadline). A TPE statement goes to tpe.settle_tpe. A bank
    statement gets Experio's own « Auto Point » (confirmed), the invoices the client named, its TPE remittances tied to
    their TPE statements, then an ok result with the counts (pointed, ready, posted), the balance checks and what
    Experio says still blocks lines.
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
    context = in_dossier(statement["company_id"][0])
    with refused(f"settle_statement {statement_id}", f"Relevé n° {statement_id} : Experio n'a pas pu le pointer."):
        pointed = auto_point(statement, context) if statement["state"] not in POSTED else 0
        tied = named_invoices(statement, client_message, context)
        to_tpe = tie_tpe(statement, context) if statement["state"] not in POSTED else None
        statement = read_statement(statement_id)
        reasons, ready, posted = blocked_reasons(statement, context)
        opening = opening_note(statement, context)
    parts = [f"Relevé n° {statement_id} : {len(statement['line_ids'])} opération(s) lue(s)"]
    if pointed:
        parts.append(f"{pointed} pointée(s) par la détection d'Experio")
    if tied:
        parts.append(f"{tied} rattachée(s) à la facture que vous avez citée")
    if to_tpe and to_tpe[0]:
        parts.append(f"{to_tpe[0]} versement(s) TPE rapproché(s) de leur relevé TPE")
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
    if to_tpe and to_tpe[1]:
        text += f" {to_tpe[1]} versement(s) TPE sans relevé TPE certain : à rapprocher par le cabinet."
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
