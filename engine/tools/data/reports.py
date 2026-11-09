"""Curated reads of a dossier's books for an exercice: the figures an accountant is asked for, said the way an
accountant says them (débiteur, créditeur), so the agent never works out a sign, an exercice or a caveat itself.

They share query_data's one read path: the same gate (opened: the dossier proven, the conversation allowed, Experio's
context), the same journal items (the registry's exp.move.line, posted and exported, closure entries left out) and the
same read-only calls. The exercice is resolved as dossier_progress does (exercices_of): ``year`` names it, 0 is the
latest one."""

import re

from engine.tools.core.app import ToolError, log, tool
from engine.tools.core.experio import odoo_call, refused
from engine.tools.data.query import opened
from engine.tools.data.registry import MODELS
from engine.tools.dossiers.progress import exercices_of, french_date
from engine.tools.invoices.listing import amount

LINES = MODELS["exp.move.line"]
STATES = MODELS["exp.fiscal.entries"].columns["state"].values
PREFIX = re.compile(r"\d{1,8}")
MAX_GROUPS, MAX_LINES = 300, 100  # accounts or third parties read; lines shown
OPEN_NOTE = (
    "Exercice non clôturé : ce sont les écritures passées à ce jour ; stock, amortissements, provisions et impôt "
    "peuvent rester à passer, ce n'est pas un résultat définitif."
)
NO_OPENING_NOTE = (
    "Aucune écriture d'à-nouveau dans cet exercice : les comptes 1 à 5 ne montrent que les mouvements de l'exercice, "
    "sans l'ouverture ; un solde de bilan se cumule depuis l'origine."
)


def side(balance):
    """Return a balance (debit minus credit) as « débiteur 1 234.00 », « créditeur 50.00 » or « soldé »."""
    balance = round(balance, 2)
    if not balance:
        return "soldé"
    return f"{'débiteur' if balance > 0 else 'créditeur'} {amount(abs(balance))}"


def prefix_term(prefix, required=False):
    """Return the domain terms keeping the accounts whose code starts with ``prefix`` (digits, e.g. "6", "3421");
    none for an empty one unless ``required``. Raises ToolError for anything but digits."""
    prefix = str(prefix or "").strip()
    if not prefix and not required:
        return []
    if not PREFIX.fullmatch(prefix):
        raise ToolError(
            "account_prefix : le début d'un code de compte, en chiffres (6 charges, 7 produits, 3421, 4411)."
        )
    return [("account_id.code", "=like", f"{prefix}%")]


def exercice_of(name, dossier_id, year, context):
    """Return the dossier's exercice of ``year`` (int), else its latest, as {name, date_start, date_end, state}.
    Raises ToolError when it has none."""
    if isinstance(year, bool) or not isinstance(year, int) or not 0 <= year <= 2100:
        raise ToolError("year : une année sur quatre chiffres, ou 0 pour le dernier exercice.")
    with refused("exercice_of", f"Experio n'a pas pu lire les exercices du dossier `{name}` : {{reason}}"):
        found = exercices_of([dossier_id], year, context).get(dossier_id)
    if not found:
        raise ToolError(f"Le dossier `{name}` n'a pas d'exercice{f' {year}' if year else ''} dans Experio.")
    return found


def heading(name, exercice):
    """Return the first line of a report: the dossier, the exercice, its dates and state."""
    start, end = french_date(exercice["date_start"]), french_date(exercice["date_end"])
    return f"Dossier `{name}`, {exercice['name']} du {start} au {end} ({STATES[exercice['state']]})."


def domain_of(dossier_id, exercice, extra, since_origin=False):
    """Return the journal items' domain: the dossier's posted items of the exercice (up to its end only when
    ``since_origin``), then ``extra``."""
    dates = [("date", "<=", exercice["date_end"])] if since_origin else [
        ("date", ">=", exercice["date_start"]),
        ("date", "<=", exercice["date_end"]),
    ]  # fmt: skip
    return [("company_id", "=", dossier_id), *LINES.base, *dates, *extra]


def totals(dossier_id, exercice, context, extra, groupby, orderby="", since_origin=False):
    """Return the debit and credit sums of the journal items grouped by ``groupby`` (Experio's read_group rows), at
    most MAX_GROUPS + 1."""
    with refused("reports totals", "Experio n'a pas pu lire les écritures : {reason}"):
        return odoo_call(
            "exp.move.line",
            "read_group",
            domain_of(dossier_id, exercice, extra, since_origin),
            ["debit:sum", "credit:sum"],
            groupby,
            lazy=False,
            orderby=orderby,
            limit=MAX_GROUPS + 1,
            context=context,
        )


