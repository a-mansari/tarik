"""Phase 11: withholding tax (retenue a la source, RAS).

Experio already remembers each tiers' RAS: it saves the rule and rates on the tiers when an
invoice is posted (memorize_partner_values) and applies them when the tiers is picked in the
form. tarik-master also wrote them onto the tiers itself when the client answered; that is
Experio's job at posting. Left here: a foreign supplier's last posted rule (Experio only
remembers it when there was a retenue), and the questions tarik-master asks -- a foreign
supplier with no known rule, local fees (6136) with none.
"""

from tarik.core.app import ok, question, server, with_ids
from tarik.core.experio import (dry_run, experio_messages, fill_invoice, invoice_context, odoo_call, odoo_json, refused,
                                require_draft, require_invoice, test_mode_note)
from tarik.core.model import ask_model

QUESTIONS = {
    "foreign": ("quelle retenue a la source etrangere ?\n"
                "1. Sans retenue\n2. A la charge du fournisseur\n3. A la charge de la societe\n"
                "(ajoutez « hors Maroc » si le service est rendu a l'etranger)"),
    "local": "y a-t-il une retenue a la source ? Repondez « sans », ou le taux (ex. « 10 » ou « 10 et 75 »).",
}


# The client's answer is read by the model, then checked here against the only values Experio takes:
# free text ("a la charge de la societe, contrat 2025", "sans, facture du 15/03") defeats any pattern.
ANSWER_PROMPT = """You read a client's answer to a question about the withholding tax (retenue a la source) of
one Moroccan invoice. Report only what the answer itself says. Numbers that are dates, contract or invoice
numbers are not rates. When the answer does not clearly answer the question, say so with null."""
FOREIGN_SHAPE = ('Answer with only a JSON object: {"rule": "none"|"supplier"|"society"|null, "abroad": true|false}. '
                 'rule: none = sans retenue (1), supplier = a la charge du fournisseur (2), society = a la charge de la '
                 'societe (3), null when the answer does not say. abroad: the service was rendered outside Morocco '
                 '("hors Maroc", "a l\'etranger").')
LOCAL_SHAPE = ('Answer with only a JSON object: {"retenue": true|false|null, "is_ir": 5|10|15|20|30|null, "tva": 0|75|100}. '
               'retenue: false for no withholding ("sans", "non"), true when the answer gives a rate, null when it does '
               'not say. is_ir: the IS/IR withholding rate it gives, else null. tva: the TVA withholding rate it gives, else 0.')
IS_IR_RATES, TVA_RATES = {5, 10, 15, 20, 30}, {0, 75, 100}


def read_answer(kind, reply):
    """The model's reading of the client's answer to QUESTIONS[kind]."""
    return ask_model(ANSWER_PROMPT, [], f"Question: {QUESTIONS[kind]}\nClient's answer: {reply}\n\n"
                     + (FOREIGN_SHAPE if kind == "foreign" else LOCAL_SHAPE), "Lecture de la reponse")


def rate(value):
    """A whole rate the model gave (20, "20", 20.0), or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value == int(value) else None
    return int(value) if str(value).strip().isdigit() else None


def withholding_answer(reply):
    """The foreign rule the client chose, or None when their answer doesn't say."""
    answer = read_answer("foreign", reply)
    rule = answer.get("rule")
    if rule not in ("none", "supplier", "society"):
        return None
    return {"retenue": rule != "none", "foreign_withholding": rule, "local_service": answer.get("abroad") is not True}


def local_withholding_answer(reply):
    """No withholding, or its IS/IR and TVA rates; None when the answer doesn't say."""
    answer = read_answer("local", reply)
    if answer.get("retenue") is False:
        return {"retenue": False, "local_withholding_percentage": False, "tax_withholding_percentage": "0"}
    is_ir, tva = rate(answer.get("is_ir")), rate(answer.get("tva", 0)) or 0
    if answer.get("retenue") is not True or (is_ir is not None and is_ir not in IS_IR_RATES) or tva not in TVA_RATES \
            or (is_ir is None and not tva):
        return None
    return {"retenue": True, "local_withholding_percentage": str(is_ir) if is_ir else False, "tax_withholding_percentage": str(tva)}


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
    require_draft(invoice_id, invoice)
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
    with refused(f"settle_withholding {invoice_id}", f"Facture {invoice_id} : Experio a refuse la retenue a la source."):
        fill_invoice(invoice_id, values, context)  # Experio's onchange sets the RAS lines and accounts, as on screen
        if not dry_run():
            odoo_json("exp.invoice", "action_refresh", [invoice_id], context=context)
        messages = experio_messages(invoice_id, context)
    return ok(f"{test_mode_note()}Facture {invoice_id} : {described(values)}." + ("" if not messages else " " + " ".join(messages)))
