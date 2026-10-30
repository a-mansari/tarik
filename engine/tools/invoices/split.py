"""Split a draft invoice's charges over several expense accounts: what the invoice bills, line by line, each on the
account that fits it.

Experio keeps one account per TVA line (exp.invoice.line.account_id) and rebuilds those lines from the invoice's header
at each refresh (action_refresh), so a split written by hand is lost by the next refresh. Its wizard
(exp.invoice.line.split, opened from a line by action_open_split_wizard) replaces the line by the lines given and
rebuilds the accounting writing (validate_lines), the way its Lines screen does. The split is therefore the last
thing written on a draft: Tarik refreshes nothing after it, and posting refreshes only an invoice with no writing.
Experio refuses to post lines whose HT does not add up to the invoice's untaxed amount (validate_tva_lines): the
parts must add up to the line's HT, checked here first.

Tarik decides the split itself, from the lines the scan check read (charge_lines): each line's account is chosen by
the model as for an invoice's one account (model_choice), lines on one account are merged, and an invoice whose lines
all fit one account is left alone. Experio has one charge line per TVA rate: each rate is split on its own.
"""

from engine.tools.core.app import ToolError, ok, routine, step_tool
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import (
    dry_run,
    invoice_context,
    odoo_call,
    odoo_json,
    refused,
    require_draft_invoice,
    require_invoice,
    test_mode_note,
)
from engine.tools.invoices.accounts import model_choice
from engine.tools.invoices.check import stored_check
from engine.tools.invoices.classify import side_of

WIZARD = "exp.invoice.line.split"
LINE = "exp.invoice.line"
LINE_FIELDS = ["line_type", "amount_ht", "amount_tva", "tax_id", "tax_rate", "code_id", "account_id"]
CHARGE_CLASSES = {"purchase": ("2", "6"), "sale": ("7",)}  # the classes Experio's own account warning allows
TOLERANCE = 0.05  # the scan's lines may miss the invoice's HT by a rounding: the largest part takes it


def untaxed_lines(invoice_id, context):
    """Return the invoice's untaxed (charge) TVA lines (dicts), one per rate as Experio builds them."""
    invoice = odoo_call("exp.invoice", "read", [invoice_id], fields=["tva_line_ids"], context=context)[0]
    lines = odoo_call(LINE, "read", invoice["tva_line_ids"], fields=LINE_FIELDS, context=context)
    return [line for line in lines if line["line_type"] == "untaxed"]