def notes_of(dossier_id, exercice, prefix, context):
    """Return the caveats of a report on accounts starting with ``prefix`` ("" for all): an exercice not closed for
    the result accounts, no opening entries for the balance sheet ones."""
    notes = []
    if exercice["state"] not in ("close", "shut") and prefix[:1] in ("", "6", "7"):
        notes.append(OPEN_NOTE)
    if prefix[:1] in ("", "1", "2", "3", "4", "5"):
        opening = [("journal_id.journal_category", "=", "a_nouveau")]
        with refused("reports opening", "Experio n'a pas pu lire les écritures : {reason}"):
            found = odoo_call(
                "exp.move.line",
                "search_count",
                domain_of(dossier_id, exercice, opening),
                context=context,
            )
        if not found:
            notes.append(NO_OPENING_NOTE)
    return notes


def code_of(account):
    """Return an account's code from its Experio display name « 61110000 Achats »."""
    return (account[1] if account else "sans compte").split(" ")[0]


def cut_note(rows):
    """Return the line saying a list was cut at MAX_GROUPS, "" when it was not."""
    return f"Liste coupée à {MAX_GROUPS} : précisez account_prefix." if len(rows) > MAX_GROUPS else ""


@tool
def trial_balance(dossier_id: int, session_id: str, year: int = 0, account_prefix: str = "") -> str:
    """Return, as French text, a dossier's trial balance (balance générale) for an exercice: each account's debit,
    credit and balance (« débiteur » or « créditeur »), then the totals, from the posted and exported journal items,
    closure entries left out.

    Args:
        dossier_id: the dossier (from find_dossier).
        session_id: the conversation (filled in by Tarik).
        year: the exercice's year the client names; 0 for the dossier's latest exercice.
        account_prefix: only the accounts whose code starts with it: "6" charges, "7" produits, "61" achats, "3421"
            clients, "4411" fournisseurs, "5" trésorerie; empty for every account.

    Read the caveats the report ends with before answering: an exercice not closed gives no final result (stock,
    depreciation, provisions and tax may be to book), and without opening entries the balance-sheet accounts show the
    exercice's movements only. Never call the difference of produits and charges a profit or a loss without saying so.

    Raises:
        ToolError: the dossier is unknown or not the conversation's, ``year`` has no exercice, ``account_prefix`` is
            not digits, or Experio can't answer."""
    name, context = opened(dossier_id, session_id)
    exercice = exercice_of(name, dossier_id, year, context)
    extra = prefix_term(account_prefix)
    log(f"trial_balance dossier {dossier_id} exercice {exercice['name']} prefix {account_prefix!r}")
    rows = totals(dossier_id, exercice, context, extra, ["account_id"])
    lines = [heading(name, exercice)]
    if not rows:
        return "\n".join(
            [
                *lines,
                "Aucune écriture comptabilisée pour ces comptes.",
                *notes_of(dossier_id, exercice, account_prefix, context),
            ]
        )
    shown = sorted(rows[:MAX_GROUPS], key=lambda row: code_of(row["account_id"]))
    for row in shown:
        label = row["account_id"][1] if row["account_id"] else "sans compte"
        lines.append(
            f"- {label} : débit {amount(row['debit'])}, crédit {amount(row['credit'])}, "
            f"solde {side(row['debit'] - row['credit'])}"
        )
    debit, credit = sum(row["debit"] for row in shown), sum(row["credit"] for row in shown)
    lines.append(f"Total : débit {amount(debit)}, crédit {amount(credit)}, solde {side(debit - credit)}.")
    return (
        "\n".join([*lines, cut_note(rows), *notes_of(dossier_id, exercice, account_prefix, context)])
        .replace("\n\n", "\n")
        .strip()
    )


@tool
def monthly_totals(dossier_id: int, session_id: str, account_prefix: str, year: int = 0) -> str:
    """Return, as French text, the month by month debit, credit and balance of the accounts starting with
    ``account_prefix`` over an exercice (posted and exported journal items, closure entries left out): the series
    behind a trend, e.g. the sales ("71"), the purchases ("611"), the VAT ("345", "4455").

    Args:
        dossier_id: the dossier (from find_dossier).
        session_id: the conversation (filled in by Tarik).
        account_prefix: the start of the accounts' code, in digits (required).
        year: the exercice's year the client names; 0 for the dossier's latest exercice.

    A month with no entry is absent: the last month listed is the last one booked, say so when it is before the end
    of the exercice.

    Raises:
        ToolError: the dossier is unknown or not the conversation's, ``year`` has no exercice, ``account_prefix`` is
            missing or not digits, or Experio can't answer."""
    name, context = opened(dossier_id, session_id)
    exercice = exercice_of(name, dossier_id, year, context)
    extra = prefix_term(account_prefix, required=True)
    log(f"monthly_totals dossier {dossier_id} exercice {exercice['name']} prefix {account_prefix!r}")
    rows = totals(dossier_id, exercice, context, extra, ["date:month"], orderby="date:month")
    lines = [heading(name, exercice), f"Comptes {account_prefix}…, mois par mois :"]
    if not rows:
        return "\n".join([*lines, "Aucune écriture comptabilisée pour ces comptes."])
    for row in rows:
        lines.append(
            f"- {row['date:month']} : débit {amount(row['debit'])}, crédit {amount(row['credit'])}, "
            f"solde {side(row['debit'] - row['credit'])}"
        )
    return "\n".join([*lines, *notes_of(dossier_id, exercice, account_prefix, context)])


