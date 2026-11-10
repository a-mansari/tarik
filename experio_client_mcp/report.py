"""The accounts report of an exercice as JSON: the result so far, the month by month activity, the cash, what clients
and
suppliers still owe, the VAT, and the points d'attention by rules of this server's own, never a model's. A port of
Tarik's accounts report (engine/tools/reports/accounts.py): a rule changed there is changed here too.

The accounts are Experio's own ranges: the clients are 342 (3421, doubtful 3424, effets 3425) and the suppliers 441
(exp_account/models/exp_account.py:1494-1497); the VAT due is 4455 minus the recoverable 3455, then 4456 once declared
(liasse/models/tax_package_tva.py:24-36, declaration/models/res_company.py:51-62; a credit is carried on 3456,
:68-82); the depreciation accounts are 6191 to 6193 (liasse/models/tax_package_controle.py:2479) and what
depreciates is the classes 21 to 23 (not 239, in progress: liasse/models/tax_package_amort.py:36)."""

from experio_client_mcp.books import (
    ALL,
    DEPRECIATION,
    FIXED,
    IN_PROGRESS,
    OPENING_NOTE,
    RUBRIQUES,
    STOCK,
    TOP,
    about,
    amount,
    balance_of,
    code_of,
    domain_of,
    exercice_of,
    french_date,
    has_opening,
    name_of,
    notes_of,
    open_amounts,
    side,
    totals,
)
from experio_client_mcp.experio import experio_errors, in_dossier, odoo_call, require_dossier


def read(dossier_id, exercice, *prefixes, groupby="account_id", **more):
    """Return the debit and credit rows of the journal items of the accounts starting with ``prefixes`` for the
    exercice,
    grouped by ``groupby``, up to ALL. ``more``: orderby, since_origin, context."""
    terms = [("account_id.code", "=like", f"{prefix}%") for prefix in prefixes]
    return totals(dossier_id, exercice, ["|"] * (len(terms) - 1) + terms, [groupby], limit=ALL, **more)


def sums(rows, *prefixes):
    """Return the debit minus credit of the account rows whose code starts with any of ``prefixes``."""
    return sum(row["debit"] - row["credit"] for row in rows if code_of(row["account_id"]).startswith(prefixes))


def last_booked(dossier_id, exercice):
    """Return the date of the latest posted journal item of the exercice, "" when there is none."""
    with experio_errors("la lecture des écritures"):
        found = odoo_call("exp.move.line", "search_read", domain_of(dossier_id, exercice, []), fields=["date"],
                          order="date desc", limit=1, context=in_dossier(dossier_id))  # fmt: skip
    return found[0]["date"] if found else ""


def open_items(dossier_id, exercice, prefix, opening, order):
    """Return (the biggest TOP open third parties, the total of all of them): the biggest read ordered by amount
    (``order``:
    desc for the debtors, asc for the creditors), the total apart, so a long list is never cut by name."""
    top = open_amounts(dossier_id, exercice, prefix, opening, orderby=f"amount_residual {order}", limit=TOP)
    every = open_amounts(dossier_id, exercice, prefix, opening, groupby=(), limit=1)
    return top, sum(row.get("amount_residual") or 0 for row in every)