def checked_parts(invoice_id, kind, line, parts, context):
    """Return (the wizard's lines, the accounts' codes) for ``parts`` ([(account id, HT)]) of ``line``: [{"account_id",
    "amount_ht", "amount_tva"}], the TVA of each part at the line's rate and the first part taking the rounding, as
    the wizard does.

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
    wrong = [a["code"] for a in accounts if not a["code"].startswith(CHARGE_CLASSES[kind])]
    if wrong:
        raise ToolError(
            f"Facture {invoice_id} : compte {', '.join(wrong)} hors des classes {'/'.join(CHARGE_CLASSES[kind])} "
            f"d'{'un achat' if kind == 'purchase' else 'une vente'}."
        )
    values = [
        {"account_id": account, "amount_ht": amount, "amount_tva": round(amount * line["tax_rate"] / 100, 2)}
        for account, amount in parts
    ]
    values[0]["amount_tva"] = round(line["amount_tva"] - sum(v["amount_tva"] for v in values[1:]), 2)
    return values, [a["code"] for a in accounts]


def split_over_accounts(invoice_id, by_rate):
    """Split draft invoice ``invoice_id``'s charges over ``by_rate`` ({TVA rate: [(account id, HT)]}), through Experio's
    wizard, one charge line per rate; return a French sentence for the client. Nothing is written in test mode, nor
    before every rate is checked.

    Raises ToolError: not a draft, a rate without exactly one charge line, parts not adding up or on a wrong class of
    account, or Experio refusing."""
    invoice = require_draft_invoice(invoice_id, ["state", "company_id", "exercice_id", "type"], "être répartie")
    context = invoice_context(invoice)
    planned, codes = [], []
    with refused(f"split_over_accounts {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu lire ses lignes."):
        lines = untaxed_lines(invoice_id, context)
        for rate, parts in by_rate.items():
            same = [line for line in lines if line["tax_rate"] == rate]
            if len(same) != 1:
                raise ToolError(
                    f"Facture {invoice_id} : {'plusieurs lignes' if same else 'aucune ligne'} de charges à {rate} % ; "
                    "le cabinet la répartit dans Experio (ligne TVA, « Ajuster »)."
                )
            values, found = checked_parts(invoice_id, invoice["type"], same[0], parts, context)
            planned.append((same[0], values))
            codes += [code for code in found if code not in codes]
    said = f"charges réparties sur {len(codes)} comptes ({', '.join(codes)})"
    if dry_run():
        return f"{TEST_MODE}Facture {invoice_id} : {said}."
    with refused(f"split_over_accounts {invoice_id}", f"Facture {invoice_id} : Experio a refusé la répartition."):
        for line, values in planned:
            action = odoo_json(LINE, "action_open_split_wizard", [line["id"]], context=context)
            wizard_id = action["res_id"]
            # The wizard's first line is the original: replaced by the parts, each on the original's tax and code.
            tax, code = line["tax_id"] and line["tax_id"][0], line["code_id"] and line["code_id"][0]
            commands = [[5, 0, 0]] + [[0, 0, {**v, "tax_id": tax, "code_id": code}] for v in values]
            odoo_call(WIZARD, "write", [wizard_id], {"line_ids": commands}, context=context)
            odoo_json(WIZARD, "validate_lines", [wizard_id], context=context)
    return f"Facture {invoice_id} : {said}."


def charge_groups(charge_lines, untaxed):
    """Return (the charge lines to split per rate, why a rate is left whole): {rate: [(designation, HT)]} and [French
    reasons].

    A rate is split only when the scan's lines were read in full (a designation, an HT and a rate each), there are at
    least two of them, Experio has one charge line at that rate (two means it is split already) and their HT adds up
    to its (TOLERANCE)."""
    usable = [
        line
        for line in charge_lines
        if line.get("designation") and (line.get("amount_ht") or 0) > 0 and line.get("rate") is not None
    ]
    if len(usable) != len(charge_lines):
        return {}, ["les lignes du scan ne sont pas toutes lisibles"]
    groups = {}
    for line in usable:
        groups.setdefault(int(line["rate"]), []).append((str(line["designation"]).strip(), line["amount_ht"]))
    chosen, left = {}, []
    for rate, lines in groups.items():
        same = [line for line in untaxed if line["tax_rate"] == rate]
        if len(lines) < 2:
            continue
        if len(same) != 1:
            left.append(f"à {rate} % Experio a {len(same)} lignes de charges" if same else f"pas de ligne à {rate} %")
        elif abs(round(sum(ht for _, ht in lines), 2) - same[0]["amount_ht"]) > TOLERANCE:
            left.append(f"les lignes à {rate} % ne font pas le HT de la facture")
        else:
            chosen[rate] = lines
    return chosen, left


def parts_of(invoice, kind, lines, untaxed_ht):
    """Return ([(account id, HT)], [labels]) for ``lines`` ([(designation, HT)]) of one rate: the model's account for
    each designation, lines on the same account merged, the largest part taking the rounding to ``untaxed_ht``.

    Returns (None, reason) when a line has no account the model is sure of, or one on class 2 (an immobilisation, which
    only the client confirms); (None, "") when every line fits one account."""
    accounts = {}  # designation -> account {id, code, name}
    for designation, _ in lines:
        if designation not in accounts:
            account, _reason = model_choice(invoice, kind, designation)
            if not account:
                return None, f"aucun compte sûr pour « {designation} »"
            if account["code"].startswith("2"):
                return None, f"« {designation} » ressemble à une immobilisation"
            accounts[designation] = account
    merged = {}
    for designation, ht in lines:
        account = accounts[designation]
        merged[account["id"]] = round(merged.get(account["id"], 0) + ht, 2)
    if len(merged) < 2:
        return None, ""
    parts = list(merged.items())
    largest = max(range(len(parts)), key=lambda i: parts[i][1])
    parts[largest] = (parts[largest][0], round(parts[largest][1] + untaxed_ht - sum(ht for _, ht in parts), 2))
    labels = {a["id"]: f"{a['code']} {a['name']}" for a in accounts.values()}
    return parts, [f"{ht:.2f} en `{labels[account]}`" for account, ht in parts]


@step_tool
def split_charges(invoice_id: int) -> dict:
    """Split a draft invoice's charges over the accounts that fit each of its lines, when it bills things of different
    kinds (an invoice for goods and for transport, for rent and for charges). Call it last on the draft: Experio's
    refresh rebuilds the lines from the header and would undo it.

    Tarik decides: the lines the scan check read, each on the model's account for it, the lines at one account
    merged. An ok result says the split made; a routine one that the invoice stays on its one account (one line, all
    lines on one account, lines not read in full, an immobilisation among them, a draft already split, a posted
    invoice). A refusal by Experio leaves the invoice on its one account, and says so."""
    invoice = require_invoice(invoice_id, ["state", "type", "company_id", "exercice_id"])
    if invoice["state"] != "draft":
        return routine(f"Facture {invoice_id} : pas un brouillon, rien à répartir.")
    check = stored_check(invoice_id)
    charge_lines = (check[1].get("charge_lines") if check else None) or []
    if len(charge_lines) < 2:
        return routine(f"Facture {invoice_id} : une seule ligne de charges, un seul compte.")
    kind = side_of(invoice)
    context = invoice_context(invoice)
    with refused(f"split_charges {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu lire ses lignes."):
        untaxed = untaxed_lines(invoice_id, context)
    groups, left = charge_groups(charge_lines, untaxed)
    by_rate, said = {}, []
    for rate, lines in groups.items():
        height = next(line["amount_ht"] for line in untaxed if line["tax_rate"] == rate)
        parts, labels = parts_of(invoice, kind, lines, height)
        if parts:
            by_rate[rate] = parts
            said.append(f"à {rate} % : {', '.join(labels)}")
        elif labels:
            left.append(labels)
    note = f" Laissé en un compte : {' ; '.join(left)}." if left else ""
    if not by_rate:
        return routine(f"Facture {invoice_id} : un seul compte suffit.{note}")
    try:
        split_over_accounts(invoice_id, by_rate)
    except ToolError as error:  # nothing is written when it fails: the invoice keeps its one account, told
        return ok(f"{error} Charges laissées sur un seul compte.{note}")
    return ok(f"{test_mode_note()}Facture {invoice_id} : charges réparties {' ; '.join(said)}.{note}")
