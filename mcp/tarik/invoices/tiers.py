"""The invoice's tiers (supplier for a purchase, customer for a sale).

Experio does most of it: when Bluejay reads a draft, orion.wizard.get_partner_data finds the tiers by ICE then IF,
creates it from the national registry when the dossier lacks it, and links it. And
its own wizard assigns the Compte Tiers. So this step only links the right detected
party and has Experio assign its Compte Tiers.
"""

import re

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, question, server, with_ids
from tarik.core.config import TIERS_MIN_SIMILARITY
from tarik.core.experio import (
    dry_run,
    fill_invoice,
    in_dossier,
    invoice_context,
    odoo_call,
    odoo_json,
    refused,
    require_draft,
    require_invoice,
    still_reading,
    test_mode_note,
)
from tarik.core.words import normalized, ranked
from tarik.invoices.classify import side_of

# Per invoice side: the party Experio detected, the name printed on the scan, the tiers' Compte Tiers field.
SIDE = {
    "purchase": {
        "detected": "detected_supplier_id",
        "printed": "supplier_name",
        "account": "third_party_supplier_account",
        "autofill": "third_party_supplier_autofill",
        "role": "fournisseur",
    },
    "sale": {
        "detected": "detected_customer_id",
        "printed": "customer_name",
        "account": "third_party_customer_account",
        "autofill": "third_party_customer_autofill",
        "role": "client",
    },
}
NO_AUTOFILL = (
    "pas de Compte Tiers {role} : dans ce dossier le cabinet les attribue lui-meme "
    "(numerotation automatique desactivee), la facture ne pourra pas etre comptabilisee avant"
)
# A proposed tiers in find_tiers' question: "NAME (id 123" after "Tiers proches : " or "; ".
PROPOSED = re.compile(r"(?:Tiers proches : |; )([^;]+?) \(id (\d+)")
INVOICE_FIELDS = [
    "state",
    "type",
    "company_id",
    "exercice_id",
    "partner_id",
    "detected_supplier_id",
    "detected_customer_id",
    "supplier_name",
    "customer_name",
    "bluejay_state",
]


def ensure_tiers_account(invoice_id, partner_id, kind, context):
    """The tiers' Compte Tiers for side ``kind``, created if missing through Experio's own "assign third-party
    account" wizard, as the invoice screen does: a phrase for the message. Experio proposes a code only in
    dossiers with automatic numbering on; elsewhere the cabinet assigns it, so Tarik doesn't invent one."""
    field, role = SIDE[kind]["account"], SIDE[kind]["role"]
    partner = odoo_call("exp.partner", "read", [partner_id], fields=["name", field], context=context)[0]
    if partner[field]:
        return f"Compte Tiers {role} {partner[field]}"
    if dry_run():  # the wizard proposes its code only once created: test mode reads the dossier's switch instead
        autofill = SIDE[kind]["autofill"]
        company = odoo_call("res.company", "read", context["allowed_company_ids"], fields=[autofill])[0]
        if company[autofill]:
            return f"Compte Tiers {role} serait cree par Experio (numerotation automatique)"
        return NO_AUTOFILL.format(role=role)
    wizard = "exp.invoice.partner.account.wizard"
    wizard_id = odoo_call(wizard, "create", {"invoice_id": invoice_id}, context=context)
    code = odoo_call(wizard, "read", [wizard_id], fields=["third_party_account"], context=context)[0][
        "third_party_account"
    ]
    if not code:
        return NO_AUTOFILL.format(role=role)
    odoo_json(wizard, "action_confirm", [wizard_id], context=context)
    return f"Compte Tiers {role} {code} cree"


def link(invoice, partner_id, kind):
    """Put tiers ``partner_id`` on ``invoice`` (dict) through Experio's form (its onchange fills the memorized
    account, withholding... as on screen), then make sure it has its Compte Tiers: ok."""
    context = invoice_context(invoice)
    partner = odoo_call("exp.partner", "read", [partner_id], fields=["name", "company_id", "active"], context=context)
    if not partner or partner[0]["company_id"][0] != invoice["company_id"][0]:
        raise ToolError(f"Le tiers n° {partner_id} n'appartient pas au dossier de la facture {invoice['id']}.")
    name = partner[0]["name"]
    with refused(
        f"tiers {invoice['id']} -> {partner_id}", f"Facture {invoice['id']} : Experio a refuse le tiers {name}."
    ):
        if not invoice["partner_id"] or invoice["partner_id"][0] != partner_id:
            fill_invoice(invoice["id"], {"partner_id": partner_id}, context)
        account = ensure_tiers_account(invoice["id"], partner_id, kind, context)
    return ok(f"{test_mode_note()}Facture {invoice['id']} : {SIDE[kind]['role']} {name}, {account}.")


