"""Set up a new dossier after its creation (dossiers/identity.py), through Experio's own screens: copy the
configuration of a model dossier, import the entries (FEC or grand livre) or the setup files (plan comptable,
journaux, fournisseurs, clients), give each bank journal its bank, then finish the onboarding.

A dossier is a res.company created from the Folders list. "Appliquer la configuration"
(company.config.wizard) copies a model dossier's accounts, taxes, journals, bank and TPE operations and
settings. The Importation screen (base_import into experio.history) creates what the entries need and the
dossier lacks (accounts, journals, tiers, exercices), and Experio then fills the settings still empty.
While the onboarding is open, imported lines wait in state "new". Finishing it (res.company write of
show_onboarding False) re-sends the waiting lines with action_resend, which only takes lines sent more than
a minute ago: finish_setup waits for that minute, or leaves the dossier in tarik_setups for a later run when
the run's time left is too short. After the onboarding, experio.history's create sends new lines to
processing at once, so a FEC can come any time later.

Order: copy the configuration before importing. The wizard writes every setting of the model and empties
each setting whose account, journal or tax it can't find in the dossier: after an import it would erase what
the import detected, while before it the import only fills what the model lacks.
"""

import calendar
import csv
import io
import itertools
import re
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import openpyxl

from engine.tools.core.app import ToolError, log, ok, question, routine, step_tool, stop, tool, wait
from engine.tools.core.cabinets import cabinet_db, current
from engine.tools.core.config import ENTRY_FILES, TEST_MODE
from engine.tools.core.experio import (
    dossier_lock,
    dry_run,
    experio_refusal,
    in_dossier,
    misplaced_journals,
    odoo_call,
    odoo_json,
    refused,
    require_dossier,
    test_mode_note,
    would,
)
from engine.tools.core.queries import Q
from engine.tools.core.words import counted, normalized, said
from engine.tools.dossiers.banks import bank_named, give_bank, journal_bank, padded
from engine.tools.invoices.exercice import open_exercice
from engine.tools.invoices.intake import file_row
from engine.tools.invoices.journal import SET, set_journal

# The import screen's options; the separator is left empty for Experio to detect it.
IMPORT_OPTIONS = {
    "has_headers": True,
    "separator": "",
    "quoting": '"',
    "encoding": "utf-8",
    "limit": 1_000_000,  # the whole file in one go: Experio's import ignores ``skip``
}
# What each Experio import screen loads, as the web user picks it from its menu: the model, the context
# that screen opens with, and how the client reads it.
TARGETS = {
    "ecritures": ("experio.history", {}, "Écritures"),
    "plan": ("exp.account", {}, "Plan comptable"),
    "journaux": ("exp.journal", {}, "Journaux"),
    "fournisseurs": ("exp.partner", {"default_is_supplier": True}, "Fournisseurs"),
    "clients": ("exp.partner", {"default_is_customer": True}, "Clients"),
}
SETUP_FILES = ("plan", "journaux", "fournisseurs", "clients")  # a dossier set up without a FEC sends these
# Headers Experio's import doesn't suggest a column for, which the user then maps by hand on its
# screen. Entries: a Sage export's "Numero de piece" is the entry's reference (Experio's own table maps "piece"
# to it): Experio groups the lines into entries by it, and without it merges unrelated ones. The setup files:
# the columns of the procedure de parametrage (parties 4, 5, 9, 10).
# shortcut: only the headers met so far; add a client's export's header when its import leaves a column unmapped.
MANUAL_COLUMNS = {
    "ecritures": dict.fromkeys(
        ("numero de piece", "n de piece", "n piece", "num piece", "no piece", "numero piece"), "reference"
    ),
    "plan": {"type": "account_type", "type de compte": "account_type", "signe": "sign", "lettrage": "reconcile"},
    "journaux": {
        "compte par defaut": "default_account_id",
        "compte": "default_account_id",
        "nom du journal": "name",
        "code journal": "code",
        "banque": "bank_id",
    },
    "fournisseurs": {"compte tiers fournisseur": "third_party_supplier_account", "if": "vat", "ice": "ice"},
    "clients": {"compte tiers client": "third_party_customer_account", "if": "vat", "ice": "ice"},
}
FEC_HEADERS = {"journalcode", "ecrituredate", "comptenum", "debit", "credit"}
ACCOUNT_HEADERS = ("comptenum", "compte", "numerocompte", "numcompte")


def make_advance_accounts_reconcilable(model_dossier_id):
    """Mark reconcilable the accounts used by the model dossier's advance operations; return how many changed.

    Experio requires a reconcilable account on an advance operation (bank.operation.type
    _check_advance_account_is_reconcilable), yet exp.account's reconcile defaults to False, so 4421 and 3411
    (Avance Client, Avance Fournisseur) often are not. company.config.wizard copies each account's reconcile flag
    and then the advance operations, which that rule would refuse."""
    advances = odoo_call(
        "bank.operation.type",
        "search_read",
        [("company_id", "=", model_dossier_id), ("type", "in", ["sale_advance", "purchase_advance"])],
        fields=["account_id"],
    )
    accounts = [op["account_id"][0] for op in advances if op["account_id"]]
    unset = odoo_call("exp.account", "search", [("id", "in", accounts), ("reconcile", "=", False)]) if accounts else []
    if unset:
        odoo_call("exp.account", "write", unset, {"reconcile": True})
    return len(unset)


