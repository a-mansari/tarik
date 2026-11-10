"""A dossier's books for an exercice, as an accountant says them: trial balance, month by month, what clients and
suppliers still owe, the ledger of an account, and the accounts report with its points d'attention (rules of this
server's own, never a model's). Structured JSON, the caveats given as data (``notes``).

A port of Tarik's reports (engine/tools/data/reports.py, engine/tools/reports/accounts.py): a rule changed there is
changed here too. Every figure is the user's own reading of Experio: the same gate (experio.require_dossier), the
registry's
posted journal items (closure entries left out), Experio's residual for open amounts, any year (SKIP_EXERCICE)."""

import re

from experio_client_mcp import registry
from experio_client_mcp.experio import Refusal, an_id, experio_errors, in_dossier, odoo_call, require_dossier

LINES = registry.MODELS["exp.move.line"]
STATES = registry.MODELS["exp.fiscal.entries"].columns["state"].values
PREFIX = re.compile(r"[0-9]{1,8}")
MAX_GROUPS, MAX_LINES, ALL, TOP = (
    150,
    100,
    500,
    5,
)  # groups read; ledger lines shown; plan read for the result; third parties listed
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
    f"Liste coupée à {MAX_GROUPS}, dans l'ordre des comptes ou des tiers et non des montants : ni total ni "
    "classement complet. Précisez account_prefix."
)
TODAY_NOTE = "Les montants des tiers suivent le lettrage d'aujourd'hui, même pour un exercice passé."
# The CGNC's own titles of the result accounts (Tarik's engine/knowledge/cgnc/accounts.jsonl).
RUBRIQUES = {
    "61": "Charges d'exploitation",
    "63": "Charges financières",
    "65": "Charges non courantes",
    "67": "Impôts sur les résultats",
    "71": "Produits d'exploitation",
    "73": "Produits financiers",
    "75": "Produits non courants",
}
STOCK = ("6114", "713")  # variation of the stocks (goods; work in progress and finished goods)
DEPRECIATION = "619"  # operating depreciation: 6191 non-valeurs, 6192 intangible, 6193 tangible
FIXED = ("21", "22", "23")  # the fixed assets that depreciate (the financial ones, 24, never do)


def amount(value):
    return f"{float(value or 0):,.2f}".replace(",", " ")


def side(balance):
    """Return a balance (debit minus credit) as « débiteur », « créditeur » or « soldé »."""
    balance = round(balance, 2)
    return "soldé" if not balance else ("débiteur" if balance > 0 else "créditeur")


def balance_of(row):
    return round(row["debit"] - row["credit"], 2)


def name_of(pair, default):
    """Return the display name of an Experio many2one value [id, name], ``default`` when it is unset (false)."""
    return pair[1] if pair else default


def code_of(account):
    """Return an account's code from its Experio display name « 61110000 Achats »."""
    return name_of(account, "sans compte").split(" ")[0]


def french_date(day):
    return f"{day[8:10]}/{day[5:7]}/{day[:4]}"


def prefix_term(prefix, required=False, minimum=1):
    """Return the domain terms keeping the accounts whose code starts with ``prefix`` (ASCII digits), none for an empty
    one unless ``required``. Raises Refusal for anything but ``minimum`` to 8 digits."""
    prefix = str(prefix or "").strip()
    if not prefix and not required:
        return []
    if not PREFIX.fullmatch(prefix) or len(prefix) < minimum:
        more = f" (au moins {minimum} chiffres)" if minimum > 1 else ""
        raise Refusal(f"account_prefix : le début d'un code de compte, en chiffres{more} (6 charges, 7 produits, 342).")
    return [("account_id.code", "=like", f"{prefix}%")]


def exercice_of(name, dossier_id, year):
    """Return the dossier's exercice of ``year``, else its latest, as {name, date_start, date_end, state}. Raises
    Refusal
    when ``year`` is not a year or the dossier has no such exercice."""
    if year and (not an_id(year) or year > 2100):
        raise Refusal("year : une année sur quatre chiffres, ou 0 pour le dernier exercice.")
    domain = [("company_id", "=", dossier_id)]
    if year:
        domain += [("date_start", "<=", f"{year}-12-31"), ("date_end", ">=", f"{year}-01-01")]
    with experio_errors("la lecture des exercices"):
        found = odoo_call(
            "exp.fiscal.entries",
            "search_read",
            domain,
            fields=["name", "date_start", "date_end", "state"],
            order="date_end desc",
            limit=1,
            context=in_dossier(dossier_id),
        )
    if not found:
        raise Refusal(f"Le dossier n'a pas d'exercice{f' {year}' if year else ''} dans Experio.")
    return found[0]


