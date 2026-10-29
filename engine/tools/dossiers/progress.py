"""Where a dossier stands, as Experio counts it: its progress, then for an exercice its invoices, bank and TPE
statements, their operations and the operations in suspense, and its declarations of the year, each by Experio's own
status (French labels of its selections, invoice/i18n, bank/i18n, declaration/i18n fr.po).

The progress is Experio's own « avancement », computed as its manager dashboard does (dashboard_manager
_PIECES_MODELS, _OPERATIONS_MODELS, _grouped_booked_totals, _dossier_progress): pieces (invoices and receipts) and
operations (bank, cash and TPE lines) posted or exported over those not cancelled, every exercice, averaged over the
sides that have any.

Many dossiers are read at once (progress_of_many): each Experio call covers them all (allowed_company_ids), its counts
grouped by dossier. Experio's read_group keeps only the selected exercice's posted rows, the first allowed dossier's
(exp_account base.py _apply_fiscal_year_filter); every count by status here passes skip_fiscal_year_filter, as
dashboard_manager does, and limits each dossier's dates itself.
"""

from engine.tools.core.app import ToolError, log, tool
from engine.tools.core.experio import is_refusal, odoo_call, refused, require_dossier
from engine.tools.core.words import counted as numbered  # counted() here counts Experio's records

# Experio's states of invoices, statements and their lines, as its fr.po names them, in their order: the masculine
# word (relevés), then the feminine (factures, opérations)
STATES = {
    "pending": ("en cours", "en cours"),
    "draft": ("en brouillon", "en brouillon"),
    "partial": ("partiel", "partielle"),
    "ready": ("prêt", "prête"),
    "suspense": ("en suspens", "en suspens"),
    "post": ("comptabilisé", "comptabilisée"),
    "export": ("exporté", "exportée"),
}
PAYMENT_STATES = {"unpaid": ("impayé", "impayée"), "partial": ("payé en partie", "payée en partie")}
PAYMENT_STATES["paid"] = ("payé", "payée")
DECLARATION_STATES = {"draft": "en brouillon", "done": "déclarée"}
BOOKED = ("post", "export")
PIECES = ["exp.invoice", "exp.receipt"]  # dashboard_manager _PIECES_MODELS
OPERATIONS = ["exp.bank.statement.line", "exp.cash.statement.line", "exp.monetary.statement.line"]
STATEMENTS = [("exp.bank.statement", "bancaires"), ("exp.monetary.statement", "TPE")]
# The posted bank operations in suspense as Tarik's posting reports them (statement.suspense): unpaid or partly paid,
# not reconciled
SUSPENSE = [("exp_deleted", "=", False), ("state", "in", list(BOOKED))]
SUSPENSE += [("payment_state", "in", ["unpaid", "partial"]), ("is_reconciled", "=", False)]
# The declarations of the manager dashboard's columns in module declaration (_dashboard_manager_columns): model, its
# French name, and its type for exp.withholding.table. A model not installed is left out.
DECLARATIONS = [
    ("exp.deduction.table", "TVA", None),
    ("exp.tax.withholding.table", "RAS TVA", None),
    ("exp.withholding.table", "RAS IS", "is"),
    ("exp.withholding.table", "RAS IR", "ir"),
    ("exp.foreign.withholding.table", "RAS IS Non-Résident", None),
    ("exp.foreign.withholding.table.yearly", "Rém. versées à des non-résidents", None),
    ("exp.payment.delay", "Délais de paiement", None),
    ("exp.professional.tax.table", "Taxe professionnelle", None),
]


def counted(model, domain, groups, context):
    """Return {group value(s): count} of ``model``'s records in ``domain``, grouped by ``groups`` (a value, or a tuple
    for several; a relation's id), all exercices (skip_fiscal_year_filter)."""
    found = odoo_call(
        model,
        "read_group",
        domain,
        groups[:1],
        groups,
        lazy=False,
        context={**context, "skip_fiscal_year_filter": True},
    )
    counts = {}
    for group in found:
        value = tuple(group[name][0] if isinstance(group[name], list) else group[name] for name in groups)
        counts[value[0] if len(groups) == 1 else value] = group["__count"]
    return counts


def of(counts, dossier_id):
    """Return the counts {(dossier id, *values): n} of ``dossier_id``, keyed by their values (one alone when one)."""
    return {(key[1:] if len(key) > 2 else key[1]): n for key, n in counts.items() if key[0] == dossier_id}


