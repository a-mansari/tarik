"""A TPE statement (relevé des opérations monétiques: CMI, NAPS, AL BARID, VPS, ATTIJARI PAYMENT), as the cabinet
handles it on Experio's own TPE screens (module bank: exp.monetary.statement, not a bank statement).

- Network: Experio asks it on import (exp.monetary.statement.import type_selection); it picks Experio's reading. Tarik
  reads it on the statement's own header, else in the client's words, else asks.
- Import: Experio's own upload screen (action_add_documents_from_javascript with the document's ``type``, as
  form_with_drop_area.js sends it): the network becomes the statement's type and Experio's reading's doctype. (The
  wizard's direct create drops the network: exp_bank_statement_import._get_default_values.)
- Operations: Experio's reading makes each remittance's lines (exp_bluejay_job, label + TOTAL REMISE, COMMISSION HT,
  COMMISSION TVA, NET REMISE) without an operation, and has no detection for them. Tarik gives each its operation
  of Experio's own table (exp_account.MONETARY_OPERATION_TYPE_INFO): the sale is VENTE (client account, 34210), the
  commission FRAIS BANCAIRES (61471, its TVA taken from the line of type « tax » of the same remittance,
  monetary_statement_line._get_move_values), the commission's TVA TAXE BANCAIRE, the net paid to the bank SOLDE NET
  REMISE (funds transfer, 51150). An operation missing from the dossier is made as Experio makes it on its chart's
  import (create_monetary_operation_type), when the dossier has its account.
- A VENTE line needs its client (« Le tiers est obligatoire pour cette opération »): Experio finds it from an
  invoice of the line's number (_onchange_invoice_ids); for the others the client is asked once per statement.
- Posting: Experio's own action_post. Its warning that the network changed since the last TPE statement is
  confirmed (the network was read on the statement or given by the client); its duplicate refusal stops.
"""

import base64
import re

import pypdfium2

from engine.tools.core.app import ok, question, routine, stop
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import TEST_MODE
from engine.tools.core.conversation import own_words
from engine.tools.core.experio import dry_run, experio_refusal, in_dossier, odoo_call, odoo_json, plain
from engine.tools.core.queries import Q
from engine.tools.core.words import normalized

STATEMENT = "exp.monetary.statement"
LINE = "exp.monetary.statement.line"
OPERATION = "monetary.operation.type"
WIZARD = "exp.monetary.statement.import"
JOURNAL_WARNING = "exp.monetary.statement.journal.warning.wizard"
POSTED = ("post", "export")

# Experio's networks (type_selection), as the statement and the client name them (normalized).
NETWORKS = {
    "cmi": ("CMI", r"cmi|centre monetique interbancaire"),
    "naps": ("NAPS", r"naps"),
    "albarid": ("AL BARID", r"al barid"),
    "vps": ("VPS", r"vps"),
    "attijari": ("ATTIJARI PAYMENT", r"attijari payment"),
}
NETWORK_CHOICES = dict(enumerate(NETWORKS, 1))  # a question's choices are numbered
# What only a TPE statement says (normalized): a remittance of card sales.
TPE_WORDS = re.compile(
    r"(?<![a-z])(?:operations? monetiques?|remises? (?:des )?cartes?|remises? tpe|releves? tpe|terminal de paiement)"
)

# Experio's table (exp_account.MONETARY_OPERATION_TYPE_INFO): the operation's name, its account's code prefix,
# bank_type, type, tax (rate, type) and tax code (PURCHASE_TAX_CODE_MAP).
OPERATIONS = {
    "VENTE": ("34210", False, "normal", None, None),
    "TAXE BANCAIRE": (None, "tax", "bank_fee", None, None),
    "FRAIS BANCAIRES": ("61471", "frais_without_tax", "bank_fee", (10, "purchase"), ("exp_account", "tax52")),
    "SOLDE NET REMISE": ("51150", False, "funds_transfer", None, None),
}
# The suffix Experio's reading gives each line of a remittance (exp_bluejay_job), and its operation.
SUFFIXES = (
    ("TOTAL REMISE", "VENTE"),
    ("COMMISSION HT", "FRAIS BANCAIRES"),
    ("COMMISSION TVA", "TAXE BANCAIRE"),
    ("NET REMISE", "SOLDE NET REMISE"),
)
STAN = re.compile(r" \d{2}/\d{2}/\d{4} STAN ")  # a transaction line, when the dossier asks them in detail: a sale
FIELDS = ["state", "statement_processed", "company_id", "journal_id", "type", "line_ids", "name"]
LINE_FIELDS = ["payment_ref", "operation_id", "partner_id", "invoice_ids", "state", "error_msg", "debit", "credit"]


