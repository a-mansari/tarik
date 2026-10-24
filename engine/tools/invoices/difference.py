"""A payment difference (écart de paiement): what is left owing on a posted invoice, written off through Experio's own
wizard (exp.invoice.difference.change, Actions > « Générer écriture d'écart »).

Experio's wizard takes one posted invoice, proposes its remainder (amount_residual), the dossier's first 6585 account
and its latest general journal, then posts a two-line entry reconciled with the invoice, which leaves it paid. Tarik
uses it as is, after the client confirmed the invoice, the amount and the account it would use.
"""

from engine.tools.core.app import ToolError, ok, question, routine, step_tool, stop
from engine.tools.core.config import POSTED, TEST_MODE
from engine.tools.core.experio import dry_run, in_dossier, odoo_call, odoo_json, refused, require_dossier
from engine.tools.core.words import normalized

WIZARD = "exp.invoice.difference.change"
FIELDS = ["invoice_number", "partner_id", "state", "amount_residual", "payment_state", "company_id"]


def posted_invoice(dossier_id, facture):
    """The dossier's one posted invoice ``facture`` names (Tarik's number, or its own number, spaces aside); a
    ToolError naming the trouble otherwise."""
    words = normalized(facture or "").replace(" ", "")
    if not words:
        raise ToolError("Quelle facture solder ? Donnez son numéro.")
    domain = [("company_id", "=", dossier_id), ("state", "in", list(POSTED))]
    by_id = [("id", "=", int(words))] if words.isdigit() and len(words) < 10 else []
    found = odoo_call("exp.invoice", "search_read", [*domain, *by_id], fields=FIELDS) if by_id else []
    if not found:
        number = [("invoice_number", "ilike", facture.strip())]
        found = [
            invoice
            for invoice in odoo_call("exp.invoice", "search_read", [*domain, *number], fields=FIELDS)
            if normalized(invoice["invoice_number"] or "").replace(" ", "") == words
        ]
    if len(found) != 1:
        listed = ", ".join(f"n° {i['id']} ({i['invoice_number']})" for i in found[:5])
        why = (
            f"plusieurs factures comptabilisées portent ce numéro : {listed}"
            if found
            else "aucune facture comptabilisée"
        )
        raise ToolError(f"« {facture} » : {why} dans ce dossier. Précisez laquelle (son numéro chez Tarik).")
    return found[0]


@step_tool
def settle_difference(dossier_id: int, facture: str, client_confirmed: bool = False, session_id: str = "") -> dict:
    """Write off what is left owing on a posted invoice (écart de paiement), with Experio's own wizard: its amount
    (the invoice's remainder), the dossier's 6585 account and general journal, as Experio proposes them. Asks the
    client to confirm first (``client_confirmed``: they did, after reading the amount and the account)."""
    dossier = require_dossier(dossier_id)
    invoice = posted_invoice(dossier_id, facture)
    invoice_id, label = invoice["id"], f"Facture {invoice['id']} ({invoice['invoice_number']})"
    if not round(invoice["amount_residual"] or 0, 2):
        return routine(f"{label} : déjà soldée, rien à passer en écart.", invoice_id=invoice_id)
    context = {**in_dossier(dossier_id), "active_ids": [invoice_id]}
    with refused(f"settle_difference {invoice_id}", f"{label} : Experio n'a pas pu préparer l'écriture d'écart."):
        action = odoo_json(WIZARD, "action_open_difference_change_wizard", context=context)
        wizard_id = action["res_id"]
        fields = ["difference_amount", "difference_account_id", "journal_id"]
        wizard = odoo_call(WIZARD, "read", [wizard_id], fields=fields, context=context)[0]
    amount = round(wizard["difference_amount"] or 0, 2)
    if not wizard["difference_account_id"] or not wizard["journal_id"]:
        missing = (
            "de compte 6585 (écarts)" if not wizard["difference_account_id"] else "de journal des opérations diverses"
        )
        return stop(
            f"{label} : le dossier {dossier} n'a pas {missing} : à créer par le cabinet.", invoice_id=invoice_id
        )
    account, journal = wizard["difference_account_id"][1], wizard["journal_id"][1]
    tier = (invoice["partner_id"] or [0, "tiers inconnu"])[1].split(" - ")[0]
    if not client_confirmed:
        return question(
            f"{label}, {tier} : il reste {amount:.2f} DH dû. Je passe cet écart en compte {account} (journal "
            f"{journal}) et la facture sera soldée. Je le fais ? oui / non",
            invoice_id=invoice_id,
        )
    if dry_run():
        return ok(f"{TEST_MODE}{label} : l'écart de {amount:.2f} DH serait passé en compte {account}.")
    with refused(f"settle_difference {invoice_id}", f"{label} : Experio a refusé l'écriture d'écart."):
        odoo_json(WIZARD, "action_apply", [wizard_id], context=context)
        after = odoo_call("exp.invoice", "read", [invoice_id], fields=["payment_state"], context=context)[0]
    state = "soldée" if after["payment_state"] == "paid" else f"état de paiement : {after['payment_state']}"
    return ok(f"{label} : écart de {amount:.2f} DH passé en compte {account}, facture {state}.", invoice_id=invoice_id)
