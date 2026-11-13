"""A bank's notice of operation (« avis d'opération »), used to adjust the statement line it details.

A statement prints one line for an operation that is several on the bank's notice: a transfer of 10 000 with a
commission of 20 and its TVA of 2 is one line of 10 022. Experio does not read notices; Tarik reads them with the model,
finds the one open line whose amount is the notice's net amount, and splits it as the cabinet does with the line's
« Ajuster » button: Experio's wizard bank.statement.split.wizard (bank/wizard/bank_statement_split_wizard.py), whose
first line is the original, left with what the other lines do not take (its onchange, _onchange_line_ids), and whose
validate_lines checks the balance and links the lines as one adjusted group (original_line_id).

The principal part stays on the original line, with the operation and tiers it has or will get from the pointing. The
other parts take the dossier's operations as a bank's fee lines do on a statement (fee_pairs): a commission the HT bank
fee, its TVA written as printed on the commission and as a « Taxe Bancaire » line, a stamp duty its own operation. A
notice with a part of another kind, two principal parts or a total it does not add up to is left to the cabinet.
Nothing is posted.
"""

from datetime import date, timedelta

from engine.tools.core.app import ToolError, log, ok, question, routine, step_tool, stop
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import dry_run, in_dossier, odoo_call, odoo_json, refused
from engine.tools.core.model import ask_model, pdf_pages, tool_prompt
from engine.tools.core.words import normalized
from engine.tools.invoices.intake import as_pdf, file_row, require_kind
from engine.tools.invoices.statement import LINE, fee_operations, open_statements, operation_on, operations_of

WIZARD = "bank.statement.split.wizard"
NOTICE_FIELDS = ["state", "date", "payment_ref", "debit", "credit", "statement_id", "original_line_id"]
NOTICE_FIELDS += ["currency_id", "company_currency_id"]
ADJUSTABLE = ("draft", "suspense", "ready")  # the lines Experio's action_adjust accepts (bank_statement_line.py:921)
KINDS = ("principal", "commission", "tva", "timbre")  # the parts Tarik places; any other kind is the cabinet's
NO_DETECTION = {"ignore_bank_line_info_detection": True}  # bank_statement_line.py create (:2327)
NEAR_DAYS = 5  # the notice's date or value date and the line's, when several lines have the notice's amount


def checked_parts(read):
    """Return (the parts [{"kind", "amount"}] with amounts signed as the statement's (credit positive), "") when the
    notice read can be applied, (None, why in French) when not, (None, "") when it has one part (nothing to adjust).

    Applied means: readable amounts, parts adding up to the net total (to the cent) and all of its sign, one principal
    part, bigger than the others together, a TVA only with a commission and at 10 % of it (as fee_pairs takes it), no
    other kind. The model reads the notice: nothing it says is written unless it is that coherent."""
    try:
        parts = [{"kind": p.get("kind"), "amount": round(float(p.get("amount") or 0), 2)} for p in read["parts"]]
        total = round(float(read["total"]), 2)
    except AttributeError, KeyError, TypeError, ValueError:
        return None, "je n'ai pas pu lire ses montants"
    if len(parts) < 2:
        return None, ""
    if abs(round(sum(p["amount"] for p in parts), 2) - total) > 0.005:
        return None, "ses lignes ne font pas le montant total que j'ai lu"
    other = sorted({str(p["kind"]) for p in parts if p["kind"] not in KINDS})
    if other or any(p["amount"] == 0 for p in parts):
        return None, f"il a une ligne que je ne sais pas placer ({', '.join(other) or 'montant nul'})"
    if any(p["amount"] * total < 0 for p in parts):
        return None, "ses lignes ne sont pas toutes du même côté que son total"
    kinds = [p["kind"] for p in parts]
    if kinds.count("principal") != 1:
        return None, "il n'a pas exactement une ligne principale"
    amounts = {p["kind"]: abs(p["amount"]) for p in parts}
    if kinds.count("tva") > 1 or kinds.count("commission") > 1 or kinds.count("tva") > kinds.count("commission"):
        return None, "ses commissions et sa TVA ne vont pas par paires"
    if sum(a for kind, a in amounts.items() if kind != "principal") >= amounts["principal"]:
        return None, "ses frais dépassent l'opération elle-même"
    if "tva" in amounts and abs(amounts["tva"] - amounts["commission"] * 0.1) > 0.02:
        return None, "sa TVA n'est pas 10 % de la commission"
    return parts, ""


def line_net(line):
    """Return the net amount of statement ``line`` (credit positive), to the cent."""
    return round((line["credit"] or 0) - (line["debit"] or 0), 2)


