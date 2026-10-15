"""Phase 8: the invoice's account (Compte), class 6 for a purchase, class 7 for a sale.

tarik-master runs an agent: memorized account, else classify what was billed, else ask.
Experio already holds the memorized chain (last account used for the tiers, then the
tiers sheet, then the dossier default) and applies it when the tiers is picked in the
form, and its own refresh rebuilds the lines from the header. So the model only does
what Experio can't: classify what was billed among the dossier's real accounts.
"""

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import with_ids, ok, question, server
from tarik.core.cabinets import cabinet_db
from tarik.core.experio import dry_run, fill_invoice, invoice_context, odoo_call, odoo_json, require_invoice
from tarik.core.model import ask_model
from tarik.invoices.tiers import side_of

# tarik-master/tarik/agents/account.py, its CGNC guidance and rules verbatim; the steps about
# its own tools (memorized_account, search_accounts) are what Experio and the list below replace.
ACCOUNT_PROMPT = """You choose the accounting account (Compte) of an invoice in a
Moroccan cabinet, following the CGNC plan.

You are told whether this is a purchase (achat) or a sale (vente), and what the
invoice bills, as printed on it. Classify THAT.

CGNC, class 6 (charges) -- for a purchase, the usual ones:

- 6111 achats de marchandises (revente en l'etat)
- 6121 matieres premieres / 6122 matieres et fournitures consommables
- 6125 achats non stockes (eau, electricite, fournitures de bureau)
- 6126 achats de travaux, etudes et prestations de service -- sous-traitance,
  main d'oeuvre facturee par un tiers, travaux de chantier
- 6131 locations (locaux, materiel) / 6133 entretien et reparations
- 6134 primes d'assurance / 6136 remunerations d'intermediaires et honoraires
- 6141 transports / 6142 deplacements, missions et receptions
- 6145 frais postaux et telecommunications / 6147 services bancaires
- 6167 impots et taxes / 617 charges de personnel

CGNC, class 7 (produits) -- for a sale, the usual ones:

- 7111 ventes de marchandises (revente en l'etat)
- 7121 ventes de biens produits / 7124 ventes de services produits -- prestations
  facturees a un client, travaux, honoraires factures
- 7127 ventes de produits accessoires (transport facture, emballages...)
- 7136 locations rendues / 7181 reprises de subventions
- 7381 interets et produits assimiles

Rules:
- Classify on the designation, never on the amount: 100 000 DH of main d'oeuvre
  and 100 000 DH of marchandise are two different accounts.
- Several accounts share a label ("Achats de marchandises" twice, different
  codes): take the lowest code, it is the main one.
- You cannot create an account. If the plan has nothing suitable, set nothing
  and say which account is missing.
"""
CLASS = {"purchase": "6", "sale": "7"}


def account_question(invoice, designation):
    """What the client reads when no account fits (tarik-master orchestrator.account_question)."""
    about = ", ".join(str(x) for x in ((invoice["partner_id"] or [0, None])[1], f"{invoice['amount_total']} DH TTC"
                                       if invoice["amount_total"] else None) if x)
    facture = f"Facture {invoice['invoice_number'] or invoice['id']}" + (f" ({about})" if about else "")
    why = (f"je n'ai pas pu choisir le compte pour « {designation} »" if designation
           else "la designation est illisible, je n'ai pas pu choisir le compte")
    return f"{facture} : {why}.\nQuel compte utiliser (code ou intitule) ?"


def apply_account(invoice, account_id, why):
    """Put the account on the invoice through Experio's form, then let Experio's own refresh
    carry it onto the HT lines (tarik-master wrote the lines itself)."""
    context = invoice_context(invoice)
    try:
        fill_invoice(invoice["id"], {"account_id": account_id}, context)
        if not dry_run():
            odoo_json("exp.invoice", "action_refresh", [invoice["id"]], context=context)
    except Exception as error:
        print(f"apply_account {invoice['id']} -> {account_id}: {error}", flush=True)
        raise ToolError(f"Facture {invoice['id']} : Experio a refuse le compte.") from None
    account = odoo_call("exp.account", "read", [account_id], fields=["code", "name"])[0]
    head = "MODE TEST : aucune modification enregistree dans Experio. " if dry_run() else ""
    return ok(f"{head}Facture {invoice['id']} : compte {account['code']} {account['name']} ({why}).")


INVOICE_FIELDS = ["state", "type", "company_id", "exercice_id", "partner_id", "account_id", "invoice_number",
                  "amount_total", "bluejay_state"]


def draft_invoice(invoice_id):
    invoice = require_invoice(invoice_id, INVOICE_FIELDS)
    if invoice["state"] != "draft":
        raise ToolError(f"Facture {invoice_id} : seule une facture brouillon peut changer de compte.")
    return invoice


