"""The dossier accounts report: an exercice's accounts as a document to keep and to send (Tarik_rapport, kind
"comptes"): the result so far, the month by month activity, the cash, what clients and suppliers still owe, the VAT, and
what the figures show by rules of Tarik's own, never a model's.

The figures are the books' own, read with the reports' tools (data/reports.py: the same gate, the same journal items,
the same open-amount rule, the same caveats). A rule says only what the figures say, and the report says it is an
automatic reading, to be checked: Tarik states what is written in Experio, never a final result nor an advice.

The accounts of Experio's own ranges: the clients are the accounts 342 (3421, doubtful 3424, effets 3425) and the
suppliers 441 (exp_account/models/exp_account.py:1494-1497); the VAT due is 4455 minus the recoverable 3455, then 4456
once declared (liasse/models/tax_package_tva.py:24-36, declaration/models/res_company.py:51-62); the depreciation
accounts are 6191 to 6193 (liasse/models/tax_package_controle.py:2479) and what depreciates the classes 21 to 23 (the
financial assets, 24, never do)."""

from datetime import UTC, datetime

from engine.tools.core.app import log
from engine.tools.core.experio import odoo_call, refused
from engine.tools.data.query import opened
from engine.tools.data.reports import (
    OPENING_NOTE,
    code_of,
    domain_of,
    exercice_of,
    has_opening,
    name_of,
    notes_of,
    open_amounts,
    side,
    totals,
)
from engine.tools.dossiers.progress import french_date
from engine.tools.invoices.listing import amount
from engine.tools.reports.document import parse

ALL = 500  # accounts read for the result: a plan this long is not read, the result is not given
# The CGNC's own titles of the result accounts (engine/knowledge/cgnc/accounts.jsonl).
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
FIXED = ("21", "22", "23")  # the fixed assets that depreciate
TOP = 5  # third parties listed


def read(dossier_id, exercice, context, *prefixes, groupby="account_id", **more):
    """Return the debit and credit rows (Experio's read_group) of the journal items of the accounts starting with
    ``prefixes`` for the exercice, grouped by ``groupby``, up to ALL. ``more``: orderby, since_origin."""
    terms = [("account_id.code", "=like", f"{prefix}%") for prefix in prefixes]
    return totals(dossier_id, exercice, context, ["|"] * (len(terms) - 1) + terms, [groupby], limit=ALL, **more)


def sums(rows, *prefixes):
    """Return the debit minus credit of the account rows whose code starts with any of ``prefixes``."""
    return sum(row["debit"] - row["credit"] for row in rows if code_of(row["account_id"]).startswith(prefixes))


def last_booked(dossier_id, exercice, context):
    """Return the date of the latest posted journal item of the exercice, "" when there is none."""
    with refused("accounts last_booked", "Experio n'a pas pu lire les écritures : {reason}"):
        found = odoo_call(
            "exp.move.line",
            "search_read",
            domain_of(dossier_id, exercice, []),
            fields=["date"],
            order="date desc",
            limit=1,
            context=context,
        )
    return found[0]["date"] if found else ""


def open_items(dossier_id, exercice, context, prefix, opening, order):
    """Return (the biggest TOP open third parties, the total of all of them) of the accounts starting with ``prefix``:
    the biggest read ordered by amount (``order``: "desc" for the debtors, "asc" for the creditors), the total apart,
    so a long list is never cut by name."""
    top = open_amounts(dossier_id, exercice, context, prefix, opening, orderby=f"amount_residual {order}", limit=TOP)
    every = open_amounts(dossier_id, exercice, context, prefix, opening, groupby=(), limit=1)
    return top, sum(row.get("amount_residual") or 0 for row in every)


def read_accounts(session_id, dossier_id, year):
    """Return everything the report shows for dossier ``dossier_id`` and its exercice of ``year`` (0: the latest), read
    once. The balance-sheet accounts (cash, VAT, fixed assets, open third parties) are read from the origin when the
    exercice holds no opening entries, as the open amounts are. Raises ToolError as opened and exercice_of do, or when
    Experio can't answer."""
    name, context = opened(dossier_id, session_id)
    exercice = exercice_of(name, dossier_id, year, context)
    log(f"accounts report dossier {dossier_id} exercice {exercice['name']}")
    opening = has_opening(dossier_id, exercice, context, "")
    french = {**context, "lang": "fr_FR"}  # the month labels follow the language of the context
    balance = {"since_origin": not opening}
    results = read(dossier_id, exercice, context, "6", "7")
    whole = len(results) < ALL
    return {
        "name": name,
        "exercice": exercice,
        "opening": opening,
        "last": last_booked(dossier_id, exercice, context),
        "results": results,
        "whole": whole,
        "months_sales": read(dossier_id, exercice, french, "71", groupby="date:month", orderby="date:month"),
        "months_charges": read(dossier_id, exercice, french, "61", groupby="date:month", orderby="date:month"),
        "cash": read(dossier_id, exercice, context, "51", **balance),
        "clients": open_items(dossier_id, exercice, context, "342", opening, "desc"),
        "suppliers": open_items(dossier_id, exercice, context, "441", opening, "asc"),
        "vat": read(dossier_id, exercice, context, "3455", "4455", "4456", **balance),
        "assets": read(dossier_id, exercice, context, *FIXED, **balance) if whole else [],
    }