def found_line(lines, read):
    """Return (the one line among ``lines`` the notice details, why not): the lines whose net amount is the notice's, or
    their group already adjusted; among them, those near the notice's dates (when it gives one), then when several the
    one whose label cites its reference. (None, "adjusted") for an adjusted group that already adds up, (None,
    "none") for no line, (None, "several") for no single one."""
    total = round(float(read["total"]), 2)
    singles = [line for line in lines if not line["original_line_id"] and line["state"] in ADJUSTABLE]
    groups = {}
    for line in lines:
        if line["original_line_id"]:
            groups.setdefault(line["original_line_id"], []).append(line)
    same = [line for line in singles if line_net(line) == total]
    if not same and any(round(sum(map(line_net, group)), 2) == total for group in groups.values()):
        return None, "adjusted"
    days = [d for d in (read.get("date"), read.get("value_date")) if d]
    if days:  # the amount alone never decides: an old line of the same amount, or another dossier's notice
        same = [line for line in same if any(close(line["date"], d) for d in days)]
    if len(same) > 1 and read.get("reference"):
        cited = [line for line in same if normalized(str(read["reference"])) in normalized(line["payment_ref"] or "")]
        same = cited or same
    if len(same) == 1:
        return same[0], ""
    return None, "several" if same else "none"


def close(first, second):
    """True when ISO dates ``first`` and ``second`` are at most NEAR_DAYS apart (False for a date that is not one)."""
    try:
        return abs(date.fromisoformat(first) - date.fromisoformat(second)) <= timedelta(days=NEAR_DAYS)
    except TypeError, ValueError:
        return False


def wizard_lines(line, parts, operations):
    """Return ([the original's new amounts {"debit", "credit"}, then each other part's wizard line values], what is
    missing in French or ""): the principal stays on ``line``, the other parts on the dossier's operations."""
    fee, tax = fee_operations(operations)
    operation = {
        "commission": fee,
        "tva": tax,
        "timbre": operation_on(operations, "6167", type=False),
    }
    others = [p for p in parts if p["kind"] != "principal"]
    names = {"commission": "frais bancaires HT (6147)", "tva": "Taxe Bancaire", "timbre": "droit de timbre (6167)"}
    lacking = sorted({names[p["kind"]] for p in others if not operation[p["kind"]]})
    if lacking:
        return None, f"le dossier n'a pas l'opération {', '.join(lacking)}"
    tva = next((p["amount"] for p in others if p["kind"] == "tva"), 0)
    rest = round(line_net(line) - sum(p["amount"] for p in others), 2)
    made = [{"debit": -rest if rest < 0 else 0, "credit": rest if rest > 0 else 0}]
    for part in others:
        values = {
            "statement_id": line["statement_id"][0],
            "date": line["date"],
            "payment_ref": line["payment_ref"],
            "operation_id": operation[part["kind"]]["id"],
            "debit": -part["amount"] if part["amount"] < 0 else 0,
            "credit": part["amount"] if part["amount"] > 0 else 0,
        }
        if part["kind"] == "commission" and tva:
            values["tax_amount"] = abs(tva)  # the TVA as the bank printed it, as fee_pairs writes it
        made.append(values)
    return made, ""


def read_notice(file_id):
    """Return (the file's name, what the model reads of notice ``file_id``): {"date", "value_date", "reference",
    "total", "parts": [{"kind", "label", "amount"}]}, amounts signed (credit positive)."""
    with cabinet_db() as db:
        name, mime, data, kind = file_row(db, file_id, "file_name", "mime_type", "data", "kind")
    require_kind(name, kind, "notice")
    pages = pdf_pages(as_pdf(mime, data))
    instruction = (
        'Answer with only a JSON object: {"date": "YYYY-MM-DD" or null, "value_date": "YYYY-MM-DD" or null, '
        '"reference": string or null, "total": number, "parts": [{"label": string, "kind": '
        '"principal"|"commission"|"tva"|"timbre"|"autre", "amount": number}]}'
    )
    return name, ask_model(
        tool_prompt("read-bank-notice"), pages, instruction, "Lecture de l'avis", keys=("parts", "total")
    )


def lines_of(dossier_id, total, context):
    """Return the bank statement lines of dossier ``dossier_id``'s open statements a notice of net ``total`` may detail,
    in one read: those of that amount, and the lines of adjusted groups (original_line_id), to know a group that
    already adds up to it."""
    statements = [statement["id"] for statement in open_statements(dossier_id)]
    amount = abs(total)
    within = [
        term
        for side in ("debit", "credit")
        for term in ("&", (side, ">=", amount - 0.005), (side, "<=", amount + 0.005))
    ]
    domain = [("statement_id", "in", statements), "|", "|", ("original_line_id", "!=", 0), *within]
    return (
        odoo_call(LINE, "search_read", domain, fields=NOTICE_FIELDS, order="id", context=context) if statements else []
    )