def any_of(domains):
    """Return one domain matching any of ``domains`` (Odoo's prefix notation, each one's terms and-ed)."""
    branches = []
    for domain in domains:
        terms = sum(isinstance(term, tuple) for term in domain) - sum(term in ("|", "&") for term in domain)
        branches += ["&"] * (terms - 1) + domain
    return ["|"] * (len(domains) - 1) + branches


def experio_progress(ids, context):
    """Return {dossier id: (progress in %, None when nothing to do yet; pieces booked, pieces, operations booked,
    operations)} of the dossiers ``ids`` as Experio's dashboard computes them, every exercice."""
    sides = []
    for models in (PIECES, OPERATIONS):
        mine = [("company_id", "in", ids)]
        totals = [counted(m, [*mine, ("state", "!=", "cancel")], ["company_id"], context) for m in models]
        booked = [counted(m, [*mine, ("state", "in", list(BOOKED))], ["company_id"], context) for m in models]
        sides.append({i: (sum(b.get(i, 0) for b in booked), sum(t.get(i, 0) for t in totals)) for i in ids})
    found = {}
    for i in ids:
        ratios = [booked / total for booked, total in (sides[0][i], sides[1][i]) if total]
        found[i] = (round(100 * sum(ratios) / len(ratios)) if ratios else None), *sides[0][i], *sides[1][i]
    return found


def by_state(counts, labels, feminine=False):
    """Return ``counts`` {state: n} as « 3 en brouillon et 12 comptabilisées » in ``labels``' order, their feminine
    word when ``feminine``; "" when none."""
    said = []
    for state, words in labels.items():
        if counts.get(state):
            word = words[feminine]
            first, _, rest = word.partition(" ")
            many = word if first == "en" else f"{first}s {rest}".strip()  # « en brouillon » never takes an s
            said.append(numbered(counts[state], "{} " + word, "{} " + many))
    return " et ".join([", ".join(said[:-1]), said[-1]] if len(said) > 1 else said)


def french_date(day):
    """Return Experio's ISO date ``day`` (« 2026-01-31 ») as the client writes it: « 31/01/2026 »."""
    return f"{day[8:10]}/{day[5:7]}/{day[:4]}"


def exercices_of(ids, year, context):
    """Return {dossier id: its exercice ({name, date_start, date_end, state})} of ``year`` (int), else each dossier's
    latest one; a dossier without one is left out."""
    domain = [("company_id", "in", ids)]
    if year:
        domain += [("date_start", "<=", f"{year}-12-31"), ("date_end", ">=", f"{year}-01-01")]
    found = odoo_call(
        "exp.fiscal.entries",
        "search_read",
        domain,
        fields=["company_id", "name", "date_start", "date_end", "state"],
        order="company_id, date_end desc",
        context=context,
    )
    exercices = {}
    for exercice in found:  # each dossier's first: its latest
        exercices.setdefault(exercice.pop("company_id")[0], exercice)
    return exercices


def invoice_dates(exercice):
    """Return the domain of the invoices of ``exercice`` (those still without a date included: Experio has not read
    them yet); [] for every invoice when there is none."""
    if not exercice:
        return []
    return [
        *["|", ("exercice_id", "=", False), ("invoice_date", ">=", exercice["date_start"])],
        *["|", ("exercice_id", "=", False), ("invoice_date", "<=", exercice["date_end"])],
    ]


def statement_dates(exercice):
    """Return the domain of the statements and operations of ``exercice``; [] for all when there is none."""
    return [("date", ">=", exercice["date_start"]), ("date", "<=", exercice["date_end"])] if exercice else []


def invoice_counts(ids, exercices, context):
    """Return (by state, by payment state): the counts {(dossier id, type, state): n} of the dossiers' purchase and
    sale invoices of their exercice, cancelled ones left out, then of the posted ones by payment state."""
    dated = any_of([[("company_id", "=", i), *invoice_dates(exercices.get(i))] for i in ids])
    domain = [*dated, ("state", "!=", "cancel")]
    states = counted("exp.invoice", domain, ["company_id", "type", "state"], context)
    posted = [*domain, ("state", "=", "post")]
    return states, counted("exp.invoice", posted, ["company_id", "type", "payment_state"], context)