def result_table(found):
    """Return the table of the result by rubrique and the (charges, produits) totals; (None, None) when the plan was
    too long to be read whole."""
    if not found["whole"]:
        return None, None
    rubriques = {}
    for row in found["results"]:
        key = code_of(row["account_id"])[:2]
        rubriques[key] = rubriques.get(key, 0.0) + row["debit"] - row["credit"]
    rows, charges, produits = [], 0.0, 0.0
    for key, net in sorted(rubriques.items()):
        label = f"{key} {RUBRIQUES.get(key, f'Comptes {key}')}"
        if key[0] == "6":
            charges += net
            rows.append([label, amount(net), ""])
        else:
            produits -= net
            rows.append([label, "", amount(-net)])
    gap = round(produits - charges, 2)
    gap_said = f"{'excédent' if gap > 0 else 'déficit'} {amount(abs(gap))}" if gap else "nul"
    rows += [
        ["Total", amount(charges), amount(produits)],
        ["Écart produits moins charges, avant écritures de clôture", "", gap_said],
    ]
    headers = ["Rubrique", "Charges", "Produits"]
    table = {
        "type": "table",
        "caption": "Compte de résultat de l'exercice, à ce jour",
        "headers": headers,
        "rows": rows,
    }
    return table, (charges, produits)


def by_month(found):
    """Return the months with entries, oldest first, as {label, start, sales, charges} (sales and charges None when the
    month has none): sales are produits d'exploitation (71), charges the charges d'exploitation (61)."""
    months = {}
    for key, rows, sign in (("sales", found["months_sales"], -1), ("charges", found["months_charges"], 1)):
        for row in rows:
            start = row["__range"]["date:month"]["from"]
            month = months.setdefault(
                start, {"label": row["date:month"], "start": start, "sales": None, "charges": None}
            )
            month[key] = sign * (row["debit"] - row["credit"])
    return [months[start] for start in sorted(months)]


def table(caption, headers, body):
    """Return a table block of ``caption``, ``headers`` and ``body`` rows, None when there is no row."""
    return {"type": "table", "caption": caption, "headers": headers, "rows": body} if body else None


def months_table(found):
    """Return the table of the month by month sales and charges, None when no month has an entry."""
    rows = [
        [month["label"]] + [amount(month[key]) if month[key] is not None else "" for key in ("sales", "charges")]
        for month in by_month(found)
    ]
    return table("Mois par mois", ["Mois", "Produits d'exploitation (71)", "Charges d'exploitation (61)"], rows)


def balances_table(caption, rows):
    """Return the table of the account ``rows`` with their balance (« débiteur » or « créditeur »), None when empty."""
    body = [
        [name_of(row["account_id"], "sans compte"), side(row["debit"] - row["credit"])]
        for row in sorted(rows, key=lambda row: code_of(row["account_id"]))
    ]
    return table(caption, ["Compte", "Solde"], body)


def third_party_table(caption, listed):
    """Return the table of the biggest open third parties of ``listed`` (the biggest, the total of all), None when
    nobody is open."""
    rows, total = listed
    body = [[name_of(row["partner_id"], "sans tiers"), side(row["amount_residual"])] for row in rows]
    return table(caption, ["Tiers", "Solde ouvert"], [*body, ["Total de tous les tiers", side(total)]] if rows else [])


def state_rules(found):
    """Return the sentences on the exercice itself: not closed, no opening entries (reports.notes_of's own words)."""
    return [note for note in notes_of(found["exercice"], "", found["opening"]) if note != OPENING_NOTE]


def result_rules(found, totals_):
    """Return the sentences on the result's accounts: no stock variation, no depreciation, charges above produits.
    Nothing when the result was not read (``totals_`` None): a rule never rests on a part of the plan."""
    if not totals_:
        return []
    results, out = found["results"], []
    if sums(results, "611") > 0 and not any(sums(results, prefix) for prefix in STOCK):
        out.append(
            "Aucune variation de stock (6114, 713) n'est passée alors que des achats de marchandises (611) le "
            "sont : la marge brute ne peut pas être calculée."
        )
    if sums(found["assets"], *FIXED) > 0 and not sums(results, DEPRECIATION):
        out.append(
            "Aucune dotation aux amortissements (6191 à 6193) n'est passée alors que des immobilisations "
            "amortissables existent."
        )
    charges, produits = totals_
    if charges > produits:
        out.append(
            f"Les charges dépassent les produits de {amount(charges - produits)} à ce jour, avant les "
            "écritures ci-dessus."
        )
    return out


