"""Phase 6: the invoice's tiers (supplier for a purchase, customer for a sale).

tarik-master runs a 5-tool agent for this. Experio already does most of it: when
Bluejay reads a draft, orion.wizard.get_partner_data finds the tiers by ICE then IF,
creates it from the national registry when the dossier lacks it, and links it. And
its own wizard assigns the Compte Tiers. So this phase only links the right detected
party and has Experio assign its Compte Tiers.
"""

import re

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import with_ids, ok, question, server
from tarik.dossiers.search import normalized, similarity, without_if
from tarik.core.experio import (dry_run, fill_invoice, invoice_context, odoo_call, odoo_json, refused, require_draft,
                                require_invoice, test_mode_note)
from tarik.invoices.classify import side_of

# Per invoice side: the party Experio detected, the name printed on the scan, the tiers' Compte Tiers field.
SIDE = {
    "purchase": {"detected": "detected_supplier_id", "printed": "supplier_name", "account": "third_party_supplier_account",
                 "autofill": "third_party_supplier_autofill", "role": "fournisseur"},
    "sale": {"detected": "detected_customer_id", "printed": "customer_name", "account": "third_party_customer_account",
             "autofill": "third_party_customer_autofill", "role": "client"},
}
# Stricter than dossiers (0.7): tiers names share generic words ("distribution", "SARL"). On 449 real tiers,
# right matches scored 0.89+ and wrong ones up to 0.71 (e.g. two "... DISTRIBUTION" companies).
TIERS_MIN_SIMILARITY = 0.8
NO_AUTOFILL = ("pas de Compte Tiers {role} : dans ce dossier le cabinet les attribue lui-meme "
               "(numerotation automatique desactivee), la facture ne pourra pas etre comptabilisee avant")
INVOICE_FIELDS = ["state", "type", "company_id", "exercice_id", "partner_id", "detected_supplier_id",
                  "detected_customer_id", "supplier_name", "customer_name", "bluejay_state"]


def ensure_tiers_account(invoice_id, partner_id, kind, context):
    """The tiers' Compte Tiers for this side, created if missing -- through Experio's own
    "assign third-party account" wizard, as the invoice screen does. Experio proposes a
    code only in dossiers with automatic numbering on; elsewhere the cabinet assigns it
    by hand, so Tarik doesn't invent one (tarik-master generated it regardless)."""
    field, role = SIDE[kind]["account"], SIDE[kind]["role"]
    partner = odoo_call("exp.partner", "read", [partner_id], fields=["name", field], context=context)[0]
    if partner[field]:
        return f"Compte Tiers {role} {partner[field]}"
    if dry_run():  # the wizard proposes its code only once created: test mode reads the dossier's switch instead
        company = odoo_call("res.company", "read", context["allowed_company_ids"], fields=[SIDE[kind]["autofill"]])[0]
        return (f"Compte Tiers {role} serait cree par Experio (numerotation automatique)" if company[SIDE[kind]["autofill"]]
                else NO_AUTOFILL.format(role=role))
    wizard_id = odoo_call("exp.invoice.partner.account.wizard", "create", {"invoice_id": invoice_id}, context=context)
    code = odoo_call("exp.invoice.partner.account.wizard", "read", [wizard_id], fields=["third_party_account"],
                     context=context)[0]["third_party_account"]
    if not code:
        return NO_AUTOFILL.format(role=role)
    odoo_json("exp.invoice.partner.account.wizard", "action_confirm", [wizard_id], context=context)
    return f"Compte Tiers {role} {code} cree"


def link(invoice, partner_id, kind):
    """Put the tiers on the invoice through Experio's form (its onchange fills the memorized
    account, withholding... as on screen), then make sure it has its Compte Tiers."""
    context = invoice_context(invoice)
    partner = odoo_call("exp.partner", "read", [partner_id], fields=["name", "company_id", "active"], context=context)
    if not partner or partner[0]["company_id"][0] != invoice["company_id"][0]:
        raise ToolError(f"Le tiers n° {partner_id} n'appartient pas au dossier de la facture {invoice['id']}.")
    name = partner[0]["name"]
    with refused(f"tiers {invoice['id']} -> {partner_id}", f"Facture {invoice['id']} : Experio a refuse le tiers {name}."):
        if not invoice["partner_id"] or invoice["partner_id"][0] != partner_id:
            fill_invoice(invoice["id"], {"partner_id": partner_id}, context)
        account = ensure_tiers_account(invoice["id"], partner_id, kind, context)
    return ok(f"{test_mode_note()}Facture {invoice['id']} : {SIDE[kind]['role']} {name}, {account}.")