def invoice_lines(states, payments):
    """Return the lines of a dossier's purchase and sale invoices from its counts {(type, state): n} by state and by
    payment state (invoice_counts)."""
    lines = []
    for side, word in (("purchase", "d'achat"), ("sale", "de vente")):
        mine = {state: n for (kind, state), n in states.items() if kind == side}
        total = sum(mine.values())
        paid = {state: n for (kind, state), n in payments.items() if kind == side}
        text = f"- Factures {word} : {total}"
        text += f", dont {by_state(mine, STATES, True)}" if mine else ""
        if paid:
            text += f"\n  - Parmi les comptabilisées : {by_state(paid, PAYMENT_STATES, True)}"
        lines.append(text)
    return lines


def statement_counts(ids, exercices, context):
    """Return (counts, suspense): {model: (its statements, their operations)} counted {(dossier id, state): n} for
    the dossiers' bank and TPE statements of their exercice, and {dossier id: its bank operations in suspense}."""
    mine = any_of([[("company_id", "=", i), *statement_dates(exercices.get(i))] for i in ids])
    counts = {}
    for model, _ in STATEMENTS:
        statements = counted(model, [*mine, ("state", "!=", "cancel")], ["company_id", "state"], context)
        line_domain = mine + ([("exp_deleted", "=", False)] if model == "exp.bank.statement" else [])
        counts[model] = statements, counted(f"{model}.line", line_domain, ["company_id", "state"], context)
    return counts, counted("exp.bank.statement.line", [*mine, *SUSPENSE], ["company_id"], context)


def statement_lines(counts, left):
    """Return the lines of a dossier's bank and TPE statements and their operations from its counts {model:
    (statements, operations)} by state, then its ``left`` bank operations in suspense."""
    lines = []
    for model, word in STATEMENTS:
        statements, operations = counts[model]
        text = f"- Relevés {word} : {sum(statements.values())}"
        text += f", dont {by_state(statements, STATES)}" if statements else ""
        if operations:
            text += f"\n  - Leurs opérations : {sum(operations.values())}, dont {by_state(operations, STATES, True)}"
        lines.append(text)
    if left:
        lines.append(f"- Opérations bancaires en suspens, sans facture lettrée : {left}")
    return lines


def declarations_of(years, context):
    """Return [(name, {dossier id: its periods})] of each declaration for the dossiers' ``years`` {dossier id: year},
    each dossier's own year only. A declaration model not installed is left out."""
    found = []
    for model, name, kind in DECLARATIONS if years else []:
        domain = [("company_id", "in", list(years)), ("fiscal_year", "in", sorted({str(y) for y in years.values()}))]
        domain += [("state", "!=", "cancel"), *([("type", "=", kind)] if kind else [])]
        try:
            rows = odoo_call(
                model,
                "search_read",
                domain,
                fields=["company_id", "fiscal_year", "state", "period_id"],
                order="company_id, start_date",
                context=context,
            )
        except Exception as error:
            if not is_refusal(error):  # a crash or a cut: the whole read fails, the routine reads each dossier alone
                raise
            log(f"dossier_progress {model}: {error}")  # « Object … doesn't exist »: the module is not installed
            continue
        periods = {}
        for row in rows:
            if row["fiscal_year"] == str(years[row["company_id"][0]]):  # another dossier's year shares the call
                periods.setdefault(row["company_id"][0], []).append(row)
        found.append((name, periods))
    return found


def declaration_lines(declarations, dossier_id, year):
    """Return one line per declaration (declarations_of) the dossier has for ``year``: its periods by Experio's
    status."""
    lines = []
    for name, periods in declarations:
        if periods.get(dossier_id):
            said = ", ".join(
                f"{(d['period_id'] or [0, str(year)])[1]} {DECLARATION_STATES.get(d['state'], d['state'])}"
                for d in periods[dossier_id]
            )
            lines.append(f"- {name} : {said}")
    return lines or ["- Aucune déclaration préparée dans Experio pour cette année."]


