"""The invoice's account (Compte): class 6 (or 2, a fixed asset) for a purchase, class 7 for a sale.

Order: the account the client named, else the one Experio memorized for the tiers, else one the model picks from
what the invoice bills, else a question to the client. Experio already holds the memorized chain (last account used
for the tiers, then the tiers sheet, then the dossier default: exp.invoice get_memorized_values_from_partner) and
applies it when the tiers is picked in the form; its own refresh then rebuilds the lines from the header. So the
model only does what Experio can't: match what was billed to one of the dossier's real accounts.
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
from engine.tools.core.words import counted, normalized
from engine.tools.invoices.check import stored_check
from engine.tools.invoices.classify import side_of
from engine.tools.invoices.journal import JOURNAL_FIELDS, check_journal, journal_stop
from engine.tools.invoices.listing import SIDES

CLASS = {"purchase": "6", "sale": "7"}
# What the model classifies a purchase among: charges, and fixed assets (class 2, Experio then makes the asset at
# posting: immobilisations' generate_asset_assets). No Experio rule tells one from the other: a class 2 it picks is
# put on the draft and said (« À vérifier »), the client says if it is a charge; nothing is posted before they ask.
MODEL_CLASSES = {"purchase": ("6", "2"), "sale": ("7",)}
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


def invoice_named(invoice):
    """Return how an account question names ``invoice``: « Facture <number> (`<tiers>`, <TTC> DH TTC) »."""
    total = f"{invoice['amount_total']} DH TTC" if invoice["amount_total"] else None
    partner = f"`{invoice['partner_id'][1]}`" if invoice["partner_id"] else None
    about = ", ".join(str(x) for x in (partner, total) if x)
    return f"Facture {invoice['invoice_number'] or invoice['id']}" + (f" ({about})" if about else "")


def account_question(invoice, designation):
    """Return the question the client reads when no account fits ``designation`` (str; None when illegible)."""
    facture = invoice_named(invoice)
    why = (
        f"je n'ai pas pu choisir le compte pour « {designation} »"
        if designation
        else "la désignation est illisible, je n'ai pas pu choisir le compte"
    )
    return f"{facture} : {why}.\nQuel compte utiliser (code ou intitulé) ?"


def chart(company_id, *domain):
    """Return the dossier's accounts as [{id, code, name}], sorted by code.

    ``domain``: extra Odoo domain terms (tuples, or "|" operators) added after the dossier filter, one argument
    each; none returns the whole chart."""
    domain = [("company_id", "=", company_id), *domain]
    return odoo_call("exp.account", "search_read", domain, fields=["id", "code", "name"], order="code")


def usable(invoice_id, accounts):
    """Return those of ``accounts`` ([{id, code, name}]) the invoice's Compte field offers, in the classes Experio
    posts without a warning (WARNING_FREE).

    The field's offer is Experio's account_id_domain: the dossier's accounts, not reconcilable, no tiers account
    (receivable or payable), narrowed to classes 2/6 or 7/4 when the dossier limits it."""
    invoice = odoo_call("exp.invoice", "read", [invoice_id], fields=["account_id_domain", "type"])[0]
    domain = ast.literal_eval(invoice["account_id_domain"] or "[]")
    allowed = set(odoo_call("exp.account", "search", domain)) if domain else {a["id"] for a in accounts}
    classes = WARNING_FREE.get(invoice["type"], ())
    return [a for a in accounts if a["id"] in allowed and a["code"].startswith(classes)]


def listing(accounts):
    """Return ``accounts`` as the model reads them: one "code name" per line."""
    return "\n".join(f"{a['code']} {a['name']}" for a in accounts)


def apply_account(invoice, account_id, why=""):
    """Put account ``account_id`` on ``invoice`` through Experio's form, then run Experio's refresh, which carries
    it onto the HT lines. Returns an ok result naming the account and ``why`` it was chosen (« appliqué » alone when
    the client gave it: nothing to explain)."""
    invoice_id, context = invoice["id"], invoice_context(invoice)
    with refused(f"apply_account {invoice_id} -> {account_id}", f"Facture {invoice_id} : Experio a refusé le compte."):
        fill_invoice(invoice_id, {"account_id": account_id}, context)
        refresh(invoice_id, context)
    said = f"({why})" if why else "appliqué"
    return ok(f"{test_mode_note()}Facture {invoice_id} : compte `{account_label(account_id)}` {said}.")


def account_code_of(account_id):
    return odoo_call("exp.account", "read", [account_id], fields=["code"])[0]["code"]


def account_label(account_id):
    """Return the account's "code name", the name in full (Experio's own label cuts it short: « …des biens mobili »)."""
    account = odoo_call("exp.account", "read", [account_id], fields=["code", "name"])[0]
    return f"{account['code']} {account['name']}"


def _fill_empty_chart(invoice_id, company_id):
    """Create the dossier's base CGNC accounts when its chart is empty, with Experio's exp.account.missing.wizard.

    Returns None, except in test mode with an empty chart: then nothing is created and an ok result says they
    would be."""
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
    """Return the account id Experio's memorized chain gives the invoice's tiers, else None (no tiers, or no
    account in the chain). The form is read, not saved."""
    if not invoice["partner_id"]:
        return None
    # Picking the tiers again in the form runs Experio's memorized chain (last used, tiers sheet, dossier default):
    # only read, so nothing else it would refill (payment method, currency set from the scan) is saved.
    return fill_invoice(invoice["id"], {"partner_id": invoice["partner_id"][0]}, context, save=False).get("account_id")


def model_choice(invoice, kind, designation):
    """Ask the model which of the dossier's accounts fits ``designation`` (what the invoice bills).

    ``kind``: "purchase" (classes 6 and 2 offered) or "sale" (class 7). Returns {account, reason, close, new}:
    ``account`` the one it chose ({id, code, name}), None when it names no code of the chart; ``close`` the accounts
    of the chart it names as nearest when unsure (3 at most); ``new`` {parent, name} when none is near: the account of
    the chart a new one belongs under, and its short label."""
    classes = [("code", "=like", f"{prefix}%") for prefix in MODEL_CLASSES[kind]]
    accounts = chart(invoice["company_id"][0], *(["|"] * (len(classes) - 1)), *classes)
    by_code = {a["code"]: a for a in accounts}
    instruction = (
        f"Type: {'vente' if kind == 'sale' else 'achat'}\nBilled: {designation}\n\nThis cabinet's accounts:\n"
        + listing(accounts)
        + '\n\nAnswer with only a JSON object: {"code": "<a code from the list above>" or null, '
        '"close": ["<up to 3 codes from the list, nearest first>"], '
        '"new": {"parent": "<a code from the list>", "name": "<short French label>"} or null, '
        '"reason": "one short French sentence"}'
    )
    answer = ask_model(tool_prompt("choose-account"), [], instruction, "Choix du compte")
    code = str(answer["code"]) if answer.get("code") is not None else None
    if code and code not in by_code:
        log(f"assign_account {invoice['id']}: model chose {code!r}, not in the chart")
    close = [by_code[str(c)] for c in (answer.get("close") or [])[:3] if str(c) in by_code and str(c) != code]
    new = answer.get("new") if isinstance(answer.get("new"), dict) else None
    parent = by_code.get(str((new or {}).get("parent")))
    name = " ".join(str((new or {}).get("name") or "").split())[:60]
    return {
        "account": by_code.get(code),
        "reason": str(answer.get("reason") or ""),
        "close": close,
        "new": {"parent": parent, "name": name} if parent and name else None,
    }


def chart_length(company_id, code):
    """Return the length of the dossier's account codes (accounts_chart_length), else that of ``code``."""
    found = odoo_call("res.company", "read", [company_id], fields=["accounts_chart_length"])[0]
    return found["accounts_chart_length"] or len(code)