def activity_rules(found):
    """Return the sentence on sales that stop before the last entry (by the months' dates, not their labels; a month
    whose sales net to zero is no sales month)."""
    sold = [month for month in by_month(found) if month["sales"] is not None and round(month["sales"], 2) != 0]
    if not (sold and found["last"] and sold[-1]["start"] < found["last"][:7] + "-01"):
        return []
    return [
        f"Aucune vente (71) n'est comptabilisée après {sold[-1]['label']}, alors que les écritures vont jusqu'au "
        f"{french_date(found['last'])} : saisie en retard ou activité arrêtée, Experio ne le dit pas."
    ]


def cash_rules(found):
    """Return the sentence on the banks in credit (accounts 514), told to check the bank reconciliation when clients
    are still open (their total, whole)."""
    overdrawn = [
        row for row in found["cash"] if code_of(row["account_id"]).startswith("514") and row["credit"] > row["debit"]
    ]
    if not overdrawn:
        return []
    names = ", ".join(f"{name_of(row['account_id'], '')} ({amount(row['credit'] - row['debit'])})" for row in overdrawn)
    more = (
        "Avec des clients encore ouverts, le rapprochement bancaire est probablement à vérifier."
        if found["clients"][1] > 0
        else "À vérifier."
    )
    return [f"Des comptes bancaires sont créditeurs : {names}. {more}"]


def vat_rules(found):
    """Return the sentences on the VAT as Experio's own table computes it: the due amount (4455 collected minus 3455
    recoverable), and what is declared and not yet paid (4456)."""
    out = []
    due = -sums(found["vat"], "4455") - sums(found["vat"], "3455")
    if due > 0:
        out.append(f"TVA à payer selon les comptes (4455 moins 3455) : {amount(due)}, à rapprocher des déclarations.")
    declared = -sums(found["vat"], "4456")
    if declared > 0:
        out.append(f"TVA déclarée restant à payer (4456) : {amount(declared)}.")
    return out


def attention(found, totals_):
    """Return what the figures show, one sentence each, by Tarik's own rules (see the module's words); ``totals_`` is
    the (charges, produits) of result_table, None when the result was not read."""
    out = [
        *state_rules(found),
        *result_rules(found, totals_),
        *activity_rules(found),
        *cash_rules(found),
        *vat_rules(found),
    ]
    return out or ["Rien à signaler dans ces chiffres."]


def accounts_document(found, today=None):
    """Return the Document of a dossier's accounts from ``found`` (read_accounts), dated ``today`` (a date; now when
    not given)."""
    today = today or datetime.now(UTC).date()
    exercice = found["exercice"]
    period = f"{exercice['name']}, du {french_date(exercice['date_start'])} au {french_date(exercice['date_end'])}"
    result, totals_ = result_table(found)
    last = f" ; écritures jusqu'au {french_date(found['last'])}" if found["last"] else ""
    too_long = "Le plan comptable est trop long pour être lu en entier : le résultat n'est pas donné."
    blocks = [{"type": "heading", "level": 1, "text": "Le résultat à ce jour"}]
    blocks.append(result or {"type": "paragraph", "text": too_long})
    for title, block in (
        ("Activité", months_table(found)),
        ("Trésorerie", balances_table("Trésorerie à ce jour (comptes 51)", found["cash"])),
        ("Clients", third_party_table("Clients : plus gros soldes ouverts (342)", found["clients"])),
        ("Fournisseurs", third_party_table("Fournisseurs : plus gros soldes ouverts (441)", found["suppliers"])),
        ("TVA", balances_table("TVA à ce jour (3455, 4455, 4456)", found["vat"])),
    ):
        if block:
            blocks += [{"type": "heading", "level": 1, "text": title}, block]
    blocks += [
        {"type": "heading", "level": 1, "text": "Points d'attention"},
        {"type": "bullets", "items": attention(found, totals_)},
        {
            "type": "note",
            "text": f"Chiffres lus dans Experio le {today:%d/%m/%Y}{last}, dans la devise du dossier. Les points "
            "d'attention sont une lecture automatique de ces chiffres, à valider : ce n'est ni un bilan ni un conseil. "
            "Les soldes des tiers suivent le lettrage d'aujourd'hui.",
        },
    ]
    return parse(f"Comptes du dossier {found['name']}", blocks, period)


def comptes(session_id, dossier_id, year):
    """Return the accounts report Document of dossier ``dossier_id`` for the exercice of ``year`` (0: the latest). The
    conversation must be allowed to read the dossier (query.opened)."""
    return accounts_document(read_accounts(session_id, dossier_id, year))