def read_accounts(dossier_id, year):
    """Return everything the report shows for the dossier and its exercice of ``year`` (0: the latest), read once. The
    balance-sheet accounts (cash, VAT, fixed assets, open third parties) are read from the origin when the exercice
    holds
    no opening entries."""
    name = require_dossier(dossier_id)
    exercice = exercice_of(dossier_id, year)
    opening = has_opening(dossier_id, exercice, "")
    french = {"context": {**in_dossier(dossier_id), "lang": "fr_FR"}}  # the month labels follow the context's language
    balance = {"since_origin": not opening}
    results = read(dossier_id, exercice, "6", "7")
    whole = len(results) < ALL
    return {
        "name": name, "exercice": exercice, "opening": opening, "last": last_booked(dossier_id, exercice),
        "results": results, "whole": whole,
        "months_sales": read(dossier_id, exercice, "71", groupby="date:month", orderby="date:month", **french),
        "months_charges": read(dossier_id, exercice, "61", groupby="date:month", orderby="date:month", **french),
        "cash": read(dossier_id, exercice, "51", **balance),
        "clients": open_items(dossier_id, exercice, "342", opening, "desc"),
        "suppliers": open_items(dossier_id, exercice, "441", opening, "asc"),
        "vat": read(dossier_id, exercice, "3455", "3456", "4455", "4456", **balance),
        "assets": read(dossier_id, exercice, *FIXED, **balance) if whole else [],
    }  # fmt: skip


def result_of(found):
    """Return {rows, charges, produits, gap}: the result by rubrique; None when the plan was too long to be read
    whole."""
    if not found["whole"]:
        return None
    rubriques = {}
    for row in found["results"]:
        key = code_of(row["account_id"])[:2]
        rubriques[key] = rubriques.get(key, 0.0) + row["debit"] - row["credit"]
    rows, charges, produits = [], 0.0, 0.0
    for key, net in sorted(rubriques.items()):
        label = f"{key} {RUBRIQUES.get(key, f'Comptes {key}')}"
        if key[0] == "6":
            charges += net
            rows.append({"rubrique": label, "charges": round(net, 2), "produits": None})
        else:
            produits -= net
            rows.append({"rubrique": label, "charges": None, "produits": round(-net, 2)})
    gap = round(produits - charges, 2)
    return {"rows": rows, "charges": round(charges, 2), "produits": round(produits, 2),
            "gap": {"amount": gap, "meaning": "excédent" if gap > 0 else "déficit" if gap < 0 else "nul"}}  # fmt: skip


def by_month(found):
    """Return the months with entries, oldest first, as {label, start, sales, charges} (None when the month has
    none)."""
    months = {}
    for key, rows, sign in (("sales", found["months_sales"], -1), ("charges", found["months_charges"], 1)):
        for row in rows:
            start = row["__range"]["date:month"]["from"]
            month = months.setdefault(
                start, {"label": row["date:month"], "start": start, "sales": None, "charges": None}
            )
            month[key] = round(sign * (row["debit"] - row["credit"]), 2)
    return [months[start] for start in sorted(months)]


def state_rules(found):
    """The sentences on the exercice itself: not closed, no opening entries (books.notes_of's own words)."""
    return [note for note in notes_of(found["exercice"], "", found["opening"]) if note != OPENING_NOTE]


def result_rules(found, result):
    """The sentences on the result's accounts. Nothing when the result was not read: a rule never rests on a part."""
    if not result:
        return []
    results, out = found["results"], []
    if sums(results, "611") > 0 and not any(sums(results, prefix) for prefix in STOCK):
        out.append(
            "Aucune variation de stock (6114, 713) n'est passée alors que des achats de marchandises (611) le sont : "
            "la marge brute ne peut pas être calculée."
        )
    depreciable = sums(found["assets"], *FIXED) - sums(found["assets"], IN_PROGRESS)
    if depreciable > 0 and not sums(results, *DEPRECIATION):
        out.append(
            "Aucune dotation aux amortissements (6191 à 6193) n'est passée alors que des immobilisations "
            "amortissables existent."
        )
    if result["charges"] > result["produits"]:
        out.append(
            f"Les charges dépassent les produits de {amount(result['charges'] - result['produits'])} à ce jour, "
            "avant les écritures ci-dessus."
        )
    return out


def activity_rules(found):
    """The sentence on sales that stop before the last entry (by the months' dates; a month netting to zero is no
    sales)."""
    sold = [m for m in by_month(found) if m["sales"] is not None and round(m["sales"], 2) != 0]
    if not (sold and found["last"] and sold[-1]["start"] < found["last"][:7] + "-01"):
        return []
    return [
        f"Aucune vente (71) n'est comptabilisée après {sold[-1]['label']}, alors que les écritures vont jusqu'au "
        f"{french_date(found['last'])} : saisie en retard ou activité arrêtée, Experio ne le dit pas."
    ]