def free_code(parent, accounts, length):
    """Return the first code free under account ``parent`` ({code}) in ``accounts`` (the chart): its code without the
    trailing zeros and one digit more, at the dossier's ``length`` (71240000 -> 71241000, 71242000...). None when all
    nine are taken, or when the parent is too broad (under 4 digits: 6140 + 1 would be 6141, CGNC's Transports)."""
    stem = parent["code"].rstrip("0")
    if len(stem) < 4:
        return None
    taken = {a["code"] for a in accounts}
    codes = (f"{stem}{n}".ljust(length, "0") for n in range(1, 10))
    return next((code for code in codes if code not in taken), None)


def proposals(invoice, designation, chosen):
    """Return the question result when the model chose no account: the near accounts the invoice can take to choose
    from (one: oui / non), else a new account under the right parent to create on the client's oui (choices
    {"new <parent code>": "<code> <name>"}: create_account), else the plain question."""
    facture = invoice_named(invoice)
    near = usable(invoice["id"], chosen["close"]) if chosen["close"] else []
    if len(near) == 1:
        label = f"{near[0]['code']} {near[0]['name']}"
        return question(f"{facture} : je la passe en compte `{label}` ? oui / non", choices={near[0]["id"]: label})
    if near:
        listed = ", ".join(f"`{a['code']} {a['name']}`" for a in near)
        return question(
            f"{facture} : quel compte pour « {designation} » ? Les plus proches : {listed}.",
            choices={a["id"]: f"{a['code']} {a['name']}" for a in near},
        )
    new, company = chosen["new"], invoice["company_id"][0]
    code = new and free_code(new["parent"], chart(company), chart_length(company, new["parent"]["code"]))
    if code:
        label = f"{code} {new['name']}"
        return question(
            f"{facture} : aucun compte du dossier ne convient à « {designation} ». Je crée le compte `{label}` "
            f"(sous `{new['parent']['code']} {new['parent']['name']}`) ? oui / non",
            choices={f"new {new['parent']['code']}": label},
        )
    return question(account_question(invoice, designation))


