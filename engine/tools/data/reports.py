"""Curated reads of a dossier's books for an exercice: the figures an accountant is asked for, said the way an
accountant says them (débiteur, créditeur), so the agent never works out a sign, an exercice or a caveat itself.

They share query_data's one read path: the same gate (opened: the dossier proven, the conversation allowed, Experio's
context), the same journal items (the registry's exp.move.line, posted and exported, closure entries left out) and the
same read-only calls. The exercice is resolved as dossier_progress does (exercices_of): ``year`` names it, 0 is the
latest one."""

import re

from engine.tools.core.app import ToolError, log, tool
from engine.tools.core.experio import odoo_call, refused
from engine.tools.data.query import MAX_CHARS, an_id, limit_of, opened
from engine.tools.data.registry import MODELS
from engine.tools.dossiers.progress import exercices_of, french_date
from engine.tools.invoices.listing import amount

LINES = MODELS["exp.move.line"]
STATES = MODELS["exp.fiscal.entries"].columns["state"].values
PREFIX = re.compile(r"[0-9]{1,8}")
MAX_GROUPS, MAX_LINES = 150, 100  # accounts or third parties read; lines shown
BALANCE_SHEET = ("", "1", "2", "3", "4", "5")  # first digits of the accounts that need an opening
OPEN_NOTE = (
    "Exercice non clôturé : ce sont les écritures passées à ce jour ; stock, amortissements, provisions et impôt "
    "peuvent rester à passer, ce n'est pas un résultat définitif."
)
NO_OPENING_NOTE = (
    "Aucune écriture d'ouverture (à-nouveaux) dans cet exercice : les comptes 1 à 5 ne montrent que les mouvements de "
    "l'exercice, sans l'ouverture (normal pour le premier exercice du dossier, ou tant que l'exercice précédent n'est "
    "pas clôturé) ; un solde de bilan se cumule depuis l'origine."
)
OPENING_NOTE = (
    "Le débit et le crédit comprennent les écritures d'ouverture (à-nouveaux) ; seul le solde correspond au cumul de "
    "la balance d'Experio."
)
CUT_NOTE = (
    f"Liste coupée à {MAX_GROUPS}, dans l'ordre des comptes ou des tiers et non des montants : ni total ni classement "
    "complet. Précisez account_prefix."
)


def side(balance):
    """Return a balance (debit minus credit) as « débiteur 1 234.00 », « créditeur 50.00 » or « soldé »."""
    balance = round(balance, 2)
    if not balance:
        return "soldé"
    return f"{'débiteur' if balance > 0 else 'créditeur'} {amount(abs(balance))}"


def figures(row):
    """Return a row's debit, credit and balance said the way the reports say them."""
    return f"débit {amount(row['debit'])}, crédit {amount(row['credit'])}, solde {side(row['debit'] - row['credit'])}"


def name_of(pair, default):
    """Return the display name of an Experio many2one value [id, name], ``default`` when it is unset (false)."""
    return pair[1] if pair else default


def prefix_term(prefix, required=False, minimum=1):
    """Return the domain terms keeping the accounts whose code starts with ``prefix`` (digits, e.g. "6", "3421");
    none for an empty one unless ``required``. Raises ToolError for anything but ``minimum`` to 8 digits."""
    prefix = str(prefix or "").strip()
    if not prefix and not required:
        return []
    if not PREFIX.fullmatch(prefix) or len(prefix) < minimum:
        more = f" (au moins {minimum} chiffres)" if minimum > 1 else ""
        raise ToolError(
            f"account_prefix : le début d'un code de compte, en chiffres{more} (6 charges, 7 produits, 3421)."
        )
    return [("account_id.code", "=like", f"{prefix}%")]


