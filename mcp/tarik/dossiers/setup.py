"""A new dossier's setup after its creation (dossiers/identity.py): configuration copied from a model
dossier, entries (FEC or grand livre) imported, onboarding finished -- each through Experio's own screens.

Experio has no creation wizard: a dossier is a res.company created from the Folders list.
"Appliquer la configuration" (company.config.wizard) copies a model dossier's accounts, taxes,
journals, bank and TPE operations and settings. The Importation screen (base_import into
experio.history) creates whatever the entries need and the dossier lacks -- accounts, journals,
tiers, exercices -- and auto-detects the settings left empty. While the onboarding is open its lines
wait, and the stepper's "Enregistrer et terminer" sends those imported more than a minute ago to
Experio's processing (res.company's write): finish_setup waits for them, or leaves the dossier in
tarik_setups for a later run when the time left is too short. Once it is finished, imported
lines go to the processing at once (experio.history's create): a FEC can come any time after.

Order: the copy comes before the import. The wizard writes every setting of the model and
empties those whose account it can't find in the dossier; after an import it would erase what
the import detected, while before it the import only adds what the model lacks.
"""

import calendar
import csv
import io
import itertools
import re
import time
from collections import Counter
from datetime import date, datetime

import openpyxl
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, server, stop, wait, with_ids
from tarik.core.cabinets import cabinet_db
from tarik.core.config import ENTRY_FILES, TEST_MODE
from tarik.core.experio import dry_run, in_dossier, odoo_call, odoo_json, refused, require_dossier
from tarik.core.text import normalized
from tarik.invoices.exercice import open_exercice

# The import screen's options; the separator is left empty for Experio to detect it.
IMPORT_OPTIONS = {
    "has_headers": True,
    "separator": "",
    "quoting": '"',
    "encoding": "utf-8",
    "limit": 1_000_000,  # the whole file in one go: Experio's import ignores ``skip``
}
# Headers Experio's import doesn't suggest a column for, which the user then maps by hand on its
# screen. A Sage export's "Numero de piece" is the entry's reference (Experio's own table maps "piece"
# to it): Experio groups the lines into entries by it, and without it merges unrelated ones.
# shortcut: only the headers met so far; add a client's export's header when its import leaves a column unmapped.
MANUAL_COLUMNS = dict.fromkeys(
    ("numero de piece", "n de piece", "n piece", "num piece", "no piece", "numero piece"), "reference"
)
FEC_HEADERS = {"journalcode", "ecrituredate", "comptenum", "debit", "credit"}
ACCOUNT_HEADERS = ("comptenum", "compte", "numerocompte", "numcompte")


@server.tool()
@with_ids
def copy_configuration(dossier_id: int, model_dossier_id: int) -> dict:
    """Copy a model dossier's accounts, taxes, journals, bank and TPE operations and
    settings into a new dossier (Experio's "Appliquer la configuration"). The agent asks for it
    only when the client named the dossier to copy (skill "Nouveau dossier"). Run it before
    importing the entries: it overwrites the dossier's settings."""
    dossier, model = require_dossier(dossier_id), require_dossier(model_dossier_id)
    if dossier_id == model_dossier_id:
        raise ToolError("Le dossier modele doit etre un autre dossier.")
    if odoo_call("experio.history", "search_count", [("company_id", "=", dossier_id)]):
        raise ToolError(
            f"Le dossier {dossier} a deja des ecritures importees : copier la configuration "
            "effacerait les reglages qu'Experio en a tires."
        )
    if dry_run():
        return ok(f"{TEST_MODE}La configuration du dossier {model} serait copiee dans {dossier}.")
    parts = ("accounts", "taxes", "journals", "bank_operations", "monetary_operations", "configuration")
    wizard = {
        "source_company_id": model_dossier_id,
        "target_company_ids": [(6, 0, [dossier_id])],
        "state": "select_companies",
        **{f"apply_{part}": True for part in parts},
    }
    with refused(
        f"copy_configuration {model_dossier_id} -> {dossier_id}",
        "Experio n'a pas pu copier la configuration : {reason}",
    ):
        wizard_id = odoo_json("company.config.wizard", "create", wizard)
        odoo_json("company.config.wizard", "action_apply", [wizard_id])
    return ok(
        f"Configuration du dossier {model} copiee dans {dossier} : plan comptable, taxes, journaux, "
        "operations bancaires et TPE, parametres."
    )