@step_tool
def copy_configuration(dossier_id: int, model_dossier_id: int) -> dict:
    """Copy a model dossier's accounts, taxes, journals, bank and TPE operations and settings into a new
    dossier (Experio's "Appliquer la configuration", company.config.wizard).

    The agent calls it only when the client named the dossier to copy (skill "Nouveau dossier"). Returns an ok
    result (in test mode, what would be copied).

    Raises:
        ToolError: the model is the dossier itself, or the dossier already has imported entries (the copy
            overwrites the settings Experio detected from them).
    """
    dossier, model = require_dossier(dossier_id), require_dossier(model_dossier_id)
    if dossier_id == model_dossier_id:
        raise ToolError("Le dossier modèle doit être un autre dossier.")
    if odoo_call("experio.history", "search_count", [("company_id", "=", dossier_id)]):
        raise ToolError(
            f"Le dossier `{dossier}` a déjà des écritures importées : copier la configuration "
            "effacerait les réglages qu'Experio en a tirés."
        )
    if dry_run():
        return ok(f"{TEST_MODE}La configuration du dossier `{model}` serait copiée dans `{dossier}`.")
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
        make_advance_accounts_reconcilable(model_dossier_id)
        wizard_id = odoo_json("company.config.wizard", "create", wizard)
        odoo_json("company.config.wizard", "action_apply", [wizard_id])
    return ok(
        f"J'ai copié la configuration du dossier `{model}` dans `{dossier}` : plan comptable, taxes, journaux, "
        "opérations bancaires et TPE, paramètres."
    )


# --- Importing the entries ---


def file_text(data):
    """Return the file's bytes ``data`` as text (UTF-8, else cp1252, else latin-1): the import screen reads raw
    text, and JSON only carries text."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def cell_text(value):
    """Return one Excel cell as the import screen reads it: "" for empty, dates as YYYYMMDD, whole floats
    without ".0"."""
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y%m%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def sheet_text(data):
    """Return the first sheet of an Excel file (bytes ``data``) as ";"-separated CSV text, empty rows dropped.
    The import screen uploads Excel files itself, but JSON-RPC only carries text."""
    sheet = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    for row in sheet.iter_rows(values_only=True):
        cells = [cell_text(value) for value in row]
        if any(cells):
            writer.writerow(cells)
    return out.getvalue()


def table_text(name, data):
    """Return a spreadsheet file's text from its ``name`` and bytes ``data``: the first sheet as CSV for .xlsx
    or .xlsm, the decoded file otherwise."""
    return sheet_text(bytes(data)) if name.lower().endswith((".xlsx", ".xlsm")) else file_text(bytes(data))


def separator_of(text):
    """Return the CSV separator of ``text``: whichever of ; , tab | its first line holds most of."""
    first = text.split("\n", 1)[0]
    return max(";,\t|", key=first.count)


def entries_kind(text, separator):
    """Return "fec" or "grand livre" for an entries file's ``text`` split by ``separator``.

    Experio has one import button for each and no detector. A FEC follows the journals and dates, so its
    account numbers go back and forth; a grand livre lists them in order (under 2 % going back). With fewer
    than 20 account numbers, the FEC's own headers decide."""
    rows = list(csv.reader(io.StringIO(text), delimiter=separator or ";"))
    if not rows:
        return "fec"
    headers = [normalized(h).replace(" ", "") for h in rows[0]]
    column = next((i for i, h in enumerate(headers) if h in ACCOUNT_HEADERS), None)
    accounts = []
    if column is not None:
        accounts = [r[column].strip() for r in rows[1:] if column < len(r) and r[column].strip()[:1].isdigit()]
    if len(accounts) >= 20:
        backwards = sum(1 for x, y in itertools.pairwise(accounts) if y < x)
        return "grand livre" if backwards / len(accounts) < 0.02 else "fec"
    return "fec" if set(headers) >= FEC_HEADERS else "grand livre"


def taken(file_id):
    """Mark the file ``file_id`` as taken (kind "entries"), imported or refused: a corrected file comes as a
    new one."""
    with cabinet_db() as db:
        db.execute(Q.file_taken_as_entries, (file_id,))


def setup_waits(dossier_id, file_id=None):
    """Record or clear, in tarik_setups, that the dossier waits for its onboarding to be finished.

    With ``file_id`` (a file whose import went through): record the wait, in that file's conversation.
    Without it: delete the wait (onboarding finished, nothing to wait for, or an import refused, after which
    the client sends a corrected file)."""
    with cabinet_db() as db:
        if file_id:
            db.execute(
                Q.upsert_setup_wait,
                (dossier_id, file_id),
            )
        else:
            db.execute(Q.delete_setup_wait, (dossier_id,))


@tool
def waiting_setups(session_id: str) -> dict:
    """Return an ok result listing (``dossiers``: ids) the dossiers waiting in tarik_setups for this
    conversation: an import went through and the onboarding is not yet finished, or lines are still processed."""
    with cabinet_db() as db:
        rows = db.execute(Q.setups_waiting_in_session, (session_id,)).fetchall()
    told = counted(
        len(rows), "{} dossier attend la fin de son intégration.", "{} dossiers attendent la fin de leur intégration."
    )
    return ok(told, dossiers=[dossier_id for (dossier_id,) in rows])


class Refused(Exception):
    """An entries or setup file not imported: its text is the stop message; import_entries marks the file
    taken."""


def entries_text(file_id, name, mime, data):
    """Return the file's text (CSV, TXT, or an Excel sheet as CSV); raise Refused for another kind of file or
    an unreadable Excel file."""
    if not (name.lower().endswith(ENTRY_FILES) or (mime or "").startswith("text/")):
        raise Refused(f"`{name}` : envoyez le FEC ou le grand livre en CSV, TXT ou Excel (.xlsx).")
    with refused(f"import_entries {file_id}", f"`{name}` : fichier Excel illisible.", Refused):
        return table_text(name, data)


def previewed(file_id, name, text, context, model="experio.history"):
    """Upload ``text`` to Experio's import screen for ``model`` (base_import.import) and return
    (import_id, preview), the preview being parse_preview's dict (headers, matches, options).
    Raises Refused when Experio can't read the file."""
    with refused(f"import_entries {file_id}", f"`{name}` : Experio n'a pas pu lire le fichier ({{reason}}).", Refused):
        upload = {"res_model": model, "file": text, "file_name": name, "file_type": "text/csv"}
        import_id = odoo_json("base_import.import", "create", upload, context=context)
        preview = odoo_json("base_import.import", "parse_preview", [import_id], IMPORT_OPTIONS, context=context)
    if preview.get("error"):
        raise Refused(f"`{name}` : Experio n'a pas pu lire le fichier ({preview['error']}).")
    return import_id, preview


