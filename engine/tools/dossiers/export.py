"""The export of a dossier's entries or tiers for another accounting software, made with Experio's own export
(exp_account, exp.export.wizard), its file handed to the client: Experio keeps none (it lives on the wizard, a
temporary record), so Tarik keeps it in the conversation's outbox (tarik_outbox), sent with the reply.

- What: the journals (Experio's « Journal Ledger », by default), the tiers (« Partner Ledger »), or the FEC.
- Model: the one the client names, else Experio's own default (_get_default_export_template: the only active one,
  else the last used for the dossier, else the first); none makes Experio refuse in its words.
- Dates: the period the client names, else Experio's default (_get_default_dates: the open exercice); the import
  dates when they ask.
- Format: Excel, or text (a ZIP of one file per journal) with the separator they name (Experio's default: tab).
- « Lettrage » only when they ask. « Intégration » (Experio's default) marks the pieces exported (create_batch_id:
  their moves set to state exported), which locks them; the FEC never marks any. Tarik exports for analysis unless
  the client asks for an integration, and asks first. Tiers without their own account (Experio's warning, only when
  the dossier's block_export_without_tiers is set) are asked too; « Exporter quand même » is Experio's own.
- Reported to the client: entries, lines, total debit and credit of what Experio selected (Experio counts none),
  Experio's refusal in its words (no model, no line, a line without account).
"""

import base64
import re
from datetime import date
from urllib.parse import unquote

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
from engine.tools.core.outbox import keep_file
from engine.tools.core.words import counted, normalized
from engine.tools.declarations.tva import MONTHS

WIZARD = "exp.export.wizard"
TIERS_WIZARD = "exp.export.missing.tiers.wizard"
KINDS = {"journal": "écritures par journal", "partner": "tiers", "fec": "FEC"}
SEPARATORS = {"virgule": ",", "point virgule": ";", "tabulation": "\t", ";": ";", ",": ","}


def dates_named(words):
    """Return (date_from, date_to) as ISO strings for the period the client's ``words`` name.

    Understands « du 01/07/2026 au 30/09/2026 », « T3 2026 », « septembre 2026 », « 2026 », and « T3 » or
    « septembre » without a year (this year's). Returns (None, None) when the words name no period.
    """
    words = normalized(words or "")
    found = re.findall(r"(?<!\d)(\d{1,2}) (\d{1,2}) (20\d\d)(?!\d)", words)  # 01/07/2026, normalized
    if len(found) == 2:
        (d1, m1, y1), (d2, m2, y2) = found
        return f"{y1}-{int(m1):02d}-{int(d1):02d}", f"{y2}-{int(m2):02d}-{int(d2):02d}"
    year = re.search(r"(?<!\d)(20\d\d)(?!\d)", words)
    quarter = re.search(r"(?<![a-z0-9])(?:t|q)([1-4])(?![0-9])|(?<!\d)([1-4]) ?(?:er|e|eme)? trimestre", words)
    month = next((n for n, name in enumerate(MONTHS, 1) if re.search(rf"(?<![a-z]){name}(?![a-z])", words)), None)
    if not year and not (quarter or month):
        return None, None
    year = int(year.group(1)) if year else date.today().year  # « T3 », « septembre »: this year's
    if quarter:
        number = int(quarter.group(1) or quarter.group(2))
        first, last = 3 * number - 2, 3 * number
    elif month:
        first = last = month
    else:
        first, last = 1, 12
    end = {2: 29 if year % 4 == 0 else 28, 4: 30, 6: 30, 9: 30, 11: 30}.get(last, 31)
    return f"{year}-{first:02d}-01", f"{year}-{last:02d}-{end:02d}"


def template_named(model, words, context):
    """Return the id of the export model (``model``) whose name is in the client's ``words``, else None.

    None leaves Experio's default model.

    Raises:
        ToolError: the words match several models.
    """
    words = normalized(words or "")
    if not words:
        return None
    found = [
        t
        for t in odoo_call(model, "search_read", [], fields=["name"], context=context)
        if normalized(t["name"]) and normalized(t["name"]) in words
    ]
    if len(found) > 1:
        raise ToolError("Plusieurs modèles d'export correspondent : " + ", ".join(t["name"] for t in found) + ".")
    return found[0]["id"] if found else None


def models_note(model, kind, context):
    """Return the French note listing Experio's export models (``model``) for ``kind``, added to its refusal.

    Only the models of that kind are listed, so the client never names a journal export model for the tiers.
    """
    names = [t["name"] for t in odoo_call(model, "search_read", [], fields=["name"], context=context)]
    what = "de tiers" if kind == "partner" else "d'écritures"
    if not names:
        return f" Experio n'a aucun modèle d'export {what} : il est à créer dans Experio."
    return f" Modèles d'export {what} d'Experio : " + ", ".join(f"`{name}`" for name in names) + "."


