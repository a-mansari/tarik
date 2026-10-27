"""Banks as a client or a journal names them (Experio's res.bank list), and the bank journal Tarik makes in a dossier
that has none at the bank of a statement.

Experio has no matching of its own: it never reads a bank from a journal's name, and its statement import only lists
the bank journals (bank/wizard/exp_bank_statement_import.py:24-53). A journal is made as its form does
(exp_account/models/exp_journal.py create): name, a code unique in the dossier archived ones included
(ensure_unique_codes), type bank, the bank (required by the statement import, exp_bank_statement_import.py:142) and a
treasury account (ensure_journal_account_id: Experio makes none)."""

import re

from engine.tools.core.app import ToolError
from engine.tools.core.experio import dry_run, odoo_call
from engine.tools.core.words import normalized, said

GENERIC_BANK_WORDS = {"bank", "banque", "du", "de", "le", "la", "et", "pour", "s", "a", "maroc"}
BANK_ALIASES = {  # the names clients use -> words of the bank's name in Experio's list
    "societe generale": "saham",
    "sgmb": "saham",
    "bmce": "bank of africa",
    "boa": "bank of africa",
    "bp": "populaire",
    "bcp": "populaire",
    "chaabi": "populaire",
    "awb": "attijariwafa",
    "attijari": "attijariwafa",
    "cdm": "credit du maroc",
    "tgr": "tresorerie generale",
    "cam": "credit agricole",
    "wafa": "attijariwafa",
    "bnp": "bmci",
}


def read_as(text):
    """Return (written, read): ``text`` normalized, and the same with the names clients use read as the bank's words
    (BANK_ALIASES)."""
    written = words = normalized(text or "")
    for old, new in BANK_ALIASES.items():
        if said(old, words):
            words = words.replace(old, new)
    return written, words


def bank_words(bank):
    """Return the words of ``bank``'s name that tell it apart (GENERIC_BANK_WORDS left out)."""
    return [w for w in normalized(bank["name"]).split() if w not in GENERIC_BANK_WORDS]


def written_in_full(text, banks):
    """Return the ``banks`` whose whole name (without its « (...) ») ``text`` writes, as written or as read."""
    written, words = read_as(text)
    both = f"{written} {words}"  # « attijari payment » as written, « cdm » as read
    return [bank for bank in banks if said(normalized(re.sub(r"\([^)]*\)", " ", bank["name"])), both)]


def bank_named(text, banks, whole=False):
    """Return the bank of ``banks`` (Experio's res.bank list, [{id, name}]) that ``text`` names ("CIH",
    "attijari", "la Banque Populaire"), or None.

    A word of ``text`` matches a word of the bank's name that starts with it; old bank names are read as the
    new ones (BANK_ALIASES). A bank whose whole name is written wins; else the one with the most matched letters;
    None when two banks match as many (« BQ CREDIT »: CREDIT AGRICOLE or CREDIT DU MAROC), so the client is asked.
    ``whole``: a word must be a whole word of the bank's name, for free text (« ci-joint » is not CIH)."""
    full = written_in_full(text, banks)
    if len(full) == 1:  # « credit du maroc » written out: that bank, not CREDIT AGRICOLE DU MAROC
        return full[0]
    given = {w for w in read_as(text)[1].split() if len(w) >= 2 and w not in GENERIC_BANK_WORDS}
    best, score, tied = None, (0, 0), False
    for bank in banks:
        fits = (lambda w, g: w == g) if whole else str.startswith
        matched = [max((len(g) for g in given if fits(w, g)), default=0) for w in bank_words(bank)]
        hit = (sum(matched), -sum(1 for m in matched if not m))  # letters matched, then fewest words unmatched
        if hit[0] and hit[0] > score[0]:
            best, score, tied = bank, hit, False
        elif hit[0] and hit[0] == score[0]:  # « BQ CREDIT »: CREDIT AGRICOLE or CREDIT DU MAROC, asked
            tied = True
    return None if tied else best


def one_bank_named(text, banks):
    """Return the one bank ``text`` names among ``banks``, None when none or several (« relevés BMCE et SGMB »).

    Each bank is tried alone, by whole words (bank_named's ``whole``: « ci-joint » is not CIH); a bank named only by
    words of a bank written in full (CREDIT AGRICOLE DU MAROC beside « crédit du maroc ») is not another one."""
    full = written_in_full(text, banks)
    taken = {word for bank in full for word in bank_words(bank)}
    named = full + [
        bank
        for bank in banks
        if bank not in full and bank_named(text, [bank], whole=True) and not taken & set(bank_words(bank))
    ]
    return named[0] if len(named) == 1 else None


