"""Where a dossier stands, as Experio counts it: its progress, then for an exercice its invoices, bank and TPE
statements, their operations and the operations in suspense, and its declarations of the year, each by Experio's own
status (French labels of its selections, invoice/i18n, bank/i18n, declaration/i18n fr.po).

The progress is Experio's own « avancement », computed as its manager dashboard does (dashboard_manager
_PIECES_MODELS, _OPERATIONS_MODELS, _grouped_booked_totals, _dossier_progress): pieces (invoices and receipts) and
operations (bank, cash and TPE lines) posted or exported over those not cancelled, every exercice, averaged over the
sides that have any.

Experio's read_group keeps only the selected exercice's posted rows (exp_account base.py _apply_fiscal_year_filter);
every count by status here passes skip_fiscal_year_filter, as dashboard_manager does, and limits the dates itself.
"""

from engine.tools.core.app import ToolError, log, tool
from engine.tools.core.experio import in_dossier, odoo_call, refused, require_dossier
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


def experio_progress(dossier_id, context):
    """Return (progress in %, None when nothing to do yet; pieces booked, pieces, operations booked, operations) as
    Experio's dashboard computes them, every exercice (search_count is not limited to the selected exercice)."""
    sides = []
    for models in (PIECES, OPERATIONS):
        mine = [("company_id", "=", dossier_id)]
        total = sum(odoo_call(m, "search_count", [*mine, ("state", "!=", "cancel")], context=context) for m in models)
        booked = sum(
            odoo_call(m, "search_count", [*mine, ("state", "in", list(BOOKED))], context=context) for m in models
        )
        sides.append((booked, total))
    ratios = [booked / total for booked, total in sides if total]
    return (round(100 * sum(ratios) / len(ratios)) if ratios else None), *sides[0], *sides[1]


def counted(model, domain, groups, context):
    """Return {group value(s): count} of ``model``'s records in ``domain``, grouped by ``groups`` (a value, or a tuple
    for several), all exercices (skip_fiscal_year_filter)."""
    found = odoo_call(
        model,
        "read_group",
        domain,
        groups[:1],
        groups,
        lazy=False,
        context={**context, "skip_fiscal_year_filter": True},
    )
    key = (lambda g: g[groups[0]]) if len(groups) == 1 else (lambda g: tuple(g[name] for name in groups))
    return {key(group): group["__count"] for group in found}


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


def exercice_of(dossier_id, year, context):
    """Return the exercice ({name, date_start, date_end, state}) of ``year`` (int), else the dossier's latest one;
    None when the dossier has none."""
    domain = [("company_id", "=", dossier_id)]
    if year:
        domain += [("date_start", "<=", f"{year}-12-31"), ("date_end", ">=", f"{year}-01-01")]
    found = odoo_call(
        "exp.fiscal.entries",
        "search_read",
        domain,
        fields=["name", "date_start", "date_end", "state"],
        order="date_end desc",
        limit=1,
        context=context,
    )
    return found[0] if found else None


def invoice_lines(dossier_id, exercice, context):
    """Return the lines of the dossier's purchase and sale invoices of the exercice (those still without a date
    included: Experio has not read them yet), cancelled ones left out."""
    domain = [("company_id", "=", dossier_id), ("state", "!=", "cancel")]
    if exercice:
        domain += ["|", ("exercice_id", "=", False), ("invoice_date", ">=", exercice["date_start"])]
        domain += ["|", ("exercice_id", "=", False), ("invoice_date", "<=", exercice["date_end"])]
    states = counted("exp.invoice", domain, ["type", "state"], context)
    payments = counted("exp.invoice", [*domain, ("state", "=", "post")], ["type", "payment_state"], context)
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


def statement_lines(dossier_id, exercice, context):
    """Return (lines, suspense): the lines of the dossier's bank and TPE statements of the exercice and their
    operations, with the posted bank operations in suspense as Tarik's posting reports them (statement.suspense:
    unpaid or partly paid, not reconciled), and how many those are."""
    dated = [("date", ">=", exercice["date_start"]), ("date", "<=", exercice["date_end"])] if exercice else []
    mine = [("company_id", "=", dossier_id), *dated]
    lines = []
    for model, word in (("exp.bank.statement", "bancaires"), ("exp.monetary.statement", "TPE")):
        statements = counted(model, [*mine, ("state", "!=", "cancel")], ["state"], context)
        line_domain = mine + ([("exp_deleted", "=", False)] if model == "exp.bank.statement" else [])
        operations = counted(f"{model}.line", line_domain, ["state"], context)
        text = f"- Relevés {word} : {sum(statements.values())}"
        text += f", dont {by_state(statements, STATES)}" if statements else ""
        if operations:
            text += f"\n  - Leurs opérations : {sum(operations.values())}, dont {by_state(operations, STATES, True)}"
        lines.append(text)
    suspense = [*mine, ("exp_deleted", "=", False), ("state", "in", list(BOOKED))]
    suspense += [("payment_state", "in", ["unpaid", "partial"]), ("is_reconciled", "=", False)]
    left = odoo_call("exp.bank.statement.line", "search_count", suspense, context=context)
    if left:
        lines.append(f"- Opérations bancaires en suspens, sans facture lettrée : {left}")
    return lines, left


def declaration_lines(dossier_id, year, context):
    """Return one line per declaration the dossier has for ``year``: its periods by Experio's status. A declaration
    model not installed is left out."""
    lines = []
    for model, name, kind in DECLARATIONS:
        domain = [("company_id", "=", dossier_id), ("fiscal_year", "=", str(year)), ("state", "!=", "cancel")]
        domain += [("type", "=", kind)] if kind else []
        try:
            found = odoo_call(
                model, "search_read", domain, fields=["state", "period_id"], order="start_date", context=context
            )
        except Exception as error:  # the dossier's Experio has no such declaration (module not installed)
            log(f"dossier_progress {model}: {error}")
            continue
        if found:
            periods = ", ".join(
                f"{(d['period_id'] or [0, str(year)])[1]} {DECLARATION_STATES.get(d['state'], d['state'])}"
                for d in found
            )
            lines.append(f"- {name} : {periods}")
    return lines or ["- Aucune déclaration préparée dans Experio pour cette année."]


def progress_of(dossier_id, year=0):
    """Return (text, figures): where a dossier stands, as dossier_progress says it, and its figures for a table
    (dossier, progress in % or None, pieces booked and all, operations booked and all, operations in suspense,
    exercice). Raises ToolError as dossier_progress does."""
    dossier = require_dossier(dossier_id)
    context = in_dossier(dossier_id)
    with refused(f"dossier_progress {dossier_id}", f"Dossier `{dossier}` : Experio n'a pas pu donner son état."):
        exercice = exercice_of(dossier_id, year, context)
        if year and not exercice:
            raise ToolError(f"Le dossier `{dossier}` n'a pas d'exercice {year} dans Experio.")
        progress, pieces_booked, pieces, operations_booked, operations = experio_progress(dossier_id, context)
        invoices = invoice_lines(dossier_id, exercice, context)
        statements, suspense = statement_lines(dossier_id, exercice, context)
        shown_year = int(exercice["date_end"][:4]) if exercice else year
        declarations = declaration_lines(dossier_id, shown_year, context) if shown_year else []
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
        blocks.append("\n".join([f"Déclarations {shown_year} :", *declarations]))
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
