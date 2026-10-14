"""Phase 6: the invoice's tiers (supplier for a purchase, customer for a sale).

tarik-master runs a 5-tool agent for this. Experio already does most of it: when
Bluejay reads a draft, orion.wizard.get_partner_data finds the tiers by ICE then IF,
creates it from the national registry when the dossier lacks it, and links it. And
its own wizard assigns the Compte Tiers. So this phase only links the right detected
party and has Experio assign its Compte Tiers.
"""

import re

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from app import with_ids, ok, question, server
from dossiers import normalized, similarity
from experio import DRY_RUN, POSTGRES, fill_invoice, invoice_context, odoo_call, odoo_json, require_dossier, require_invoice

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
    return ok(f"{head}Facture {invoice['id']} : {SIDE[kind][3]} {name}, {account}.")


def draft_invoice(invoice_id):
    invoice = require_invoice(invoice_id, INVOICE_FIELDS)
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : seule une facture brouillon peut changer de tiers.")
    if invoice["bluejay_state"] in ("pending", "sent"):
        raise ToolError(f"Facture {invoice_id} : Experio est encore en train de la lire, reessayez dans une minute.")
    return invoice


@server.tool()
@with_ids
def link_tiers(invoice_id: int) -> dict:
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
        return question(f"Facture {invoice_id} : aucun {role} detecte sur le scan. Quel est son nom ?")
    return question(f"Facture {invoice_id} : {role} « {printed} » introuvable par ICE/IF. " + find_tiers(invoice_id, printed))


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
@with_ids
def set_tiers(invoice_id: int, partner_id: int) -> dict:
    """Put the tiers the client confirmed (an id from find_tiers) on a draft
    invoice, and make sure it has its Compte Tiers."""
    if not isinstance(partner_id, int) or isinstance(partner_id, bool) or partner_id <= 0:
        raise ToolError("partner_id manquant ou invalide : cherchez le tiers avec find_tiers.")
    invoice = draft_invoice(invoice_id)
    return link(invoice, partner_id, side_of(invoice))


AUTOFILL = {"third_party_supplier_autofill": "third_party_supplier_account",
            "third_party_customer_autofill": "third_party_customer_account"}  # switch -> the prefix it needs


@server.tool()
def enable_tiers_autofill(dossier_id: int) -> str:
    """Turn on automatic Compte Tiers numbering (suppliers and customers) for a
    dossier, so link_tiers can create missing ones. A cabinet setting: only when
    the cabinet asks for it explicitly."""
    dossier = require_dossier(dossier_id)
    context = {"allowed_company_ids": [dossier_id]}
    company = odoo_call("res.company", "read", [dossier_id], fields=[*AUTOFILL, *AUTOFILL.values()], context=context)[0]
    if all(company[switch] for switch in AUTOFILL):
        return f"Dossier {dossier} : numerotation automatique des Comptes Tiers deja active."
    missing = [prefix for switch, prefix in AUTOFILL.items() if not company[prefix]]
    if missing:
        return f"Dossier {dossier} : prefixes de Comptes Tiers non configures ({', '.join(missing)}), a regler par le cabinet d'abord."
    if DRY_RUN:
        return f"MODE TEST : aucune modification enregistree dans Experio. Dossier {dossier} : la numerotation automatique serait activee."
    values = {switch: True for switch in AUTOFILL}
    try:
        # Configuration > Parametres as the user saves it (tarik-master odoo.save_settings), never the dossier record itself.
        settings_id = odoo_call("res.config.settings", "create", values, context=context)
        try:
            odoo_json("res.config.settings", "execute", [settings_id], context=context)
        except RuntimeError as error:  # "Enregistrer" is for admins; a manager's form save already went through
            if "administrat" not in str(error).lower():
                raise
        saved = odoo_call("res.company", "read", [dossier_id], fields=list(values), context=context)[0]
    except Exception as error:
        print(f"enable_tiers_autofill {dossier_id}: {error}", flush=True)
        raise ToolError(f"Experio n'a pas pu modifier les parametres du dossier {dossier}.") from None
    if not all(saved[switch] for switch in values):
        return f"Dossier {dossier} : reglage refuse par Experio, a activer par le cabinet (Configuration > Parametres)."
    return f"Dossier {dossier} : numerotation automatique des Comptes Tiers activee."


AFFIRMATIVE = ("oui", "yes", "c'est ca", "c'est bien", "exact", "ok", "d'accord")


def pick_tiers(partners, answer, proposed_ids=()):
    """The tiers the client means, or None. Their answer is free text: it may carry a
    proposed tiers' id or full name ("oui, c'est bien ATLAS DE ROULEMENT"), be a plain yes
    to a single proposal, or be a name to compare (80 % and 10 points ahead)."""
    by_id = {p["id"]: p for p in partners}
    named = [int(n) for n in re.findall(r"\d+", answer) if int(n) in by_id]
    if len(set(named)) == 1:
        return by_id[named[0]]
    said = normalized(answer)
    contained = [p for p in partners if normalized(re.sub(r"\s+-\s+\d+\s*$", "", p["name"])) in said]
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
    partners = odoo_call("exp.partner", "search_read", [("company_id", "=", invoice["company_id"][0])], fields=["id", "name"],
                         context={"allowed_company_ids": [invoice["company_id"][0]]})
    proposed = [int(n) for n in re.findall(r"\(id (\d+)", question_asked)]
    partner = pick_tiers(partners, answer, proposed)
    if partner:
        return link(invoice, partner["id"], side_of(invoice))
    return question(f"Facture {invoice_id} : « {answer} » ne designe pas un seul tiers. " + find_tiers(invoice_id, answer))