def restored(line, wizard, context):
    """Put ``line`` back as it was after a failed adjustment (its amounts, the lines made for the wizard removed); True
    when it is done, False (logged) when Experio refuses that too."""
    try:
        made = odoo_call(WIZARD, "read", [wizard], fields=["line_ids"], context=context)[0]["line_ids"]
        extra = [i for i in made if i != line["id"]]
        if extra:
            odoo_call(LINE, "unlink", extra, context=context)
        odoo_call(LINE, "write", [line["id"]], {"debit": line["debit"], "credit": line["credit"]}, context=context)
        return True
    except Exception as failure:
        log(f"restored {line['id']}: {failure}")
        return False


def adjusted(line, made, context):
    """Adjust ``line`` as ``made`` says (wizard_lines), through Experio's wizard; the first item holds the original's
    new debit and credit.

    The wizard's lines are the statement's real lines (action_adjust), so the write changes them at once: when the
    validation then fails, the line is put back (restored). The new lines are created without Experio's detection of
    a tiers, method or invoice from the label (ignore_bank_line_info_detection): their label is the transfer's, which
    would give a commission the transfer's supplier."""
    action = odoo_json(LINE, "action_adjust", [line["id"]], context=context)
    wizard = action["res_id"]
    commands = [[1, line["id"], made[0]]] + [[0, 0, values] for values in made[1:]]
    try:
        odoo_call(WIZARD, "write", [wizard], {"line_ids": commands}, context={**context, **NO_DETECTION})
        odoo_json(WIZARD, "validate_lines", [wizard], context=context)
    except Exception:
        if not restored(line, wizard, context):
            raise ToolError(
                f"L'opération « {line['payment_ref']} » est peut-être à moitié ajustée : le cabinet doit la vérifier."
            ) from None
        raise


@step_tool
def adjust_from_notice(file_id: int, dossier_id: int) -> dict:
    """Adjust (« Ajuster ») the bank statement line a notice of operation (avis d'opération) details, from what the
    notice prints: the model reads its parts, Tarik finds the one open line of the dossier with the notice's net
    amount and splits it into its principal, its commission and the TVA of it, as the cabinet does in Experio. A
    routine result when the notice has one part (nothing to adjust) or its line is adjusted already; a question when
    several lines fit; a stop result, nothing written, when no open line fits yet (the statement is not imported) or
    the notice has a part Tarik cannot place. Posts nothing."""
    name, read = read_notice(file_id)
    parts, why = checked_parts(read)
    if not parts:
        if why:
            return stop(f"`{name}` : avis non pris en charge, {why} : le cabinet l'ajuste dans Experio.")
        return routine(f"`{name}` : avis d'une seule ligne, rien à ajuster.")
    context = in_dossier(dossier_id)
    with refused(f"adjust_from_notice {file_id}", f"`{name}` : Experio n'a pas pu lire les opérations du dossier."):
        line, missing = found_line(lines_of(dossier_id, float(read["total"]), context), read)
    total = f"{abs(float(read['total'])):.2f}"
    if missing == "adjusted":
        return routine(f"`{name}` : l'opération de {total} est déjà ajustée.")
    if missing == "none":
        return stop(
            f"`{name}` : aucune opération non comptabilisée de {total}, à la date de l'avis, dans les relevés du "
            "dossier : importez d'abord le relevé qui la contient, puis renvoyez l'avis."
        )
    if missing == "several":
        return question(
            f"`{name}` : plusieurs opérations de {total} pourraient être celle de cet avis : à quelle date est-elle "
            "sur le relevé ? Rien n'a été ajusté."
        )
    if line["currency_id"][0] != line["company_currency_id"][0]:
        return stop(f"`{name}` : l'opération est en devise, le cabinet l'ajuste dans Experio.")
    made, lacking = wizard_lines(line, parts, operations_of(dossier_id, context))
    if not made:
        return stop(f"`{name}` : {lacking} : le cabinet l'ajuste dans Experio.")
    named = {"principal": "opération", "commission": "commission", "tva": "TVA", "timbre": "droit de timbre"}
    said = ", ".join(f"{named[p['kind']]} {abs(p['amount']):.2f}" for p in parts)
    text = f"`{name}` : l'opération « {line['payment_ref']} » de {total} est ajustée en {said}."
    if dry_run():
        return ok(TEST_MODE + text)
    with refused(
        f"adjust_from_notice {file_id}",
        f"`{name}` : Experio a refusé l'ajustement ({{reason}}), l'opération est remise comme avant.",
    ):
        adjusted(line, made, context)
    return ok(text)
