"""Split a draft invoice's HT over several expense accounts, with Experio's own « Ajuster ligne TVA » wizard.

Experio keeps one account per TVA line (exp.invoice.line.account_id) and rebuilds those lines from the invoice's header
at each refresh (action_refresh), so a split written by hand is lost by the next refresh. Its wizard
(exp.invoice.line.split, opened from a line by action_open_split_wizard) replaces the line by the lines given and
rebuilds the accounting writing (validate_lines), the way its Lines screen does. The split is therefore the last
thing written on a draft: Tarik refreshes nothing after it, and posting refreshes only an invoice with no writing.
Experio refuses to post lines whose HT does not add up to the invoice's untaxed amount (validate_tva_lines): the
parts must add up to the line's HT, checked here first.
"""

from engine.tools.core.app import ToolError
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import dry_run, invoice_context, odoo_call, odoo_json, refused, require_draft_invoice

WIZARD = "exp.invoice.line.split"
LINE = "exp.invoice.line"
LINE_FIELDS = ["line_type", "amount_ht", "amount_tva", "tax_id", "tax_rate", "code_id", "account_id"]
EXPENSE_CLASSES = {"purchase": ("2", "6"), "sale": ("7",)}  # the classes Experio's own account warning allows


def untaxed_line(invoice_id, context):
    """Return the invoice's one untaxed TVA line (dict).

    Raises ToolError when it has none or several (several rates: the cabinet adjusts them in Experio)."""
    invoice = odoo_call("exp.invoice", "read", [invoice_id], fields=["tva_line_ids"], context=context)[0]
    lines = odoo_call(LINE, "read", invoice["tva_line_ids"], fields=LINE_FIELDS, context=context)
    untaxed = [line for line in lines if line["line_type"] == "untaxed"]
    if len(untaxed) != 1:
        raise ToolError(
            f"Facture {invoice_id} : {'plusieurs lignes' if untaxed else 'aucune ligne'} de charges à répartir ; "
            "le cabinet la répartit dans Experio (ligne TVA, « Ajuster »)."
        )
    return untaxed[0]


def checked_parts(invoice_id, kind, line, parts, context):
    """Return (the wizard's lines, the accounts' codes) for ``parts`` ([(account id, HT)]): [{"account_id", "amount_ht",
    "amount_tva"}], the TVA of each part at the line's rate and the first part taking the rounding, as the wizard does.

    Raises ToolError unless there are at least two parts, each with a positive HT on an account of its class whose
    HT adds up to the line's."""
    if len(parts) < 2 or any(amount <= 0 for _, amount in parts):
        raise ToolError(f"Facture {invoice_id} : une répartition compte au moins deux comptes, chacun avec un montant.")
    total = round(sum(amount for _, amount in parts), 2)
    if abs(total - line["amount_ht"]) > 0.005:
        raise ToolError(
            f"Facture {invoice_id} : la répartition donne {total:.2f} HT, la facture en compte "
            f"{line['amount_ht']:.2f} : les montants doivent faire le même total."
        )
    accounts = odoo_call("exp.account", "read", [account for account, _ in parts], fields=["code"], context=context)
    wrong = [a["code"] for a in accounts if not a["code"].startswith(EXPENSE_CLASSES[kind])]
    if wrong:
        raise ToolError(
            f"Facture {invoice_id} : compte {', '.join(wrong)} hors des classes {'/'.join(EXPENSE_CLASSES[kind])} "
            f"d'{'un achat' if kind == 'purchase' else 'une vente'}."
        )
    values = [
        {"account_id": account, "amount_ht": amount, "amount_tva": round(amount * line["tax_rate"] / 100, 2)}
        for account, amount in parts
    ]
    values[0]["amount_tva"] = round(line["amount_tva"] - sum(v["amount_tva"] for v in values[1:]), 2)
    return values, [a["code"] for a in accounts]


def split_over_accounts(invoice_id, parts):
    """Split draft invoice ``invoice_id``'s HT over ``parts`` ([(account id, HT)]), through Experio's wizard; return
    a French sentence for the client. Nothing is written in test mode.

    Raises ToolError: not a draft, no single line to split, parts not adding up or on a wrong class of account, or
    Experio refusing."""
    invoice = require_draft_invoice(invoice_id, ["state", "company_id", "exercice_id", "type"], "être répartie")
    context = invoice_context(invoice)
    with refused(f"split_over_accounts {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu lire ses lignes."):
        line = untaxed_line(invoice_id, context)
        values, codes = checked_parts(invoice_id, invoice["type"], line, parts, context)
    said = f"charges réparties sur {len(values)} comptes ({', '.join(codes)})"
    if dry_run():
        return f"{TEST_MODE}Facture {invoice_id} : {said}."
    with refused(f"split_over_accounts {invoice_id}", f"Facture {invoice_id} : Experio a refusé la répartition."):
        action = odoo_json(LINE, "action_open_split_wizard", [line["id"]], context=context)
        wizard_id = action["res_id"]
        # The wizard's first line is the original: replaced by the parts, each on the original's tax and code.
        tax, code = line["tax_id"] and line["tax_id"][0], line["code_id"] and line["code_id"][0]
        commands = [[5, 0, 0]] + [[0, 0, {**v, "tax_id": tax, "code_id": code}] for v in values]
        odoo_call(WIZARD, "write", [wizard_id], {"line_ids": commands}, context=context)
        odoo_json(WIZARD, "validate_lines", [wizard_id], context=context)
    return f"Facture {invoice_id} : {said}."