def draft_invoice(invoice_id):
    """The invoice's INVOICE_FIELDS (dict); ToolError unless it is a draft Experio has finished reading."""
    invoice = require_invoice(invoice_id, INVOICE_FIELDS)
    require_draft(invoice_id, invoice, "changer de tiers")
    if waiting := still_reading(invoice):
        raise ToolError(waiting["message"])
    return invoice


def dossier_tiers(company_id):
    """The dossier's tiers: [{id, name, ice}]."""
    return odoo_call(
        "exp.partner",
        "search_read",
        [("company_id", "=", company_id)],
        fields=["id", "name", "ice"],
        context=in_dossier(company_id),
    )


@server.tool()
@with_ids
def link_tiers(invoice_id: int) -> dict:
    """Give a draft invoice its tiers -- the supplier of a purchase, the customer
    of a sale -- as Experio detected it from the scan, and make sure that tiers has
    its Compte Tiers (needed to post). Call it after classify_invoice. When no tiers
    was detected, the question suggests close names; the client's answer goes to choose_tiers (answer_pending)."""
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
    return question(
        f"Facture {invoice_id} : {role} « {printed} » introuvable par ICE/IF. " + find_tiers(invoice_id, printed)
    )


def find_tiers(invoice_id, name):
    """The tiers of the invoice's dossier whose name looks like ``name`` (misspelled or partial is fine), the 5
    closest first with their id, for the client to choose: a phrase for the question. However many tiers the
    dossier has, the client is shown 5 at most, and narrows them with another name or the tiers' ICE."""
    company_id = require_invoice(invoice_id, ["company_id"])["company_id"][0]
    close = ranked(name, dossier_tiers(company_id), TIERS_MIN_SIMILARITY, top=5)
    if not close:
        return (
            f"Aucun tiers du dossier ne ressemble a « {name} » : donnez un autre nom ou l'ICE du tiers, "
            "ou il est a creer par le cabinet."
        )
    proposed = "; ".join(
        f"{p['name']} (id {p['id']}{', ICE ' + p['ice'] if p['ice'] else ''}) {round(score * 100)} %"
        for score, p in close
    )
    return f"Tiers proches : {proposed}. Lequel est-ce ? Sinon, donnez un autre nom ou l'ICE du tiers."


def proposed_choice(decision, question_asked):
    """The id of the tiers ``decision`` names among those ``question_asked`` proposed ("NAME (id 123" as find_tiers
    writes them): its id, "id 123", or its name as proposed. None for anything else."""
    proposed = {int(tiers_id): normalized(name) for name, tiers_id in PROPOSED.findall(question_asked)}
    decision = normalized(str(decision or ""))
    as_id = decision.removeprefix("id ")
    if as_id.isdigit() and int(as_id) in proposed:
        return int(as_id)
    named = [tiers_id for tiers_id, name in proposed.items() if name == decision]
    return named[0] if len(named) == 1 else None


def choose_tiers(invoice_id, decision, answer, question_asked=""):
    """Link the tiers the client chose for a tiers question (``question_asked``, which proposes some by id). The
    agent's ``decision``, read from the client's ``answer`` (their own words), names the chosen tiers (proposed_choice),
    taken only when the question proposed it; or it is the other name or ICE the client gave instead: the ICE of one
    of the dossier's tiers links it, a name is searched for and the closest are proposed again."""
    invoice = draft_invoice(invoice_id)
    chosen = proposed_choice(decision, question_asked)
    if chosen:
        return link(invoice, chosen, side_of(invoice))
    decision = str(decision or "").strip()
    # Not one proposed: the name or ICE the client gave instead, as the agent read it, else their own words.
    an_id = decision.isdigit() and len(decision) < 15
    given = decision if decision and not an_id else answer
    written = re.findall(r"(?<!\d)\d{15}(?!\d)", given)  # an ICE: 15 digits
    by_ice = [p for p in dossier_tiers(invoice["company_id"][0]) if p["ice"] and p["ice"] in written]
    if len(by_ice) == 1:
        return link(invoice, by_ice[0]["id"], side_of(invoice))
    unclear = f"Facture {invoice_id} : « {given} » ne designe pas un seul tiers. "
    return question(unclear + find_tiers(invoice_id, given))
