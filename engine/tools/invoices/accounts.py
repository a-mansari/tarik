"""The invoice's account (Compte), class 6 for a purchase, class 7 for a sale.

Memorized account, else classify what was billed, else ask. Experio already holds the
memorized chain (last account used for the tiers, then the tiers sheet, then the dossier
default) and applies it when the tiers is picked in the form, and its own refresh rebuilds
the lines from the header. So the model only does what Experio can't: classify what was
billed among the dossier's real accounts.
"""

import ast
import re
from difflib import SequenceMatcher

from engine.tools.core.app import ToolError, log, ok, question, step_tool, tool, with_ids
from engine.tools.core.config import TEST_MODE
from engine.tools.core.conversation import client_messages, instructed, without_subject
from engine.tools.core.experio import (
    dry_run,
    fill_invoice,
    in_dossier,
    invoice_context,
    odoo_call,
    odoo_json,
    refresh,
    refused,
    require_dossier,
    require_draft_invoice,
    require_invoice,
    test_mode_note,
)
from engine.tools.core.model import ask_model, tool_prompt
from engine.tools.core.words import normalized
from engine.tools.invoices.check import stored_check
from engine.tools.invoices.classify import side_of
from engine.tools.invoices.journal import JOURNAL_FIELDS, check_journal, journal_stop
from engine.tools.invoices.listing import SIDES

CLASS = {"purchase": "6", "sale": "7"}
# The classes Experio posts without a warning (validate_warning_account_id): 2 and 6 for a purchase, 7 for a sale.
WARNING_FREE = {"purchase": ("2", "6"), "sale": ("7",)}


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
    """What the client reads when no account fits ``designation`` (str or None: illegible)."""
    total = f"{invoice['amount_total']} DH TTC" if invoice["amount_total"] else None
    partner = invoice["partner_id"][1] if invoice["partner_id"] else None
    about = ", ".join(str(x) for x in (partner, total) if x)
    facture = f"Facture {invoice['invoice_number'] or invoice['id']}" + (f" ({about})" if about else "")
    why = (
        f"je n'ai pas pu choisir le compte pour « {designation} »"
        if designation
        else "la designation est illisible, je n'ai pas pu choisir le compte"
    )
    return f"{facture} : {why}.\nQuel compte utiliser (code ou intitulé) ?"


def chart(company_id, *domain):
    """The dossier's accounts matching ``domain`` (all by default), as [{id, code, name}] sorted by code."""
    domain = [("company_id", "=", company_id), *domain]
    return odoo_call("exp.account", "search_read", domain, fields=["id", "code", "name"], order="code")


def usable(invoice_id, accounts):
    """Of ``accounts`` ([{id, code, name}]), those the invoice's Account field offers (Experio's account_id_domain:
    the dossier's, not reconcilable, no tiers account) in the classes Experio posts without a warning."""
    invoice = odoo_call("exp.invoice", "read", [invoice_id], fields=["account_id_domain", "type"])[0]
    domain = ast.literal_eval(invoice["account_id_domain"] or "[]")
    allowed = set(odoo_call("exp.account", "search", domain)) if domain else {a["id"] for a in accounts}
    classes = WARNING_FREE.get(invoice["type"], ())
    return [a for a in accounts if a["id"] in allowed and a["code"].startswith(classes)]


def listing(accounts):
    """``accounts`` as the model reads them: one "code name" per line."""
    return "\n".join(f"{a['code']} {a['name']}" for a in accounts)


def apply_account(invoice, account_id, why):
    """Put the account ``account_id`` on the invoice through Experio's form, then let Experio's own refresh
    carry it onto the HT lines. The ok result says ``why``."""
    invoice_id, context = invoice["id"], invoice_context(invoice)
    with refused(f"apply_account {invoice_id} -> {account_id}", f"Facture {invoice_id} : Experio a refusé le compte."):
        fill_invoice(invoice_id, {"account_id": account_id}, context)
        refresh(invoice_id, context)
    return ok(f"{test_mode_note()}Facture {invoice_id} : compte {account_label(account_id)} ({why}).")


def account_code_of(account_id):
    return odoo_call("exp.account", "read", [account_id], fields=["code"])[0]["code"]


def account_label(account_id):
    """The account's code and whole name (Experio's own label for it is cut short: « …des biens mobili »)."""
    account = odoo_call("exp.account", "read", [account_id], fields=["code", "name"])[0]
    return f"{account['code']} {account['name']}"