def start(dossier_id, year, prefix, required=True, minimum=1):
    """Return (the dossier's name, its exercice, the prefix's domain terms): the start of every report. Raises Refusal
    as require_dossier, exercice_of and prefix_term do."""
    name = require_dossier(dossier_id)
    return name, exercice_of(name, dossier_id, year), prefix_term(prefix, required, minimum)


def about(name, exercice):
    """Return the dossier and the exercice as a report names them."""
    return {
        "dossier": name,
        "exercice": {
            "name": exercice["name"],
            "from": exercice["date_start"],
            "to": exercice["date_end"],
            "state": exercice["state"],
            "state_meaning": STATES.get(exercice["state"], exercice["state"]),
        },
    }


def domain_of(dossier_id, exercice, extra, since_origin=False):
    """Return the journal items' domain: the dossier's posted items of the exercice (up to its end only when
    ``since_origin``), then ``extra``."""
    dates = [("date", "<=", exercice["date_end"])]
    if not since_origin:
        dates.append(("date", ">=", exercice["date_start"]))
    return [("company_id", "=", dossier_id), *LINES.base, *dates, *extra]


def totals(dossier_id, exercice, extra, groupby, values=("debit", "credit"), context=None, **more):
    """Return the sums of ``values`` of the journal items grouped by ``groupby`` (Experio's read_group rows), at most
    MAX_GROUPS + 1 unless ``more`` gives a ``limit``. ``more``: orderby, since_origin, limit."""
    since_origin = more.pop("since_origin", False)
    limit = more.pop("limit", MAX_GROUPS + 1)
    with experio_errors("la lecture des écritures"):
        return odoo_call(
            "exp.move.line",
            "read_group",
            domain_of(dossier_id, exercice, extra, since_origin),
            [f"{value}:sum" for value in values],
            groupby,
            lazy=False,
            limit=limit,
            context=context or in_dossier(dossier_id),
            **more,
        )


def open_amounts(dossier_id, exercice, prefix, opening, groupby=("partner_id",), **more):
    """Return the open amounts (Experio's residual of the items not fully reconciled, a part payment counted what is
    left)
    of the accounts starting with ``prefix``, grouped by ``groupby`` (none: one row for the whole): from the exercice's
    start when it holds opening entries, else from the origin."""
    extra = [("account_id.code", "=like", f"{prefix}%"), ("amount_residual", "!=", 0)]
    return totals(dossier_id, exercice, extra, list(groupby), ("amount_residual",), since_origin=not opening, **more)


def has_opening(dossier_id, exercice, prefix):
    """Return whether the exercice holds opening (carry_forward) entries, as Experio's balance report tells them
    (exp_report_balance.py:660-663); False without asking Experio for accounts that never have one (6, 7)."""
    if prefix[:1] not in BALANCE_SHEET:
        return False
    domain = domain_of(dossier_id, exercice, [("move_id.move_category", "=", "carry_forward")])
    with experio_errors("la lecture des écritures"):
        return bool(odoo_call("exp.move.line", "search", domain, limit=1, context=in_dossier(dossier_id)))


def notes_of(exercice, prefix, opening):
    """Return the caveats of a report on accounts starting with ``prefix`` ("" for all)."""
    notes = []
    if exercice["state"] not in ("close", "shut") and prefix[:1] in ("", "6", "7"):
        notes.append(OPEN_NOTE)
    if prefix[:1] in BALANCE_SHEET:
        notes.append(OPENING_NOTE if opening else NO_OPENING_NOTE)
    return notes


def trial_balance(dossier_id, year=0, account_prefix=""):
    """A dossier's trial balance for an exercice: each account's debit, credit and balance (debit minus credit, with its
    side), then the totals, from the posted items, closure entries left out. A list cut at 150 accounts gives no
    total."""
    name, exercice, terms = start(dossier_id, year, account_prefix, required=False)
    opening = has_opening(dossier_id, exercice, account_prefix)
    rows = totals(dossier_id, exercice, terms, ["account_id"])
    shown = sorted(rows[:MAX_GROUPS], key=lambda row: code_of(row["account_id"]))
    cut = len(rows) > MAX_GROUPS
    out = [
        {
            "account": name_of(r["account_id"], "sans compte"),
            "debit": r["debit"],
            "credit": r["credit"],
            "balance": balance_of(r),
            "side": side(balance_of(r)),
        }
        for r in shown
    ]
    total = {"debit": round(sum(r["debit"] for r in shown), 2), "credit": round(sum(r["credit"] for r in shown), 2)}
    total["balance"] = round(total["debit"] - total["credit"], 2)
    return {**about(name, exercice), "rows": out, "total": None if cut else total, "truncated": cut,
            "notes": [*([CUT_NOTE] if cut else []), *notes_of(exercice, account_prefix, opening)]}  # fmt: skip


