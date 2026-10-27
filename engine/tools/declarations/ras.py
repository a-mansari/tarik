"""The withholding declarations (déclarations RAS), prepared as the cabinet does them on Experio's own screens (module
declaration), declared only when the client asks and confirms: RAS TVA (exp.tax.withholding.table), RAS IS and RAS IR
of residents (exp.withholding.table, by its type), RAS IS of non-residents, monthly and yearly
(exp.foreign.withholding.table, .yearly).

- Which: the ones the client names, else those the dossier files (res.company declaration_ras_tva / _is / _ir).
- Period: Experio keeps no frequency for them on the dossier (mode_type defaults to monthly, yearly for the yearly
  one): monthly, quarterly when the client names a quarter, the year for the yearly one; else the last ended, as
  Experio picks it (exp.declaration.base._get_last_ended_period_and_fiscal_year).
- Lines: Experio's « Actualiser » (action_refresh). RAS TVA has none, so Tarik runs the computation its form's
  onchange runs (get_tax_withholding_values, post_process_values) and writes the lines as it does. For RAS TVA and
  IS/IR the refresh clears every line, including those added by hand, so a declaration holding some is not
  refreshed; the non-residents' refresh keeps them.
- Checks: Experio checks only on « Déclarer » and on the XML. Tarik reports first what « Déclarer » would refuse
  (period not ended, RAS TVA before July 2024, period declared, tiers without ICE or IF, RAS TVA line without expense
  type or rate), then makes the XML, which runs Experio's own checks. Also reported: the IS/IR withholdings Experio
  leaves out (tiers neither company nor person; withholdings entered on a bank statement, which its computation
  skips: exp.withholding.table.fetch_withholding_operations only takes moves already flagged
  local_withholding_declared), and the RAS TVA lines at 100% (no valid attestation de régularité when the invoice was
  posted).
- « Déclarer » (action_post: no accounting entry; the declaration set done and its source moves flagged declared) only
  on the client's words, after a confirmation. Experio's double-declaration check (exp.declaration.base
  ._compute_already_declared) doesn't tell IS from IR: once one is declared for a period the other is refused, and
  the client is warned before. The DGI deposit stays a person's; Tarik never cancels a declaration.
"""

import re
from datetime import date

from engine.tools.core.app import ToolError, ok, question, step_tool, stop
from engine.tools.core.config import TEST_MODE
from engine.tools.core.experio import (
    ExperioRefusal,
    dry_run,
    experio_refusal,
    in_dossier,
    odoo_call,
    odoo_json,
    refused,
    require_dossier,
)
from engine.tools.core.outbox import keep_record_file
from engine.tools.core.words import normalized
from engine.tools.declarations.tva import find_period, last_ended, period_named