def exercice_of(name, dossier_id, year, context):
    """Return the dossier's exercice of ``year`` (int), else its latest, as {name, date_start, date_end, state}.
    Raises ToolError when ``year`` is not a year or the dossier has no such exercice."""
    if year and (not an_id(year) or year > 2100):
        raise ToolError("year : une année sur quatre chiffres, ou 0 pour le dernier exercice.")
    with refused("exercice_of", f"Experio n'a pas pu lire les exercices du dossier `{name}` : {{reason}}"):
        found = exercices_of([dossier_id], year, context).get(dossier_id)
    if not found:
        raise ToolError(f"Le dossier `{name}` n'a pas d'exercice{f' {year}' if year else ''} dans Experio.")
    return found


def start(tool_name, dossier_id, session_id, year, prefix, required=True, minimum=1):
    """Return (the dossier's name, the context of its reads, its exercice, the prefix's domain terms): the start of
    every report. Raises ToolError as opened, exercice_of and prefix_term do."""
    name, context = opened(dossier_id, session_id)
    exercice = exercice_of(name, dossier_id, year, context)
    terms = prefix_term(prefix, required, minimum)
    log(f"{tool_name} dossier {dossier_id} exercice {exercice['name']} prefix {prefix!r}")
    return name, context, exercice, terms


def heading(name, exercice):
    """Return the first line of a report: the dossier, the exercice, its dates and state."""
    start_, end = french_date(exercice["date_start"]), french_date(exercice["date_end"])
    state = STATES.get(exercice["state"], exercice["state"])
    return f"Dossier `{name}`, {exercice['name']} du {start_} au {end} ({state})."


def domain_of(dossier_id, exercice, extra, since_origin=False):
    """Return the journal items' domain: the dossier's posted items of the exercice (up to its end only when
    ``since_origin``), then ``extra``."""
    dates = [("date", "<=", exercice["date_end"])]
    if not since_origin:
        dates.append(("date", ">=", exercice["date_start"]))
    return [("company_id", "=", dossier_id), *LINES.base, *dates, *extra]


def totals(dossier_id, exercice, context, extra, groupby, values=("debit", "credit"), **more):
    """Return the sums of ``values`` (journal item fields) of the items grouped by ``groupby`` (Experio's read_group
    rows), at most MAX_GROUPS + 1 unless ``more`` gives a ``limit``. ``more``: orderby, since_origin, limit."""
    since_origin = more.pop("since_origin", False)
    limit = more.pop("limit", MAX_GROUPS + 1)
    with refused("reports totals", "Experio n'a pas pu lire les écritures : {reason}"):
        return odoo_call(
            "exp.move.line",
            "read_group",
            domain_of(dossier_id, exercice, extra, since_origin),
            [f"{value}:sum" for value in values],
            groupby,
            lazy=False,
            limit=limit,
            context=context,
            **more,
        )


def open_amounts(dossier_id, exercice, context, prefix, opening, groupby=("partner_id",), **more):
    """Return the open amounts (Experio's residual of the items not fully reconciled) of the accounts starting with
    ``prefix``, as read_group rows grouped by ``groupby`` (none: one row for the whole): from the exercice's start when
    it holds opening entries (``opening``), else from the origin. The one rule of partner_balances and of the accounts
    report. ``more``: orderby, limit."""
    extra = [("account_id.code", "=like", f"{prefix}%"), ("amount_residual", "!=", 0)]
    return totals(
        dossier_id, exercice, context, extra, list(groupby), ("amount_residual",), since_origin=not opening, **more
    )


def has_opening(dossier_id, exercice, context, prefix):
    """Return whether the exercice holds opening (carry_forward) entries, as Experio's balance report tells them
    (exp_report_balance.py:660-663); False without asking Experio for accounts that never have one (6, 7)."""
    if prefix[:1] not in BALANCE_SHEET:
        return False
    domain = domain_of(dossier_id, exercice, [("move_id.move_category", "=", "carry_forward")])
    with refused("reports opening", "Experio n'a pas pu lire les écritures : {reason}"):
        return bool(odoo_call("exp.move.line", "search", domain, limit=1, context=context))