@server.tool()
@with_ids
def assign_account(invoice_id: int) -> dict:
    """Give a draft invoice its account (Compte): the one Experio memorized for
    its tiers, else the one matching what the invoice bills. Call it after
    link_tiers and check_invoice. When none fits, the answer is the question to
    ask the client; give their answer to set_account."""
    invoice = draft_invoice(invoice_id)
    if invoice["account_id"]:
        return ok(f"Facture {invoice_id} : compte deja renseigne, {invoice['account_id'][1]}.")
    kind = side_of(invoice)
    company_id = invoice["company_id"][0]
    context = invoice_context(invoice)
    try:
        if not odoo_call("exp.account", "search_count", [("company_id", "=", company_id)]):
            if dry_run():
                return ok(f"MODE TEST : plan comptable vide, les comptes de base seraient crees (dossier de la facture {invoice_id}).")
            # Experio's "Creer les comptes manquants" button: its wizard lists the base CGNC accounts the dossier lacks.
            wizard_id = odoo_call("exp.account.missing.wizard", "create", {}, context={"allowed_company_ids": [company_id]})
            odoo_json("exp.account.missing.wizard", "action_confirm", [wizard_id], context={"allowed_company_ids": [company_id]})
        if invoice["partner_id"]:
            # Picking the tiers again in the form runs Experio's memorized chain (last used, tiers sheet, dossier default).
            memorized = fill_invoice(invoice_id, {"partner_id": invoice["partner_id"][0]}, context).get("account_id")
            if memorized:
                return apply_account(invoice, memorized, "compte memorise pour ce tiers")
    except ToolError:
        raise
    except Exception as error:
        print(f"assign_account {invoice_id}: {error}", flush=True)
        raise ToolError(f"Facture {invoice_id} : Experio n'a pas pu proposer de compte.") from None
    with cabinet_db() as db:
        row = db.execute("select extracted->>'designation' from tarik_checks where invoice_id = %s", (invoice_id,)).fetchone()
    if not row:
        raise ToolError(f"Facture {invoice_id} : pas encore verifiee, appelez d'abord check_invoice (il faut ce qui est facture).")
    designation = row[0]
    if not designation:
        return question(account_question(invoice, None))
    accounts = odoo_call("exp.account", "search_read", [("company_id", "=", company_id), ("code", "=like", f"{CLASS[kind]}%")],
                         fields=["id", "code", "name"], order="code")
    by_code = {a["code"]: a for a in accounts}
    answer = ask_model(ACCOUNT_PROMPT, [], (
        f"Type: {'vente' if kind == 'sale' else 'achat'}\nBilled: {designation}\n\nThis cabinet's accounts:\n"
        + "\n".join(f"{a['code']} {a['name']}" for a in accounts)
        + '\n\nAnswer with only a JSON object: {"code": "<a code from the list above>" or null, "reason": "one short French sentence"}'),
        "Choix du compte")
    code, reason = answer.get("code"), str(answer.get("reason") or "")
    if code not in by_code:
        if code:
            print(f"assign_account {invoice_id}: model chose {code!r}, not in the chart", flush=True)
        return question(account_question(invoice, designation))
    return apply_account(invoice, by_code[code]["id"], reason)


@server.tool()
def search_accounts(invoice_id: int, query: str) -> str:
    """Search the invoice's dossier chart by code prefix or label word ("6125",
    "carburant", "honoraires"), to show the client what exists."""
    invoice = require_invoice(invoice_id, ["company_id"])
    found = odoo_call("exp.account", "search_read", [("company_id", "=", invoice["company_id"][0]), "|",
                      ("code", "=like", f"{query.strip()}%"), ("name", "ilike", query.strip())],
                      fields=["id", "code", "name"], order="code", limit=15)
    if not found:
        return f"Aucun compte du dossier ne correspond a « {query} »."
    return "Comptes : " + "; ".join(f"{a['code']} {a['name']}" for a in found)


def resolve_account(accounts, answer):
    """The account the client means, from their own words: a code (or its start), or a label.
    One match, or several sharing one label (take the lowest code, account.py's rule) -> that
    account; otherwise None. ``accounts``: [{id, code, name}] sorted by code."""
    words = answer.strip()
    by_code = [a for a in accounts if a["code"].startswith(words.replace(" ", ""))] if words.replace(" ", "").isdigit() else []
    found = by_code or [a for a in accounts if words.lower() in a["name"].lower()]
    if len(found) == 1:
        return found[0]
    exact = [a for a in found if a["name"].strip().lower() == words.lower()]
    group = exact or found
    if group and len({a["name"].strip().lower() for a in group}) == 1:
        return group[0]
    return None


@server.tool()
@with_ids
def set_account(invoice_id: int, answer: str) -> dict:
    """Put the account the client asked for on a draft invoice. ``answer``: their
    own words, a code ("6111", "61110000") or a label ("achats de marchandises").
    When it is ambiguous, the answer lists the accounts to choose from."""
    if not (answer or "").strip():
        raise ToolError("answer manquant : le code ou l'intitule donne par le client.")
    invoice = draft_invoice(invoice_id)
    accounts = odoo_call("exp.account", "search_read", [("company_id", "=", invoice["company_id"][0])],
                         fields=["id", "code", "name"], order="code")
    account = resolve_account(accounts, answer)
    if account:
        return apply_account(invoice, account["id"], f"demande du client : {answer.strip()}")
    return question(f"Facture {invoice_id} : « {answer.strip()} » est ambigu. " + search_accounts(invoice_id, answer))