def monthly_totals(dossier_id, account_prefix, year=0):
    """The month by month debit, credit and balance of the accounts starting with ``account_prefix`` over an exercice;
    a month with no entry is absent."""
    name, exercice, terms = start(dossier_id, year, account_prefix)
    opening = has_opening(dossier_id, exercice, account_prefix)
    rows = totals(
        dossier_id,
        exercice,
        terms,
        ["date:month"],
        orderby="date:month",
        context={**in_dossier(dossier_id), "lang": "fr_FR"},
    )
    months = [
        {
            "month": r["date:month"],
            "debit": r["debit"],
            "credit": r["credit"],
            "balance": balance_of(r),
            "side": side(balance_of(r)),
        }
        for r in rows
    ]
    return {**about(name, exercice), "rows": months, "notes": notes_of(exercice, account_prefix, opening)}


def partner_balances(dossier_id, account_prefix, year=0):
    """What is still open per third party on the accounts starting with ``account_prefix`` (at least 3 digits), at
    the end
    of an exercice: Experio's residual of the items not fully reconciled, the biggest first, then the total."""
    name, exercice, _ = start(dossier_id, year, account_prefix, True, 3)
    opening = has_opening(dossier_id, exercice, account_prefix)
    rows = open_amounts(dossier_id, exercice, account_prefix, opening)
    shown = sorted(rows[:MAX_GROUPS], key=lambda row: -abs(row["amount_residual"]))
    cut = len(rows) > MAX_GROUPS
    out = [
        {
            "partner": name_of(r["partner_id"], "sans tiers"),
            "balance": round(r["amount_residual"], 2),
            "side": side(r["amount_residual"]),
        }
        for r in shown
    ]
    total = round(sum(r["amount_residual"] for r in shown), 2)
    how = (
        "Éléments pris depuis le début de l'exercice, ouverture comprise."
        if opening
        else "Aucune écriture d'ouverture dans l'exercice : éléments pris depuis l'origine."
    )
    return {
        **about(name, exercice),
        "up_to": exercice["date_end"],
        "rows": out,
        "total": None if cut else {"balance": total, "side": side(total)},
        "truncated": cut,
        "notes": [*([CUT_NOTE] if cut else []), how, TODAY_NOTE],
    }


def account_ledger(dossier_id, account_prefix, year=0, partner_id=0, limit=50):
    """The journal items of the accounts starting with ``account_prefix`` over an exercice, oldest first (at most
    100)."""
    name, exercice, terms = start(dossier_id, year, account_prefix)
    if partner_id and not an_id(partner_id):
        raise Refusal("partner_id : un numéro entier positif est attendu, ou 0 pour tous les tiers.")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise Refusal(f"limit : un entier de 1 à {MAX_LINES} est attendu.")
    shown = min(limit, MAX_LINES)
    domain = domain_of(dossier_id, exercice, [*terms, *([("partner_id", "=", partner_id)] if partner_id else [])])
    fields = ["date", "journal_id", "move_id", "name", "partner_id", "debit", "credit"]
    context = in_dossier(dossier_id)
    with experio_errors("la lecture des écritures"):
        rows = odoo_call(
            "exp.move.line", "search_read", domain, fields=fields, order="date, id", limit=shown, context=context
        )
        total = len(rows) if len(rows) < shown else odoo_call("exp.move.line", "search_count", domain, context=context)
    lines = [
        {
            "date": r["date"],
            "journal": name_of(r["journal_id"], "?"),
            "entry": name_of(r["move_id"], "?"),
            "label": r["name"] or "",
            "partner": name_of(r["partner_id"], "sans tiers"),
            "debit": r["debit"],
            "credit": r["credit"],
        }
        for r in rows
    ]
    opening = has_opening(dossier_id, exercice, account_prefix)
    more = (
        [f"{total - len(rows)} ligne(s) de plus non affichées : précisez le compte, le tiers ou la limite."]
        if total > len(rows)
        else []
    )
    return {
        **about(name, exercice),
        "total_lines": total,
        "rows": lines,
        "notes": [*more, *notes_of(exercice, account_prefix, opening)],
    }
