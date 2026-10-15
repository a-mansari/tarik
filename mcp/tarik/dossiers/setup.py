"""A new dossier's setup after its creation (dossiers/identity.py): configuration copied from a model
dossier, entries (FEC or grand livre) imported, onboarding finished -- each through Experio's own screens.

Experio has no creation wizard: a dossier is a res.company created from the Folders list.
"Appliquer la configuration" (company.config.wizard) copies a model dossier's accounts, taxes,
journals, bank and TPE operations and settings. The Importation screen (base_import into
experio.history) creates whatever the entries need and the dossier lacks -- accounts, journals,
tiers, exercices -- and auto-detects the settings left empty. Its lines wait until the onboarding
stepper's "Enregistrer et terminer", which hands them to Experio's processing.

Order: the copy comes before the import. The wizard writes every setting of the model and
empties those whose account it can't find in the dossier; after an import it would erase what
the import detected, while before it the import only adds what the model lacks.
"""

import calendar
import csv
import io
import re
import time
from collections import Counter
from datetime import date, datetime

import openpyxl
from mcp.server.mcpserver.exceptions import ToolError

from tarik.core.app import ok, server, stop, with_ids
from tarik.core.cabinets import cabinet_db
from tarik.core.conversation import client_messages
from tarik.core.experio import TEST_MODE, dry_run, experio_refusal, odoo_call, odoo_json, refused, require_dossier
from tarik.dossiers.search import normalized
from tarik.invoices.exercice import open_exercice

# The import screen's options; the separator is left empty for Experio to detect it.
IMPORT_OPTIONS = {"has_headers": True, "separator": "", "quoting": '"', "encoding": "utf-8",
                  # The whole file in one go: Experio's import ignores ``skip``.
                  "limit": 1_000_000}
# Headers Experio's import doesn't suggest a column for, which the user then maps by hand on its
# screen. A Sage export's "Numero de piece" is the entry's reference (Experio's own table maps "piece"
# to it): Experio groups the lines into entries by it, and without it merges unrelated ones.
# shortcut: only the headers met so far; add a client's export's header when its import leaves a column unmapped.
MANUAL_COLUMNS = {"numero de piece": "reference", "n de piece": "reference", "n piece": "reference",
                  "num piece": "reference", "no piece": "reference", "numero piece": "reference"}
FEC_HEADERS = {"journalcode", "ecrituredate", "comptenum", "debit", "credit"}


COPY_WORDS = ("configuration", "config", "parametrage", "copie", "copier", "modele", "comme", "meme")
GENERIC_WORDS = {"sarl", "sa", "sas", "ste", "societe", "test", "the", "et", "de", "du", "la", "le"}


def copy_asked(messages, model_name):
    """Did the client ask, in their own words, for this dossier's configuration? One of
    their messages names it and asks to copy it: MiniMax offered the copy unprompted."""
    names = {word for word in normalized(model_name).split() if len(word) >= 3 and word not in GENERIC_WORDS}
    for message in messages:
        words = normalized(message or "").split()
        if names & set(words) and any(word.startswith(COPY_WORDS) for word in words):
            return True
    return False


@server.tool()
@with_ids
def copy_configuration(dossier_id: int, model_dossier_id: int, session_id: str = "", client_message: str = "") -> dict:
    """Copy a model dossier's accounts, taxes, journals, bank and TPE operations and
    settings into a new dossier (Experio's "Appliquer la configuration"), only when the
    client asked for it in this conversation. Run it before importing the entries: it
    overwrites the dossier's settings."""
    dossier, model = require_dossier(dossier_id), require_dossier(model_dossier_id)
    if dossier_id == model_dossier_id:
        raise ToolError("Le dossier modele doit etre un autre dossier.")
    if not copy_asked(client_messages(session_id, client_message), model):  # the rest goes on, with the dossier as created
        return ok(f"Configuration de {model} non copiee : le client ne l'a pas demandee.")
    if odoo_call("experio.history", "search_count", [("company_id", "=", dossier_id)]):
        raise ToolError(f"Le dossier {dossier} a deja des ecritures importees : copier la configuration "
                        "effacerait les reglages qu'Experio en a tires.")
    if dry_run():
        return ok(f"{TEST_MODE}La configuration du dossier {model} serait copiee dans {dossier}.")
    try:
        wizard_id = odoo_json("company.config.wizard", "create", {
            "source_company_id": model_dossier_id, "target_company_ids": [(6, 0, [dossier_id])], "state": "select_companies",
            **{f"apply_{part}": True for part in ("accounts", "taxes", "journals", "bank_operations",
                                                  "monetary_operations", "configuration")}})
        odoo_json("company.config.wizard", "action_apply", [wizard_id])
    except Exception as error:
        print(f"copy_configuration {model_dossier_id} -> {dossier_id}: {error}", flush=True)
        raise ToolError(f"Experio n'a pas pu copier la configuration : {experio_refusal(error)}") from None
    return ok(f"Configuration du dossier {model} copiee dans {dossier} : plan comptable, taxes, journaux, "
              "operations bancaires et TPE, parametres.")