def notes_of(exercice, prefix, opening):
    """Return the caveats of a report on accounts starting with ``prefix`` ("" for all): an exercice not closed for the
    result accounts; for the balance-sheet ones, the opening entries counted in the debit and credit, or missing."""
    notes = []
    if exercice["state"] not in ("close", "shut") and prefix[:1] in ("", "6", "7"):
        notes.append(OPEN_NOTE)
    if prefix[:1] in BALANCE_SHEET:
        notes.append(OPENING_NOTE if opening else NO_OPENING_NOTE)
    return notes


def code_of(account):
    """Return an account's code from its Experio display name « 61110000 Achats »."""
    return name_of(account, "sans compte").split(" ")[0]


def said(lines):
    """Return the report's ``lines`` as text. Raises ToolError when it is longer than MAX_CHARS: it would sit in the
    conversation for every later turn."""
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        raise ToolError("Rapport trop long : précisez account_prefix.")
    return text


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
    depreciation, provisions and tax may be to book), the debit and credit include the opening entries (only the
    balance matches Experio's cumulative balance), and without opening entries the balance-sheet accounts show the
    exercice's movements only. Never call the difference of produits and charges a profit or a loss without saying so.
    A list of more than 150 accounts is cut in the order of the codes: it then gives no total.

    Raises:
        ToolError: the dossier is unknown or not the conversation's, ``year`` has no exercice, ``account_prefix`` is
            not digits, or Experio can't answer."""
    name, context, exercice, terms = start("trial_balance", dossier_id, session_id, year, account_prefix, False)
    opening = has_opening(dossier_id, exercice, context, account_prefix)
    rows = totals(dossier_id, exercice, context, terms, ["account_id"])
    lines = [heading(name, exercice)]
    if not rows:
        lines.append("Aucune écriture comptabilisée pour ces comptes.")
    else:
        shown = sorted(rows[:MAX_GROUPS], key=lambda row: code_of(row["account_id"]))
        lines += [f"- {name_of(row['account_id'], 'sans compte')} : {figures(row)}" for row in shown]
        if len(rows) > MAX_GROUPS:
            lines.append(CUT_NOTE)
        else:
            total = {"debit": sum(row["debit"] for row in shown), "credit": sum(row["credit"] for row in shown)}
            lines.append(f"Total : {figures(total)}.")
    return said([*lines, *notes_of(exercice, account_prefix, opening)])


@tool
def monthly_totals(dossier_id: int, session_id: str, account_prefix: str, year: int = 0) -> str:
    """Return, as French text, the month by month debit, credit and balance of the accounts starting with
    ``account_prefix`` over an exercice (posted and exported journal items, closure entries left out): the series
    behind a trend, e.g. the sales ("71"), the purchases and charges ("61", "611" is only the goods resold), the VAT
    ("3455" récupérable, "4455" facturée, "4456" due).

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
    name, context, exercice, terms = start("monthly_totals", dossier_id, session_id, year, account_prefix)
    opening = has_opening(dossier_id, exercice, context, account_prefix)
    # French month labels whatever the language of Tarik's Experio user (they follow the context's)
    rows = totals(dossier_id, exercice, {**context, "lang": "fr_FR"}, terms, ["date:month"], orderby="date:month")
    lines = [heading(name, exercice), f"Comptes {account_prefix}…, mois par mois :"]
    if not rows:
        lines.append("Aucune écriture comptabilisée pour ces comptes.")
    lines += [f"- {row['date:month']} : {figures(row)}" for row in rows]
    return said([*lines, *notes_of(exercice, account_prefix, opening)])


@tool
def partner_balances(dossier_id: int, session_id: str, account_prefix: str, year: int = 0) -> str:
    """Return, as French text, what is still open per third party on the accounts starting with ``account_prefix``
    (« 3421 » clients, « 4411 » fournisseurs, at least 3 digits): each third party's open amount (Experio's own
    residual of the items not fully reconciled, a part payment counted), the biggest first, then the total. Items are
    taken from the start of the exercice when it holds opening entries, else from the origin.

    The amounts follow today's reconciliation, up to the end of the exercice named: for a past exercice they are
    what is still unmatched now, not what was open at its end. Not the invoices' residual amounts: when the two
    differ, give both and say where each comes from. A list of more than 150 third parties is cut in the order of the
    names: it then gives no total nor complete ranking.

    Args:
        dossier_id: the dossier (from find_dossier).
        session_id: the conversation (filled in by Tarik).
        account_prefix: the start of the third-party accounts' code, in at least 3 digits (required).
        year: the exercice's year the client names; 0 for the dossier's latest exercice.

    Raises:
        ToolError: the dossier is unknown or not the conversation's, ``year`` has no exercice, ``account_prefix`` is
            missing or too short, or Experio can't answer."""
    name, context, exercice, _ = start("partner_balances", dossier_id, session_id, year, account_prefix, True, 3)
    opening = has_opening(dossier_id, exercice, context, account_prefix)
    rows = open_amounts(dossier_id, exercice, context, account_prefix, opening)
    lines = [
        heading(name, exercice),
        f"Comptes {account_prefix}…, éléments non lettrés à ce jour, "
        f"écritures jusqu'au {french_date(exercice['date_end'])} :",
    ]
    if not rows:
        lines.append("Aucun solde ouvert.")
    else:
        shown = sorted(rows[:MAX_GROUPS], key=lambda row: -abs(row["amount_residual"]))
        lines += [f"- {name_of(row['partner_id'], 'sans tiers')} : {side(row['amount_residual'])}" for row in shown]
        lines.append(
            CUT_NOTE if len(rows) > MAX_GROUPS else f"Total : {side(sum(r['amount_residual'] for r in shown))}."
        )
    lines.append(
        "Éléments pris depuis le début de l'exercice, ouverture comprise."
        if opening
        else "Aucune écriture d'ouverture dans l'exercice : éléments pris depuis l'origine."
    )
    return said(lines)