@tool
def partner_balances(dossier_id: int, session_id: str, account_prefix: str, year: int = 0) -> str:
    """Return, as French text, what is still open per third party on the accounts starting with ``account_prefix``
    (« 3421 » clients, « 4411 » fournisseurs) at the end of an exercice: the balance of each third party's items not
    yet reconciled (lettrées), the biggest first, then the total. This is the ledger's own balance, from the origin
    to the exercice's end, not the invoices' residual amounts.

    Args:
        dossier_id: the dossier (from find_dossier).
        session_id: the conversation (filled in by Tarik).
        account_prefix: the start of the third-party accounts' code, in digits (required).
        year: the exercice's year the client names; 0 for the dossier's latest exercice.

    Raises:
        ToolError: the dossier is unknown or not the conversation's, ``year`` has no exercice, ``account_prefix`` is
            missing or not digits, or Experio can't answer."""
    name, context = opened(dossier_id, session_id)
    exercice = exercice_of(name, dossier_id, year, context)
    extra = [*prefix_term(account_prefix, required=True), ("reconciled", "=", False)]
    log(f"partner_balances dossier {dossier_id} exercice {exercice['name']} prefix {account_prefix!r}")
    rows = totals(dossier_id, exercice, context, extra, ["partner_id"], since_origin=True)
    lines = [
        heading(name, exercice),
        f"Comptes {account_prefix}…, non lettrés au {french_date(exercice['date_end'])} :",
    ]
    if not rows:
        return "\n".join([*lines, "Aucun solde ouvert."])
    shown = sorted(rows[:MAX_GROUPS], key=lambda row: -abs(row["debit"] - row["credit"]))
    for row in shown:
        who = row["partner_id"][1] if row["partner_id"] else "sans tiers"
        lines.append(f"- {who} : {side(row['debit'] - row['credit'])}")
    balance = sum(row["debit"] - row["credit"] for row in shown)
    lines.append(f"Total : {side(balance)}.")
    return "\n".join([*lines, cut_note(rows)]).strip()


@tool
def account_ledger(
    dossier_id: int, session_id: str, account_prefix: str, year: int = 0, partner_id: int = 0, limit: int = 50
) -> str:
    """Return, as French text, the journal items (grand livre) of the accounts starting with ``account_prefix`` over
    an exercice, oldest first: date, journal, entry, label, third party, debit, credit. Posted and exported items,
    closure entries left out.

    Args:
        dossier_id: the dossier (from find_dossier).
        session_id: the conversation (filled in by Tarik).
        account_prefix: the start of the accounts' code, in digits (required).
        year: the exercice's year the client names; 0 for the dossier's latest exercice.
        partner_id: only this third party's items (its number from query_data on exp.partner); 0 for all.
        limit: lines shown, 1 to 100; the report says how many exist.

    Raises:
        ToolError: the dossier is unknown or not the conversation's, ``year`` has no exercice, ``account_prefix`` is
            missing or not digits, ``partner_id`` or ``limit`` is not a whole number, or Experio can't answer."""
    name, context = opened(dossier_id, session_id)
    exercice = exercice_of(name, dossier_id, year, context)
    for value, what in ((partner_id, "partner_id"), (limit, "limit")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ToolError(f"{what} : un entier est attendu.")
    shown = min(max(limit, 1), MAX_LINES)
    extra = [*prefix_term(account_prefix, required=True), *([("partner_id", "=", partner_id)] if partner_id else [])]
    domain = domain_of(dossier_id, exercice, extra)
    log(f"account_ledger dossier {dossier_id} exercice {exercice['name']} prefix {account_prefix!r}")
    fields = ["date", "journal_id", "move_id", "name", "partner_id", "debit", "credit"]
    with refused("account_ledger", "Experio n'a pas pu lire les écritures : {reason}"):
        rows = odoo_call(
            "exp.move.line", "search_read", domain, fields=fields, order="date, id", limit=shown, context=context
        )
        total = len(rows) if len(rows) < shown else odoo_call("exp.move.line", "search_count", domain, context=context)
    lines = [heading(name, exercice), f"Comptes {account_prefix}… : {total} ligne(s)."]
    for row in rows:
        who = row["partner_id"][1] if row["partner_id"] else "sans tiers"
        journal, entry = (row[key][1] if row[key] else "?" for key in ("journal_id", "move_id"))
        lines.append(
            f"- {french_date(row['date'])} | {journal} | {entry} | {row['name'] or 'sans libellé'} | {who} | "
            f"débit {amount(row['debit'])} | crédit {amount(row['credit'])}"
        )
    if total > len(rows):
        lines.append(f"{total - len(rows)} ligne(s) de plus non affichées : précisez le compte, le tiers ou la limite.")
    return "\n".join(lines)