def selected_lines(wizard_id, context):
    """Return the ids of the journal items Experio's export selects (exp.export.wizard.get_line_ids, its own filters).

    Over JSON-RPC the method's recordset comes back as its text, « exp.move.line(1, 2) », which is parsed here.
    """
    text = odoo_json(WIZARD, "get_line_ids", [wizard_id], context=context) or ""
    return [int(i) for i in re.findall(r"\d+", text.partition("(")[2])]


def summary(line_ids, context):
    """(entries, lines, total debit, total credit) of the journal items ``line_ids``."""
    if not line_ids:
        return 0, 0, 0.0, 0.0
    rows = odoo_call("exp.move.line", "read", line_ids, fields=["move_id", "debit", "credit"], context=context)
    moves = {row["move_id"][0] for row in rows if row["move_id"]}
    return len(moves), len(rows), sum(r["debit"] or 0 for r in rows), sum(r["credit"] or 0 for r in rows)


def fec_lines(dossier_id, journals, wizard, context):
    """Return the ids of the journal items Experio's FEC exports: _get_fec_line_ids's own domain (exp_export_wizard.py),
    which RPC can't call (a private method). Unlike get_line_ids it ignores the template, the exported flag and the
    import dates: the dossier's posted or exported lines of ``journals``, between ``wizard``'s date_from and date_to.
    """
    domain = [
        ("company_id", "=", dossier_id),
        ("parent_state", "in", ["posted", "exported"]),
        ("exp_deleted", "=", False),
        ("move_id.exp_deleted", "=", False),
        ("journal_id", "in", journals),
    ]
    domain += [("date", ">=", wizard["date_from"])] if wizard["date_from"] else []
    domain += [("date", "<=", wizard["date_to"])] if wizard["date_to"] else []
    return odoo_call("exp.move.line", "search", domain, context=context)


def totals(kind, wizard_id, context, fec=None):
    """Return « : 1 écriture, 2 lignes, débit, crédit » of what Experio's export selects now; "" for the tiers.

    ``fec``: for the FEC, its line ids (fec_lines); the other exports are counted with get_line_ids.
    """
    if kind == "partner":
        return ""
    lines = fec if kind == "fec" else selected_lines(wizard_id, context)
    entries, count, debit, credit = summary(lines, context)
    entries, count = counted(entries, "{} écriture", "{} écritures"), counted(count, "{} ligne", "{} lignes")
    return f" : {entries}, {count}, débit {debit:.2f}, crédit {credit:.2f}"