def create_account(invoice_id, parent_code, proposed, confirmed):
    """Apply the client's oui / non to the new account proposals asked to create (``proposed``: "<code> <name>", under
    account ``parent_code``): oui creates it in the dossier as Experio's form does (exp.account create: code, name,
    dossier; Experio sets its type and group from the code) and puts it on the invoice; non asks which account to use
    instead. The chart is read again first: an account of that name under the parent, made since (another invoice's
    oui), is taken as it is, and a code taken since gives the next free one."""
    if not confirmed:
        return question(f"Facture {invoice_id} : quel compte utiliser alors (code ou intitulé) ?")
    invoice = require_draft_invoice(invoice_id, INVOICE_FIELDS, "changer de compte")
    company = invoice["company_id"][0]
    name = proposed.partition(" ")[2]
    accounts = chart(company)
    stem = parent_code.rstrip("0")
    made = next((a for a in accounts if a["code"].startswith(stem) and a["name"] == name), None)
    parent = next((a for a in accounts if a["code"] == parent_code), {"code": parent_code})
    code = made["code"] if made else free_code(parent, accounts, chart_length(company, parent_code))
    if not code:
        return question(f"Facture {invoice_id} : plus de code libre sous `{parent_code}`. Quel compte utiliser ?")
    why = "compte créé à votre demande"
    if code.startswith("2"):  # an immobilisation, as assign_account says it
        why += " ; immobilisé et amorti, pas en charge : dites-moi si c'est une charge"
    if dry_run():
        return ok(f"{TEST_MODE}Facture {invoice_id} : le compte `{code} {name}` serait créé et appliqué.")
    if made:
        return apply_account(invoice, made["id"], why)
    with refused(f"create_account {code}", f"Facture {invoice_id} : Experio a refusé de créer le compte `{code}`."):
        values = {"code": code, "name": name, "company_id": company}
        account_id = odoo_call("exp.account", "create", values, context=invoice_context(invoice))
    return apply_account(invoice, account_id, why)


POSITIONS = {"premier": 0, "1er": 0, "deuxieme": 1, "second": 1, "2e": 1, "troisieme": 2, "3e": 2}