def draft_invoice(invoice_id):
    invoice = require_invoice(invoice_id, INVOICE_FIELDS)
    require_draft(invoice_id, invoice, "changer de tiers")
    if invoice["bluejay_state"] in ("pending", "sent"):
        raise ToolError(f"Facture {invoice_id} : Experio est encore en train de la lire, reessayez dans une minute.")
    return invoice


@server.tool()
@with_ids
def link_tiers(invoice_id: int) -> dict:
    """Give a draft invoice its tiers -- the supplier of a purchase, the customer
    of a sale -- as Experio detected it from the scan, and make sure that tiers has
    its Compte Tiers (needed to post). Call it after classify_invoice. When no tiers
    was detected, the question suggests close names; the client's answer goes to set_tiers_by_name."""
    invoice = draft_invoice(invoice_id)
    kind = side_of(invoice)
    role = SIDE[kind]["role"]
    detected = invoice[SIDE[kind]["detected"]]
    if detected:
        return link(invoice, detected[0], kind)
    if invoice["partner_id"]:  # set by hand or by an earlier answer: keep it
        return link(invoice, invoice["partner_id"][0], kind)
    printed = invoice[SIDE[kind]["printed"]]
    if not printed:
        return question(f"Facture {invoice_id} : aucun {role} detecte sur le scan. Quel est son nom ?")
    return question(f"Facture {invoice_id} : {role} « {printed} » introuvable par ICE/IF. " + find_tiers(invoice_id, printed))


def find_tiers(invoice_id, name):
    """The tiers of the invoice's dossier whose name looks like ``name`` (misspelled or partial is
    fine), closest first with their id, for the client to pick one."""
    invoice = require_invoice(invoice_id, ["company_id"])
    partners = odoo_call("exp.partner", "search_read", [("company_id", "=", invoice["company_id"][0])],
                         fields=["id", "name", "ice"], context={"allowed_company_ids": [invoice["company_id"][0]]})
    ranked = sorted(((similarity(name, p["name"]), p) for p in partners), key=lambda pair: -pair[0])
    close = [(score, p) for score, p in ranked[:5] if score >= TIERS_MIN_SIMILARITY]
    if not close:
        return f"Aucun tiers du dossier ne ressemble a « {name} » : a creer par le cabinet."
    return "Tiers proches : " + "; ".join(
        f"{p['name']} (id {p['id']}{', ICE ' + p['ice'] if p['ice'] else ''}) {round(score * 100)} %" for score, p in close)


AFFIRMATIVE = ("oui", "yes", "c'est ca", "c'est bien", "exact", "ok", "d'accord")


def pick_tiers(partners, answer, proposed_ids=()):
    """The tiers the client means, or None. Their answer is free text: it may carry an
    ICE, a proposed tiers' id or full name ("oui, c'est bien ATLAS DE ROULEMENT"), be a plain yes
    to a single proposal, or be a name to compare (80 % and 10 points ahead)."""
    by_id = {p["id"]: p for p in partners}
    numbers = re.findall(r"\d+", answer)
    by_ice = [p for p in partners if p.get("ice") and p["ice"] in numbers]  # duplicates of one company differ by ICE
    if len(by_ice) == 1:
        return by_ice[0]
    named = [int(n) for n in numbers if int(n) in by_id]
    if len(set(named)) == 1:
        return by_id[named[0]]
    said = normalized(answer)
    contained = [p for p in partners if normalized(without_if(p["name"])) in said]
    if len(contained) == 1:
        return contained[0]
    if len(proposed_ids) == 1 and proposed_ids[0] in by_id and said.startswith(tuple(normalized(a) for a in AFFIRMATIVE)):
        return by_id[proposed_ids[0]]
    ranked = sorted(((similarity(answer, p["name"]), p) for p in partners), key=lambda pair: -pair[0])
    if ranked and ranked[0][0] >= TIERS_MIN_SIMILARITY and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.1):
        return ranked[0][1]
    return None


def set_tiers_by_name(invoice_id, answer, question_asked=""):
    """Link the tiers the client named in answer to a tiers question; else the close ones."""
    invoice = draft_invoice(invoice_id)
    partners = odoo_call("exp.partner", "search_read", [("company_id", "=", invoice["company_id"][0])], fields=["id", "name", "ice"],
                         context={"allowed_company_ids": [invoice["company_id"][0]]})
    proposed = [int(n) for n in re.findall(r"\(id (\d+)", question_asked)]
    partner = pick_tiers(partners, answer, proposed)
    if partner:
        return link(invoice, partner["id"], side_of(invoice))
    return question(f"Facture {invoice_id} : « {answer} » ne designe pas un seul tiers. " + find_tiers(invoice_id, answer))