def network_in(text):
    """The network a TPE statement's ``text`` names (its header, or the client's words), None when it is no TPE
    statement or names none or several."""
    words = normalized(text or "")
    found = [key for key, (_, pattern) in NETWORKS.items() if re.search(rf"(?<![a-z])(?:{pattern})(?![a-z])", words)]
    if len(found) > 1 and "cmi" in found:  # CMI is also a card brand, in the legend of the others' statements
        found.remove("cmi")  # (Attijari Payment's: « V VISA M MASTERCARD ... C CMI »)
    return found[0] if len(found) == 1 else None


def network_label(key):
    """Experio's label of network ``key`` (« ATTIJARI PAYMENT »)."""
    return NETWORKS.get(key, (key,))[0]


def is_tpe_text(text):
    return bool(TPE_WORDS.search(normalized(text or "")))


def first_page_text(raw):
    """The text of PDF ``raw``'s first page ("" for a scan)."""
    try:
        document = pypdfium2.PdfDocument(raw)
        return document[0].get_textpage().get_text_range() if len(document) else ""
    except pypdfium2.PdfiumError:
        return ""


def tpe_network(raw, words):
    """(is a TPE statement, its network or None), from the PDF's own first page, else the client's ``words``
    (« relevé TPE CMI »). shortcut: a scan without text is a TPE statement only by the client's words."""
    text = first_page_text(raw)
    if is_tpe_text(text):
        return True, network_in(text) or network_in(words)
    if is_tpe_text(words):
        return True, network_in(words)
    return False, None


def network_question(name):
    """The question when the network can't be read: Experio's own list."""
    return question(
        f"`{name}` : relevé TPE de quel réseau ? " + ", ".join(label for label, _ in NETWORKS.values()),
        choices={number: NETWORKS[key][0] for number, key in NETWORK_CHOICES.items()},
    )


def network_answered(choices, chosen):
    """The network a question of network_question asked for (its ``choices`` as recorded) and the client ``chosen``
    (its number), else None (another question)."""
    labels = {label for label, _ in NETWORKS.values()}
    if not choices or set(choices.values()) != labels:
        return None
    return NETWORK_CHOICES.get(chosen)


def last_journal(dossier_id):
    """The journal of the dossier's last TPE statement (Experio's default for the next one), else None."""
    found = odoo_call(STATEMENT, "search_read", [("company_id", "=", dossier_id)], fields=["journal_id"], limit=1)
    return found[0]["journal_id"][0] if found and found[0]["journal_id"] else None


def import_tpe(name, raw, dossier_id, journal_id, network):
    """Experio's own TPE upload of PDF ``raw`` (the screen's route: the document with its network as ``type``),
    into journal ``journal_id``: the statement's id. RuntimeError unless it made exactly one."""
    context = in_dossier(dossier_id)
    wizard = odoo_call(
        WIZARD,
        "create",
        {"to_document_manager": True, "invoice_journal_id": journal_id, "type_selection": network},
        context=context,
    )
    document = {
        "name": name.rsplit(".", 1)[0],
        "mimetype": "application/pdf",
        "is_scanned": False,
        "datas": base64.b64encode(raw).decode(),
        "type": network,
        "journal_id": journal_id,
    }
    made = odoo_json(WIZARD, "action_add_documents_from_javascript", [wizard], [document], context=context)
    if len(made or []) != 1:
        raise RuntimeError(f"{WIZARD} {wizard} made {made}, expected one statement")
    return made[0]


