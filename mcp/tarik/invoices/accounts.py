"""Phase 8: the invoice's account (Compte), class 6 for a purchase, class 7 for a sale.

tarik-master runs an agent: memorized account, else classify what was billed, else ask.
Experio already holds the memorized chain (last account used for the tiers, then the
tiers sheet, then the dossier default) and applies it when the tiers is picked in the
form, and its own refresh rebuilds the lines from the header. So the model only does
what Experio can't: classify what was billed among the dossier's real accounts.
"""

from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import log, ok, question, server, with_ids
from tarik.core.cabinets import cabinet_db
from tarik.core.config import TEST_MODE
from tarik.core.experio import (
    dry_run,
    fill_invoice,
    in_dossier,
    invoice_context,
    odoo_call,
    odoo_json,
    refresh,
    refused,
    require_draft,
    require_invoice,
    test_mode_note,
)
from tarik.core.model import ask_model
from tarik.invoices.classify import side_of

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


INVOICE_FIELDS = [
    "state",
    "type",
    "company_id",
    "exercice_id",
    "partner_id",
    "account_id",
    "invoice_number",
    "amount_total",
]


def account_question(invoice, designation):
    """What the client reads when no account fits ``designation`` (str or None: illegible)
    (tarik-master orchestrator.account_question)."""
    total = f"{invoice['amount_total']} DH TTC" if invoice["amount_total"] else None
    about = ", ".join(str(x) for x in ((invoice["partner_id"] or [0, None])[1], total) if x)
    facture = f"Facture {invoice['invoice_number'] or invoice['id']}" + (f" ({about})" if about else "")
    why = (
        f"je n'ai pas pu choisir le compte pour « {designation} »"
        if designation
        else "la designation est illisible, je n'ai pas pu choisir le compte"
    )
    return f"{facture} : {why}.\nQuel compte utiliser (code ou intitule) ?"


def chart(company_id, *domain):
    """The dossier's accounts matching ``domain`` (all by default), as [{id, code, name}] sorted by code."""
    domain = [("company_id", "=", company_id), *domain]
    return odoo_call("exp.account", "search_read", domain, fields=["id", "code", "name"], order="code")


def listing(accounts):
    """``accounts`` as the model reads them: one "code name" per line."""
    return "\n".join(f"{a['code']} {a['name']}" for a in accounts)


def draft_invoice(invoice_id):
    """The invoice's INVOICE_FIELDS (dict); ToolError unless it is a draft."""
    invoice = require_invoice(invoice_id, INVOICE_FIELDS)
    require_draft(invoice_id, invoice, "changer de compte")
    return invoice


def apply_account(invoice, account_id, why):
    """Put the account ``account_id`` on the invoice through Experio's form, then let Experio's own refresh
    carry it onto the HT lines (tarik-master wrote the lines itself). The ok result says ``why``."""
    invoice_id, context = invoice["id"], invoice_context(invoice)
    with refused(f"apply_account {invoice_id} -> {account_id}", f"Facture {invoice_id} : Experio a refuse le compte."):
        fill_invoice(invoice_id, {"account_id": account_id}, context)
        refresh(invoice_id, context)
    account = odoo_call("exp.account", "read", [account_id], fields=["code", "name"])[0]
    return ok(f"{test_mode_note()}Facture {invoice_id} : compte {account['code']} {account['name']} ({why}).")


def _fill_empty_chart(invoice_id, company_id):
    """Create the dossier's base accounts when its chart is empty. In test mode, the ok result saying so
    (nothing created), else None."""
    if odoo_call("exp.account", "search_count", [("company_id", "=", company_id)]):
        return None
    if dry_run():
        return ok(
            f"{TEST_MODE}Plan comptable vide, les comptes de base seraient crees (dossier de la facture {invoice_id})."
        )
    # Experio's "Creer les comptes manquants" button: its wizard lists the base CGNC accounts the dossier lacks.
    wizard_id = odoo_call("exp.account.missing.wizard", "create", {}, context=in_dossier(company_id))
    odoo_json("exp.account.missing.wizard", "action_confirm", [wizard_id], context=in_dossier(company_id))
    return None


def _memorized(invoice, context):
    """The account id Experio memorized for the invoice's tiers, else None."""
    if not invoice["partner_id"]:
        return None
    # Picking the tiers again in the form runs Experio's memorized chain (last used, tiers sheet, dossier default):
    # only read, so nothing else it would refill (payment method, currency set from the scan) is saved.
    return fill_invoice(invoice["id"], {"partner_id": invoice["partner_id"][0]}, context, save=False).get("account_id")


def _designation(invoice_id):
    """What the invoice bills, as check_invoice read it (str or None). ToolError when not checked yet."""
    with cabinet_db() as db:
        row = db.execute(
            "select extracted->>'designation' from tarik_checks where invoice_id = %s", (invoice_id,)
        ).fetchone()
    if not row:
        raise ToolError(
            f"Facture {invoice_id} : pas encore verifiee, appelez d'abord check_invoice (il faut ce qui est facture)."
        )
    return row[0]