def _fill_empty_chart(invoice_id, company_id):
    """Create the dossier's base accounts when its chart is empty. In test mode, the ok result saying so
    (nothing created), else None."""
    if odoo_call("exp.account", "search_count", [("company_id", "=", company_id)]):
        return None
    if dry_run():
        return ok(
            f"{TEST_MODE}Plan comptable vide, les comptes de base seraient créés (dossier de la facture {invoice_id})."
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
    answer = ask_model(tool_prompt("choose-account"), [], instruction, "Choix du compte")
    code = str(answer["code"]) if answer.get("code") is not None else None
    if code not in by_code:
        if code:
            log(f"assign_account {invoice['id']}: model chose {code!r}, not in the chart")
        return None, None
    return by_code[code], str(answer.get("reason") or "")


@step_tool
def assign_account(
    invoice_id: int, answer: str = "", client_message: str = "", session_id: str = "", confirm: bool = False
) -> dict:
    """Give a draft invoice its account (Compte): the one the client named with
    the invoice (``answer``, taken only when the client wrote it, in ``client_message`` or an earlier message of
    the conversation ``session_id``: set_account), else the one Experio memorized for its tiers, else the one
    matching what the invoice bills. Call it after link_tiers and check_invoice. When none fits, the
    answer is the question to ask the client; their answer goes to set_account
    (answer_pending). An account asked for that the client's words don't hold is asked about, never skipped: the
    invoice must not be posted on another one."""
    said_before = client_messages(session_id, client_message) if session_id else [client_message]
    told = instructed(answer, "\n".join(said_before))
    if told:
        return set_account(invoice_id, told, confirm)
    if (answer or "").strip():
        return question(
            f"Facture {invoice_id} : quel compte voulez-vous exactement (code ou intitulé) ? "
            f"Je n'ai pas retrouvé « {answer.strip()} » dans vos messages."
        )
    invoice = require_draft_invoice(invoice_id, INVOICE_FIELDS, "changer de compte")
    if invoice["account_id"]:
        return ok(f"Facture {invoice_id} : compte déjà renseigné, {account_label(invoice['account_id'][0])}.")
    # Without the dossier's own journal Experio cannot propose any account, and says so obscurely: an invoice made
    # before the dossier had journals still sits on another dossier's, which check_journal moves to its own.
    refusal = journal_stop(invoice_id, require_invoice(invoice_id, JOURNAL_FIELDS))
    if refusal:
        return refusal
    check_journal(invoice_id)
    kind = side_of(invoice)
    with refused(f"assign_account {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu proposer de compte."):
        simulated = _fill_empty_chart(invoice_id, invoice["company_id"][0])
        if simulated:
            return simulated
        memorized = _memorized(invoice, invoice_context(invoice))
        # One Experio would warn about (outside the classes of the side) is not taken: what is billed decides.
        if memorized and usable(invoice_id, [{"id": memorized, "code": account_code_of(memorized)}]):
            return apply_account(invoice, memorized, "compte mémorisé pour ce tiers")
    check = stored_check(invoice_id)
    if not check:
        raise ToolError(
            f"Facture {invoice_id} : pas encore vérifiée, appelez d'abord check_invoice (il faut ce qui est facture)."
        )
    designation = check[1].get("designation")
    if not designation:
        return question(account_question(invoice, None))
    account, reason = _model_choice(invoice, kind, designation)
    if not account:
        return question(account_question(invoice, designation))
    return apply_account(invoice, account["id"], reason)


def searched(accounts, search):
    """The ``accounts`` ``search`` names: a code's start, or words each close to one of the label's (misspelled,
    accents or plural aside: "hotelrie" finds "Hôtellerie"). Words under 3 letters ("de", "ou") count for nothing."""
    code = search.replace(" ", "")
    if code.isdigit():
        return [a for a in accounts if a["code"].startswith(code)]
    wanted = [word for word in normalized(search).split() if len(word) >= 3]

    def close(word, label):
        # the same first letter: "deplacement" is not "placement", though they look 90 % alike
        return any(
            found.startswith(word) or (found[0] == word[0] and SequenceMatcher(None, word, found).ratio() >= 0.8)
            for found in label
        )

    return [a for a in accounts if wanted and all(close(w, normalized(a["name"]).split()) for w in wanted)]


@tool
def list_accounts(dossier_id: int, search: str = "", side: str = "") -> str:
    """The dossier's accounts (plan comptable) whose code starts with ``search``, or whose label holds its words,
    even misspelled ("hotelrie", "transports"): one "code label" per line. One word or code per call: "transport ou
    hotellerie" is two calls. ``side``: "achat" for a purchase's accounts only (class 6), "vente" for a sale's (class
    7), empty for all classes; any other value is refused. Empty ``search``: all of them, at most 100."""
    dossier = require_dossier(dossier_id)
    side = (side or "").strip()
    if side and side not in SIDES:
        raise ToolError(f"Type « {side} » inconnu : achat, vente, ou vide pour tous les comptes.")
    in_class = [("code", "=like", CLASS[SIDES[side][0]] + "%")] if side else []
    with refused(f"list_accounts {dossier_id}", "Experio n'a pas pu donner le plan comptable."):
        accounts = chart(dossier_id, *in_class)
    search = (search or "").strip()
    found = searched(accounts, search) if search else accounts
    if not found:
        return f"Aucun compte du dossier {dossier} ne correspond a « {search} »."
    more = " (les 100 premiers : precisez la recherche)" if len(found) > 100 else ""
    return f"{len(found)} compte(s) du dossier {dossier}{more} :\n" + listing(found[:100])


def resolve_account(accounts, answer):
    """The account the client means, from their own words: a code (or its start), or a label.
    One match, or several sharing one label (take the lowest code, choose-account.md's rule) -> that
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


def account_meant(accounts, answer):
    """The account the client's free-text answer names ("Pour MEDI TELECOM, utilisez le compte 6145"), read by
    the model and checked against the chart; None when it names none clearly."""
    instruction = (
        f"Client's answer: {answer}\n\nThe dossier's accounts:\n"
        + listing(accounts)
        + '\n\nAnswer with only a JSON object: {"code": "<a code from the list above>" or null}'
    )
    code = ask_model(tool_prompt("read-account-answer"), [], instruction, "Lecture de la réponse").get("code")
    if code is None or not str(code).strip().isdigit():
        return None
    code = str(code).strip()
    # The digits the client wrote, when the model's code starts with them: "6143" is 61431000 or 61436000, the model
    # picks one, the client chose neither.
    written = next((digits for digits in re.findall(r"\d{3,}", answer) if code.startswith(digits)), code)
    return resolve_account(accounts, written)


@with_ids
def set_account(invoice_id, answer, confirm=False):
    """Put the account the client asked for on a draft invoice (their answer to assign_account's question): a code
    ("6111", "61110000") or a label ("achats de marchandises") on its own, else read from their sentence. When it
    names none clearly, the question again. ``confirm``: an account they gave for this invoice while answering about
    another: it is asked to be confirmed (oui / non), never applied on their sentence alone."""
    if not (answer or "").strip():
        raise ToolError("answer manquant : le code ou l'intitulé donne par le client.")
    invoice = require_draft_invoice(invoice_id, INVOICE_FIELDS, "changer de compte")
    accounts = usable(invoice_id, chart(invoice["company_id"][0]))
    account = resolve_account(accounts, answer) or account_meant(accounts, answer)
    if account and confirm:
        label = f"{account['code']} {account['name']}"
        return question(
            f"Facture {invoice['invoice_number'] or invoice_id} : je la passe en compte {label}, comme vous l'avez "
            "demandé ? oui / non",
            choices={account["id"]: label},
        )
    if account:
        return apply_account(invoice, account["id"], f"demande du client : {answer.strip()}")
    return question(
        f"Facture {invoice_id} : « {without_subject(answer)} » ne désigne pas un seul compte du dossier. "
        "Quel compte utiliser (code ou intitulé) ?"
    )


@with_ids
def confirm_account(invoice_id, account_id, confirmed):
    """The client's oui / non (``confirmed``) to the account set_account asked them to confirm (``account_id``)."""
    if not confirmed:
        return question(f"Facture {invoice_id} : quel compte utiliser alors (code ou intitulé) ?")
    invoice = require_draft_invoice(invoice_id, INVOICE_FIELDS, "changer de compte")
    return apply_account(invoice, account_id, "confirmé par le client")
