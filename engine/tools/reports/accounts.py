"""The dossier accounts report: an exercice's accounts as a document to keep and to send (Tarik_rapport, kind
"comptes"): the result so far, the month by month activity, the cash, what clients and suppliers still owe, the VAT, and
what the figures show by rules of Tarik's own, never a model's.

The figures are the books' own, read with the reports' tools (data/reports.py: the same gate, the same journal items,
the same caveats). A rule says only what the figures say, and the report says it is an automatic reading, to be
checked: Tarik states what is written in Experio, never a final result nor an advice."""

from datetime import UTC, datetime

from engine.tools.core.app import log
from engine.tools.core.experio import odoo_call, refused
from engine.tools.data.query import opened
from engine.tools.data.reports import (
    MAX_GROUPS,
    NO_OPENING_NOTE,
    OPEN_NOTE,
    code_of,
    domain_of,
    exercice_of,
    has_opening,
    name_of,
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
DEPRECIATION = "6193"  # operating depreciation of tangible assets
FIXED = ("22", "23", "24")  # intangible, tangible and financial fixed assets
TOP = 5  # third parties listed


def either(*prefixes):
    """Return the domain terms keeping the accounts starting with any of ``prefixes`` (Odoo's prefix "or")."""
    terms = [("account_id.code", "=like", f"{prefix}%") for prefix in prefixes]
    return ["|"] * (len(terms) - 1) + terms


def read(dossier_id, exercice, context, *prefixes, groupby="account_id", **more):
    """Return the debit and credit rows (Experio's read_group) of the journal items of the accounts starting with
    ``prefixes`` for the exercice, grouped by ``groupby``, up to ALL."""
    return totals(dossier_id, exercice, context, either(*prefixes), [groupby], limit=ALL, **more)


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


def open_items(dossier_id, exercice, context, prefix, opening):
    """Return (third party rows, total): the open amounts per third party on the accounts of ``prefix`` as
    partner_balances reads them, biggest of the first MAX_GROUPS first, and the total of all of them (read apart when
    the list was cut)."""
    extra = [("account_id.code", "=like", f"{prefix}%"), ("amount_residual", "!=", 0)]
    rows = totals(dossier_id, exercice, context, extra, ["partner_id"], ("amount_residual",), since_origin=not opening)
    if len(rows) > MAX_GROUPS:
        every = totals(dossier_id, exercice, context, extra, [], ("amount_residual",), since_origin=not opening)
        total = sum(row["amount_residual"] for row in every)
    else:
        total = sum(row["amount_residual"] for row in rows)
    return sorted(rows[:MAX_GROUPS], key=lambda row: -abs(row["amount_residual"])), total


def read_accounts(session_id, dossier_id, year):
    """Return everything the report shows for dossier ``dossier_id`` and its exercice of ``year`` (0: the latest), read
    once. Raises ToolError as opened and exercice_of do, or when Experio can't answer."""
    name, context = opened(dossier_id, session_id)
    exercice = exercice_of(name, dossier_id, year, context)
    log(f"accounts report dossier {dossier_id} exercice {exercice['name']}")
    opening = has_opening(dossier_id, exercice, context, "")
    french = {**context, "lang": "fr_FR"}  # the month labels follow the language of the context
    results = read(dossier_id, exercice, context, "6", "7")
    return {
        "name": name,
        "exercice": exercice,
        "opening": opening,
        "last": last_booked(dossier_id, exercice, context),
        "results": results,
        "whole": len(results) < ALL,
        "months_sales": read(dossier_id, exercice, french, "71", groupby="date:month", orderby="date:month"),
        "months_charges": read(dossier_id, exercice, french, "61", groupby="date:month", orderby="date:month"),
        "cash": read(dossier_id, exercice, context, "51"),
        "clients": open_items(dossier_id, exercice, context, "3421", opening),
        "suppliers": open_items(dossier_id, exercice, context, "4411", opening),
        "vat": read(dossier_id, exercice, context, "3455", "4455", "4456"),
        "assets": read(dossier_id, exercice, context, "2"),
    }


def result_of(found):
    """Return ((label, net, is a charge) lines, total charges, total produits), None when the plan was too long to
    be read whole."""
    if not found["whole"]:
        return None
    rubriques = {}
    for row in found["results"]:
        key = code_of(row["account_id"])[:2]
        rubriques[key] = rubriques.get(key, 0.0) + row["debit"] - row["credit"]
    lines = [
        (f"{key} {RUBRIQUES.get(key, f'Comptes {key}')}", net, key[0] == "6") for key, net in sorted(rubriques.items())
    ]
    charges = sum(net for _, net, is_charge in lines if is_charge)
    return lines, charges, -sum(net for _, net, is_charge in lines if not is_charge)


def result_table(found):
    """Return the table of the result by rubrique and the (charges, produits) totals; (None, None) when not read."""
    result = result_of(found)
    if not result:
        return None, None
    lines, charges, produits = result
    rows = [[label, amount(net), ""] if is_charge else [label, "", amount(-net)] for label, net, is_charge in lines]
    gap = round(produits - charges, 2)
    gap_said = f"{'excédent' if gap > 0 else 'déficit'} {amount(abs(gap))}" if gap else "nul"
    rows += [
        ["Total", amount(charges), amount(produits)],
        ["Écart produits moins charges, avant écritures de clôture", "", gap_said],
    ]
    table = {
        "type": "table",
        "caption": "Compte de résultat de l'exercice, à ce jour",
        "headers": ["Rubrique", "Charges", "Produits"],
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


def months_table(found):
    """Return the table of the month by month sales and charges, None when no month has an entry."""
    rows = [
        [month["label"]] + [amount(month[key]) if month[key] is not None else "" for key in ("sales", "charges")]
        for month in by_month(found)
    ]
    headers = ["Mois", "Produits d'exploitation (71)", "Charges d'exploitation (61)"]
    return {"type": "table", "caption": "Mois par mois", "headers": headers, "rows": rows} if rows else None


def balances_table(caption, rows):
    """Return the table of the account ``rows`` with their balance (« débiteur » or « créditeur »), None when empty."""
    body = [
        [name_of(row["account_id"], "sans compte"), side(row["debit"] - row["credit"])]
        for row in sorted(rows, key=lambda row: code_of(row["account_id"]))
    ]
    return {"type": "table", "caption": caption, "headers": ["Compte", "Solde"], "rows": body} if body else None


def third_party_table(caption, listed):
    """Return the table of the biggest open third parties of ``listed`` (rows, total) with the total of all; None when
    nobody is open."""
    rows, total = listed
    if not rows:
        return None
    body = [[name_of(row["partner_id"], "sans tiers"), side(row["amount_residual"])] for row in rows[:TOP]]
    body.append(["Total de tous les tiers", side(total)])
    return {"type": "table", "caption": caption, "headers": ["Tiers", "Solde ouvert"], "rows": body}


def state_rules(found):
    """Return the sentences on the exercice itself: not closed, no opening entries."""
    out = [OPEN_NOTE] if found["exercice"]["state"] not in ("close", "shut") else []
    return out + ([] if found["opening"] else [NO_OPENING_NOTE])


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
            f"Aucune dotation aux amortissements ({DEPRECIATION}) n'est passée alors que des immobilisations existent."
        )
    charges, produits = totals_
    if charges > produits:
        out.append(
            f"Les charges dépassent les produits de {amount(charges - produits)} à ce jour, avant les "
            "écritures ci-dessus."
        )
    return out


def activity_rules(found):
    """Return the sentence on sales that stop before the last entry (by the months' dates, not their labels)."""
    sold = [month for month in by_month(found) if month["sales"] is not None]
    if not (sold and found["last"] and sold[-1]["start"] < found["last"][:7] + "-01"):
        return []
    return [
        f"Aucune vente (71) n'est comptabilisée après {sold[-1]['label']}, alors que les écritures vont jusqu'au "
        f"{french_date(found['last'])} : saisie en retard ou activité arrêtée, Experio ne le dit pas."
    ]


def cash_rules(found, totals_):
    """Return the sentences on the cash and the clients: banks in credit, clients open beyond the produits."""
    out = []
    overdrawn = [
        row for row in found["cash"] if code_of(row["account_id"]).startswith("514") and row["credit"] > row["debit"]
    ]
    owed = sum(row["amount_residual"] for row in found["clients"][0] if row["amount_residual"] > 0)
    if overdrawn:
        names = ", ".join(
            f"{name_of(row['account_id'], '')} ({amount(row['credit'] - row['debit'])})" for row in overdrawn
        )
        more = (
            "Avec des clients encore ouverts, le rapprochement bancaire est probablement à vérifier."
            if owed
            else "À vérifier."
        )
        out.append(f"Des comptes bancaires sont créditeurs : {names}. {more}")
    if totals_ and totals_[1] and owed > totals_[1]:
        out.append(
            f"Les clients ouverts ({amount(owed)}) dépassent les produits de l'exercice ({amount(totals_[1])}) : "
            "les encaissements sont à vérifier."
        )
    return out


def vat_rules(found):
    """Return the sentence on the VAT collected and not paid (the 4455 account in credit)."""
    due = -sums(found["vat"], "4455")
    return [f"TVA facturée non reversée (4455) : {amount(due)}, à rapprocher des déclarations."] if due > 0 else []


def attention(found, totals_):
    """Return what the figures show, one sentence each, by Tarik's own rules (see the module's words); ``totals_`` is
    the (charges, produits) of result_table, None when the result was not read."""
    out = [
        *state_rules(found),
        *result_rules(found, totals_),
        *activity_rules(found),
        *cash_rules(found, totals_),
        *vat_rules(found),
    ]
    return out or ["Rien à signaler dans ces chiffres."]


def accounts_document(found, today=None):
    """Return the Document of a dossier's accounts from ``found`` (read_accounts), dated ``today`` (a date; now when
    not given)."""
    today = today or datetime.now(UTC).date()
    exercice = found["exercice"]
    period = f"{exercice['name']}, du {french_date(exercice['date_start'])} au {french_date(exercice['date_end'])}"
    table, totals_ = result_table(found)
    last = f" ; écritures jusqu'au {french_date(found['last'])}" if found["last"] else ""
    blocks = [{"type": "heading", "level": 1, "text": "Le résultat à ce jour"}]
    if table:
        blocks.append(table)
    else:
        blocks.append(
            {
                "type": "paragraph",
                "text": "Le plan comptable est trop long pour être lu en entier : le résultat n'est pas donné.",
            }
        )
    for title, block in (
        ("Activité", months_table(found)),
        ("Trésorerie", balances_table("Trésorerie à ce jour (comptes 51)", found["cash"])),
        ("Clients", third_party_table("Clients : soldes ouverts (3421)", found["clients"])),
        ("Fournisseurs", third_party_table("Fournisseurs : soldes ouverts (4411)", found["suppliers"])),
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