# Each declaration: its model, its type (exp.withholding.table), its lines' field and model, the dossier's flag, its
# modes, the line amounts added up, its XML action.
KINDS = {
    "tva": {
        "label": "RAS TVA",
        "model": "exp.tax.withholding.table",
        "lines": "tax_withholding_ids",
        "line": "exp.tax.withholding.table.line",
        "flag": "declaration_ras_tva",
        "sums": {"amount_ht": "HT", "amount_tva": "TVA", "amount_withholding": "retenue"},
        "xml": "action_print_xml_ras_report",
    },
    "is": {
        "label": "RAS IS",
        "model": "exp.withholding.table",
        "type": "is",
        "lines": "is_withholding_ids",
        "line": "exp.withholding.table.line",
        "flag": "declaration_ras_is",
        "sums": {"amount_ht": "HT", "amount_withholding": "retenue"},
        "xml": "action_generate_xml_ras",
    },
    "ir": {
        "label": "RAS IR",
        "model": "exp.withholding.table",
        "type": "ir",
        "lines": "ir_withholding_ids",
        "line": "exp.withholding.table.line",
        "flag": "declaration_ras_ir",
        "sums": {"amount_ht": "HT", "amount_withholding": "retenue"},
        "xml": "action_generate_xml_ras",
    },
    "non_resident": {
        "label": "RAS IS non-résidents",
        "model": "exp.foreign.withholding.table",
        "lines": "foreign_withholding_ids",
        "line": "exp.foreign.withholding.table.line",
        "modes": ("monthly",),
        "sums": {"amount_brute_mad": "brut", "amount_foreign_withholding": "retenue"},
        "xml": "action_download_xml_declaration",
    },
    "non_resident_yearly": {
        "label": "RAS IS non-résidents annuelle",
        "model": "exp.foreign.withholding.table.yearly",
        "lines": "foreign_withholding_ids",
        "line": "exp.foreign.withholding.table.yearly.line",
        "modes": ("yearly",),
        "sums": {"amount_brute_mad": "brut", "amount_foreign_withholding": "retenue"},
        "xml": "action_download_xml_declaration",
    },
}
# The client's words for each (normalized).
KIND_WORDS = {
    "non_resident_yearly": r"(?:ras is )?non residents? annuel(?:le)?|annuel(?:le)? (?:ras is )?non residents?",
    "non_resident": r"(?:ras is )?non residents?",
    "tva": r"ras tva|retenues? (?:a la source )?(?:de |sur (?:la )?)?tva|tva",
    "is": r"ras is|is",
    "ir": r"ras ir|ir",
}
QUARTER = re.compile(r"(?<![a-z0-9])(?:t|q)[1-4](?![0-9])|(?<!\d)[1-4] ?(?:er|e|eme)? trimestre")
FIELDS = ["state", "period_id", "fiscal_year", "start_date", "end_date", "name"]
LINE_FIELDS = ["partner_id", "added_manually", "withholding_percentage", "expense_type", "warning_msg"]


def kinds_named(words, company):
    """Return the KINDS keys the client's ``words`` name, else those the dossier files (``company``'s RAS flags)."""
    words = normalized(words or "")
    found = []
    for kind, pattern in KIND_WORDS.items():
        if re.search(rf"(?<![a-z])(?:{pattern})(?![a-z])", words):
            found.append(kind)
            words = re.sub(rf"(?<![a-z])(?:{pattern})(?![a-z])", " ", words)  # « non résidents annuelle » once
    if found:
        return found
    return [kind for kind in ("tva", "is", "ir") if company.get(KINDS[kind]["flag"])]


def period_of(kind, words, today):
    """Return (mode, period number, year) of the ``kind`` declaration the client's ``words`` name.

    The yearly one: ("yearly", None, the year named, else the year before). The others: monthly, or quarterly when
    the client names a quarter; the period named, else the last ended.

    Raises:
        ToolError: a quarter named for a declaration that is only monthly (non-residents).
    """
    allowed = KINDS[kind].get("modes", ("monthly", "quarterly"))
    words = normalized(words or "")
    if allowed == ("yearly",):
        year = re.search(r"(?<!\d)(20\d\d)(?!\d)", words)
        return "yearly", None, int(year.group(1)) if year else today.year - 1  # Experio's default: the year before
    mode = "quarterly" if QUARTER.search(words) else "monthly"
    if mode not in allowed:
        raise ToolError(f"La {KINDS[kind]['label']} se déclare chaque mois : quel mois ?")
    number, year = period_named(mode, words, today) or last_ended(mode, today)
    return mode, number, year


def declaration_of(kind, dossier_id, mode, period, year, context):
    """Find or create the dossier's ``kind`` declaration for ``period`` (None for the yearly one) and ``year``.

    Returns (declaration_id, created): the declared one, else the latest draft, with created False; else a new draft
    in ``mode`` with created True. (None, False) in test mode when there is none to take up.
    """
    spec = KINDS[kind]
    domain = [("company_id", "=", dossier_id), ("fiscal_year", "=", str(year)), ("state", "!=", "cancel")]
    if period:
        domain.append(("period_id", "=", period["id"]))
    if spec.get("type"):
        domain.append(("type", "=", spec["type"]))
    found = odoo_call(
        spec["model"], "search_read", domain, fields=["state"], order="state desc, id desc", context=context
    )
    if found:
        return found[0]["id"], False
    if dry_run():
        return None, False
    values = {"company_id": dossier_id, "fiscal_year": str(year), "mode_type": mode}
    values["date_declaration"] = date.today().isoformat()
    if period:
        values["period_id"] = period["id"]
    if spec.get("type"):
        values["type"] = spec["type"]
    return odoo_call(spec["model"], "create", values, context=context), True