# --- Importing the entries ---


def file_text(data):
    """The file (bytes) as text: the import screen reads raw text, and JSON only carries text."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def cell_text(value):
    """One Excel cell as the import screen reads it: dates as YYYYMMDD, whole floats without ".0"."""
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y%m%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def sheet_text(data):
    """An Excel file's (bytes) first sheet as the CSV text the import screen reads (it uploads Excel
    files itself, but JSON-RPC only carries text)."""
    sheet = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    for row in sheet.iter_rows(values_only=True):
        cells = [cell_text(value) for value in row]
        if any(cells):
            writer.writerow(cells)
    return out.getvalue()


def entries_kind(text, separator):
    """ "fec" or "grand livre", from the file alone (Experio has two buttons and no
    detector; tarik-master parametrage.entries_kind). A FEC follows the journals and
    the dates, so its accounts go back and forth; a grand livre lists them in order."""
    rows = list(csv.reader(io.StringIO(text), delimiter=separator or ";"))
    if not rows:
        return "fec"
    headers = [normalized(h).replace(" ", "") for h in rows[0]]
    column = next((i for i, h in enumerate(headers) if h in ACCOUNT_HEADERS), None)
    accounts = [
        r[column].strip()
        for r in rows[1:]
        if column is not None and column < len(r) and r[column].strip()[:1].isdigit()
    ]
    if len(accounts) >= 20:
        backwards = sum(1 for x, y in itertools.pairwise(accounts) if y < x)
        return "grand livre" if backwards / len(accounts) < 0.02 else "fec"
    return "fec" if set(headers) >= FEC_HEADERS else "grand livre"


def taken(file_id):
    """The file is done with, imported or refused: a corrected file comes as a new one."""
    with cabinet_db() as db:
        db.execute("update tarik_files set kind = 'entries' where id = %s", (file_id,))


def setup_waits(dossier_id, file_id=None):
    """The dossier waits for its onboarding, in the conversation of ``file_id`` (an import of it went through);
    without ``file_id``, no longer (finished, or an import refused: the client sends a corrected file)."""
    with cabinet_db() as db:
        if file_id:
            db.execute(
                "insert into tarik_setups (dossier_id, session_id) select %s, session_id from tarik_files "
                "where id = %s on conflict (dossier_id) do update set session_id = excluded.session_id",
                (dossier_id, file_id),
            )
        else:
            db.execute("delete from tarik_setups where dossier_id = %s", (dossier_id,))


@server.tool()
def waiting_setups(session_id: str) -> dict:
    """This conversation's dossiers whose entries were imported and whose onboarding still waits."""
    with cabinet_db() as db:
        rows = db.execute(
            "select dossier_id from tarik_setups where session_id = %s order by created_at", (session_id,)
        ).fetchall()
    return ok(f"{len(rows)} dossier(s) a terminer.", dossiers=[dossier_id for (dossier_id,) in rows])


class Refused(Exception):
    """An entries file not imported: its text is the stop message, and the file is taken."""


def entries_text(file_id, name, mime, data):
    """The file's text (CSV, TXT or an Excel sheet as CSV); Refused for another kind of file."""
    excel = name.lower().endswith((".xlsx", ".xlsm"))
    if not (name.lower().endswith(ENTRY_FILES) or (mime or "").startswith("text/")):
        raise Refused(f"{name} : envoyez le FEC ou le grand livre en CSV, TXT ou Excel (.xlsx).")
    with refused(f"import_entries {file_id}", f"{name} : fichier Excel illisible.", Refused):
        return sheet_text(bytes(data)) if excel else file_text(bytes(data))


def previewed(file_id, name, text, context):
    """The file uploaded to Experio's import screen: (import id, its preview: headers, matches, options)."""
    with refused(f"import_entries {file_id}", f"{name} : Experio n'a pas pu lire le fichier ({{reason}}).", Refused):
        upload = {"res_model": "experio.history", "file": text, "file_name": name, "file_type": "text/csv"}
        import_id = odoo_json("base_import.import", "create", upload, context=context)
        preview = odoo_json("base_import.import", "parse_preview", [import_id], IMPORT_OPTIONS, context=context)
    if preview.get("error"):
        raise Refused(f"{name} : Experio n'a pas pu lire le fichier ({preview['error']}).")
    return import_id, preview