def cash_rules(found):
    """The sentence on the banks in credit (accounts 514), told to check the bank reconciliation when clients are
    open."""
    overdrawn = [r for r in found["cash"] if code_of(r["account_id"]).startswith("514") and r["credit"] > r["debit"]]
    if not overdrawn:
        return []
    names = ", ".join(f"{name_of(r['account_id'], '')} ({amount(r['credit'] - r['debit'])})" for r in overdrawn)
    more = (
        "Avec des clients encore ouverts, le rapprochement bancaire est probablement à vérifier."
        if found["clients"][1] > 0
        else "À vérifier."
    )
    return [f"Des comptes bancaires sont créditeurs : {names}. {more}"]


def vat_rules(found):
    """The sentences on the VAT as Experio's own table computes it: due (4455 minus 3455), declared and not paid
    (4456)."""
    out = []
    due = -sums(found["vat"], "4455") - sums(found["vat"], "3455")
    if due > 0:
        out.append(f"TVA à payer selon les comptes (4455 moins 3455) : {amount(due)}, à rapprocher des déclarations.")
    declared = -sums(found["vat"], "4456")
    if declared > 0:
        out.append(f"TVA déclarée restant à payer (4456) : {amount(declared)}.")
    credit = sums(found["vat"], "3456")
    if credit > 0:
        out.append(f"Crédit de TVA reporté (3456) : {amount(credit)}, à rapprocher des déclarations.")
    return out


def balances(rows):
    """Return the account rows as {account, balance, side}, by account code."""
    return [
        {"account": name_of(r["account_id"], "sans compte"), "balance": balance_of(r), "side": side(balance_of(r))}
        for r in sorted(rows, key=lambda r: code_of(r["account_id"]))
    ]


def third_parties(listed):
    """Return the biggest open third parties and the total of all of them, with their sides."""
    biggest, total = listed
    return {
        "biggest": [
            {
                "partner": name_of(r["partner_id"], "sans tiers"),
                "balance": round(r["amount_residual"], 2),
                "side": side(r["amount_residual"]),
            }
            for r in biggest
        ],  # fmt: skip
        "total": {"balance": round(total, 2), "side": side(total)},
    }


TOO_LONG = "Le plan comptable est trop long pour être lu en entier : le résultat n'est pas donné."
REPORT_NOTES = [
    "Chiffres lus dans Experio, dans la devise du dossier. Les points d'attention sont une lecture automatique de ces "
    "chiffres, à valider : ce n'est ni un bilan ni un conseil.",
    "Les soldes des tiers suivent le lettrage d'aujourd'hui.",
]


def accounts_report(dossier_id, year=0):
    """The accounts of an exercice: the result by rubrique, the month by month sales and charges, the cash, the biggest
    open clients and suppliers with the total of all, the VAT, and the points d'attention (rules of this server's
    own)."""
    found = read_accounts(dossier_id, year)
    result = result_of(found)
    attention = [
        *state_rules(found),
        *result_rules(found, result),
        *activity_rules(found),
        *cash_rules(found),
        *vat_rules(found),
    ]
    return {
        **about(found["name"], found["exercice"]),
        "last_entry": found["last"] or None,
        "result": result or TOO_LONG,
        "months": [{"month": m["label"], "sales": m["sales"], "charges": m["charges"]} for m in by_month(found)],
        "cash": balances(found["cash"]),
        "clients": third_parties(found["clients"]),
        "suppliers": third_parties(found["suppliers"]),
        "vat": balances(found["vat"]),
        "points_d_attention": attention or ["Rien à signaler dans ces chiffres."],
        "notes": REPORT_NOTES,
    }
