"""Phase 6: the invoice's tiers (supplier for a purchase, customer for a sale).

tarik-master runs a 5-tool agent for this. Experio already does most of it: when
Bluejay reads a draft, orion.wizard.get_partner_data finds the tiers by ICE then IF,
creates it from the national registry when the dossier lacks it, and links it. And
its own wizard assigns the Compte Tiers. So this phase only links the right detected
party and has Experio assign its Compte Tiers.
"""

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from app import server
from dossiers import similarity
from experio import DRY_RUN, POSTGRES, fill_invoice, invoice_context, odoo_call, odoo_json, require_invoice

# Per invoice type: the detected party, the partner's role flag and its Compte Tiers field.
SIDE = {
    "purchase": ("detected_supplier_id", "is_supplier", "third_party_supplier_account", "fournisseur", "supplier_name"),
    "sale": ("detected_customer_id", "is_customer", "third_party_customer_account", "client", "customer_name"),
}
# Stricter than dossiers (0.7): tiers names share generic words ("distribution", "SARL"). On 449 real tiers,
# right matches scored 0.89+ and wrong ones up to 0.71 (e.g. two "... DISTRIBUTION" companies).
TIERS_MIN_SIMILARITY = 0.8
INVOICE_FIELDS = ["state", "type", "company_id", "exercice_id", "partner_id", "detected_supplier_id",
                  "detected_customer_id", "supplier_name", "customer_name", "bluejay_state"]


def side_of(invoice):
    """The invoice's side, from its classification when there is one, else from Experio's type."""
    with psycopg.connect(**POSTGRES) as db:
        row = db.execute("select category from tarik_classifications where invoice_id = %s", (invoice["id"],)).fetchone()
    if row and row[0] == "rb":
        raise ToolError(f"Facture {invoice['id']} : c'est un releve bancaire, pas une facture : pas de tiers.")
    kind = {"achat": "purchase", "vente": "sale"}.get(row[0]) if row else invoice["type"]
    if kind not in SIDE:
        raise ToolError(f"Facture {invoice['id']} : ni achat ni vente, classez-la d'abord avec classify_invoice.")
    return kind


def ensure_tiers_account(invoice_id, partner_id, kind, context):
    """The tiers' Compte Tiers for this side, created if missing -- through Experio's own
    "assign third-party account" wizard, as the invoice screen does. Experio proposes a
    code only in dossiers with automatic numbering on; elsewhere the cabinet assigns it
    by hand, so Tarik doesn't invent one (tarik-master generated it regardless)."""
    _, _, field, role, _ = SIDE[kind]
    partner = odoo_call("exp.partner", "read", [partner_id], fields=["name", field], context=context)[0]
    if partner[field]:
        return f"Compte Tiers {role} {partner[field]}"
    wizard_id = odoo_call("exp.invoice.partner.account.wizard", "create", {"invoice_id": invoice_id}, context=context)
    code = odoo_call("exp.invoice.partner.account.wizard", "read", [wizard_id], fields=["third_party_account"],
                     context=context)[0]["third_party_account"]
    if not code:
        return (f"pas de Compte Tiers {role} : dans ce dossier le cabinet les attribue lui-meme "
                "(numerotation automatique desactivee), la facture ne pourra pas etre comptabilisee avant")
    if DRY_RUN:
        return f"Compte Tiers {role} {code} serait cree"
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
    try:
        if not invoice["partner_id"] or invoice["partner_id"][0] != partner_id:
            fill_invoice(invoice["id"], {"partner_id": partner_id}, context)
        account = ensure_tiers_account(invoice["id"], partner_id, kind, context)
    except ToolError:
        raise
    except Exception as error:
        print(f"tiers {invoice['id']} -> {partner_id}: {error}", flush=True)
        raise ToolError(f"Facture {invoice['id']} : Experio a refuse le tiers {name}.") from None
    head = "MODE TEST : aucune modification enregistree dans Experio. " if DRY_RUN else ""
    return f"{head}Facture {invoice['id']} : {SIDE[kind][3]} {name}, {account}."


def draft_invoice(invoice_id):
    invoice = require_invoice(invoice_id, INVOICE_FIELDS)
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : seule une facture brouillon peut changer de tiers.")
    if invoice["bluejay_state"] in ("pending", "sent"):
        raise ToolError(f"Facture {invoice_id} : Experio est encore en train de la lire, reessayez dans une minute.")
    return invoice


@server.tool()
def link_tiers(invoice_id: int) -> str:
    """Give a draft invoice its tiers -- the supplier of a purchase, the customer
    of a sale -- as Experio detected it from the scan, and make sure that tiers has
    its Compte Tiers (needed to post). Call it after classify_invoice. When no tiers
    was detected, the answer suggests names: confirm one with the client, then set_tiers."""
    invoice = draft_invoice(invoice_id)
    kind = side_of(invoice)
    detected_field, _, _, role, name_field = SIDE[kind]
    detected = invoice[detected_field]
    if detected:
        return link(invoice, detected[0], kind)
    if invoice["partner_id"]:  # set by hand or by an earlier set_tiers: keep it
        return link(invoice, invoice["partner_id"][0], kind)
    printed = invoice[name_field]
    if not printed:
        return f"Facture {invoice_id} : aucun {role} detecte sur le scan. Demandez au client son nom, puis find_tiers."
    return f"Facture {invoice_id} : {role} « {printed} » introuvable par ICE/IF. " + find_tiers(invoice_id, printed)


@server.tool()
def find_tiers(invoice_id: int, name: str) -> str:
    """Search the invoice's dossier for tiers whose name looks like ``name``
    (misspelled or partial is fine). Returns the closest ones with their id; let
    the client confirm one, then set_tiers."""
    invoice = require_invoice(invoice_id, ["company_id"])
    partners = odoo_call("exp.partner", "search_read", [("company_id", "=", invoice["company_id"][0])],
                         fields=["id", "name", "ice"], context={"allowed_company_ids": [invoice["company_id"][0]]})
    ranked = sorted(((similarity(name, p["name"]), p) for p in partners), key=lambda pair: -pair[0])
    close = [(score, p) for score, p in ranked[:5] if score >= TIERS_MIN_SIMILARITY]
    if not close:
        return f"Aucun tiers du dossier ne ressemble a « {name} » : a creer par le cabinet."
    return "Tiers proches : " + "; ".join(
        f"{p['name']} (id {p['id']}{', ICE ' + p['ice'] if p['ice'] else ''}) {round(score * 100)} %" for score, p in close)


@server.tool()
def set_tiers(invoice_id: int, partner_id: int) -> str:
    """Put the tiers the client confirmed (an id from find_tiers) on a draft
    invoice, and make sure it has its Compte Tiers."""
    if not isinstance(partner_id, int) or isinstance(partner_id, bool) or partner_id <= 0:
        raise ToolError("partner_id manquant ou invalide : cherchez le tiers avec find_tiers.")
    invoice = draft_invoice(invoice_id)
    return link(invoice, partner_id, side_of(invoice))