def columns(preview, target):
    """Return the Experio field of each column of ``preview`` (False: column not imported): the import screen's
    best suggestion, else the ``target``'s MANUAL_COLUMNS entry a user would map by hand, if no other column
    has that field yet."""
    fields = []
    for i in range(len(preview["headers"])):
        suggested = preview["matches"].get(str(i))  # the screen's suggestions for column i, best first
        fields.append(suggested[0] if suggested else False)
    for i, header in enumerate(preview["headers"]):
        guess = MANUAL_COLUMNS[target].get(normalized(header))
        if not fields[i] and guess and guess not in fields:
            fields[i] = guess
    return fields


KIND_WORDS = {  # what the agent (the client's words) calls a file -> its kind
    "fec": "fec",
    "grand livre": "grand livre",
    "gl": "grand livre",
    "plan": "plan",
    "plan comptable": "plan",
    "comptes": "plan",
    "journaux": "journaux",
    "journal": "journaux",
    "fournisseurs": "fournisseurs",
    "clients": "clients",
}


def told_kind(kind):
    """Return the file kind named by ``kind`` (the client's words, passed by the agent), or None."""
    wanted = normalized(kind or "")
    if wanted in KIND_WORDS:
        return KIND_WORDS[wanted]
    for word, found in KIND_WORDS.items():
        if said(word, wanted):
            return found
    return None


def file_kind(kind, text):
    """Return the file's kind for Experio's import screens: "fec", "grand livre", "plan", "journaux",
    "fournisseurs" or "clients"; None when neither ``kind`` (the client's words) nor the headers of ``text``
    tell (the client is then asked)."""
    told = told_kind(kind)
    if told:
        return told
    separator = separator_of(text)
    headers = {normalized(h) for h in text.split("\n", 1)[0].split(separator)}
    joined = " ".join(headers)
    if headers & FEC_HEADERS or {"debit", "credit"} <= headers or "ecriture" in joined:
        return entries_kind(text, separator)
    if said("fournisseur", joined) or "compte tiers fournisseur" in headers:
        return "fournisseurs"
    if said("client", joined) or "compte tiers client" in headers:
        return "clients"
    if "journal" in joined or said("banque", joined):
        return "journaux"
    if {"type de compte", "signe", "lettrage"} & headers or (
        headers & {"code", "compte", "numero"} and {"intitule", "libelle", "nom"} & headers
    ):
        return "plan"
    return None


# --- Banks: Experio's own list (res.bank), as a client names one (engine/tools/dossiers/banks.py) ---


def with_bank_names(text, separator):
    """Return the journals file ``text`` with each "Banque" cell ("CIH", "attijari") replaced by that bank's
    exact name in Experio's list, since the import matches names exactly. A bank Experio doesn't know is
    emptied (ask_banks asks for it later). Text without a "Banque" column comes back unchanged."""
    rows = list(csv.reader(io.StringIO(text), delimiter=separator))
    at = next((i for i, h in enumerate(rows[0]) if normalized(h) == "banque"), None) if rows else None
    if at is None:
        return text
    banks = odoo_call("res.bank", "search_read", [], fields=["name"])
    for row in rows[1:]:
        if at < len(row) and row[at].strip():
            found = bank_named(row[at], banks)
            row[at] = found["name"] if found else ""
    out = io.StringIO()
    csv.writer(out, delimiter=separator).writerows(rows)
    return out.getvalue()


def errors_of(answer):
    """Return the error messages (list of str) in an execute_import ``answer``."""
    return [m.get("message", "") for m in answer.get("messages") or [] if m.get("type") == "error"]


def tested(file_id, name, label, arguments, context):
    """Run Experio's test of the import (execute_import with dryrun True) and return its answer.
    ``label`` names the file to the client; ``arguments`` is ([import_id], fields, headers, options).
    Raises Refused when the test finds errors."""
    with refused(
        f"import_entries {file_id} test", f"{label} `{name}` : Experio a refusé le fichier ({{reason}}).", Refused
    ):
        test = odoo_json("base_import.import", "execute_import", *arguments, True, context=context)
    errors = errors_of(test)
    if errors:
        found = counted(len(errors), "{} erreur", "{} erreurs")
        raise Refused(
            f"{label} `{name}` : le test d'Experio a trouvé {found}, je n'ai rien importé. Corrigez le fichier puis "
            "renvoyez-le." + "".join(f"\n- {error}" for error in errors[:3])
        )
    return test


def imported(file_id, name, label, arguments, context):
    """Run the import itself (execute_import, same ``arguments`` as tested) and return Experio's answer.
    Raises Refused when it reports errors."""
    with refused(f"import_entries {file_id}", f"{label} `{name}` : Experio a refusé l'import ({{reason}}).", Refused):
        result = odoo_json("base_import.import", "execute_import", *arguments, False, context=context)
    errors = errors_of(result)
    if errors:
        raise Refused(
            f"{label} `{name}` : Experio a refusé l'import." + "".join(f"\n- {error}" for error in errors[:3])
        )
    return result