@step_tool
def export_entries(
    dossier_id: int,
    quoi: str = "",
    periode: str = "",
    modele: str = "",
    format_fichier: str = "",
    lettrage: bool = False,
    integration: bool = False,
    date_import: bool = False,
    client_confirmed: bool = False,
    session_id: str = "",
) -> dict:
    """Export the dossier's entries, tiers or FEC with Experio's own export; the file goes with Tarik's reply.

    Args:
        quoi: what to export: « écritures » (default), « tiers », « FEC ».
        periode: the period (« T3 2026 », « 2026 », « du 01/07/2026 au 30/09/2026 »); empty for the open exercice.
        modele: the export model's name; empty for Experio's default.
        format_fichier: Excel by default, or text (« texte avec ; »).
        lettrage: include the reconciliation.
        integration: the client wants the pieces marked exported (locked); asked first. Ignored for the FEC, which
            Experio never marks exported (exp_export_wizard.py action_print_fec).
        date_import: ``periode`` gives import dates, not entry dates.
        client_confirmed: the client confirmed the integration or the export despite tiers without account.

    Returns a question result before an integration or when tiers lack their account, a stop result when Experio
    refuses or makes no file, else an ok result with ``file_name``.
    """
    dossier = require_dossier(dossier_id)
    context = in_dossier(dossier_id)
    words = normalized(quoi)
    kind = (
        "fec"
        if "fec" in words
        else "partner"
        if re.search(r"tiers|partenaire|fournisseurs|clients", words)
        else ("journal")
    )
    integration = integration and kind != "fec"
    with refused(f"export_entries {dossier_id}", "Experio n'a pas pu préparer l'export ({reason})."):
        start, end = dates_named(periode)
        values = {"company_id": dossier_id, "export_type": kind, "reconciliation": bool(lettrage)}
        values["integration"] = False  # analysis first: integration only once confirmed
        if start:
            values.update(
                {"create_date_from": start, "create_date_to": end}
                if date_import
                else {"date_from": start, "date_to": end}
            )
        fmt = normalized(format_fichier)
        if "texte" in fmt or "txt" in fmt or "csv" in fmt:
            values["exp_export_file_type"] = "text"
            separator = next(
                (s for word, s in SEPARATORS.items() if word in format_fichier.lower() or word in fmt), None
            )
            if separator:
                values["exp_text_file_separator"] = separator
        model = "exp.export.partner.template" if kind == "partner" else "exp.export.template"
        chosen = template_named(model, modele, context)
        if chosen:
            values["partner_export_template_id" if kind == "partner" else "export_template_id"] = chosen
        journals = []
        if kind != "partner":
            journals = odoo_call("exp.journal", "search", [("company_id", "=", dossier_id)], context=context)
            values["journal_ids"] = [(6, 0, journals)]
        if dry_run():
            return ok(f"{TEST_MODE}L'export ({KINDS[kind]}) du dossier `{dossier}` serait préparé.")
        wizard_id = odoo_call(WIZARD, "create", values, context=context)
        wizard = odoo_call(
            WIZARD,
            "read",
            [wizard_id],
            fields=["date_from", "date_to", "export_template_id", "partner_export_template_id"],
            context=context,
        )[0]
        template = wizard["partner_export_template_id" if kind == "partner" else "export_template_id"]
        period = f"du {wizard['date_from']} au {wizard['date_to']}" if wizard["date_from"] else "sans dates"
        shown = f"`{template[1]}`" if template else "aucun"
        model_note = "" if kind == "fec" else f", modèle {shown}"  # the FEC has its own
        head = f"Export {KINDS[kind]} du dossier `{dossier}` ({period}{model_note})"
        fec = fec_lines(dossier_id, journals, wizard, context) if kind == "fec" else None
        told = head + totals(kind, wizard_id, context, fec)
        tiers_note = ""
        method = "action_print_fec" if kind == "fec" else "print_excel_report"
        try:
            action = odoo_json(WIZARD, method, [wizard_id], context=context)
            if isinstance(action, dict) and action.get("res_model") == TIERS_WIZARD:
                found = odoo_call(TIERS_WIZARD, "read", [action["res_id"]], fields=["missing_count"], context=context)
                missing = found[0]["missing_count"] if found else 0
                tiers_note = (
                    f" {counted(missing, '{} ligne a', '{} lignes ont')} un tiers sans compte tiers dans Experio : "
                    "la colonne tiers restera vide dans le fichier (à corriger par le cabinet sur la fiche du tiers)."
                )
                if not client_confirmed:
                    return question(f"{told}.{tiers_note} J'exporte quand même ? oui / non")
                confirmed = {**context, "exp_export_tiers_confirmed": True}
                action = odoo_json(WIZARD, method, [wizard_id], context=confirmed)
        except ExperioRefusal as error:
            models = "" if template else models_note(model, kind, context)
            return stop(f"{told}. Experio refuse l'export : {experio_refusal(error)}{models}")
        if integration:
            if not client_confirmed:
                return question(
                    f"{told}.{tiers_note} En « Intégration », Experio marque ces pièces exportées : elles ne seront "
                    "plus modifiables (annulable par un administrateur, « Annuler l'exportation »). Je le fais ?"
                )
            locked = {**context, "exp_export_tiers_confirmed": True}
            odoo_call(WIZARD, "write", [wizard_id], {"integration": True}, context=context)
            told = head + totals(kind, wizard_id, context)  # integration leaves out the pieces already exported
            action = odoo_json(WIZARD, method, [wizard_id], context=locked)
        url = action.get("url", "") if isinstance(action, dict) else ""
        name = unquote(url.rsplit("/", 1)[-1].split("?")[0]) or f"Export {dossier}"
        extension = "txt" if kind == "fec" else "zip" if values.get("exp_export_file_type") == "text" else "xlsx"
        if not name.lower().endswith(f".{extension}"):
            name = f"{name}.{extension}"
        data = odoo_call(WIZARD, "read", [wizard_id], fields=["datas"], context=context)[0]["datas"]
    if not data:
        return stop(f"{told}. Experio n'a produit aucun fichier.")
    keep_file(session_id, name, base64.b64decode(data))
    locked_note = (
        " Pièces marquées exportées dans Experio (Intégration)."
        if integration
        else (" Export d'analyse : rien n'est verrouillé dans Experio.")
    )
    return ok(f"{told}.{tiers_note}{locked_note} Fichier `{name}` joint à cette réponse.", file_name=name)