def read_tpe(statement_id):
    return odoo_call(STATEMENT, "read", [statement_id], fields=FIELDS)[0]


def being_read(statement):
    """True while Experio has not read it: its reading sets statement_processed (and the lines, state draft)."""
    return statement["state"] == "pending" and not statement["statement_processed"]


def dossier_operations(dossier_id, context):
    return odoo_call(
        OPERATION,
        "search_read",
        [("company_id", "=", dossier_id)],
        fields=["name", "type", "bank_type", "account_id"],
        context=context,
    )


def ensure_operations(dossier_id, context):
    """The dossier's TPE operations, Experio's 4 made when missing as Experio makes them on its chart's import (one
    per account of the code, its tax by rate and type): (operations, names made, names that can't be made for
    want of their account)."""
    operations = dossier_operations(dossier_id, context)
    have = {(o["name"], o["type"], o["bank_type"] or False) for o in operations}
    made, missing = [], []
    for name, (prefix, bank_type, kind, tax, code) in OPERATIONS.items():
        if (name, kind, bank_type) in have:
            continue
        accounts = [False]
        if prefix:
            accounts = odoo_call(
                "exp.account",
                "search",
                [("company_id", "=", dossier_id), ("code", "=like", f"{prefix}%")],
                order="code",
                context=context,
            )
        if not accounts:
            missing.append(name)
            continue
        if dry_run():
            made.append(name)
            continue
        values = {"name": name, "bank_type": bank_type, "type": kind, "company_id": dossier_id}
        if tax:
            found = odoo_call(
                "exp.tax",
                "search",
                [("company_id", "=", dossier_id), ("rate", "=", tax[0]), ("type", "=", tax[1])]
                + [("is_immo_tax", "=", False)],
                limit=1,
                context=context,
            )
            values["tax_id"] = found[0] if found else False
        if code:
            ref = odoo_call(
                "ir.model.data", "search_read", [("module", "=", code[0]), ("name", "=", code[1])], fields=["res_id"]
            )
            values["code_id"] = ref[0]["res_id"] if ref else False
        odoo_call(OPERATION, "create", [{**values, "account_id": account} for account in accounts], context=context)
        made.append(name)
    if made and not dry_run():
        operations = dossier_operations(dossier_id, context)
    return operations, made, missing


def operation_named(line):
    """The operation Experio's own label suffix gives a line read from the statement, None for another line."""
    label = (line["payment_ref"] or "").upper()
    if STAN.search(label):
        return "VENTE"
    for suffix, name in SUFFIXES:
        if label.endswith(suffix):
            return name
    return None


def sale_operation(operations, partner, context):
    """The VENTE operation of the client ``partner`` (its customer account's, as Experio picks it from an
    invoice: _onchange_invoice_ids), else the first VENTE of the dossier."""
    sales = [o for o in operations if o["type"] == "normal" and o["name"] == "VENTE"]
    if partner:
        account = odoo_call("exp.partner", "read", [partner], fields=["customer_account_id"], context=context)
        account = account[0]["customer_account_id"] if account else False
        own = [o for o in sales if account and o["account_id"] and o["account_id"][0] == account[0]]
        if own:
            return own[0]
    return min(sales, key=lambda o: o["account_id"][1] if o["account_id"] else "~") if sales else None


def set_operations(statement, operations, client, context):
    """Each line read without operation given Experio's operation for its suffix; a sale line without client gets
    ``client`` (a partner id, or None). (lines given an operation, sale lines still without client)."""
    lines = odoo_call(LINE, "read", statement["line_ids"], fields=LINE_FIELDS, context=context)
    by_name = {}
    for operation in operations:
        by_name.setdefault(operation["name"], operation)
    given, unknown = 0, 0
    for line in lines:
        if line["state"] in POSTED:
            continue
        name = operation_named(line)
        values = {}
        if name == "VENTE":
            partner = line["partner_id"][0] if line["partner_id"] else client
            if partner and not line["partner_id"]:
                values["partner_id"] = partner
            if not line["operation_id"]:
                operation = sale_operation(operations, partner, context)
                if operation:
                    values["operation_id"] = operation["id"]
            if not partner:
                unknown += 1
        elif name and not line["operation_id"] and name in by_name:
            values["operation_id"] = by_name[name]["id"]
        if values:
            if not dry_run():
                odoo_call(LINE, "write", [line["id"]], values, context=context)
            given += "operation_id" in values
    return given, unknown