@step_tool
def import_entries(dossier_id: int, file_id: int, kind: str = "") -> dict:
    """Import a FEC or grand livre file, or a setup file (plan comptable, journaux, fournisseurs, clients),
    into the dossier through Experio's Importation screens. CSV, TXT or Excel.

    ``kind``: "fec", "grand livre", "plan", "journaux", "fournisseurs", "clients" (the client's words are
    accepted), or empty to tell from the file.

    Returns:
        - a question result when nothing tells the file's kind (the file stays untaken);
        - a stop result when the same content was already imported into this dossier, or when Experio refuses
          the file or its test finds errors (nothing imported, file taken, dossier no longer waiting);
        - an ok result with the number of lines imported and, for entries, what Experio created (accounts,
          journals, tiers, exercices); the dossier then waits in tarik_setups for its onboarding.
          In test mode, only Experio's test runs.
    """
    dossier = require_dossier(dossier_id)
    with cabinet_db() as db:
        name, mime, data = file_row(db, file_id, "file_name", "mime_type", "data")
    context = in_dossier(dossier_id)
    with cabinet_db() as db:
        twin = db.execute(Q.entries_of_same_content, (bytes(data), dossier_id, file_id)).fetchone()
    if twin:  # the very same file sent again: its lines would be imported twice
        taken(file_id)
        return stop(
            f"`{name}` a le même contenu que `{twin[0]}`, déjà importé dans le dossier `{dossier}` le "
            f"{twin[1]:%d/%m/%Y} : je ne l'importe pas une seconde fois.",
            file_id=file_id,
        )
    # Every refusal is a stop with the file taken: a corrected file comes as a new one, this one is not tried again.
    try:
        text = entries_text(file_id, name, mime, data)
        kind = file_kind(kind, text)
        if not kind:  # the file stays: the agent calls again with the client's answer as kind
            return question(
                f"`{name}` : qu'est-ce que ce fichier ? Des écritures (FEC ou grand livre), le plan comptable, "
                "les journaux, les fournisseurs ou les clients ?",
                file_id=file_id,
            )
        target = kind if kind in SETUP_FILES else "ecritures"
        model, opened, label = TARGETS[target]
        if target == "journaux":
            text = with_bank_names(text, separator_of(text))
        context = {**context, **opened}
        import_id, preview = previewed(file_id, name, text, context, model)
        options = {**IMPORT_OPTIONS, **preview.get("options", {})}
        headers, fields = preview["headers"], columns(preview, target)
        # "Importer GL" is the same import with the lines sorted (by reference too when the file has one).
        gl = kind == "grand livre"
        options.update(order_lines=gl, order_lines_with_reference=gl and "reference" in fields)
        if target == "ecritures":
            label = "Grand livre" if gl else "FEC"
        arguments = ([import_id], fields, headers, options)
        test = tested(file_id, name, label, arguments, context)
        if dry_run():
            lines = counted(len(test.get("ids") or []), "{} ligne", "{} lignes")
            return ok(
                f"{TEST_MODE}{label} `{name}` : le test d'Experio a réussi sur {lines}, je n'ai rien importé.",
                file_id=file_id,
            )
        before = setup_counts(dossier_id, context) if target == "ecritures" else None
        result = imported(file_id, name, label, arguments, context)
        made = created_note(before, setup_counts(dossier_id, context)) if before else ""
    except Refused as refusal:
        taken(file_id)
        setup_waits(dossier_id)
        return stop(str(refusal), file_id=file_id)
    taken(file_id)
    setup_waits(dossier_id, file_id)
    return ok(
        f"{label} `{name}` : {counted(len(result.get('ids') or []), '{} ligne importée', '{} lignes importées')} "
        f"dans le dossier `{dossier}`.{made}",
        file_id=file_id,
        target=target,
    )


CREATED = {
    "exp.account": ("{} compte", "{} comptes"),
    "exp.journal": ("{} journal", "{} journaux"),
    "exp.partner": ("{} tiers", "{} tiers"),
    "exp.fiscal.entries": ("{} exercice", "{} exercices"),
}


def setup_counts(dossier_id, context):
    """Return {model: count} of the dossier's accounts, journals, tiers and exercices (the models of CREATED)."""
    return {
        model: odoo_call(model, "search_count", [("company_id", "=", dossier_id)], context=context) for model in CREATED
    }


def created_note(before, after):
    """Return the French sentence saying what Experio created during the import (accounts, journals, tiers,
    exercices), from setup_counts ``before`` and ``after``; a sentence saying it created nothing otherwise."""
    made = [counted(after[m] - before[m], *words) for m, words in CREATED.items() if after[m] > before[m]]
    return f" Experio a créé : {', '.join(made)}." if made else " Experio n'a rien eu à créer."


# --- Each bank journal's bank ---


def bank_journals_without_bank(dossier_id):
    """Return the dossier's bank journals whose Banque (bank_id) is still empty, as [{id, name, rib}] by name.
    A FEC or a grand livre names no bank, and a statement can't be imported into a journal without one."""
    return odoo_call(
        "exp.journal",
        "search_read",
        [("company_id", "=", dossier_id), ("type", "=", "bank"), ("bank_id", "=", False)],
        fields=["id", "name", "rib"],
        order="name",
    )


@step_tool
def ask_banks(dossier_id: int) -> dict:
    """Ask which bank each of the dossier's bank journals is at, one journal at a time (a FEC names no bank).

    Returns:
        - a routine ok result when every bank journal has its bank;
        - the result of set_journal_bank when the first journal's RIB or name says one bank only (journal_bank:
          « 007 780 … », « Banque ATTIJARIWAFA BANK », « BMCE »), outside test mode: set, and the client told
          to correct it if wrong;
        - otherwise a question result listing Experio's banks (``choices``: {bank_id: name}) with the
          ``journal_id`` asked about; the client's answer goes to set_journal_bank (answer_pending).
    """
    dossier = require_dossier(dossier_id)
    with refused(f"ask_banks {dossier_id}", f"Dossier `{dossier}` : Experio n'a pas pu lire les journaux de banque."):
        journals = bank_journals_without_bank(dossier_id)
        banks = odoo_call("res.bank", "search_read", [], fields=["name", "code"], order="name") if journals else []
    if not journals:
        return routine(f"Dossier `{dossier}` : chaque journal de banque a sa banque.")
    journal = journals[0]
    found, how = journal_bank(journal, banks)
    if found and not dry_run():  # « BMCE », a RIB at 007: one bank only, set and told
        return set_journal_bank(dossier_id, journal["id"], found["id"], read_from=how)
    listed = ", ".join(f"`{bank['name']}` (n° `{bank['id']}`)" for bank in banks)
    left = len(journals) - 1
    others = counted(
        left, " Il reste ensuite {} autre journal à renseigner.", " Il reste ensuite {} autres journaux à renseigner."
    )
    return question(
        f"Dossier `{dossier}` : à quelle banque est le journal `{journal['name']}` ? Les banques d'Experio : "
        f"{listed}.{others if left else ''}",
        choices={bank["id"]: bank["name"] for bank in banks},
        journal_id=journal["id"],
    )