def told(dossier, exercice, progress, invoices, statements, suspense, declarations):
    """Return (text, figures) of a dossier named ``dossier`` from what Experio counts of it, as progress_of says it."""
    progress, pieces_booked, pieces, operations_booked, operations = progress
    period = "aucun dans Experio"
    if exercice:
        period = f"{exercice['name']}, du {french_date(exercice['date_start'])} au {french_date(exercice['date_end'])}"
    progress_words = "rien à traiter pour l'instant" if progress is None else f"{progress} %"
    booked = numbered(pieces_booked, "{} pièce comptabilisée", "{} pièces comptabilisées")
    operations_said = numbered(
        operations_booked,
        "{} opération bancaire ou de caisse comptabilisée",
        "{} opérations bancaires et de caisse comptabilisées",
    )
    # One block per subject, a blank line between them: in the chat a line right after a « - » item joins it
    blocks = [
        f"Dossier `{dossier}`, avancement selon Experio (tous exercices) : {progress_words}\n"
        f"- {booked} sur {pieces}\n- {operations_said} sur {operations}",
        f"Exercice : {period}",
        "\n".join(["Pièces :", *invoices]),
        "\n".join(["Banque :", *statements]),
    ]
    if declarations:
        blocks.append("\n".join([f"Déclarations {exercice['date_end'][:4]} :", *declarations]))
    figures = {
        "dossier": dossier,
        "progress": progress,
        "pieces_booked": pieces_booked,
        "pieces": pieces,
        "operations_booked": operations_booked,
        "operations": operations,
        "suspense": suspense,
        "exercice": exercice["name"] if exercice else "",
    }
    return "\n\n".join(blocks), figures


def progress_of_many(dossiers, year=0):
    """Return {dossier id: (text, figures)} of ``dossiers`` ([{id, name}], Tarik opens them all), as progress_of
    says each, in the same 26 Experio calls whatever their number. ``year``: the exercice's, 0 for each one's latest.
    One dossier Experio refuses fails them all."""
    ids = [dossier["id"] for dossier in dossiers]
    context = {"allowed_company_ids": ids}
    exercices = exercices_of(ids, year, context)
    progress = experio_progress(ids, context)
    states, payments = invoice_counts(ids, exercices, context)
    statements, suspense = statement_counts(ids, exercices, context)
    years = {i: int(exercice["date_end"][:4]) for i, exercice in exercices.items()}
    declarations = declarations_of(years, context)
    found = {}
    for dossier in dossiers:
        i = dossier["id"]
        counts = {model: (of(both[0], i), of(both[1], i)) for model, both in statements.items()}
        found[i] = told(
            dossier["name"],
            exercices.get(i),
            progress[i],
            invoice_lines(of(states, i), of(payments, i)),
            statement_lines(counts, suspense.get(i, 0)),
            suspense.get(i, 0),
            declaration_lines(declarations, i, years[i]) if i in years else [],
        )
    return found


def progress_of(dossier_id, year=0):
    """Return (text, figures): where a dossier stands, as dossier_progress says it, and its figures for a table
    (dossier, progress in % or None, pieces booked and all, operations booked and all, operations in suspense,
    exercice). Raises ToolError as dossier_progress does."""
    dossier = require_dossier(dossier_id)
    with refused(f"dossier_progress {dossier_id}", f"Dossier `{dossier}` : Experio n'a pas pu donner son état."):
        text, figures = progress_of_many([{"id": dossier_id, "name": dossier}], year)[dossier_id]
    if year and not figures["exercice"]:  # told() names every exercice found
        raise ToolError(f"Le dossier `{dossier}` n'a pas d'exercice {year} dans Experio.")
    return text, figures


@tool
def dossier_progress(dossier_id: int, year: int = 0) -> str:
    """Return, as French text, where a dossier stands in Experio: its progress (Experio's own « avancement », every
    exercice), then for the exercice its purchase and sale invoices, bank and TPE statements and their operations,
    each by Experio's status, the bank operations in suspense, and the year's declarations by period and status.

    Args:
        dossier_id: the dossier.
        year: the exercice's year the client names; 0 for the dossier's latest exercice.

    Raises:
        ToolError: Experio can't give the counts."""
    return progress_of(dossier_id, year)[0]


@tool
def dossier_progress_figures(dossier_id: int) -> dict:
    """Return where a dossier stands for a routine's report: an ok result with dossier_progress's text as its message
    and its figures for the report's table (progress_of), the dossier's latest exercice.

    Raises:
        ToolError: Experio can't give the counts."""
    text, figures = progress_of(dossier_id)
    return {"status": "ok", "message": text, "figures": figures}