def among(choices, answer):
    """Return the account of ``choices`` ({id: "<code> <name>"}, those a question listed) the client's ``answer``
    picks: by its place (« le deuxième »), its code or its name; None when it names none of them alone."""
    listed = [
        {"id": int(i), "code": label.split(" ", 1)[0], "name": label.partition(" ")[2]} for i, label in choices.items()
    ]
    words = normalized(answer).split()
    places = {POSITIONS[w] for w in words if w in POSITIONS} | ({len(listed) - 1} if "dernier" in words else set())
    if len(places) == 1 and min(places) < len(listed):
        return listed[min(places)]
    return resolve_account(listed, answer) or next(
        (a for a in listed if normalized(a["name"]) in normalized(answer)), None
    )


@step_tool
def assign_account(
    invoice_id: int, answer: str = "", client_message: str = "", session_id: str = "", confirm: bool = False
) -> dict:
    """Give a draft invoice its account (Compte). Call it after link_tiers and check_invoice.

    Order: the account the client named (``answer``), else the one Experio memorized for the tiers, else the one
    matching what the invoice bills. When none fits, returns a question result; the client's reply goes to
    set_account (answer_pending).

    Args:
        answer: the account the client asked for (code or label). Used only when the client's own words hold it,
            in ``client_message`` or an earlier message of conversation ``session_id``; otherwise the client is
            asked which account they mean, never skipped: the invoice must not be posted on another account.
            Words naming no account of the dossier at all (« gasoil ») are read as a description of the invoice:
            the account is chosen from them and what it bills, and the reply says so.
        confirm: the account was given while answering about another invoice: ask the client to confirm it."""
    said_before = client_messages(session_id, client_message) if session_id else [client_message]
    told = instructed(answer, "\n".join(said_before))
    hint = ""
    if told:
        named = set_account(invoice_id, told, confirm)
        if confirm or named["status"] != "question":
            return named
        company = require_invoice(invoice_id, ["company_id"])["company_id"][0]
        if searched(chart(company), without_subject(told)):
            return named  # words matching some accounts: which one is theirs to say
        hint = without_subject(told)
    elif (answer or "").strip():
        return question(
            f"Facture {invoice_id} : quel compte voulez-vous exactement (code ou intitulé) ? "
            f"Je n'ai pas retrouvé « {answer.strip()} » dans vos messages."
        )
    invoice = require_draft_invoice(invoice_id, INVOICE_FIELDS, "changer de compte")
    if invoice["account_id"]:
        return ok(f"Facture {invoice_id} : compte `{account_label(invoice['account_id'][0])}`.")
    # Without the dossier's own journal Experio cannot propose any account, and says so obscurely: an invoice made
    # before the dossier had journals still sits on another dossier's, which check_journal moves to its own.
    refusal = journal_stop(invoice_id, require_invoice(invoice_id, JOURNAL_FIELDS))
    if refusal:
        return refusal
    check_journal(invoice_id, tell=False)  # the journal step tells what Tarik set
    kind = side_of(invoice)
    with refused(f"assign_account {invoice_id}", f"Facture {invoice_id} : Experio n'a pas pu proposer de compte."):
        simulated = _fill_empty_chart(invoice_id, invoice["company_id"][0])
        if simulated:
            return simulated
        memorized = None if hint else _memorized(invoice, invoice_context(invoice))
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
    if hint:  # the client's own word for what it is, beside what the invoice says
        designation = f"{designation} (le client précise : « {hint} »)"
    chosen = model_choice(invoice, kind, designation)
    account, reason = chosen["account"], chosen["reason"]
    if account and hint:
        reason = f"« {hint} » ne désigne aucun compte du dossier, choisi d'après vos mots et la facture : {reason}"
    if not account:
        return proposals(invoice, designation, chosen)
    if account["code"].startswith("2"):  # an immobilisation (amortized, not a charge): put on it, said, easy to refuse
        why = f"immobilisée et amortie, pas en charge : {reason} ; dites-moi si c'est une charge"
        return {**apply_account(invoice, account["id"], why), "note": True}
    return apply_account(invoice, account["id"], reason)