def set_journal_bank(dossier_id, journal_id, bank_id, read_from=""):
    """Set the bank journal's Banque to ``bank_id``, as the journal form does, then ask about the next journal.

    When the journal has no tiers bancaire (partner_id), it also gets the dossier's tiers named after the bank,
    if one exists. ``read_from``: what of the journal Tarik read the bank from ("son RIB", "son nom"); the reply
    says it and asks the client to correct it if wrong. Returns the next journal's question result (ask_banks) with
    this change prefixed, or an ok result when no journal is left.

    Raises:
        ToolError: the journal is not one of the dossier's.
    """
    dossier = require_dossier(dossier_id)
    context = in_dossier(dossier_id)
    with refused(f"set_journal_bank {journal_id}", f"Dossier `{dossier}` : Experio a refusé la banque du journal."):
        bank = odoo_call("res.bank", "read", [bank_id], fields=["name"])[0]
        journal = odoo_call("exp.journal", "read", [journal_id], fields=["name", "company_id", "partner_id"])[0]
        if journal["company_id"][0] != dossier_id:
            raise ToolError(f"Le journal n° `{journal_id}` n'est pas un journal du dossier `{dossier}`.")
        give_bank(dossier_id, {**journal, "id": journal_id}, {**bank, "id": bank_id}, context)
    done = test_mode_note()
    done += f"Dossier `{dossier}` : journal `{journal['name']}` a la banque `{bank['name']}`"
    done += f" (d'après {read_from} ; dites-le-moi si ce n'est pas la bonne)." if read_from else "."
    following = ask_banks(dossier_id)
    if following["status"] == "question":
        return {**following, "message": f"{done} {following['message']}"}
    return ok(done, dossier_id=dossier_id)


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
PROCESSING_SECONDS = 180  # shortcut: the chat has no follow-up, so lines still pending after this go unreported


def exercice_note(dossier_id, dossier):
    """Open the current year's exercice when the dossier has no open one, and return what was done (French
    text; in test mode, what would be done). None when the dossier already has an open exercice."""
    if odoo_call("exp.fiscal.entries", "search_count", [("company_id", "=", dossier_id), ("state", "=", "open")]):
        return None
    year = date.today().year
    if dry_run():
        return f"L'exercice {year} serait ouvert."
    with refused(
        f"finish_setup {dossier_id}", f"Dossier `{dossier}` : Experio refuse d'ouvrir l'exercice {year} ({{reason}})."
    ):
        open_exercice(dossier_id, year)
    return f"J'ai ouvert l'exercice {year}."


def lines_ready_in(dossier_id, context):
    """Return the seconds until the newest waiting line (state "new") was sent over a minute ago (65 s, for
    margin); 0.0 when none waits. Finishing the onboarding re-sends only lines sent more than a minute ago
    (experio.history action_resend)."""
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
    """Walk the onboarding stepper through its 8 phases, then press "Enregistrer et terminer" with ``company``'s
    journals and tiers prefixes (STEPPER_PREFIXES where it has none).

    The journals and prefixes are saved first: Experio ends the onboarding (show_onboarding False) as soon as the
    stepper state lists 8 completed phases (base_client res_company.py set_configuration_stepper_state), and that write
    hands the waiting lines over (exp_account res_company.py write). Experio's own screen saves them after
    (stepper.js saveStepperState, then validation_dialog.js save_validation_config); saved first, every line handed
    over finds them already set."""
    config = {key: company[key] and company[key][0] for key in STEPPER_JOURNALS}
    config.update({key: company[key] or prefix for key, prefix in STEPPER_PREFIXES.items()})
    odoo_json("res.company", "save_validation_config", config, context=context)
    for phase in range(1, 9):  # its 8 phases; the 8th hands the waiting lines over
        following = min(phase + 1, 8)  # the last phase stays the current one
        state = {
            "current_phase": following,
            "completed_phases": list(range(1, phase + 1)),
            "visited_phases": list(range(1, following + 1)),
        }
        odoo_json("res.company", "set_configuration_stepper_state", state, context=context)
    odoo_json("res.company", "complete_onboarding", context=context)  # as the screen's last button does


def entries_count(dossier_id, context):
    """Return how many entries lines (experio.history, any state) were imported into the dossier."""
    return odoo_call("experio.history", "search_count", [("company_id", "=", dossier_id)], context=context)


def setup_missing(dossier_id, context):
    """Return the labels (lowercase French) of the setup files the dossier still lacks, judged by what it holds:
    no account, no journal, no supplier, no customer."""
    counts = {
        "plan": odoo_call("exp.account", "search_count", [("company_id", "=", dossier_id)], context=context),
        "journaux": odoo_call("exp.journal", "search_count", [("company_id", "=", dossier_id)], context=context),
        "fournisseurs": odoo_call(
            "exp.partner",
            "search_count",
            [("company_id", "=", dossier_id), ("is_supplier", "=", True)],
            context=context,
        ),
        "clients": odoo_call(
            "exp.partner",
            "search_count",
            [("company_id", "=", dossier_id), ("is_customer", "=", True)],
            context=context,
        ),
    }
    return [TARGETS[target][2].lower() for target in SETUP_FILES if not counts[target]]