def file_text(data):
    """The file as text: the import screen reads raw text, and JSON only carries text."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def sheet_text(data):
    """An Excel file's first sheet as the CSV text the import screen reads (it uploads Excel
    files itself, but JSON-RPC only carries text): dates as Experio reads them, the rest as is."""
    sheet = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    for row in sheet.iter_rows(values_only=True):
        cells = ["" if value is None else value.strftime("%Y%m%d") if isinstance(value, (date, datetime))
                 else str(int(value)) if isinstance(value, float) and value.is_integer() else str(value) for value in row]
        if any(cells):
            writer.writerow(cells)
    return out.getvalue()


def entries_kind(text, separator):
    """FEC or grand livre, from the file alone (Experio has two buttons and no
    detector; tarik-master parametrage.entries_kind). A FEC follows the journals and
    the dates, so its accounts go back and forth; a grand livre lists them in order."""
    rows = list(csv.reader(io.StringIO(text), delimiter=separator or ";"))
    if not rows:
        return "fec"
    headers = [normalized(h).replace(" ", "") for h in rows[0]]
    column = next((i for i, h in enumerate(headers) if h in ("comptenum", "compte", "numerocompte", "numcompte")), None)
    accounts = [r[column].strip() for r in rows[1:] if column is not None and column < len(r) and r[column].strip()[:1].isdigit()]
    if len(accounts) >= 20:
        backwards = sum(1 for x, y in zip(accounts, accounts[1:]) if y < x)
        return "grand livre" if backwards / len(accounts) < 0.02 else "fec"
    return "fec" if FEC_HEADERS <= set(headers) else "grand livre"


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
    name, mime, data = row
    excel = name.lower().endswith((".xlsx", ".xlsm"))
    if not (excel or name.lower().endswith((".csv", ".txt")) or (mime or "").startswith("text/")):
        return stop(f"{name} : envoyez le FEC ou le grand livre en CSV, TXT ou Excel (.xlsx).", file_id=file_id)
    with refused(f"import_entries {file_id}", f"{name} : fichier Excel illisible."):
        text = sheet_text(bytes(data)) if excel else file_text(bytes(data))
    context = {"allowed_company_ids": [dossier_id]}
    try:
        import_id = odoo_json("base_import.import", "create", {"res_model": "experio.history", "file": text,
                                                              "file_name": name, "file_type": "text/csv"}, context=context)
        preview = odoo_json("base_import.import", "parse_preview", [import_id], IMPORT_OPTIONS, context=context)
    except Exception as error:
        print(f"import_entries {file_id}: {error}", flush=True)
        raise ToolError(f"{name} : Experio n'a pas pu lire le fichier ({experio_refusal(error)}).") from None
    if preview.get("error"):
        raise ToolError(f"{name} : Experio n'a pas pu lire le fichier ({preview['error']}).")
    options = {**IMPORT_OPTIONS, **preview.get("options", {})}
    wanted = normalized(kind or "")
    kind = "grand livre" if "grand" in wanted or wanted == "gl" else "fec" if "fec" in wanted else entries_kind(text, options.get("separator"))
    headers = preview["headers"]
    fields = [(preview["matches"].get(str(i)) or [False])[0] for i in range(len(headers))]
    for i, header in enumerate(headers):
        guess = MANUAL_COLUMNS.get(normalized(header))
        if not fields[i] and guess and guess not in fields:
            fields[i] = guess
    # "Importer GL" is the same import with the lines sorted (by reference too when the file has one).
    options.update(order_lines=kind == "grand livre", order_lines_with_reference=kind == "grand livre" and "reference" in fields)
    label = "Grand livre" if kind == "grand livre" else "FEC"
    try:
        test = odoo_json("base_import.import", "execute_import", [import_id], fields, headers, options, True, context=context)
    except Exception as error:
        print(f"import_entries {file_id} test: {error}", flush=True)
        return stop(f"{label} {name} refuse par Experio : {experio_refusal(error)}", file_id=file_id)
    errors = [m.get("message", "") for m in test.get("messages") or [] if m.get("type") == "error"]
    if errors:
        return stop(f"{label} {name} refuse par le test Experio ({len(errors)} erreur(s)) : " + " | ".join(errors[:3]),
                    file_id=file_id)
    lines = len(test.get("ids") or [])
    if dry_run():
        return ok(f"{TEST_MODE}{label} {name} : test Experio reussi ({lines} lignes), rien n'est importe.", file_id=file_id)
    try:
        result = odoo_json("base_import.import", "execute_import", [import_id], fields, headers, options, False, context=context)
    except Exception as error:
        print(f"import_entries {file_id}: {error}", flush=True)
        raise ToolError(f"{label} {name} : Experio a refuse l'import ({experio_refusal(error)}).") from None
    errors = [m.get("message", "") for m in result.get("messages") or [] if m.get("type") == "error"]
    if errors:
        return stop(f"{label} {name} refuse par Experio : " + " | ".join(errors[:3]), file_id=file_id)
    with cabinet_db() as db:  # not an invoice: the invoice sequence leaves it alone
        db.execute("update tarik_files set kind = 'entries' where id = %s", (file_id,))
    return ok(f"{label} {name} importe dans le dossier {dossier} : {len(result.get('ids') or [])} lignes. "
              "Experio a cree les comptes, journaux, tiers et exercices manquants.", file_id=file_id)


# The stepper's last window ("Enregistrer et terminer"): the 4 journals and the tiers prefixes, as it shows them.
STEPPER_JOURNALS = ["local_purchase_journal_id", "foreign_purchase_journal_id", "local_sale_journal_id", "foreign_sale_journal_id"]
STEPPER_PREFIXES = {"third_party_supplier_account": "4411", "third_party_foreign_supplier_account": "4412",
                    "third_party_customer_account": "3421", "third_party_Foreign_customer_account": "34212"}


@server.tool()
@with_ids
def finish_setup(dossier_id: int) -> dict:
    """Finish a new dossier's onboarding as its stepper does (Suivant to the last
    step, then Enregistrer et terminer): Experio then processes the imported entries.
    Opens the current exercice when the dossier has none."""
    dossier = require_dossier(dossier_id)
    context = {"allowed_company_ids": [dossier_id]}
    company = odoo_call("res.company", "read", [dossier_id], fields=["show_onboarding", *STEPPER_JOURNALS, *STEPPER_PREFIXES])[0]
    if not company["show_onboarding"]:
        return ok(f"Dossier {dossier} : integration deja terminee.")
    notes = []
    if not odoo_call("exp.fiscal.entries", "search_count", [("company_id", "=", dossier_id), ("state", "=", "open")]):
        year = date.today().year
        if dry_run():
            notes.append(f"un exercice {year} serait ouvert")
        else:
            try:
                open_exercice(dossier_id, year)
            except Exception as error:
                print(f"finish_setup {dossier_id}: {error}", flush=True)
                raise ToolError(f"Dossier {dossier} : Experio refuse d'ouvrir l'exercice {year} ({experio_refusal(error)}).") from None
            notes.append(f"exercice {year} ouvert")
    if dry_run():
        return ok(f"{TEST_MODE}Dossier {dossier} : integration terminee ({', '.join(notes + ['stepper termine'])}).")
    # Experio hands over only lines imported more than a minute ago: wait for the newest.
    newest = odoo_call("experio.history", "search_read", [("company_id", "=", dossier_id), ("state", "=", "new")],
                       fields=["send_date"], order="send_date desc", limit=1, context=context)
    if newest and newest[0]["send_date"]:
        sent = calendar.timegm(time.strptime(newest[0]["send_date"], "%Y-%m-%d %H:%M:%S"))  # Experio stores UTC
        time.sleep(max(0.0, sent + 65 - time.time()))
    try:
        for phase in range(1, 9):
            odoo_json("res.company", "set_configuration_stepper_state", {
                "current_phase": min(phase + 1, 8), "completed_phases": list(range(1, phase + 1)),
                "visited_phases": list(range(1, min(phase + 1, 8) + 1))}, context=context)
        config = {key: company[key] and company[key][0] for key in STEPPER_JOURNALS}
        config.update({key: company[key] or prefix for key, prefix in STEPPER_PREFIXES.items()})
        odoo_json("res.company", "save_validation_config", config, context=context)
        odoo_json("res.company", "complete_onboarding", context=context)  # its write hands the waiting lines over
    except Exception as error:
        print(f"finish_setup {dossier_id}: {error}", flush=True)
        raise ToolError(f"Dossier {dossier} : Experio refuse de terminer l'integration ({experio_refusal(error)}).") from None
    waiting = odoo_call("experio.history", "search_count", [("company_id", "=", dossier_id)], context=context)
    notes.append(f"{waiting} ligne(s) d'ecritures transmises au traitement d'Experio" if waiting else "aucune ecriture importee")
    return ok(f"Dossier {dossier} : integration terminee ({', '.join(notes)}).")


# What posting an invoice needs (exp.invoice validate_local_settings / validate_foreign_settings).
POSTING_SETTINGS = {"local_supplier_account_id": "compte fournisseurs", "local_purchase_journal_id": "journal d'achats",
                    "local_customer_account_id": "compte clients", "local_sale_journal_id": "journal de ventes",
                    "foreign_supplier_account_id": "compte fournisseurs etrangers",
                    "foreign_purchase_journal_id": "journal d'achats etrangers",
                    "foreign_customer_account_id": "compte clients etrangers", "foreign_sale_journal_id": "journal de ventes etrangeres"}
HISTORY_STATES = {"new": "en attente", "pending": "en cours de traitement", "done": "traitees", "reconciliation": "a lettrer",
                  "exists": "deja presentes", "error": "en erreur"}


@server.tool()
@with_ids
def setup_status(dossier_id: int) -> dict:
    """Where a dossier's setup stands: its imported entries by state, its exercices,
    and the settings Experio needs before an invoice can be posted."""
    dossier = require_dossier(dossier_id)
    context = {"allowed_company_ids": [dossier_id]}
    with refused(f"setup_status {dossier_id}", f"Dossier {dossier} : Experio n'a pas pu donner l'etat du parametrage."):
        company = odoo_call("res.company", "read", [dossier_id], fields=["show_onboarding", *POSTING_SETTINGS])[0]
        # One query, so the counts add up while Experio processes the lines.
        groups = odoo_call("experio.history", "read_group", [("company_id", "=", dossier_id)], ["state"], ["state"], context=context)
        states = {group["state"]: group["state_count"] for group in groups}
        reasons = odoo_call("experio.history", "read_group", [("company_id", "=", dossier_id), ("state", "=", "error")],
                            ["error_type"], ["error_type"], context=context)
        exercices = odoo_call("exp.fiscal.entries", "search_read", [("company_id", "=", dossier_id)],
                              fields=["name", "state", "selected"], order="date_start", context=context)
        counts = {model: odoo_call(model, "search_count", [("company_id", "=", dossier_id)], context=context)
                  for model in ("exp.account", "exp.journal", "exp.tax", "exp.partner")}
    parts = [f"{counts['exp.account']} comptes, {counts['exp.journal']} journaux, {counts['exp.tax']} taxes, "
             f"{counts['exp.partner']} tiers"]
    entries = [f"{states[state]} {label}" for state, label in HISTORY_STATES.items() if states.get(state)]
    parts.append("ecritures importees : " + (", ".join(entries) if entries else "aucune"))
    if reasons:  # Experio's own diagnosis, to correct the source file ("3 comptes tiers ...", "4 comptes tiers ...": one reason)
        merged = Counter()
        for reason in reasons:
            merged[re.sub(r"^\d+\s+", "", (reason["error_type"] or "sans motif").strip())] += reason["error_type_count"]
        parts.append("erreurs signalees par Experio : " + " | ".join(f"{text} ({n} lignes)" for text, n in merged.most_common(3)))
    opened = [e["name"] + (" (en cours)" if e["selected"] else "") for e in exercices if e["state"] == "open"]
    parts.append("exercices ouverts : " + (", ".join(opened) if opened else "aucun"))
    missing = [label for key, label in POSTING_SETTINGS.items() if not company[key]]
    if missing:
        parts.append("a parametrer avant de comptabiliser : " + ", ".join(missing))
    if company["show_onboarding"]:
        parts.append("integration pas encore terminee")
    return ok(f"Dossier {dossier} : " + " ; ".join(parts) + ".")