def _model_choice(invoice, kind, designation):
    """(account {id, code, name}, reason) the model picks for ``designation`` among the dossier's class 6 or 7
    accounts, else (None, None)."""
    accounts = chart(invoice["company_id"][0], ("code", "=like", f"{CLASS[kind]}%"))
    by_code = {a["code"]: a for a in accounts}
    instruction = (
        f"Type: {'vente' if kind == 'sale' else 'achat'}\nBilled: {designation}\n\nThis cabinet's accounts:\n"
        + listing(accounts)
        + '\n\nAnswer with only a JSON object: {"code": "<a code from the list above>" or null, '
        '"reason": "one short French sentence"}'
    )
    answer = ask_model(ACCOUNT_PROMPT, [], instruction, "Choix du compte")
    code = str(answer["code"]) if answer.get("code") is not None else None
    if code not in by_code:
        if code:
            log(f"assign_account {invoice['id']}: model chose {code!r}, not in the chart")
        return None, None
    return by_code[code], str(answer.get("reason") or "")


@server.tool()
@with_ids
def assign_account(invoice_id: int) -> dict:
    """Give a draft invoice its account (Compte): the one Experio memorized for
    its tiers, else the one matching what the invoice bills. Call it after
    link_tiers and check_invoice. When none fits, the answer is the question to
    ask the client; their answer goes to set_account (answer_pending)."""
    invoice = draft_invoice(invoice_id)
    if invoice["account_id"]:
        return ok(f"Facture {invoice_id} : compte deja renseigne, {invoice['account_id'][1]}.")
    kind = side_of(invoice)
    with refused(f"assign_account {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu proposer de compte."):
        simulated = _fill_empty_chart(invoice_id, invoice["company_id"][0])
        if simulated:
            return simulated
        memorized = _memorized(invoice, invoice_context(invoice))
        if memorized:
            return apply_account(invoice, memorized, "compte memorise pour ce tiers")
    designation = _designation(invoice_id)
    if not designation:
        return question(account_question(invoice, None))
    account, reason = _model_choice(invoice, kind, designation)
    if not account:
        return question(account_question(invoice, designation))
    return apply_account(invoice, account["id"], reason)


def resolve_account(accounts, answer):
    """The account the client means, from their own words: a code (or its start), or a label.
    One match, or several sharing one label (take the lowest code, account.py's rule) -> that
    account; otherwise None. A short code is the account it names, padded with zeros (CGNC):
    "6145" is 61450000, not 61451000. ``accounts``: [{id, code, name}] sorted by code."""
    words = answer.strip()
    code = words.replace(" ", "")
    by_code = [a for a in accounts if a["code"].startswith(code)] if code.isdigit() else []
    padded = [a for a in by_code if a["code"].rstrip("0") == code.rstrip("0")]
    if len(padded) == 1:
        return padded[0]
    found = by_code or [a for a in accounts if words.lower() in a["name"].lower()]
    if len(found) == 1:
        return found[0]
    exact = [a for a in found if a["name"].strip().lower() == words.lower()]
    group = exact or found
    if group and len({a["name"].strip().lower() for a in group}) == 1:
        return group[0]
    return None


ANSWER_PROMPT = """You read a client's answer to the question "which account (Compte) should this invoice use?",
for a Moroccan cabinet's dossier, and say which account of the dossier's chart below they mean: by its code (a
code's start, such as 6145, means the account whose code starts with it) or by its label. Report only an account
the answer itself names; when it names none, or could mean several with different labels, answer null. Never
choose one yourself from what the invoice bills."""


def account_meant(accounts, answer):
    """The account the client's free-text answer names ("Pour MEDI TELECOM, utilisez le compte 6145"), read by
    the model and checked against the chart; None when it names none clearly."""
    instruction = (
        f"Client's answer: {answer}\n\nThe dossier's accounts:\n"
        + listing(accounts)
        + '\n\nAnswer with only a JSON object: {"code": "<a code from the list above>" or null}'
    )
    code = ask_model(ANSWER_PROMPT, [], instruction, "Lecture de la reponse").get("code")
    # Its code, or the start the client wrote ("6145"), as a number or text: only one of the chart's accounts.
    return resolve_account(accounts, str(code)) if code is not None and str(code).strip().isdigit() else None


@with_ids
def set_account(invoice_id, answer):
    """Put the account the client asked for on a draft invoice (their answer to assign_account's question): a code
    ("6111", "61110000") or a label ("achats de marchandises") on its own, else read from their sentence. When it
    names none clearly, the question again."""
    if not (answer or "").strip():
        raise ToolError("answer manquant : le code ou l'intitule donne par le client.")
    invoice = draft_invoice(invoice_id)
    accounts = chart(invoice["company_id"][0])
    account = resolve_account(accounts, answer) or account_meant(accounts, answer)
    if account:
        return apply_account(invoice, account["id"], f"demande du client : {answer.strip()}")
    return question(
        f"Facture {invoice_id} : « {answer.strip()} » ne designe pas un seul compte du dossier. "
        "Quel compte utiliser (code ou intitule) ?"
    )