@step_tool
def finish_setup(dossier_id: int, seconds_left: int = 0, finish: bool = False) -> dict:
    """Finish a new dossier's onboarding as its stepper does (Suivant to the last step, then « Enregistrer et
    terminer »), so Experio processes the imported entries. Opens the current year's exercice first when the
    dossier has none open.

    Args:
        seconds_left: the run's time left (0: no limit).
        finish: the client said every setup file was sent; without it, a dossier with no entries imported
            keeps waiting for its files.

    Returns:
        - a routine result when the onboarding was already finished;
        - an ok result asking for the missing setup files when no entries were imported and ``finish`` is
          False (the tarik_setups wait is cleared);
        - a wait result when the minute Experio needs before handing the lines over (plus STEPPER_SECONDS)
          would outlast ``seconds_left``: the dossier stays in tarik_setups for a later run;
        - an ok result once finished, with the lines handed over and any journal setting still pointing to
          another dossier's journal (in test mode, what would be done).
    """
    dossier = require_dossier(dossier_id)
    context = in_dossier(dossier_id)
    fields = ["show_onboarding", *STEPPER_JOURNALS, *STEPPER_PREFIXES]
    company = odoo_call("res.company", "read", [dossier_id], fields=fields)[0]
    if not company["show_onboarding"]:  # its lines still in Experio's processing keep it waiting (setup_status)
        return routine(
            f"Dossier `{dossier}` : l'intégration est déjà terminée. Experio traite les écritures dès leur import."
        )
    if not finish and not entries_count(dossier_id, context):
        setup_waits(dossier_id)  # nothing to wait for: the client's next files, or their word
        missing = setup_missing(dossier_id, context)
        lacking = f" Il manque : {', '.join(missing)}." if missing else ""
        return ok(
            f"Dossier `{dossier}` : l'intégration attend vos fichiers.{lacking} Envoyez-les (plan comptable, "
            "journaux, fournisseurs, clients), ou dites-moi que tout est envoyé pour que je termine l'intégration."
        )
    notes = []
    exercice = exercice_note(dossier_id, dossier)
    if exercice:
        notes.append(exercice)
    if dry_run():
        return ok(
            f"{TEST_MODE}Dossier `{dossier}` : l'intégration serait terminée." + "".join(f"\n- {n}" for n in notes)
        )
    pause = lines_ready_in(dossier_id, context)
    if seconds_left and pause > seconds_left - STEPPER_SECONDS:
        return wait(
            f"Dossier `{dossier}` : Experio enregistre encore les écritures importées. Je terminerai son intégration "
            "et vous donnerai l'état du dossier dans quelques minutes."
        )
    time.sleep(pause)
    with refused(
        f"finish_setup {dossier_id}", f"Dossier `{dossier}` : Experio refuse de terminer l'intégration ({{reason}})."
    ):
        complete_stepper(company, context)
    waiting = entries_count(dossier_id, context)
    notes.append(
        counted(
            waiting,
            "{} ligne d'écritures est transmise au traitement d'Experio.",
            "{} lignes d'écritures sont transmises au traitement d'Experio.",
        )
        if waiting
        else "Aucune écriture n'a été importée."
    )
    return ok(f"Dossier `{dossier}` : l'intégration est terminée." + "".join(f"\n- {n}" for n in notes))


# --- Where the setup stands ---

# What posting an invoice needs (exp.invoice validate_local_settings / validate_foreign_settings).
POSTING_SETTINGS = {
    "local_supplier_account_id": "compte fournisseurs",
    "local_purchase_journal_id": "journal d'achats",
    "local_customer_account_id": "compte clients",
    "local_sale_journal_id": "journal de ventes",
    "foreign_supplier_account_id": "compte fournisseurs étrangers",
    "foreign_purchase_journal_id": "journal d'achats étrangers",
    "foreign_customer_account_id": "compte clients étrangers",
    "foreign_sale_journal_id": "journal de ventes étrangères",
}
# The tiers account a posting setting falls back to (res_company.py _default_local_customer_account_id,
# _default_foreign_customer_account_id...: foreign takes the local one when no 34212 / 44112): CGNC code, name.
TIERS_ACCOUNTS = {"customer": ("3421", "Clients"), "supplier": ("4411", "Fournisseurs")}
HISTORY_STATES = {
    "new": ("en attente", "en attente"),
    "pending": ("en cours de traitement", "en cours de traitement"),
    "done": ("traitée", "traitées"),
    "reconciliation": ("à lettrer", "à lettrer"),
    "exists": ("déjà présente", "déjà présentes"),
    "error": ("en erreur", "en erreur"),
}


def setup_facts(dossier_id, context):
    """Read the dossier's setup from Experio.

    Returns (onboarding, states, exercices, counts):
        onboarding: True while the dossier's onboarding is open (res.company show_onboarding);
        states: {experio.history state: line count};
        exercices: [{name, state, selected}] by start date;
        counts: {model: count} of accounts, journals, taxes and tiers.
    """
    in_it = [("company_id", "=", dossier_id)]
    onboarding = odoo_call("res.company", "read", [dossier_id], fields=["show_onboarding"])[0]["show_onboarding"]
    # One query, so the counts add up while Experio processes the lines.
    groups = odoo_call("experio.history", "read_group", in_it, ["state"], ["state"], context=context)
    states = {group["state"]: group["state_count"] for group in groups}
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
    return onboarding, states, exercices, counts


ERROR_LINES = 8  # reasons told one by one; the rest are counted
AMOUNT = re.compile(r"'(-?\d+(?:\.\d+)?)'")  # an amount in Experio's text ('846.0'): read as a number


def error_reasons(reasons):
    """Return Experio's error reasons (error_type) from the read_group rows ``reasons`` as lines "<n> lignes : <text>",
    the commonest first, ERROR_LINES at most and the others counted in a last line. A leading count is ignored, so
    "3 comptes tiers ..." and "4 comptes tiers ..." are one reason; an amount ('846.0') reads « 846,00 »."""
    merged = Counter()
    for reason in reasons:
        text = (reason["error_type"] or "sans motif").strip()
        text = AMOUNT.sub(lambda amount: f"{float(amount[1]):,.2f}".replace(",", " ").replace(".", ","), text)
        merged[re.sub(r"^\d+\s+", "", text)] += reason["error_type_count"]  # without the leading count and spaces
    found = [(text[:1].lower() + text[1:].rstrip("."), n) for text, n in merged.most_common()]
    lines = [f"{counted(n, '{} ligne', '{} lignes')} : {text}." for text, n in found[:ERROR_LINES]]
    if len(found) > ERROR_LINES:
        rest = sum(n for _, n in found[ERROR_LINES:])
        others = counted(len(found) - ERROR_LINES, "{} autre motif", "{} autres motifs")
        lines.append(f"{counted(rest, '{} ligne', '{} lignes')} : {others}.")
    return lines


