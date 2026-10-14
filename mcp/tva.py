"""Phase 9: TVA -- recoverable or not, the cabinet's prorata, and the tax on each line.

tarik-master asks about non-recoverable TVA (vat_gate) and runs a 7-tool agent that creates
the missing tax and puts it on the lines. Experio already does most of it: picking the tiers
in the form applies its memorized non-recoverable and prorata flags, and its refresh picks
the tax for each rate from the dossier's tax settings (or says which setting is missing).
Left here: the question on the usual non-recoverable accounts, and the cabinet's prorata.
"""

import re

from mcp.server.mcpserver.exceptions import ToolError

from app import with_ids, ok, question, server
from experio import DRY_RUN, fill_invoice, invoice_context, odoo_call, odoo_json, require_invoice
from tiers import side_of

# tarik-master: "A confirmer par le comptable (CGI art. 106) avant usage en production."
NON_RECOVERABLE_ACCOUNTS = ("6142", "6143", "61251", "2340")  # travel, receptions, fuel, passenger cars


def vat_answer(reply):
    """oui = non recuperable, non = recuperable, else None (tarik-master orchestrator.vat_answer)."""
    text = (reply or "").lower().strip()
    if text.startswith(("oui", "yes", "non recup")):
        return True
    if text.startswith(("non", "no", "recup")):
        return False
    return None


def experio_messages(invoice_id, context):
    """Experio's own error and warning on the invoice, as plain text."""
    invoice = odoo_call("exp.invoice", "read", [invoice_id], fields=["error_msg", "warning_msg"], context=context)[0]
    found = []
    for label, html in (("Erreur Experio", invoice["error_msg"]), ("Avertissement Experio", invoice["warning_msg"])):
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()
        if text:
            found.append(f"{label} : {text}")
    return found


@server.tool()
@with_ids
def settle_tva(invoice_id: int, answer: str = "") -> dict:
    """Settle a draft invoice's TVA once its account is set: recoverable or not,
    the cabinet's prorata, and the tax Experio puts on each line. When the answer
    is a question (non-recoverable TVA?), ask the client and call again with
    their oui/non as ``answer``."""
    invoice = require_invoice(invoice_id, ["state", "type", "company_id", "exercice_id", "partner_id", "account_id",
                                           "amount_tva", "amount_tax_non_recoverable", "tax_prorata", "tva_line_ids"])
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : seule une facture brouillon peut etre modifiee.")
    if not invoice["account_id"]:
        if DRY_RUN:  # the account step only simulated its write
            return ok(f"MODE TEST : aucune modification enregistree dans Experio. Facture {invoice_id} : "
                      "la TVA sera reglee une fois le compte enregistre.")
        raise ToolError(f"Facture {invoice_id} : pas encore de compte, appelez d'abord assign_account (la TVA en depend).")
    kind = side_of(invoice)
    context = invoice_context(invoice)
    changes, notes = {}, []
    code = invoice["account_id"][1].split(" ")[0]
    if kind == "purchase" and not invoice["amount_tax_non_recoverable"] and invoice["amount_tva"]:
        told = vat_answer(answer) if answer else None
        if answer and told is None:
            return question(f"Facture {invoice_id} : reponse non comprise, la TVA est-elle non recuperable ? oui / non")
        if told is None and code.startswith(NON_RECOVERABLE_ACCOUNTS):
            tier = (invoice["partner_id"] or [0, "ce fournisseur"])[1]
            return question(f"Facture de {tier} : la TVA est-elle non recuperable (voyage, carburant, vehicule de tourisme) ? oui / non")
        if told:
            changes.update({"amount_tax_non_recoverable": True, "multi_tva": False})
    non_recoverable = invoice["amount_tax_non_recoverable"] or changes.get("amount_tax_non_recoverable")
    try:
        if kind == "purchase" and not non_recoverable and invoice["amount_tva"] and not invoice["tax_prorata"]:
            company = odoo_call("res.company", "read", [invoice["company_id"][0]], fields=["use_prorata", "prorata_rate"])[0]
            if company["use_prorata"]:
                changes["tax_prorata"] = True  # Experio's onchange applies the cabinet's rate
                notes.append(f"prorata de TVA {company['prorata_rate']} %")
        if changes:
            fill_invoice(invoice_id, changes, context)
            if changes.get("amount_tax_non_recoverable"):
                notes.append("TVA non recuperable")  # Experio saves it on the supplier when the invoice is posted
        if not DRY_RUN:
            odoo_json("exp.invoice", "action_refresh", [invoice_id], context=context)  # Experio picks the tax for each rate
        line_ids = odoo_call("exp.invoice", "read", [invoice_id], fields=["tva_line_ids"], context=context)[0]["tva_line_ids"]
        lines = odoo_call("exp.invoice.line", "read", line_ids, fields=["line_type", "tax_id", "tax_rate", "amount_ht", "amount_tva"],
                          context=context) if line_ids else []
        messages = experio_messages(invoice_id, context)
    except Exception as error:
        print(f"settle_tva {invoice_id}: {error}", flush=True)
        raise ToolError(f"Facture {invoice_id} : Experio a refuse la mise a jour de la TVA.") from None
    taxed = [f"{line['amount_ht']} HT a {line['tax_rate']} % ({line['tax_id'][1] if line['tax_id'] else 'sans taxe'})"
             for line in lines if line["line_type"] == "untaxed"]
    head = "MODE TEST : aucune modification enregistree dans Experio. " if DRY_RUN else ""
    body = f"Facture {invoice_id} : TVA {'non recuperable' if non_recoverable else 'recuperable'}"
    body += (f", {', '.join(notes)}" if notes and not non_recoverable else "") + ". "
    body += ("Lignes : " + "; ".join(taxed) + "." if taxed else "Pas encore de ligne de TVA.")
    return ok(head + body + ("" if not messages else " " + " ".join(messages)))