def columns(preview):
    """The Experio field of each column of ``preview``, as the import screen suggests them, with MANUAL_COLUMNS
    filling the gaps a user maps by hand (False: column not imported)."""
    fields = [(preview["matches"].get(str(i)) or [False])[0] for i in range(len(preview["headers"]))]
    for i, header in enumerate(preview["headers"]):
        guess = MANUAL_COLUMNS.get(normalized(header))
        if not fields[i] and guess and guess not in fields:
            fields[i] = guess
    return fields


def chosen_kind(kind, text, separator):
    """ "fec" or "grand livre": as the agent said (``kind``), else told from the file."""
    wanted = normalized(kind or "")
    if "grand" in wanted or wanted == "gl":
        return "grand livre"
    return "fec" if "fec" in wanted else entries_kind(text, separator)


def errors_of(answer):
    """The error messages (list of str) in an execute_import ``answer``."""
    return [m.get("message", "") for m in answer.get("messages") or [] if m.get("type") == "error"]


@server.tool()
@with_ids
def import_entries(dossier_id: int, file_id: int, kind: str = "") -> dict:
    """Import a dossier's entries from a FEC or grand livre file (CSV, TXT or Excel), through
    Experio's Importation screen: it creates the missing accounts, journals, tiers and
    exercices. ``kind``: "fec", "grand livre", or empty to tell from the file.
    Experio tests the file first; nothing is imported when its test finds errors."""
    dossier = require_dossier(dossier_id)
    with cabinet_db() as db:
        row = db.execute("select file_name, mime_type, data from tarik_files where id = %s", (file_id,)).fetchone()
    if not row:
        raise ToolError(f"Aucun fichier n° {file_id}.")
    name = row[0]
    context = in_dossier(dossier_id)
    # Every refusal is a stop with the file taken: a corrected file comes as a new one, this one is not tried again.
    try:
        text = entries_text(file_id, *row)
        import_id, preview = previewed(file_id, name, text, context)
        options = {**IMPORT_OPTIONS, **preview.get("options", {})}
        kind = chosen_kind(kind, text, options.get("separator"))
        headers, fields = preview["headers"], columns(preview)
        # "Importer GL" is the same import with the lines sorted (by reference too when the file has one).
        gl = kind == "grand livre"
        options.update(order_lines=gl, order_lines_with_reference=gl and "reference" in fields)
        label = "Grand livre" if gl else "FEC"
        arguments = ([import_id], fields, headers, options)
        with refused(f"import_entries {file_id} test", f"{label} {name} refuse par Experio : {{reason}}", Refused):
            test = odoo_json("base_import.import", "execute_import", *arguments, True, context=context)
        errors = errors_of(test)
        if errors:
            raise Refused(
                f"{label} {name} refuse par le test Experio ({len(errors)} erreur(s)) : " + " | ".join(errors[:3])
            )
        if dry_run():
            lines = len(test.get("ids") or [])
            return ok(
                f"{TEST_MODE}{label} {name} : test Experio reussi ({lines} lignes), rien n'est importe.",
                file_id=file_id,
            )
        with refused(f"import_entries {file_id}", f"{label} {name} : Experio a refuse l'import ({{reason}}).", Refused):
            result = odoo_json("base_import.import", "execute_import", *arguments, False, context=context)
        errors = errors_of(result)
        if errors:
            raise Refused(f"{label} {name} refuse par Experio : " + " | ".join(errors[:3]))
    except Refused as refusal:
        taken(file_id)
        setup_waits(dossier_id)
        return stop(str(refusal), file_id=file_id)
    taken(file_id)
    setup_waits(dossier_id, file_id)
    return ok(
        f"{label} {name} importe dans le dossier {dossier} : {len(result.get('ids') or [])} lignes. "
        "Experio a cree les comptes, journaux, tiers et exercices manquants.",
        file_id=file_id,
    )


# --- Finishing the onboarding ---

# The stepper's last window ("Enregistrer et terminer"): the 4 journals and the tiers prefixes, as it shows them.
STEPPER_JOURNALS = [
    "local_purchase_journal_id",
    "foreign_purchase_journal_id",
    "local_sale_journal_id",
    "foreign_sale_journal_id",
]
STEPPER_PREFIXES = {
    "third_party_supplier_account": "4411",
    "third_party_foreign_supplier_account": "4412",
    "third_party_customer_account": "3421",
    "third_party_Foreign_customer_account": "34212",
}