def searched(accounts, search):
    """Return the ``accounts`` that ``search`` names: by the start of their code when ``search`` is digits, else
    accounts whose label has, for each search word, a word close to it (misspelling, accents or plural aside:
    "hotelrie" finds "Hôtellerie"). Words under 3 letters ("de", "ou") are ignored; [] when no word is left."""
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
    """List the dossier's accounts (plan comptable) whose code starts with ``search``, or whose label holds its
    words, even misspelled ("hotelrie", "transports"): one "code label" per line, at most 100.

    One word or code per call: "transport ou hotellerie" is two calls. Empty ``search``: every account.
    ``side``: "achat" for a purchase's accounts only (class 6), "vente" for a sale's (class 7), empty for all
    classes; any other value raises ToolError."""
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
        return f"Aucun compte du dossier {dossier} ne correspond à « {search} »."
    more = " (les 100 premiers : précisez la recherche)" if len(found) > 100 else ""
    return counted(len(found), "{} compte", "{} comptes") + f" du dossier {dossier}{more} :\n" + listing(found[:100])


def resolve_account(accounts, answer):
    """Return the account the client's ``answer`` names on its own, a code (or its start) or a label; else None.

    ``accounts``: [{id, code, name}] sorted by code. A short code is the account it names padded with zeros (CGNC):
    "6145" is 61450000, not 61451000. Several matches sharing one label give the lowest code (choose-account.md's
    rule); several other matches give None."""
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
    """Return the account a free-text ``answer`` names ("Pour MEDI TELECOM, utilisez le compte 6145"): the model
    reads the code, then resolve_account checks it against ``accounts``. None when it names none clearly."""
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
def set_account(invoice_id, answer, confirm=False, choices=None):
    """Put the account the client asked for on a draft invoice (their reply to assign_account's question).

    ``answer``: a code ("6111", "61110000") or a label ("achats de marchandises") on its own, else a sentence the
    model reads; only accounts Experio offers without a warning count (usable). Returns an ok result once applied,
    or a question result asking again when it names no single account. ``confirm``: the account was given for
    this invoice while answering about another: a question result asks the client to confirm it (oui / non), never
    applied on their sentence alone. ``choices``: the near accounts the question listed ({id: label}), read first
    (« le deuxième », « travaux »). Raises ToolError when ``answer`` is empty."""
    if not (answer or "").strip():
        raise ToolError("answer manquant : le code ou l'intitulé donne par le client.")
    invoice = require_draft_invoice(invoice_id, INVOICE_FIELDS, "changer de compte")
    accounts = usable(invoice_id, chart(invoice["company_id"][0]))
    picked = among(choices, answer) if choices else None  # the near accounts the question listed (proposals)
    account = picked or resolve_account(accounts, answer) or account_meant(accounts, answer)
    if account and confirm:
        label = f"{account['code']} {account['name']}"
        return question(
            f"Facture {invoice['invoice_number'] or invoice_id} : je la passe en compte `{label}`, comme vous l'avez "
            "demandé ? oui / non",
            choices={account["id"]: label},
        )
    if account:
        return apply_account(invoice, account["id"])  # the client's own account: « appliqué »
    return question(
        f"Facture {invoice_id} : « {without_subject(answer)} » ne désigne pas un seul compte du dossier. "
        "Quel compte utiliser (code ou intitulé) ?"
    )


@with_ids
def confirm_account(invoice_id, account_id, confirmed):
    """Apply the client's oui / non (``confirmed``) to account ``account_id``, which set_account asked them to
    confirm: oui puts it on the invoice, non asks which account to use instead."""
    if not confirmed:
        return question(f"Facture {invoice_id} : quel compte utiliser alors (code ou intitulé) ?")
    invoice = require_draft_invoice(invoice_id, INVOICE_FIELDS, "changer de compte")
    return apply_account(invoice, account_id)