@tool
def account_ledger(
    dossier_id: int, session_id: str, account_prefix: str, year: int = 0, partner_id: int = 0, limit: int = 50
) -> str:
    """Return, as French text, the journal items (grand livre) of the accounts starting with ``account_prefix`` over
    an exercice, oldest first: date, journal, entry, label, third party, debit, credit. Posted and exported items,
    closure entries left out. The first lines are the oldest of the exercice: say so when the list is cut.

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
    name, context, exercice, terms = start("account_ledger", dossier_id, session_id, year, account_prefix)
    if partner_id and not an_id(partner_id):
        raise ToolError("partner_id : un numéro entier positif est attendu, ou 0 pour tous les tiers.")
    shown = min(limit_of(limit), MAX_LINES)
    domain = domain_of(dossier_id, exercice, [*terms, *([("partner_id", "=", partner_id)] if partner_id else [])])
    fields = ["date", "journal_id", "move_id", "name", "partner_id", "debit", "credit"]
    with refused("account_ledger", "Experio n'a pas pu lire les écritures : {reason}"):
        rows = odoo_call(
            "exp.move.line", "search_read", domain, fields=fields, order="date, id", limit=shown, context=context
        )
        total = len(rows) if len(rows) < shown else odoo_call("exp.move.line", "search_count", domain, context=context)
    lines = [heading(name, exercice), f"Comptes {account_prefix}… : {total} ligne(s)."]
    for row in rows:
        lines.append(
            f"- {french_date(row['date'])} | {name_of(row['journal_id'], '?')} | {name_of(row['move_id'], '?')} | "
            f"{row['name'] or 'sans libellé'} | {name_of(row['partner_id'], 'sans tiers')} | "
            f"débit {amount(row['debit'])} | crédit {amount(row['credit'])}"
        )
    if total > len(rows):
        lines.append(f"{total - len(rows)} ligne(s) de plus non affichées : précisez le compte, le tiers ou la limite.")
    opening = has_opening(dossier_id, exercice, context, account_prefix)
    return said([*lines, *notes_of(exercice, account_prefix, opening)])