def refresh(kind, table_id, context):
    """Run Experio's « Actualiser » on the declaration; for RAS TVA, which has none, write its form's computation."""
    spec = KINDS[kind]
    if kind != "tva":
        odoo_json(spec["model"], "action_refresh", [table_id], context=context)
        return
    values = odoo_json(spec["model"], "get_tax_withholding_values", [table_id], "purchase", context=context)
    values = odoo_json(spec["model"], "post_process_values", [table_id], values, context=context)
    lines = [(5, 0, 0)] + [(0, 0, value) for value in values]
    odoo_json(spec["model"], "write", [table_id], {spec["lines"]: lines}, context=context)


def lines_of(kind, table_id, context):
    """Return the declaration's lines as read: LINE_FIELDS plus the ``kind``'s summed amounts."""
    spec = KINDS[kind]
    fields = LINE_FIELDS + list(spec["sums"])
    table = odoo_call(spec["model"], "read", [table_id], fields=[spec["lines"]], context=context)[0]
    return odoo_call(spec["line"], "read", table[spec["lines"]], fields=fields, context=context)


def refusals(kind, table, lines, dossier_id, context):
    """Return French phrases for what Experio's « Déclarer » (action_post) would refuse, in its order; [] when none.

    The RAS TVA July 2024 rule compares the period's record id with 7, as Experio does. A period already declared is
    looked for as each declaration's action_post does: any overlapping dates (exp_declaration_base), or for the
    non-residents the same exercice, mode and, monthly, period (exp_foreign_withholding_table(_yearly).py).
    """
    spec, found = KINDS[kind], []
    if table["end_date"] and table["end_date"] >= date.today().isoformat():
        found.append("la période n'est pas terminée")
    if kind == "tva" and table["period_id"] and table["period_id"][0] < 7 and table["fiscal_year"] == "2024":
        found.append("la RAS TVA ne se déclare qu'à partir de juillet 2024")  # Experio's own condition
    declared = [("company_id", "=", dossier_id), ("state", "=", "done"), ("id", "!=", table["id"])]
    if spec.get("modes"):  # non-residents: the same exercice, mode and (monthly) period, as their action_post searches
        declared += [("fiscal_year", "=", table["fiscal_year"]), ("mode_type", "=", spec["modes"][0])]
        if spec["modes"][0] == "monthly":
            declared.append(("period_id", "=", table["period_id"] and table["period_id"][0]))
    elif table["start_date"]:  # the others: any overlapping period (exp_declaration_base's check)
        declared += [("start_date", "<=", table["end_date"]), ("end_date", ">=", table["start_date"])]
    if spec.get("modes") or table["start_date"]:
        done = odoo_call(
            spec["model"],
            "search_read",
            declared,
            fields=["type"] if spec.get("type") else ["id"],
            limit=1,
            context=context,
        )
        if done:
            other = done[0].get("type")
            if other and other != spec["type"]:
                found.append(
                    f"Experio la refuse car la RAS {other.upper()} de la même période est déjà déclarée "
                    "(son contrôle de doublon ne distingue pas IS et IR)"
                )
            else:
                found.append("la période est déjà déclarée")
    partners = sorted({line["partner_id"][0] for line in lines if line["partner_id"]})
    if partners:
        bare = odoo_call(
            "exp.partner",
            "search_read",
            [("id", "in", partners), "|", ("ice", "=", False), ("vat", "=", False)],
            fields=["name"],
            context=context,
        )
        if bare:
            found.append("tiers sans ICE ou IF : " + ", ".join(p["name"] for p in bare))
    if kind == "tva":
        if any(not line["expense_type"] for line in lines):
            found.append("ligne(s) sans type d'activité du tiers (fiche du tiers, « Type de dépense »)")
        if any(not line["withholding_percentage"] for line in lines):
            found.append("ligne(s) sans taux de retenue")
    return found