def bank_partner(dossier_id, bank_name):
    """Return the id of the dossier's tiers named after bank ``bank_name`` (its name before « (... »), None if none."""
    tiers = odoo_call(
        "exp.partner",
        "search_read",
        [("company_id", "=", dossier_id), ("name", "ilike", bank_name.split(" (")[0])],
        fields=["id"],
        limit=1,
    )
    return tiers[0]["id"] if tiers else None


def give_bank(dossier_id, journal, bank, context):
    """Set bank journal ``journal`` ({id, partner_id}) at ``bank`` ({id, name}), as the journal form does: its Banque,
    and, when it has no tiers bancaire, the dossier's tiers named after the bank if there is one. Nothing is written
    in test mode."""
    values = {"bank_id": bank["id"]}
    if not journal.get("partner_id"):
        tiers = bank_partner(dossier_id, bank["name"])
        if tiers:
            values["partner_id"] = tiers
    if not dry_run():
        odoo_call("exp.journal", "write", [journal["id"]], values, context=context)


def free_code(dossier_id, prefix, context):
    """Return a journal code free in the dossier: ``prefix``, else prefix1, prefix2... Experio's codes are unique per
    dossier, archived journals included (exp_journal.py ensure_unique_codes)."""
    codes = odoo_call(
        "exp.journal",
        "search_read",
        [("company_id", "=", dossier_id), ("active", "in", [True, False])],
        fields=["code"],
        context=context,
    )
    taken = {(j["code"] or "").strip().upper() for j in codes}
    return next(code for code in (prefix, *(f"{prefix}{n}" for n in range(1, 100))) if code not in taken)


def make_bank_journal(dossier_id, bank, context):
    """Make the dossier's bank journal at ``bank`` ({id, name}) with its own treasury account, as the journal form
    does.

    The journal: « Banque <the bank's name> » (« Banque BANK OF AFRICA »), code BQ (else BQ1, BQ2...), its bank,
    and the dossier's tiers named after the bank when there is one. Its account: a new 5141n of that name, n the
    first digit no 5141n account of the dossier uses, padded with zeros to the dossier's chart length
    (accounts_chart_length).

    Returns {id, name, bank_id, code, account} (id 0 in test mode, where nothing is written).

    Raises:
        ToolError: the dossier already has 5141 accounts 1 to 9.
        Exception: Experio's refusal; an account made for a journal refused is deleted first.
    """
    name = f"Banque {bank['name'].split(' (')[0]}"
    code = free_code(dossier_id, "BQ", context)
    accounts = odoo_call(
        "exp.account",
        "search_read",
        [("company_id", "=", dossier_id), ("code", "=like", "5141%")],
        fields=["code"],
        context=context,
    )
    used = {a["code"][:5] for a in accounts}
    account = next((f"5141{n}" for n in range(1, 10) if f"5141{n}" not in used), None)
    # Experio pads a code to the chart's length only when the dossier blocks it (account_length_block,
    # exp_account.py verify_accounts_length): padded here, as its other accounts are.
    length = odoo_call("res.company", "read", [dossier_id], fields=["accounts_chart_length"])[0]
    account = account and account.ljust(length["accounts_chart_length"] or 0, "0")
    if not account:
        raise ToolError(
            f"Le dossier a déjà les comptes 51411 à 51419 : le journal `{name}` est à créer par le cabinet."
        )
    journal = {"id": 0, "name": name, "bank_id": [bank["id"], bank["name"]], "code": code, "account": account}
    if dry_run():
        return journal
    account_id = odoo_call(
        "exp.account", "create", {"code": account, "name": name, "company_id": dossier_id}, context=context
    )
    values = {"name": name, "code": code, "type": "bank", "company_id": dossier_id, "bank_id": bank["id"]}
    values["default_account_id"] = account_id
    tiers = bank_partner(dossier_id, bank["name"])
    if tiers:
        values["partner_id"] = tiers
    try:
        journal["id"] = odoo_call("exp.journal", "create", values, context=context)
    except Exception:
        odoo_call("exp.account", "unlink", [account_id], context=context)  # the account made for it, nothing else
        raise
    journal["account"] = odoo_call("exp.account", "read", [account_id], fields=["code"], context=context)[0]["code"]
    return journal