def find_client(dossier_id, words, context):
    """The dossier's one client the client's ``words`` name (exactly, else the only one containing them), None
    otherwise; and the candidates found."""
    words = " ".join((words or "").split())
    if not words:
        return None, []
    domain = [("company_id", "=", dossier_id), ("name", "ilike", words)]
    found = odoo_call("exp.partner", "search_read", domain, fields=["name"], limit=6, context=context)
    exact = [p for p in found if normalized(p["name"]) == normalized(words)]
    if len(exact) == 1 or len(found) == 1:
        return (exact or found)[0], found
    return None, found


def client_question(statement_id, unknown, found=(), named=""):
    listed = ", ".join(f"`{p['name']}`" for p in found)
    listed = f" Plusieurs tiers possibles : {listed}." if found else ""
    if named and not found:
        listed = f" Aucun client « {named} » dans le dossier."
    return question(
        f"Relevé TPE n° {statement_id} : {unknown} vente(s) par carte sans client. À quel client de "
        f"votre dossier les rattacher (par exemple « clients divers ») ?{listed}"
    )


def answer_client(file_id, answer, decision):
    """apply_answer for a TPE statement asked its card sales' client: the dossier's client the agent read in the
    answer (``decision``, else the client's own words), given to its sale lines still without one; else asked
    again, with the candidates found."""
    with cabinet_db() as db:
        statement_id = db.execute(Q.statement_of_file, (file_id,)).fetchone()[0]
    statement = read_tpe(statement_id)
    dossier_id = statement["company_id"][0]
    context = in_dossier(dossier_id)
    client, found = find_client(dossier_id, decision, context)
    if not client and not found:
        client, found = find_client(dossier_id, own_words(answer), context)
    operations = dossier_operations(dossier_id, context)
    _, unknown = set_operations(statement, operations, client and client["id"], context)
    if not client:
        return client_question(statement_id, unknown, found, decision or own_words(answer))
    told = f"Relevé TPE n° {statement_id} : ventes par carte rattachées au client `{client['name']}`."
    return ok(f"{TEST_MODE if dry_run() else ''}{told}")


def totals(lines):
    """The statement's total remise, commissions HT, TVA on them and net remise, from its lines."""
    sums = dict.fromkeys(("VENTE", "FRAIS BANCAIRES", "TAXE BANCAIRE", "SOLDE NET REMISE"), 0.0)
    for line in lines:
        name = operation_named(line)
        if name:
            sums[name] += abs((line["credit"] or 0) - (line["debit"] or 0))
    return sums


def blocked(lines):
    """{Experio's reason: how many lines} for the lines not ready, and the counts ready and posted."""
    reasons = {}
    for line in lines:
        if line["state"] in ("draft", "pending"):
            reason = plain(line["error_msg"] or "") or line["state"]
            reasons[reason] = reasons.get(reason, 0) + 1
    return reasons, sum(line["state"] == "ready" for line in lines), sum(line["state"] in POSTED for line in lines)