STEPPER_SECONDS = 15  # what finish_setup needs after Experio's wait: the stepper's calls, then the dossier's state


def exercice_note(dossier_id, dossier):
    """Opens the current year's exercice when the dossier has no open one (not in test mode): what was done (str),
    or None when it had one."""
    if odoo_call("exp.fiscal.entries", "search_count", [("company_id", "=", dossier_id), ("state", "=", "open")]):
        return None
    year = date.today().year
    if dry_run():
        return f"un exercice {year} serait ouvert"
    with refused(
        f"finish_setup {dossier_id}", f"Dossier {dossier} : Experio refuse d'ouvrir l'exercice {year} ({{reason}})."
    ):
        open_exercice(dossier_id, year)
    return f"exercice {year} ouvert"


def lines_ready_in(dossier_id, context):
    """Seconds until the newest waiting line is over a minute old (0 when none waits): Experio hands over only
    those."""
    newest = odoo_call(
        "experio.history",
        "search_read",
        [("company_id", "=", dossier_id), ("state", "=", "new")],
        fields=["send_date"],
        order="send_date desc",
        limit=1,
        context=context,
    )
    if newest and newest[0]["send_date"]:
        sent = calendar.timegm(time.strptime(newest[0]["send_date"], "%Y-%m-%d %H:%M:%S"))  # Experio stores UTC
        return max(0.0, sent + 65 - time.time())
    return 0.0


def complete_stepper(company, context):
    """Walks the stepper to its last phase and presses "Enregistrer et terminer" with ``company``'s journals and
    tiers prefixes (the defaults where it has none)."""
    for phase in range(1, 9):
        state = {
            "current_phase": min(phase + 1, 8),
            "completed_phases": list(range(1, phase + 1)),
            "visited_phases": list(range(1, min(phase + 1, 8) + 1)),
        }
        odoo_json("res.company", "set_configuration_stepper_state", state, context=context)
    config = {key: company[key] and company[key][0] for key in STEPPER_JOURNALS}
    config.update({key: company[key] or prefix for key, prefix in STEPPER_PREFIXES.items()})
    odoo_json("res.company", "save_validation_config", config, context=context)
    odoo_json("res.company", "complete_onboarding", context=context)  # its write hands the waiting lines over


@server.tool()
@with_ids
def finish_setup(dossier_id: int, seconds_left: int = 0) -> dict:
    """Finish a new dossier's onboarding as its stepper does (Suivant to the last
    step, then Enregistrer et terminer): Experio then processes the imported entries.
    Opens the current exercice when the dossier has none. ``seconds_left``: the run's
    time left (0: no limit); when Experio's wait for the imported lines would outlast it,
    the dossier is left waiting (tarik_setups) for a later run."""
    dossier = require_dossier(dossier_id)
    context = in_dossier(dossier_id)
    fields = ["show_onboarding", *STEPPER_JOURNALS, *STEPPER_PREFIXES]
    company = odoo_call("res.company", "read", [dossier_id], fields=fields)[0]
    if not company["show_onboarding"]:
        setup_waits(dossier_id)
        return ok(f"Dossier {dossier} : integration deja terminee, Experio traite les ecritures des leur import.")
    notes = [note for note in (exercice_note(dossier_id, dossier),) if note]
    if dry_run():
        return ok(f"{TEST_MODE}Dossier {dossier} : integration terminee ({', '.join(notes + ['stepper termine'])}).")
    pause = lines_ready_in(dossier_id, context)
    if seconds_left and pause > seconds_left - STEPPER_SECONDS:
        return wait(
            f"Dossier {dossier} : Experio enregistre encore les ecritures importees ; son integration et l'etat "
            "du dossier suivront dans quelques minutes."
        )
    time.sleep(pause)
    with refused(
        f"finish_setup {dossier_id}", f"Dossier {dossier} : Experio refuse de terminer l'integration ({{reason}})."
    ):
        complete_stepper(company, context)
    setup_waits(dossier_id)
    waiting = odoo_call("experio.history", "search_count", [("company_id", "=", dossier_id)], context=context)
    notes.append(
        f"{waiting} ligne(s) d'ecritures transmises au traitement d'Experio" if waiting else "aucune ecriture importee"
    )
    return ok(f"Dossier {dossier} : integration terminee ({', '.join(notes)}).")