def left_out(kind, table, dossier_id, context):
    """Return French phrases counting the IS/IR withholdings Experio's computation leaves out; [] for other kinds."""
    if kind not in ("is", "ir"):
        return []
    said = []
    period = [("company_id", "=", dossier_id), ("invoice_date", ">=", table["start_date"])]
    period += [("invoice_date", "<=", table["end_date"]), ("retenue", "=", True), ("state", "in", ["post", "export"])]
    if kind == "is":  # once, on the IS declaration: these go to neither
        others = [("partner_id.type", "in", ["cpu", "st", "other"])]
        count = odoo_call("exp.invoice", "search_count", period + others, context=context)
        if count:
            said.append(
                f"{count} facture(s) avec retenue dont le tiers n'est ni société ni personne (ni en IS ni en IR)"
            )
    bank = [("company_id", "=", dossier_id), ("date", ">=", table["start_date"]), ("date", "<=", table["end_date"])]
    bank += [("operation_id.type", "=", "local_withholding"), ("state", "in", ["post", "export"])]
    bank += [("partner_id.type", "=", "company" if kind == "is" else "person")]
    count = odoo_call("exp.bank.statement.line", "search_count", bank, context=context)
    if count:
        said.append(
            f"{count} retenue(s) passée(s) sur un relevé bancaire (« Ajuster »), qu'Experio ne reprend pas : "
            "à ajouter à la main"
        )
    return said


def prepare(kind, dossier_id, words, declarer, client_confirmed, context, session_id="", new_only=False):
    """Prepare one ``kind`` declaration, and declare it when ``declarer`` and ``client_confirmed``. ``new_only``: a
    declaration already there is only reported, never refreshed (prepare_ras_declaration).

    Returns (status, text, declaration_id): status is "ok", "question" (declaration asked, not confirmed yet) or
    "stop" (Experio would refuse or refused); declaration_id is None in test mode when none exists.
    """
    spec = KINDS[kind]
    mode, number, year = period_of(kind, words, date.today())
    period = find_period(mode, number) if number else None
    label = f"{spec['label']} {period['name'] + ' ' if period else ''}{year}"
    table_id, made = declaration_of(kind, dossier_id, mode, period, year, context)
    if table_id is None:
        return "ok", f"{TEST_MODE}{label} : serait créée et préparée.", None
    table = odoo_call(spec["model"], "read", [table_id], fields=FIELDS, context=context)[0]
    table["id"] = table_id
    lines = lines_of(kind, table_id, context)
    if table["state"] == "done":
        return "ok", f"{label} (n° `{table_id}`) : déjà déclarée dans Experio. {totals(spec, lines)}", table_id
    if new_only and not made:
        return "ok", f"{label} (n° `{table_id}`) : déjà préparée, non actualisée. {totals(spec, lines)}", table_id
    manual = sum(bool(line["added_manually"]) for line in lines)
    kept = ""
    if manual and kind in ("tva", "is", "ir"):  # Experio's refresh deletes them
        kept = f" Non actualisée : {manual} ligne(s) ajoutée(s) à la main, qu'« Actualiser » effacerait."
    elif not dry_run():
        refresh(kind, table_id, context)
        lines = lines_of(kind, table_id, context)
    text = f"{label} (n° `{table_id}`{', créée' if made else ''}) : {totals(spec, lines)}{kept}"
    full = sum(line["withholding_percentage"] == "100" for line in lines) if kind == "tva" else 0
    if full:
        text += f" {full} retenue(s) à 100 % : tiers sans attestation de régularité valide à la comptabilisation."
    missing = left_out(kind, table, dossier_id, context)
    if missing:
        text += " Pas dans la déclaration : " + " ; ".join(missing) + "."
    refused_by = refusals(kind, table, lines, dossier_id, context)
    if refused_by:
        return "stop", f"{text} Experio ne la laissera pas déclarer : " + " ; ".join(refused_by) + ".", table_id
    if lines and not dry_run():
        try:  # Experio's own checks of the XML (company IF, each tiers' ICE and IF, amounts, payment dates)
            odoo_json(spec["model"], spec["xml"], [table_id], context=context)
        except ExperioRefusal as error:
            return "stop", f"{text} Experio refuse son fichier XML : {experio_refusal(error)}", table_id
        name = keep_record_file(session_id, spec["model"], table_id, "datas_for_ras", "datas_fname_for_ras", context)
        text += f" Fichier XML `{name}` joint à cette réponse." if name else " Fichier XML prêt sur la déclaration."
    if not declarer:
        return "ok", text, table_id
    if not client_confirmed:
        return "question", text, table_id
    if dry_run():
        return "ok", f"{TEST_MODE}{label} serait déclarée.", table_id
    try:
        odoo_json(spec["model"], "action_post", [table_id], context=context)
    except ExperioRefusal as error:
        return "stop", f"{label} : pas déclarée, Experio refuse : {experio_refusal(error)}", table_id
    return "ok", f"{label} déclarée dans Experio. Reste au cabinet : le dépôt sur le portail de la DGI.", table_id


