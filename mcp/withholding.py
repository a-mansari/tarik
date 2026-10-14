"""Phase 11: withholding tax (retenue a la source, RAS).

Experio already remembers each tiers' RAS: it saves the rule and rates on the tiers when an
invoice is posted (memorize_partner_values) and applies them when the tiers is picked in the
form. tarik-master also wrote them onto the tiers itself when the client answered; that is
Experio's job at posting. Left here: a foreign supplier's last posted rule (Experio only
remembers it when there was a retenue), and the questions tarik-master asks -- a foreign
supplier with no known rule, local fees (6136) with none.
"""

import re

from mcp.server.mcpserver.exceptions import ToolError

from app import ok, question, server, with_ids
from experio import DRY_RUN, fill_invoice, invoice_context, odoo_call, odoo_json, require_invoice
from tva import experio_messages

# tarik-master/tarik/orchestrator.py, verbatim.
WITHHOLDING_CHOICES = {"1": "none", "2": "supplier", "3": "society"}


def withholding_answer(reply):
    """1/2/3 or the words; "etranger" / "hors Maroc" = service rendered abroad."""
    text = (reply or "").lower().replace("é", "e")
    rule = next((rule for key, rule in WITHHOLDING_CHOICES.items() if key in text), None)
    rule = rule or ("none" if "sans" in text else "society" if "societe" in text
                    else "supplier" if "fournisseur" in text else None)
    if not rule:
        return None
    abroad = "etranger" in text or "hors maroc" in text
    return {"retenue": rule != "none", "foreign_withholding": rule, "local_service": not abroad}


def local_withholding_answer(reply):
    """"sans", or the rates: 5/10/15/20/30 is the RAS IS/IR, 75/100 the RAS TVA."""
    text = (reply or "").lower()
    rates = re.findall(r"\d+", text)
    is_ir = next((r for r in rates if r in ("5", "10", "15", "20", "30")), None)
    tva = next((r for r in rates if r in ("75", "100")), "0")
    if is_ir or tva != "0":
        return {"retenue": True, "local_withholding_percentage": is_ir or False, "tax_withholding_percentage": tva}
    if "sans" in text or text.strip() in ("non", "0", "1"):
        return {"retenue": False, "local_withholding_percentage": False, "tax_withholding_percentage": "0"}
    return None


QUESTIONS = {
    "foreign": ("quelle retenue a la source etrangere ?\n"
                "1. Sans retenue\n2. A la charge du fournisseur\n3. A la charge de la societe\n"
                "(ajoutez « hors Maroc » si le service est rendu a l'etranger)"),
    "local": "y a-t-il une retenue a la source ? Repondez « sans », ou le taux (ex. « 10 » ou « 10 et 75 »).",
}


LOCAL_RAS_ACCOUNTS = ("6136",)  # honoraires, commissions: the local purchases where a RAS is usual (tarik-master)
FIELDS = {"foreign": ("retenue", "foreign_withholding", "local_service"),
          "local": ("retenue", "local_withholding_percentage", "tax_withholding_percentage")}


def described(values):
    if not values.get("retenue"):
        return "sans retenue a la source"
    if "foreign_withholding" in values:
        who = {"supplier": "a la charge du fournisseur", "society": "a la charge de la societe"}.get(values["foreign_withholding"], "")
        return f"retenue etrangere {who}" + ("" if values.get("local_service", True) else ", service rendu hors Maroc")
    rates = [f"IS/IR {values['local_withholding_percentage']} %"] if values.get("local_withholding_percentage") else []
    if values.get("tax_withholding_percentage") not in (None, False, "0"):
        rates.append(f"TVA {values['tax_withholding_percentage']} %")
    return "retenue a la source " + " et ".join(rates)


@server.tool()
@with_ids
def settle_withholding(invoice_id: int, answer: str = "") -> dict:
    """Settle a draft purchase invoice's withholding tax (RAS): the tiers' known rule,
    else a question to the client; call again with their answer as ``answer``."""
    invoice = require_invoice(invoice_id, ["state", "type", "is_local_invoice", "company_id", "exercice_id", "partner_id",
                                           "account_id", "retenue", *FIELDS["foreign"][1:], *FIELDS["local"][1:]])
    if invoice["type"] != "purchase" or not invoice["partner_id"]:
        return ok(f"Facture {invoice_id} : pas de retenue a la source a regler ici (vente ou tiers absent).")
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : seule une facture brouillon peut etre modifiee.")
    kind = "local" if invoice["is_local_invoice"] else "foreign"
    if invoice["retenue"]:  # the tiers' remembered rule, applied by Experio when the tiers was picked
        return ok(f"Facture {invoice_id} : {described({name: invoice[name] for name in FIELDS[kind]})} (regle connue du tiers).")
    values = None
    if answer:
        values = (withholding_answer if kind == "foreign" else local_withholding_answer)(answer)
        if values is None:
            return question(f"Facture {invoice_id} : reponse non comprise, {QUESTIONS[kind]}")
    elif kind == "foreign":
        last = odoo_call("exp.invoice", "search_read", [
            ("partner_id", "=", invoice["partner_id"][0]), ("id", "!=", invoice_id), ("state", "in", ["post", "export"]),
            ("is_local_invoice", "=", False)], fields=list(FIELDS["foreign"]), order="invoice_date desc, id desc", limit=1)
        if last:
            values = {name: last[0][name] for name in FIELDS["foreign"]}
        else:
            return question(f"Facture de {invoice['partner_id'][1]} : {QUESTIONS['foreign']}")
    else:
        code = (invoice["account_id"] or [0, ""])[1].split(" ")[0]
        if not code.startswith(LOCAL_RAS_ACCOUNTS):
            return ok(f"Facture {invoice_id} : sans retenue a la source.")
        return question(f"Facture de {invoice['partner_id'][1]} : {QUESTIONS['local']}")
    context = invoice_context(invoice)
    try:
        fill_invoice(invoice_id, values, context)  # Experio's onchange sets the RAS lines and accounts, as on screen
        if not DRY_RUN:
            odoo_json("exp.invoice", "action_refresh", [invoice_id], context=context)
        messages = experio_messages(invoice_id, context)
    except Exception as error:
        print(f"settle_withholding {invoice_id}: {error}", flush=True)
        raise ToolError(f"Facture {invoice_id} : Experio a refuse la retenue a la source.") from None
    head = "MODE TEST : aucune modification enregistree dans Experio. " if DRY_RUN else ""
    return ok(f"{head}Facture {invoice_id} : {described(values)}." + ("" if not messages else " " + " ".join(messages)))