# --- Where the setup stands ---

# What posting an invoice needs (exp.invoice validate_local_settings / validate_foreign_settings).
POSTING_SETTINGS = {
    "local_supplier_account_id": "compte fournisseurs",
    "local_purchase_journal_id": "journal d'achats",
    "local_customer_account_id": "compte clients",
    "local_sale_journal_id": "journal de ventes",
    "foreign_supplier_account_id": "compte fournisseurs etrangers",
    "foreign_purchase_journal_id": "journal d'achats etrangers",
    "foreign_customer_account_id": "compte clients etrangers",
    "foreign_sale_journal_id": "journal de ventes etrangeres",
}
HISTORY_STATES = {
    "new": "en attente",
    "pending": "en cours de traitement",
    "done": "traitees",
    "reconciliation": "a lettrer",
    "exists": "deja presentes",
    "error": "en erreur",
}


def setup_facts(dossier_id, context):
    """What Experio says of the dossier's setup: (company settings, {entries state: count},
    error reasons read_group, exercices, {model: count} of accounts, journals, taxes and tiers)."""
    in_it = [("company_id", "=", dossier_id)]
    company = odoo_call("res.company", "read", [dossier_id], fields=["show_onboarding", *POSTING_SETTINGS])[0]
    # One query, so the counts add up while Experio processes the lines.
    groups = odoo_call("experio.history", "read_group", in_it, ["state"], ["state"], context=context)
    states = {group["state"]: group["state_count"] for group in groups}
    reasons = odoo_call(
        "experio.history",
        "read_group",
        [*in_it, ("state", "=", "error")],
        ["error_type"],
        ["error_type"],
        context=context,
    )
    exercices = odoo_call(
        "exp.fiscal.entries",
        "search_read",
        in_it,
        fields=["name", "state", "selected"],
        order="date_start",
        context=context,
    )
    counts = {
        model: odoo_call(model, "search_count", in_it, context=context)
        for model in ("exp.account", "exp.journal", "exp.tax", "exp.partner")
    }
    return company, states, reasons, exercices, counts


def error_reasons(reasons):
    """Experio's own diagnosis of the lines in error, to correct the source file: its 3 commonest reasons with
    their line counts ("3 comptes tiers ..." and "4 comptes tiers ..." are one reason)."""
    merged = Counter()
    for reason in reasons:
        text = (reason["error_type"] or "sans motif").strip()
        merged[re.sub(r"^\d+\s+", "", text)] += reason["error_type_count"]  # without the leading count and spaces
    return " | ".join(f"{text} ({n} lignes)" for text, n in merged.most_common(3))


@server.tool()
@with_ids
def setup_status(dossier_id: int) -> dict:
    """Where a dossier's setup stands: its imported entries by state, its exercices,
    and the settings Experio needs before an invoice can be posted."""
    dossier = require_dossier(dossier_id)
    with refused(f"setup_status {dossier_id}", f"Dossier {dossier} : Experio n'a pas pu donner l'etat du parametrage."):
        company, states, reasons, exercices, counts = setup_facts(dossier_id, in_dossier(dossier_id))
    parts = [
        f"{counts['exp.account']} comptes, {counts['exp.journal']} journaux, {counts['exp.tax']} taxes, "
        f"{counts['exp.partner']} tiers"
    ]
    entries = [f"{states[state]} {label}" for state, label in HISTORY_STATES.items() if states.get(state)]
    parts.append("ecritures importees : " + (", ".join(entries) if entries else "aucune"))
    if reasons:
        parts.append("erreurs signalees par Experio : " + error_reasons(reasons))
    opened = [e["name"] + (" (en cours)" if e["selected"] else "") for e in exercices if e["state"] == "open"]
    parts.append("exercices ouverts : " + (", ".join(opened) if opened else "aucun"))
    missing = [label for key, label in POSTING_SETTINGS.items() if not company[key]]
    if missing:
        parts.append("a parametrer avant de comptabiliser : " + ", ".join(missing))
    if company["show_onboarding"]:
        parts.append("integration pas encore terminee")
    return ok(f"Dossier {dossier} : " + " ; ".join(parts) + ".")