def totals(spec, lines):
    """Return the French summary of ``lines``: count and the sums ``spec`` lists."""
    if not lines:
        return "aucune retenue sur la période (déclaration à néant)."
    sums = ", ".join(f"{name} {sum(line[field] or 0 for line in lines):.2f}" for field, name in spec["sums"].items())
    return f"{len(lines)} ligne(s), {sums}."


@step_tool
def prepare_ras_declaration(
    dossier_id: int,
    declarations: str = "",
    periode: str = "",
    declarer: bool = False,
    client_confirmed: bool = False,
    session_id: str = "",
    new_only: bool = False,
) -> dict:
    """Prepare the dossier's withholding declarations (RAS) in Experio, and declare them when asked and confirmed.

    Each is created or taken up, refreshed by Experio, its XML made; what Experio leaves out and would refuse is
    reported to the client.

    Args:
        declarations: those the client names (« RAS TVA », « IS », « IR », « non-résidents », « non-résidents
            annuelle »); empty for those the dossier files.
        periode: the period named (« août 2026 », « T3 », « 2025 » for the yearly one); empty for the last ended.
        declarer: the client asks to declare them.
        client_confirmed: the client confirmed; only then is « Déclarer » run.
        new_only: a routine's unattended run: a declaration already there is reported, never taken up (« Actualiser »
            would rebuild a draft someone is working on).

    Returns a question result when any declaration waits for confirmation, else a stop result when any was refused,
    else an ok result. The DGI deposit stays the cabinet's.
    """
    dossier = require_dossier(dossier_id)
    context = in_dossier(dossier_id)
    with refused(f"prepare_ras_declaration {dossier_id}", "Experio n'a pas pu préparer la déclaration RAS ({reason})."):
        flags = [spec["flag"] for spec in KINDS.values() if spec.get("flag")]
        company = odoo_call("res.company", "read", [dossier_id], fields=flags)[0]
        kinds = kinds_named(declarations, company)
        if not kinds:
            return stop(
                f"Le dossier `{dossier}` ne déclare aucune RAS dans Experio (RAS TVA, IS et IR décochées) : dites "
                "laquelle préparer (RAS TVA, IS, IR ou non-résidents)."
            )
        results = [
            prepare(kind, dossier_id, periode, declarer, client_confirmed, context, session_id, new_only)
            for kind in kinds
        ]
    notes = [
        f"(la {KINDS[k]['label']} n'est pas cochée pour ce dossier dans Experio)"
        for k in kinds
        if KINDS[k].get("flag") and not company.get(KINDS[k]["flag"])
    ]
    text = " ".join(t for _, t, _ in results) + (" " + " ".join(notes) if notes else "")
    ids = [i for _, _, i in results if i]
    statuses = {s for s, _, _ in results}
    if "question" in statuses:
        both = {"is", "ir"} & set(kinds)
        warn = ""
        if len(both) == 1:
            other = "IR" if "is" in both else "IS"
            warn = (
                f" Attention : une fois celle-ci déclarée, Experio refusera la RAS {other} de la même période "
                "(son contrôle de doublon ne les distingue pas) : déclarez les deux ensemble."
            )
        return question(
            f"{text}{warn} Je les déclare dans Experio ? Le dépôt sur le portail de la DGI restera à "
            "faire par le cabinet. Je le fais ?",
            declarations=ids,
        )
    if "stop" in statuses:
        return stop(text, declarations=ids)
    hint = "" if declarer else " Écrivez « déclarez les RAS » pour les déclarer dans Experio."
    return ok(text + hint, declarations=ids)