def processed(dossier_id, context):
    """Wait, up to PROCESSING_SECONDS and polling every 10 s, while the dossier has lines in state "pending", so
    the status that follows shows which lines Experio put in error."""
    end = time.time() + PROCESSING_SECONDS
    pending = [("company_id", "=", dossier_id), ("state", "=", "pending")]
    while time.time() < end and odoo_call("experio.history", "search_count", pending, context=context):
        time.sleep(10)


STALE_MINUTES = 60  # handed over this long ago, still not processed: stuck, not a long import still running


def resend_stale(dossier_id, context):
    """Re-send the dossier's lines still "new" or "pending" STALE_MINUTES after they were sent, with Experio's own
    « Re-send » (experio.history action_resend, which re-queues stale and failed lines and re-runs its detection).
    Returns how many lines were re-sent: 0 while the onboarding is open (lines wait on purpose) and in test mode."""
    if odoo_call("res.company", "read", [dossier_id], fields=["show_onboarding"])[0]["show_onboarding"]:
        return 0
    since = (datetime.now(UTC) - timedelta(minutes=STALE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")  # Experio's UTC
    stale = [("company_id", "=", dossier_id), ("state", "in", ["new", "pending"]), ("send_date", "<", since)]
    ids = odoo_call("experio.history", "search", stale, context=context)
    if not ids or dry_run():
        return 0
    odoo_json("experio.history", "action_resend", ids, context={**context, "exp_resend_grouping_confirmed": True})
    return len(ids)


def other_dossiers_journals(dossier_id):
    """Return the labels (comma-joined) of the dossier's journal settings that point to another dossier's journal,
    "" when none does. A new dossier gets them by default: Experio takes the first purchase or sale journal of the
    creating user's current company (res.company _default_purchase_journal_id)."""
    wrong = misplaced_journals(dossier_id, [key for key in POSTING_SETTINGS if "journal" in key])
    return ", ".join(POSTING_SETTINGS[key] for key, name in wrong.items() if name)


def number(count):
    """Return ``count`` with a space between thousands (33 882)."""
    return f"{count:,}".replace(",", " ")


@step_tool
def setup_status(dossier_id: int) -> dict:
    """Report where a dossier's setup stands, as an ok result: imported lines by state, open exercices, account,
    journal, tax and tiers counts, and whether the onboarding is finished or lines are still being processed. What
    the client must fix is setup_problems'.

    First re-sends stale lines (resend_stale) and waits for lines being processed (processed). Clears the
    tarik_setups wait once the onboarding is finished and no line is left to process."""
    dossier = require_dossier(dossier_id)
    with refused(
        f"setup_status {dossier_id}", f"Dossier `{dossier}` : Experio n'a pas pu donner l'état du paramétrage."
    ):
        resent = resend_stale(dossier_id, in_dossier(dossier_id))
        processed(dossier_id, in_dossier(dossier_id))
        onboarding, states, exercices, counts = setup_facts(dossier_id, in_dossier(dossier_id))
    still = states.get("new", 0) + states.get("pending", 0)
    if not onboarding and not still:
        setup_waits(dossier_id)  # every line processed: nothing more to tell of it
    entries = [  # the count has its thousands spaced (number), so it is in the words given to counted
        counted(states[state], f"{number(states[state])} {one}", f"{number(states[state])} {many}")
        for state, (one, many) in HISTORY_STATES.items()
        if states.get(state)
    ]
    parts = ["Écritures importées : " + (", ".join(entries) + "." if entries else "aucune.")]
    opened = [f"`{e['name']}`" + (" (en cours)" if e["selected"] else "") for e in exercices if e["state"] == "open"]
    parts.append("Exercices ouverts : " + (", ".join(opened) + "." if opened else "aucun."))
    held = [
        counted(counts["exp.account"], "{} compte", "{} comptes"),
        counted(counts["exp.journal"], "{} journal", "{} journaux"),
        counted(counts["exp.tax"], "{} taxe", "{} taxes"),
    ]
    parts.append(f"Le dossier a {', '.join(held)} et {counts['exp.partner']} tiers.")
    if resent:
        parts.append(
            counted(
                resent,
                "{} ligne bloquée a été renvoyée au traitement d'Experio (« Renvoyer »).",
                "{} lignes bloquées ont été renvoyées au traitement d'Experio (« Renvoyer »).",
            )
        )
    if onboarding:
        parts.append("L'intégration n'est pas encore terminée.")
    elif still:
        parts.append(
            counted(
                still,
                "{} ligne est encore en traitement chez Experio : son résultat suivra.",
                "{} lignes sont encore en traitement chez Experio : leur résultat suivra.",
            )
        )
    return ok(f"Dossier `{dossier}` : état du paramétrage." + "".join(f"\n- {part}" for part in parts))


def tiers_account(dossier_id, side, company, context, done, problems):
    """Make the dossier's CGNC ``side`` ("customer" or "supplier") account (TIERS_ACCOUNTS) when one of its settings
    is unset in ``company`` and the chart has none, adding what was made, or would be in test mode, to ``done``. When
    the chart has one, the detection did not take it: its type is not receivable / payable (invoice res_company.py:102),
    added to ``problems`` for the cabinet."""
    prefix, name = TIERS_ACCOUNTS[side]
    unset = [label for key, label in POSTING_SETTINGS.items() if f"{side}_account" in key and not company[key]]
    if not unset:
        return
    in_chart = [("company_id", "=", dossier_id), ("code", "=like", prefix + "%")]
    found = odoo_call("exp.account", "search_read", in_chart, fields=["code"], limit=1, context=context)
    if not found:
        code = padded(dossier_id, prefix)
        if not dry_run():
            odoo_call("exp.account", "create", {"code": code, "name": name, "company_id": dossier_id}, context=context)
        done.append(f"compte `{code} {name}` {would('créé')}")
    elif not dry_run():  # in test mode the detection has not run yet
        problems.append(
            f"Le compte `{found[0]['code']}` n'est pas du type tiers {name.lower()} qu'Experio demande "
            f"pour le {' et le '.join(unset)} : corrigez son type dans le plan comptable."
        )


def fill_posting_settings(dossier_id, company, context):
    """Fill the posting settings (POSTING_SETTINGS) the dossier lacks, as the Configuration screen does: Experio's
    « Détection automatique » (res.company auto_detect_settings, which also moves another dossier's journal to the
    dossier's own of the same code), then the CGNC tiers account (TIERS_ACCOUNTS) when the chart has none, whose
    creation reruns the detection (exp_account.py detect_accounts_chart_length), then the purchase or sale journal
    when the dossier has none (set_journal). ``company``: the dossier's POSTING_SETTINGS as read.

    Returns (done, problems, company): what was done, or would be in test mode (French), the made items then the
    settings that changed; what Tarik could not do (a write Experio refused, a 3421 / 4411 account Experio does not
    take); the settings as they are now. Nothing is called when no setting is unset or another dossier's."""
    journals = [key for key in POSTING_SETTINGS if "journal" in key]
    wrong = misplaced_journals(dossier_id, journals)
    if all(company[key] for key in POSTING_SETTINGS) and not wrong:
        return [], [], company
    done, problems, before = [], [], company
    with dossier_lock(dossier_id):
        try:
            if not dry_run():
                odoo_json("res.company", "auto_detect_settings", [dossier_id], context=context)
                company = odoo_call("res.company", "read", [dossier_id], fields=list(POSTING_SETTINGS))[0]
                # after the detection, a journal setting is either the dossier's own or unset (none of its kind)
                wrong = {key: False for key in journals if not company[key]}
            for side in TIERS_ACCOUNTS:
                tiers_account(dossier_id, side, company, context, done, problems)
            for kind in ("purchase", "sale"):
                if any(f"{kind}_journal" in key for key in wrong):
                    done.append(set_journal(dossier_id, kind).removeprefix(TEST_MODE))
                    SET.pop((current(), dossier_id), None)  # told here, not again on the first invoice
        except ToolError as failure:
            problems.append(f"Je n'ai pas pu finir le paramétrage : {failure}")
        except Exception as failure:  # what was done before is still told
            log(f"fill_posting_settings {dossier_id}: {failure}")
            problems.append(f"Experio a refusé mon paramétrage ({experio_refusal(failure)}).")
        if dry_run():
            return done, problems, company
        after = odoo_call("res.company", "read", [dossier_id], fields=list(POSTING_SETTINGS))[0]
    changed = [label for key, label in POSTING_SETTINGS.items() if after[key] and after[key] != before[key]]
    if changed:
        done.append("configuration du dossier complétée : " + ", ".join(changed))
    return done, problems, after


@step_tool
def setup_problems(dossier_id: int) -> dict:
    """Report what the client must fix or set in a dossier, as a stop result (ok when nothing): the lines Experio put
    in error with its reasons (error_reasons), the settings an invoice needs before it can be posted, and journal
    settings on another dossier. Those settings are first filled where Experio's own defaults allow
    (fill_posting_settings), and what was done is told; not while imported lines are still processed, which may bring
    the dossier's own journals and accounts. Run after setup_status, which has waited for the lines being processed."""
    dossier = require_dossier(dossier_id)
    with refused(
        f"setup_problems {dossier_id}", f"Dossier `{dossier}` : Experio n'a pas pu donner l'état du paramétrage."
    ):
        context = in_dossier(dossier_id)
        company = odoo_call("res.company", "read", [dossier_id], fields=list(POSTING_SETTINGS))[0]
        still = [("company_id", "=", dossier_id), ("state", "in", ["new", "pending"])]
        # lines still processed may bring the dossier's own journals and accounts: settings are left to them then
        processing = odoo_call("experio.history", "search_count", still, context=context)
        made, unfilled = [], []
        if not processing:
            made, unfilled, company = fill_posting_settings(dossier_id, company, context)
        reasons = odoo_call(
            "experio.history",
            "read_group",
            [("company_id", "=", dossier_id), ("state", "=", "error")],
            ["error_type"],
            ["error_type"],
            context=context,
        )
    test = TEST_MODE if made and dry_run() else ""
    done = ("Ce que je ferais :" if test else "Ce que j'ai fait :") + "".join(f"\n  - {item}" for item in made)
    told = [done] if made else []
    lines = [*unfilled]
    if reasons:
        failed = sum(reason["error_type_count"] for reason in reasons)
        refusal = counted(
            failed,
            f"{number(failed)} ligne a été refusée par Experio : corrigez-la dans votre fichier puis renvoyez-le.",
            f"{number(failed)} lignes ont été refusées par Experio : corrigez-les dans votre fichier puis renvoyez-le.",
        )
        lines.append(refusal + "".join(f"\n  - {line}" for line in error_reasons(reasons)))
    missing = [label for key, label in POSTING_SETTINGS.items() if not company[key]]
    if missing and not processing:
        lines.append("À paramétrer avant de comptabiliser : " + ", ".join(missing) + ".")
    foreign = other_dossiers_journals(dossier_id)
    if foreign:
        lines.append(
            f"Ces réglages utilisent le journal d'un autre dossier : {foreign}. Créez ou importez leur journal dans "
            "ce dossier."
        )
    if not lines:
        # tell: an ok of setup_problems is otherwise not recorded (QUIET_WHEN_OK)
        text = f"{test}Dossier `{dossier}` : rien à corriger." + "".join(f"\n- {line}" for line in told)
        return ok(text, tell=bool(made))
    return stop(f"{test}Dossier `{dossier}` :" + "".join(f"\n- {line}" for line in told + lines))