def settle_tpe(statement_id, client_words=""):
    """settle_statement for a TPE statement once read: its operations given, its sales' client set (from the client's
    ``client_words`` when they named one), and where it stands."""
    statement = read_tpe(statement_id)
    dossier_id = statement["company_id"][0]
    context = in_dossier(dossier_id)
    operations, made, missing = ensure_operations(dossier_id, context)
    if missing:
        names = ", ".join(f"`{name}`" for name in missing)
        codes = ", ".join(f"`{OPERATIONS[name][0]}`" for name in missing if OPERATIONS[name][0])
        return stop(
            f"Relevé TPE n° {statement_id} : le dossier n'a pas l'opération TPE {names} ni son compte ({codes}) : "
            "à créer par le cabinet dans Experio (Configuration, TPE)."
        )
    client, found = find_client(dossier_id, client_words, context)
    given, unknown = set_operations(statement, operations, client and client["id"], context)
    lines = odoo_call(LINE, "read", statement["line_ids"], fields=LINE_FIELDS, context=context)
    sums = totals(lines)
    reasons, ready, posted = blocked(lines)
    text = (
        f"Relevé TPE n° {statement_id} ({network_label(statement['type'])}) : "
        f"{len(lines)} ligne(s) lue(s), total remise {sums['VENTE']:.2f}, commissions HT "
        f"{sums['FRAIS BANCAIRES']:.2f}, TVA sur commissions {sums['TAXE BANCAIRE']:.2f}, net remise "
        f"{sums['SOLDE NET REMISE']:.2f}"
    )
    if given:
        text += f" ; {given} opération(s) affectée(s)"
    if client:
        text += f" ; ventes rattachées au client `{client['name']}`"
    text += f" ; {ready} prête(s) à comptabiliser" + (f", {posted} déjà comptabilisée(s)" if posted else "") + "."
    if made:
        names = ", ".join(f"`{name}`" for name in made)
        text += f" Opération(s) TPE créée(s) dans le dossier, comme Experio le fait : {names}."
    company = odoo_call("res.company", "read", [dossier_id], fields=["monetary_operations_ignore_remise"])[0]
    if company["monetary_operations_ignore_remise"]:
        text += (
            " Le dossier a « Ignorer la ligne de solde remise » coché : pas de ligne de net remise, ce relevé ne "
            "pourra pas être rapproché du versement sur le relevé bancaire."
        )
    if unknown:
        return client_question(statement_id, unknown, found)
    if reasons:
        text += " À compléter dans Experio : " + " ; ".join(f"{why} ({n})" for why, n in reasons.items()) + "."
    return ok(f"{TEST_MODE if dry_run() else ''}{text}", statement_id=statement_id)


def post_tpe(statement_id):
    """post_statement for a TPE statement: Experio's action_post; its warning that the journal or network changed
    since the last TPE statement confirmed (action_confirm_anyway): Tarik took the network from the statement or the
    client; any other window or refusal stopping for the cabinet."""
    statement = read_tpe(statement_id)
    if statement["state"] in POSTED:
        return routine(f"Relevé TPE n° {statement_id} : déjà comptabilisé.")
    context = in_dossier(statement["company_id"][0])
    if dry_run():
        return ok(f"{TEST_MODE}Relevé TPE n° {statement_id} : ses lignes prêtes seraient comptabilisées.")
    confirmed = ""
    try:
        action = odoo_json(STATEMENT, "action_post", [statement_id], context=context)
        if isinstance(action, dict) and action.get("res_model") == JOURNAL_WARNING:
            odoo_json(JOURNAL_WARNING, "action_confirm_anyway", [action["res_id"]], context=context)
            confirmed = (
                " Experio signalait un journal ou un réseau différent du dernier relevé TPE comptabilisé : confirmé, "
                f"le réseau {network_label(statement['type'])} étant celui du relevé."
            )
            action = None
    except Exception as error:  # Experio's refusal (an exact duplicate statement): its own words
        return stop(f"Relevé TPE n° {statement_id} : pas comptabilisé, Experio refuse : {experio_refusal(error)}")
    if isinstance(action, dict) and action.get("res_model"):
        return stop(
            f"Relevé TPE n° {statement_id} : pas comptabilisé, Experio demande une confirmation à l'écran "
            f"(« {action.get('name') or action['res_model']} ») : à valider par le cabinet dans Experio."
        )
    lines = odoo_call(LINE, "read", read_tpe(statement_id)["line_ids"], fields=LINE_FIELDS, context=context)
    reasons, _, posted = blocked(lines)
    text = f"Relevé TPE n° {statement_id} : {posted} ligne(s) comptabilisée(s)"
    text += f", {sum(reasons.values())} restent à compléter" if reasons else ""
    return ok(text + "." + confirmed, statement_id=statement_id)
