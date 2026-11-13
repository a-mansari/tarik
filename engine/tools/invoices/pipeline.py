"""The fixed processing sequence that the Traitement graph runs (engine/traitement.py): waiting for Experio's reading,
recording each step's outcome, the run's summary, pending questions and the client's answers, and which drafts to take
up
again."""

import base64
import json
import re
import time
from datetime import UTC, datetime, timedelta

from engine.tools.core.app import log, ok, question, routine, step_tool, stop, tool, wait
from engine.tools.core.archives import unpacked
from engine.tools.core.cabinets import cabinet_db
from engine.tools.core.config import ENTRY_FILES, FILE_TYPES, READ_WAIT, READING, TEST_MODE
from engine.tools.core.conversation import chosen_among, own_words, yes_no
from engine.tools.core.experio import _form as experio_form
from engine.tools.core.experio import dry_run, in_dossier, odoo_call, odoo_json, require_invoice
from engine.tools.core.queries import Q
from engine.tools.core.words import counted, dirhams, normalized, said, without_if
from engine.tools.dossiers.behaviour import behaviour, dossier_behaviour
from engine.tools.dossiers.people import check_sender
from engine.tools.dossiers.setup import answer_banks
from engine.tools.invoices import tpe
from engine.tools.invoices.accounts import confirm_account, create_account, set_account
from engine.tools.invoices.classify import SIDES, client_side
from engine.tools.invoices.configuration import answer_number
from engine.tools.invoices.exercice import settle_exercice
from engine.tools.invoices.import_folder import TRANSIT, transit_last
from engine.tools.invoices.intake import BANK_QUESTION, client_kind, file_named, import_bank_statement, set_kind
from engine.tools.invoices.prepaid import settle_prepaid
from engine.tools.invoices.statement import is_canvas
from engine.tools.invoices.tiers import choose_tiers
from engine.tools.invoices.tva import settle_tva
from engine.tools.invoices.warnings import answer_warnings
from engine.tools.invoices.withholding import settle_withholding

# The sequence, in order: step tool -> what Tarik could not do when it fails there (« je n'ai pas pu … »).
STEPS = {
    "answer_pending": "prendre en compte votre réponse",
    "waiting_drafts": "reprendre les factures à comptabiliser",
    "resolve_dossier": "trouver le dossier",
    "check_sender": "vérifier votre accès au dossier",
    "check_creator": "vérifier votre droit de créer des dossiers",
    "check_manager": "vérifier votre droit de gérer les accès",
    "create_dossier": "créer le dossier",
    "update_dossier": "modifier le dossier",
    "copy_configuration": "copier la configuration",
    "import_entries": "importer les écritures",
    "finish_setup": "terminer l'intégration du dossier",
    "ask_banks": "enregistrer la banque",
    "setup_status": "lire l'état du dossier",
    "setup_problems": "lister les points à corriger du dossier",
    "import_dossiers": "importer la liste de dossiers",
    "create_person": "créer l'accès",
    "change_access": "modifier l'accès",
    "manage_routine": "régler la routine",
    "manage_behaviour": "modifier les réglages",
    "archive_dossier": "archiver le dossier",
    "files_to_process": "lire les fichiers à traiter",
    "create_import_folder": "créer le dossier d'import",
    "identify_document": "identifier le document",
    "import_bank_statement": "importer le relevé bancaire",
    "import_invoice_sheet": "importer le tableur de factures",
    "adjust_from_notice": "ajuster l'opération d'après l'avis",
    "list_open_statements": "lister les relevés à pointer",
    "settle_statement": "pointer le relevé",
    "detect_statement_lines": "obtenir la détection d'Experio sur le relevé",
    "check_statement_reading": "vérifier la lecture du relevé",
    "choose_line_operations": "choisir les types d'opération",
    "name_line_tiers": "trouver les tiers des opérations",
    "tie_cited_invoices": "rapprocher les factures citées",
    "tie_owed_invoices": "rapprocher les factures ouvertes",
    "tie_tpe_remittances": "rapprocher les versements TPE",
    "report_statement": "faire le bilan du relevé",
    "post_statement": "comptabiliser le relevé",
    "split_invoices": "découper le document en factures",
    "create_draft_invoice": "créer le brouillon",
    "wait_until_read": "obtenir la lecture d'Experio",
    "check_invoice": "vérifier la facture",
    "correct_invoice": "corriger la facture",
    "classify_invoice": "classer la facture",
    "link_tiers": "rattacher le tiers",
    "check_duplicate": "vérifier les doublons",
    "assign_account": "choisir le compte",
    "settle_tva": "régler la TVA",
    "check_journal": "vérifier le journal",
    "settle_withholding": "régler la retenue à la source",
    "settle_prepaid": "régler la charge constatée d'avance",
    "settle_exercice": "vérifier l'exercice",
    "configure_cabinet": "faire le paramétrage du cabinet",
    "settle_warnings": "traiter les avertissements",
    "split_charges": "répartir les charges sur leurs comptes",
    "hold_posting": "mettre la facture en attente",
    "post_invoice": "comptabiliser la facture",
    "apply_credit_note": "imputer l'avoir",
}
ORDER = list(STEPS)
INVOICE_CHAIN = ORDER[ORDER.index("create_draft_invoice") :]  # an invoice's own steps, from its draft (Traitement)
NOTHING = "Rien n'a été traité."  # a run's summary when it did nothing (the Emails follow-up leaves it out)
NO_FILE = (
    "Aucune pièce jointe n'est arrivée avec votre message : vérifiez qu'elle est bien jointe (certaines "
    "messageries la retirent) et renvoyez-la."
)
SETUP_STEPS = {
    "check_creator",
    "create_dossier",
    "update_dossier",
    "copy_configuration",
    "import_entries",
    "finish_setup",
    "ask_banks",
    "setup_status",
    "setup_problems",
    "import_dossiers",
    "create_person",
    "change_access",
    "manage_routine",
    "manage_behaviour",
    "check_manager",
    "archive_dossier",
}
# A pending question, and the call that applies the client's answer; the sequence resumes at the step after it.
ANSWERS = {
    "link_tiers": choose_tiers,
    "assign_account": set_account,
    "settle_tva": settle_tva,
    "settle_withholding": settle_withholding,
    "settle_prepaid": settle_prepaid,
    "settle_exercice": settle_exercice,
    "configure_cabinet": answer_number,
    "settle_warnings": answer_warnings,
}
# The oui / non questions: they take the agent's decision, read from the client's answer, never a guess from it here.
DECIDED = {"settle_tva", "settle_prepaid"}


def position(step):
    """Return where ``step`` (a step tool's name) stands in the sequence; after every step when it is not one."""
    return ORDER.index(step) if step in ORDER else len(ORDER)


READ_DEADLINES = {}  # run id -> when that run stops waiting on Experio's reading (time.monotonic)


@step_tool
def wait_until_read(invoice_id: int, run_id: str = "") -> dict:
    """Wait until Experio has finished reading draft ``invoice_id`` (its bluejay_state no longer "pending" nor "sent").

    ``run_id``: the run; all its drafts share one deadline of READ_WAIT seconds, so one stuck reading never holds the
    run. A reading already stuck over an hour (STUCK) is not waited for at all.

    Returns a wait result when the deadline passes first or the reading is stuck (the draft is resumed at the client's
    next message; a reading stuck over an hour is sent again, resend_if_stuck). Once read: an ok result with
    ``split_off``, the other drafts Experio made from the same scan, for the run to take in hand too; a routine result
    when there are none."""
    now = time.monotonic()
    for old in [run for run, deadline in READ_DEADLINES.items() if deadline < now - 3600]:
        del READ_DEADLINES[old]
    deadline = now + READ_WAIT
    if run_id:
        deadline = READ_DEADLINES.setdefault(run_id, deadline)
    while require_invoice(invoice_id, ["bluejay_state"])["bluejay_state"] in READING:
        # A reading Experio itself calls gone wrong (STUCK) is not waited for: the run's wait would only hold the
        # drafts behind it.
        if time.monotonic() >= deadline or stuck(require_invoice(invoice_id, STUCK_FIELDS)):
            resent = resend_if_stuck(invoice_id)
            return wait(
                f"Facture {invoice_id} : Experio la lit encore{resent}, elle sera reprise à votre prochain message."
            )
        time.sleep(15)
    others = split_off(invoice_id)
    if others:
        listed = ", ".join(f"n° {other}" for other in others)
        return ok(
            f"Facture {invoice_id} : lue par Experio. "
            + (
                f"Experio a tiré du même document une autre facture, la {listed}. Je la traite à la suite."
                if len(others) == 1
                else f"Experio a tiré du même document {len(others)} autres factures ({listed}), une par facture du "
                "document. Je les traite à la suite."
            ),
            split_off=others,
        )
    return routine(f"Facture {invoice_id} : lue par Experio.")


STUCK = timedelta(minutes=60)  # Experio's own threshold for a reading gone wrong (get_pending_grouped_invoices_domain)
STUCK_FIELDS = ["state", "create_date", "write_date", "company_id"]


def stuck(invoice):
    """True when ``invoice`` (read with STUCK_FIELDS) is still pending and both its creation and its last change are
    over STUCK ago: Experio's reading of it went wrong (a resend changes it, so it is stuck again an hour later)."""
    if invoice["state"] != "pending":
        return False
    now = datetime.now(UTC).replace(tzinfo=None)  # Experio's dates are UTC
    return all(now - datetime.fromisoformat(invoice[field]) > STUCK for field in ("create_date", "write_date"))


def resend_if_stuck(invoice_id):
    """Ask Experio to read draft ``invoice_id`` again (its own « Re-send » button, exp.invoice.action_resend) when it is
    still pending and both its creation and its last change are over STUCK ago (a resend changes it: once an hour at
    most).

    Returns the phrase for the client's message: "" when nothing was done; else that it was sent again or, when Experio
    refused (Tarik's Experio user lacks the right), that the cabinet can resend it in Experio."""
    invoice = require_invoice(invoice_id, STUCK_FIELDS)
    if not stuck(invoice) or dry_run():
        return ""
    try:
        odoo_json("exp.invoice", "action_resend", [invoice_id], context=in_dossier(invoice["company_id"][0]))
    except Exception as error:  # the Experio user lacks the right: the cabinet is told, nothing breaks
        log(f"wait_until_read {invoice_id}: resend refused: {error}")
        return " depuis plus d'une heure (le cabinet peut la relancer dans Experio)"
    return " depuis plus d'une heure : je lui ai demandé de la relire"


def split_off(invoice_id):
    """Return the ids of the other drafts Experio made from the same scan as Tarik's draft ``invoice_id``, still pending
    or draft, that no step has taken in hand yet. [] for a draft Tarik did not make itself, so a split-off draft never
    reports the others again.

    Experio's reading makes one draft per bill when a document holds several (water and electricity), all with the
    reading's token (exp_bluejay_job.py:1134, _update_cognitive_lines)."""
    with cabinet_db() as db:
        if not db.execute(Q.invoices_made_by_tarik, ([invoice_id],)).fetchone():
            return []
    token = require_invoice(invoice_id, ["token"])["token"]  # Experio's reading job, the same for each draft it made
    domain = [("token", "=", token), ("id", "!=", invoice_id), ("state", "in", ["pending", "draft"])]
    others = odoo_call("exp.invoice", "search", domain, order="id") if token else []
    with cabinet_db() as db:
        taken = {row[0] for row in db.execute(Q.outcomes_of_invoices, (others,))} if others else set()
    return [other for other in others if other not in taken]


def posts_unasked(invoice_id):
    """True when the invoice's dossier posts its verified invoices without being asked (its own post_invoices, else the
    cabinet's). Experio is asked for the dossier only when some cabinet or dossier setting is on."""
    with cabinet_db() as db:
        some = db.execute(Q.dossier_behaviour_on, ("post_invoices",)).fetchone()[0]
    if not (some or behaviour("post_invoices")):
        return False
    return dossier_behaviour(require_invoice(invoice_id, ["company_id"])["company_id"][0], "post_invoices")


@step_tool
def hold_posting(invoice_id: int, session_id: str, answer: str = "") -> dict:
    """Stop an invoice at its verified draft unless the client asked to post it.

    ``answer``: "oui" records the client's request to post it, "non" takes that back, empty keeps what was recorded (an
    invoice never asked about is held). The choice is kept per invoice, since later runs don't carry the answer.

    A dossier set to post its verified invoices (post_invoices) releases each new one at once: the client's later "non"
    still holds it.

    Returns a stop result while the invoice is held, else a routine result: go on to posting."""
    auto = answer == "" and posts_unasked(invoice_id)  # decided first: a failure leaves nothing held for good
    with cabinet_db() as db:
        if answer in ("oui", "non"):
            db.execute(
                Q.upsert_held_choice,
                (invoice_id, session_id, answer == "oui"),
            )
        else:
            db.execute(
                Q.insert_held,
                (invoice_id, session_id, auto),
            )
        released = db.execute(Q.held_released, (invoice_id,)).fetchone()[0]
    if not released:
        return stop(
            f"Facture {invoice_id} : importée et vérifiée, laissée en brouillon. Dites-moi quand la comptabiliser."
        )
    return routine(f"Facture {invoice_id} : à comptabiliser.")


@tool
def record_step(run_id: str, session_id: str, origin: str, result: dict) -> dict:
    """Record a step's outcome for run ``run_id`` and keep the invoice's pending question up to date.

    Args:
        run_id: the run.
        session_id: the conversation.
        origin: the step tool that ran, or what asked (then the step is the result's own ``step``).
        result: that step's result dict.

    A duplicate is recorded at check_duplicate whatever step found it. A question result becomes the invoice's pending
    question; an outcome at or after the pending step clears it, an earlier step's never does. Returns an ok result with
    ``step`` and ``status_recorded``."""
    step = origin if origin in STEPS else result.get("step") or origin
    # Found at its tiers step too (a twin without tiers): a duplicate's outcome all the same.
    if result.get("duplicate"):
        step = "check_duplicate"
    status = result.get("status", "error")
    message = result.get("message") or result.get("error") or str(result)
    invoice_id, file_id, dossier_id = result.get("invoice_id"), result.get("file_id"), result.get("dossier_id")
    choices = json.dumps(result["choices"]) if result.get("choices") else None  # {id: name} a question proposed
    row = (run_id, session_id, file_id, invoice_id, dossier_id, step, status, message, result.get("routine") is True)
    noted = bool(result.get("note") or result.get("tell"))
    with cabinet_db() as db:
        # A question on several bank journals (ask_banks) waits once per journal; any other result is one row. The
        # journals given a bank meanwhile (settled_ids) get a closing row: their question ends even in test mode.
        for journal_id in result.get("journal_ids") or [result.get("journal_id")]:
            db.execute(Q.insert_result, (*row, choices, journal_id, noted))
        for journal_id in result.get("settled_ids") or []:
            db.execute(Q.insert_result, (*row[:6], "ok", *row[7:], None, journal_id, noted))
        if invoice_id:
            db.execute(Q.claim_invoices, ([invoice_id], run_id))  # still working on it: refreshed, never taken over
        if invoice_id and status == "question":
            db.execute(
                Q.upsert_pending,
                (invoice_id, session_id, step, message, choices),
            )
        elif invoice_id:
            # Only an outcome at or after the pending step settles it: an earlier step's outcome never does.
            pending = db.execute(Q.pending_step, (invoice_id,)).fetchone()
            if pending and (pending[0] not in ORDER or position(step) >= position(pending[0])):
                db.execute(Q.delete_pending, (invoice_id,))
    return ok("Enregistré.", step=step, status_recorded=status)


# The summary's sections, in the order the client reads them: what was done, what waits (Experio still reading, a
# draft kept until the client says to post it), the duplicates left aside, what failed or was stopped, then what
# they must answer. Empty ones are left out.
SECTIONS = {
    "done": "**Fait**",  # a dossier's setup (its steps are the request), and a few drafts kept: what was done to them
    "wait": "**En attente**",  # a dossier's setup only: an invoice that waits is counted in the headline
    "problem": "**À corriger**",
    "question": "**Questions**",
    "duplicate": "**Doublons, non traités**",
    "check": "**À vérifier**",  # what Tarik decided alone and may have got wrong (noted outcomes)
}
SECTION_OF = {"ok": "done", "wait": "wait", "stop": "problem", "question": "question"}  # any other status: a problem
CODE = re.compile(r"`([^`\n]+)`")  # a name as the client reads it apart (`ATLAS`): the emails' own mark
QUESTION_MARK = "QUESTION : "  # what the agent recognizes a question by (its answer goes to Experio_repondre)


def section_of(step, status, message=""):
    """Return the summary section (a SECTIONS key) of an outcome of ``status`` at ``step``: a duplicate's stop is
    "duplicate"; a draft held unposted, and waiting_drafts' ok or stop, are "wait"; otherwise SECTION_OF says, any other
    status being a "problem". ``message`` is not used."""
    if status == "stop" and step == "check_duplicate":  # record_step records every duplicate's stop at this step
        return "duplicate"
    if step == "hold_posting" and status == "stop":  # verified, left as a draft until the client says to post it
        return "wait"
    if step == "waiting_drafts" and status in ("ok", "stop"):  # what waits: nothing to post, drafts elsewhere
        return "wait"
    return SECTION_OF.get(status, "problem")


def summary_text(key, step, status, message, labels=None):
    """Return one outcome's text for the summary, about ``key`` (an invoice id, None for a dossier's setup, or a
    negative file id): headed by the invoice (« Facture n° 12 (ATLAS) : ») whatever head the tool gave it, QUESTION_MARK
    before a question, what Tarik could not do for an error (with_problem). ``labels`` (labels_of): the invoice is named
    by its own number and supplier, never by the draft number Experio gave it."""
    text = named_for_client(message.removeprefix(TEST_MODE), key, labels, numbered=True)
    if status == "question":
        return QUESTION_MARK + text
    if status not in SECTION_OF:
        return with_problem(step, text)
    return text


HEAD = re.compile(r"[Ff]acture\b[^:\n]{0,150}? : ")  # « Facture 12 : », « Facture de `ATLAS` : », « Facture n° 5 : »


def named_for_client(text, key, labels, numbered=False):
    """Return ``text`` about invoice ``key`` headed by how the client knows it (labels_of), in place of the head the
    tool wrote (its draft number, or only the supplier): « Facture n° 277/25 (GECAFISC) : ... ». Without a label the
    text is left as it is, or with ``numbered`` headed by the draft number when it lacks it. A file (negative key) is
    named in backticks when the text does not; a dossier's setup is left as it is."""
    if not key:
        return text
    if key < 0:
        name = (labels or {}).get(key)
        return f"{name} : {text}" if name and "`" not in text else text
    head = (labels or {}).get(key)
    if head:
        return f"{head} : {HEAD.sub('', text, count=1) if HEAD.match(text) else text}"
    return f"Facture {key} : {text}" if numbered and str(key) not in text else text


def labels_of(ids):
    """Return {invoice id: its head for the client}: « Facture n° 1583109664 JW (SRM-CS) », its own number and its
    supplier (or customer) without the account and IF Experio appends, « Facture de SRM-CS » without a number; only
    what is known, {} when Experio can't tell. A file (negative key) is its name in backticks."""
    labels = file_labels(sorted({-i for i in ids if i and i < 0}))
    ids = sorted({i for i in ids if i and i > 0})
    if not ids:
        return labels
    try:
        fields = [
            "invoice_number",
            "partner_id",
            "amount_total",
            "currency_id",
            "amount_total_currency",
            "exchange_rate",
        ]
        found = odoo_call("exp.invoice", "search_read", [("id", "in", ids)], fields=fields)
    except Exception as error:  # a label is a comfort: the summary goes on with Tarik's numbers
        log(f"labels_of: {error}")
        return labels
    for invoice in found:
        who = without_if(invoice["partner_id"][1].split(" - ")[0]) if invoice["partner_id"] else ""
        currency = (invoice.get("currency_id") or [0, "MAD"])[1]
        total = dirhams(invoice.get("amount_total"), currency)
        if currency != "MAD" and total and invoice.get("amount_total_currency") and invoice.get("exchange_rate"):
            rate = f"{invoice['exchange_rate']:g}".replace(".", ",")
            total += f", soit {dirhams(invoice['amount_total_currency'])} au taux de {rate}"  # what is booked, in MAD
        about = ", ".join(part for part in (who, total) if part)
        number = (invoice["invoice_number"] or "").strip()
        if number:
            labels[invoice["id"]] = f"Facture n° {number}" + (f" ({about})" if about else "")
        elif who:
            labels[invoice["id"]] = f"Facture de {who}" + (
                f" ({about.removeprefix(who).lstrip(', ')})" if about != who else ""
            )
    return labels


def file_labels(ids):
    """Return {-file id: its name in backticks} for the files ``ids``: an error on a file must say which."""
    if not ids:
        return {}
    with cabinet_db() as db:
        names = db.execute(Q.file_names_of, (ids,)).fetchall()
    return {-file_id: f"`{name}`" for file_id, name in names}


def with_problem(step, text):
    """Return ``text`` saying what Tarik could not do (STEPS): after its « Facture N : » or `file` when it has one, else
    first: « Facture 30 : je n'ai pas pu vérifier la facture : <why> »."""
    problem = f"je n'ai pas pu {STEPS.get(step, 'aller au bout')}"
    head, sep, rest = text.partition(" : ")
    if sep and (head.lower().startswith("facture") or head.startswith("`")):
        return f"{head} : {problem} : {rest}"
    return f"{problem[0].upper()}{problem[1:]} : {text}"


def bullet(text, depth=0, mark="- "):
    """Return outcome ``text`` as a list item ``depth`` levels deep: its first line after ``mark``, its next lines (its
    own "- " sub-points) indented under it, so the chat and the email nest them."""
    pad = "  " * depth
    return pad + mark + text.replace("\n", "\n" + pad + " " * len(mark))


def listed(texts):
    """Return an invoice's (or file's) outcome ``texts`` as summary lines: one "- " item each when there is one or they
    don't start with a name (« Facture N » or a `file`); else that name on a line, then each outcome indented below."""
    head, sep, _ = texts[0].partition(" : ")
    if len(texts) == 1 or not sep or not (head.lower().startswith("facture") or head.startswith("`")):
        return [bullet(text) for text in texts]
    named = head.lower() + " : "
    return [f"- {head} :"] + [
        bullet(text[len(named) :] if text.lower().startswith(named) else text, 1) for text in texts
    ]


def released(run_id):
    """Free the invoices run ``run_id`` claimed, for another run: it is over."""
    with cabinet_db() as db:
        db.execute(Q.release_invoices, (run_id,))


@tool
def run_summary(run_id: str) -> dict:
    """Return what happened in run ``run_id``, for the client, as an ok result: a headline counting what ended where,
    then the sections that need the client: À corriger, Questions, Doublons, À vérifier (what Tarik decided alone and
    may have got wrong: the noted outcomes); empty ones left out (NOTHING when the run recorded nothing).

    A success is never listed: only a problem, a question, a duplicate or a noted outcome. A dossier's setup keeps one
    line per outcome (it is the request). Invoices are named by their own number and supplier (labels_of). Also frees
    the run's invoices for other runs and, outside test mode, notes the run in each invoice's Experio history
    (note_in_history), with all it did."""
    with cabinet_db() as db:
        rows = db.execute(
            Q.run_outcomes,
            (run_id,),
        ).fetchall()
        db.execute(Q.release_invoices, (run_id,))  # the run is over: its invoices are free for another
    if not rows:
        return ok(NOTHING)
    test = any(message.startswith(TEST_MODE) for _, _, _, message, _, _ in rows)
    if not test and not dry_run():
        note_in_history(rows)
    outcomes = outcomes_by_key(rows)
    sections = sections_of(outcomes, labels_of(outcomes))
    parts = [headline(outcomes), summary_sections(sections)]
    return ok((TEST_MODE + "\n\n" if test else "") + "\n\n".join(part for part in parts if part))


def headline(outcomes):
    """Return the one line counting where the run's invoices and statements ended (« 3 factures comptabilisées, 21
    laissées en brouillon, 10 doublons, 5 à corriger, 4 questions »); "" when none to count. A dossier's setup is
    not counted."""
    posted = held = reading = duplicates = problems = questions = 0
    # A statement's report is recorded under no file or invoice: counted wherever it stands.
    statements = sum(
        outcome[0] == "report_statement" and outcome[1] == "ok" for found in outcomes.values() for outcome in found
    )
    imported = 0  # statements imported with no pointing in this run (an answer to their bank question)
    for key, found in outcomes.items():
        if key is None:
            continue
        step, status = found[-1][:2]
        where = section_of(step, status)
        imported += key < 0 and (step, status) == ("import_bank_statement", "ok")
        if key < 0 and where in ("problem", "question"):
            problems, questions = problems + (where == "problem"), questions + (where == "question")
        elif key < 0:
            continue
        elif any(outcome[0] == "post_invoice" and outcome[1] == "ok" for outcome in found) and where == "done":
            posted += 1
        elif where == "duplicate":
            duplicates += 1
        elif where == "problem":
            problems += 1
        elif where == "question":
            questions += 1
        elif step == "hold_posting":
            held += 1
        elif where == "wait":
            reading += 1
    counts = [
        counted(statements, "{} relevé traité", "{} relevés traités"),
        counted(max(imported - statements, 0), "{} relevé importé", "{} relevés importés"),
        counted(posted, "{} facture comptabilisée", "{} factures comptabilisées"),
        counted(held, "{} facture vérifiée laissée en brouillon", "{} factures vérifiées laissées en brouillon"),
        counted(reading, "{} facture que Experio lit encore", "{} factures que Experio lit encore"),
        counted(duplicates, "{} doublon", "{} doublons"),
        counted(problems, "{} à corriger", "{} à corriger"),
        counted(questions, "{} question", "{} questions"),
    ]
    counts = [count for count in counts if not count.startswith("0 ")]
    if not counts:
        return ""
    # each draft's own line says it (sections_of), when the kept drafts are listed: the same rule
    told = any(is_held(outcomes[key]) for key in held_drafts(outcomes))
    return "Bilan : " + ", ".join(counts) + "." + (" Dites-moi quand les comptabiliser." if held and not told else "")


def note_in_history(rows):
    """Post what Tarik did on each invoice of ``rows`` (the run's outcomes) as one internal note in the invoice's
    history in Experio (message_post with subtype mail.mt_note, as its own actions are noted), written by Tarik's
    Experio user: what it set or changed, and where it left the invoice. Nothing for an invoice only routine steps
    touched. A refused note is logged, never a reason to fail the run's summary."""
    for key, found in outcomes_by_key(rows).items():
        if not key or key < 0:
            continue
        told = [
            summary_text(key, step, status, message) for step, status, message, is_routine, _ in found if not is_routine
        ]
        if not told:
            continue
        # One line: Experio keeps a text body as one <span>, its line breaks unseen in the history. Its names in « »:
        # the history shows no Markdown, a `name`'s backticks would stay raw.
        body = "Tarik (traitement automatique) : " + CODE.sub(r"« \1 »", joined_for_client(told))
        try:
            odoo_json(
                "exp.invoice", "message_post", [key], body=body, message_type="comment", subtype_xmlid="mail.mt_note"
            )
        except Exception as error:
            log(f"note_in_history {key}: {error}")


def outcomes_by_key(rows):
    """Return {key: [(step, status, message, routine, noted)]}: each invoice's (or file's) outcomes of ``rows``, in the
    sequence's order, each identical one once; a dossier's setup steps under the key None."""
    # In the sequence's order, whatever order the rows were recorded in (invoices of one run are interleaved).
    rows = sorted(rows, key=lambda row: position(row[1]))
    outcomes, seen = {}, set()
    for key, step, status, message, is_routine, is_noted in rows:
        if step in SETUP_STEPS:
            key = None  # a dossier's setup is one block, its imports in it in order
        if (key, step, message) not in seen:
            seen.add((key, step, message))
            outcomes.setdefault(key, []).append((step, status, message, is_routine, is_noted))
    return outcomes


def sections_of(outcomes, labels=None):
    """Return {section: [lines]}: each invoice (or file) of ``outcomes`` in the section of its last outcome, only when
    that is a problem, a duplicate or a question; the outcomes Tarik noted (the client should check them) under
    « À vérifier », whatever the invoice's end; a dossier's setup outcome by outcome. An invoice shows only its latest
    question, a file each of its questions."""
    sections = {name: [] for name in SECTIONS}
    held = held_drafts(outcomes)
    for key, found in outcomes.items():
        if key is None:  # a dossier's setup: each outcome in its own section, on its own line
            for step, status, message, is_routine, is_noted in found:
                if is_routine:
                    continue
                text = summary_text(key, step, status, message, labels)
                if is_noted and status == "ok" and step == "report_statement":  # recorded with no file: still to check
                    sections["check"].append(bullet(text))
                    continue
                # A question is numbered by summary_sections, never bulleted too.
                sections[section_of(step, status, message)].append(text if status == "question" else bullet(text))
            continue
        where = section_of(*found[-1][:3])
        checks = [summary_text(key, *outcome[:3], labels) for outcome in found if outcome[4] and outcome[1] == "ok"]
        sections["check"] += listed(checks) if checks else []
        if key in held:  # posted, or kept for the client's word: what was done to it (then that it waits)
            done = [outcome for outcome in found if outcome[1] == "ok" and not outcome[3] and not outcome[4]]
            texts = [summary_text(key, *outcome[:3], labels) for outcome in done]
            line = joined_for_client(texts) if texts else summary_text(key, *found[-1][:3], labels)
            sections["done"].append(bullet(f"{line} {HELD}" if texts and is_held(found) else line))
        if where in ("done", "wait"):  # a success or a wait: counted in the headline, never listed
            continue
        shown = [outcome for outcome in found if not outcome[3] or outcome is found[-1]]
        texts = [summary_text(key, *outcome[:3], labels) for outcome in shown if section_of(*outcome[:3]) == where]
        # An invoice waits on one question, its latest (asked again in other words, it is still the one); a file may
        # wait on several different ones (how to cut it, which side): each is asked.
        if where != "question":
            sections[where] += listed(texts)
        elif key > 0:
            sections[where] += texts[-1:]
        else:
            sections[where] += texts
    return sections


HELD = "Elle reste en brouillon : dites-moi quand la comptabiliser."
HELD_SHOWN = 3  # invoices posted or kept in one run that are each told what was done to them; more: only counted


def is_held(found):
    """True when an invoice's outcomes ``found`` end with it kept as a draft for the client's word (hold_posting)."""
    return found[-1][:2] == ("hold_posting", "stop")


def is_posted(found):
    """True when an invoice's outcomes ``found`` include its posting in this run (post_invoice ok, not the routine
    « déjà comptabilisée ») and end well."""
    posted = any(outcome[:2] == ("post_invoice", "ok") and not outcome[3] for outcome in found)
    return posted and section_of(*found[-1][:3]) == "done"


def held_drafts(outcomes):
    """Return the invoices of ``outcomes`` that get their own line (sections_of): those kept as drafts for the
    client's word, when they are HELD_SHOWN or fewer, and those posted, when they are HELD_SHOWN or fewer, each kind
    on its own (5 posted never hide 1 kept); the others are only counted (headline)."""
    invoices = {key: found for key, found in outcomes.items() if key and key > 0}
    held = {key for key, found in invoices.items() if is_held(found)}
    posted = {key for key, found in invoices.items() if is_posted(found)}
    return (held if len(held) <= HELD_SHOWN else set()) | (posted if len(posted) <= HELD_SHOWN else set())


def joined_for_client(texts):
    """Return an invoice's outcome ``texts`` (each headed « Facture n° … : ») as one sentence under the first head."""
    head, sep, _ = texts[0].partition(" : ")
    rest = [text.partition(" : ")[2] if text.startswith(head + sep) else text for text in texts]
    return f"{head} : " + "; ".join(part.rstrip(". ") for part in rest) + "."


def summary_sections(sections):
    """Return the non-empty ``sections`` as the client reads them: a heading, then its lines (questions numbered)."""
    parts = []
    for name, lines in sections.items():
        if not lines:
            continue
        # Questions numbered; the other lines are already listed (« - », an invoice's outcomes indented under it).
        items = [bullet(line, mark=f"{i}. ") for i, line in enumerate(lines, start=1)] if name == "question" else lines
        parts.append(SECTIONS[name] + "\n" + "\n".join(items))
    return "\n\n".join(parts)


def journal_name(journal_id):
    """Return bank journal ``journal_id``'s name while it still has no bank, else None."""
    found = odoo_call("exp.journal", "search_read", [("id", "=", journal_id), ("bank_id", "=", False)], fields=["name"])
    return found[0]["name"] if found else None


def waiting_on(session_id):
    """Return what conversation ``session_id`` waits on the client for, in this order: each draft invoice's pending
    question, each bank statement whose last import asked its bank account or network, each TPE statement asked its card
    sales' client, each bank journal asked its bank.

    A question on an invoice deleted or posted since is dropped (its pending row deleted), as is one on a journal given
    its bank since: nothing can answer it any more.

    Each item: {step, question, label (how the questions are listed: "facture 12", "relevé mars.pdf", "journal BQ"),
    names (what ``about`` may say), choices ({id: name} the question proposed, or None)}, plus invoice_id; or file_id,
    dossier_id and file_name; or dossier_id and journal_id."""
    with cabinet_db() as db:
        invoices = db.execute(
            Q.pending_of_session,
            (session_id,),
        ).fetchall()
        statements = db.execute(Q.statements_asked, (session_id,)).fetchall()
        clients = db.execute(Q.tpe_clients_asked, (session_id,)).fetchall()
        banks = db.execute(Q.banks_asked, (session_id,)).fetchall()
        ids = [i for i, _, _, _ in invoices]
        drafts = set(odoo_call("exp.invoice", "search", [("id", "in", ids), ("state", "=", "draft")])) if ids else set()
        gone = [i for i in ids if i not in drafts]
        if gone:
            db.execute(Q.delete_pending_of_invoices, (gone,))
    return (
        invoice_questions(invoices, drafts)
        + statement_questions(statements)
        + tpe_client_questions(clients)
        + bank_questions(banks)
    )


def invoice_questions(invoices, drafts):
    """Return waiting_on's items for the pending questions ``invoices`` [(invoice_id, step, question, choices)], only
    those of invoices still in ``drafts``. An invoice is listed and named the way the client knows it: its own number
    and its supplier (labels_of), Tarik's draft number too."""
    labels = labels_of(drafts)
    names = invoice_names(drafts)
    items = []
    for i, step, text, choices in invoices:
        if i in drafts:
            items.append(
                {
                    "invoice_id": i,
                    "step": step,
                    "question": named_for_client(text, i, labels),
                    "label": labels.get(i, f"facture {i}").lower(),
                    "headed": i in labels,  # the question begins with that label already
                    "names": {str(i), *names.get(i, ())},
                    "choices": choices,
                }
            )
    return items


def invoice_names(ids):
    """Return {invoice id: the names a client may answer about it by}: its own number and its supplier's names."""
    if not ids:
        return {}
    try:
        domain = [("id", "in", sorted(ids))]
        found = odoo_call("exp.invoice", "search_read", domain, fields=["invoice_number", "partner_id"])
    except Exception as error:  # the draft number still names it
        log(f"invoice_names: {error}")
        return {}
    return {draft["id"]: {*names_of(draft), normalized(draft["invoice_number"] or "")} - {""} for draft in found}


def statement_questions(statements):
    """Return waiting_on's items for the bank statements asked their bank account or network: ``statements`` [(file_id,
    dossier_id, question, file name, choices)]."""
    items = []
    for f, d, text, name, choices in statements:
        stem = name.rsplit(".", 1)[0]
        items.append(
            {
                "file_id": f,
                "dossier_id": d,
                "step": "import_bank_statement",
                "question": text,
                "label": f"relevé {name}",
                "file_name": name,
                "names": {normalized(name), normalized(stem)},
                "choices": choices,
            }
        )
    return items


def tpe_client_questions(clients):
    """Return waiting_on's items for the TPE statements asked the client of their card sales: ``clients`` [(file_id,
    dossier_id, question, file name)]."""
    return [
        {
            "file_id": f,
            "dossier_id": d,
            "step": "settle_statement",
            "question": text,
            "label": f"relevé {name}",
            "file_name": name,
            "names": {normalized(name), normalized(name.rsplit(".", 1)[0])},
            "choices": None,
        }
        for f, d, text, name in clients
    ]


def bank_questions(banks):
    """Return waiting_on's items for the bank journals asked their bank, ``banks`` [(dossier_id, journal_id, question,
    choices)], only those still without one."""
    items = []
    for d, j, text, choices in banks:
        name = journal_name(j)
        if not name:  # given its bank since
            continue
        items.append(
            {
                "dossier_id": d,
                "journal_id": j,
                "step": "ask_banks",
                "question": text,
                "label": f"journal {name}",
                "names": {normalized(name), f"journal {j}"},
                "choices": choices,
            }
        )
    return items


# The questions whose answer is the supplier's: « azur transit : sans retenue » answers each of its invoices asking it.
SUPPLIER_STEPS = {"settle_withholding", "settle_tva"}


def meant_all(waiting, about):
    """Return the ``waiting`` items the client answers: the only one, or those ``about`` names (an invoice's number, its
    supplier, a statement's name, as the questions are listed). Several only when they all ask the same supplier-level
    question (SUPPLIER_STEPS) and the client named their supplier; else [] when unclear: the client is asked which."""
    if len(waiting) == 1:
        return list(waiting)
    named = named_by(waiting, about)
    if len(named) == 1:
        return named
    steps = {item["step"] for item in named}
    return named if len(named) > 1 and len(steps) == 1 and steps <= SUPPLIER_STEPS else []


def named_by(waiting, about):
    """Return the ``waiting`` items ``about`` names: an invoice's number, its supplier, a statement's name."""
    about = normalized(str(about or "")).removeprefix("facture ").removeprefix("releve ").removeprefix("n ")
    return [item for item in waiting if about and about in item["names"]]


def meant(waiting, about):
    """Return the one ``waiting`` item the client answers (meant_all), None when unclear or several."""
    found = meant_all(waiting, about)
    return found[0] if len(found) == 1 else None


def about_this_file(item, answer):
    """True when the client's ``answer`` is about the statement file of ``item``: it names that file, or names no other
    file of the conversation (« douane.pdf n'est pas un relevé » is not about `releve.pdf`)."""
    if file_named(item["file_name"], answer):
        return True
    with cabinet_db() as db:
        names = [name for (name,) in db.execute(Q.other_file_names, (item["file_id"], item["file_id"]))]
    return not any(file_named(name, answer) for name in names)


def apply_answer(item, answer, decision):
    """Apply the client's ``answer`` (their own words) and the agent's ``decision`` (what it read in them) to the
    waiting ``item`` (from waiting_on).

    Returns the result of the call that takes it, with the ``step`` it answers and, when it went fine, the ``next_step``
    the sequence resumes at. When the decision does not settle the question (a bank not among those proposed, an unclear
    oui / non), the question is asked again."""
    if item["step"] == "import_bank_statement":
        return answer_statement(item, answer, decision)
    if item["step"] == "settle_statement":  # a TPE statement's client: its pointing goes on once given
        given = tpe.answer_client(item["file_id"], answer, decision)
        next_step = {"next_step": "settle_statement"} if given["status"] == "ok" else {}
        return {**given, "step": "settle_statement", "file_id": item["file_id"], **next_step}
    if item["step"] == "ask_banks":  # every journal the answer gives a bank, the others asked again
        return {**answer_banks(item["dossier_id"], answer, decision, item["choices"]), "step": "ask_banks"}
    result = answer_invoice(item, answer, decision)
    if result["status"] == "ok":
        result["next_step"] = ORDER[position(item["step"]) + 1]
    return {**result, "invoice_id": item["invoice_id"], "step": item["step"]}


def answer_statement(item, answer, decision):
    """apply_answer for a statement asked its bank account or network. When the client says this file is an invoice
    after all, it becomes one and the sequence resumes at identify_document, with the side they gave if any. Otherwise
    the statement is imported with the network chosen (a TPE statement) or into the journal chosen; when nothing chosen
    is understood, the import asks again."""
    # « ce n'est pas un relevé, c'est une facture », about this statement: never when it names another file only.
    if client_kind(own_words(answer)) == "invoice" and about_this_file(item, answer):
        set_kind(item["file_id"], "invoice")
        told = f"`{item['file_name']}` : une facture, comme vous l'avez dit."
        sides = [side for side in ("achat", "vente") if client_side(side, answer)]  # « une facture d'achat »
        return {
            **ok(told),
            "step": "import_bank_statement",
            "file_id": item["file_id"],
            "dossier_id": item["dossier_id"],
            "invoice_type": sides[0] if len(sides) == 1 else "",
            "next_step": "identify_document",
        }
    chosen = chosen_among(decision, item["choices"]) or 0  # 0: the question is asked again
    network = tpe.network_answered(item["choices"], chosen)  # a TPE statement asked its network
    if network:
        imported = import_bank_statement(item["file_id"], item["dossier_id"], 0, network=network)
        return {**imported, "step": "import_bank_statement"}
    if tpe.network_answered(item["choices"], 1):  # its network not understood: asked again
        imported = import_bank_statement(item["file_id"], item["dossier_id"], 0)
        return {**imported, "step": "import_bank_statement"}
    if BANK_QUESTION in item["question"]:  # the dossier had no bank journal: the bank named makes it
        bank = (item["choices"] or {}).get(str(chosen), "")  # kept as JSON: string keys
        imported = import_bank_statement(item["file_id"], item["dossier_id"], 0, client_message=f"{bank} {answer}")
        return {**imported, "step": "import_bank_statement"}
    imported = import_bank_statement(item["file_id"], item["dossier_id"], chosen)
    return {**imported, "step": "import_bank_statement"}


def answer_invoice(item, answer, decision):
    """apply_answer for an invoice's question: the result of the call that takes the answer (ANSWERS[step]), given what
    it needs:
    - an account to confirm: oui / non, from the decision, else from the answer;
    - tiers: the decision, the answer and the tiers the question proposed;
    - exercice, warnings: the answer and the decision;
    - retenue à la source: the answer (a file sent with it may be the supplier's attestation);
    - TVA, constatée d'avance: oui / non, from the decision, else a plain « oui » / « non » answer; a question result
      when neither says;
    - any other: the answer alone."""
    step = item["step"]
    apply = ANSWERS[step]
    proposed = item.get("choices") or {}
    if step == "assign_account" and len(proposed) == 1:  # an account to confirm, or a new one to create (proposals)
        settled = yes_no(decision) if yes_no(decision) is not None else yes_no(answer)
        key, label = next(iter(proposed.items()))
        if settled is not None and str(key).startswith("new "):
            return create_account(item["invoice_id"], str(key).removeprefix("new "), label, settled)
        if settled is not None:
            return confirm_account(item["invoice_id"], int(key), settled)
    if step == "assign_account" and len(proposed) > 1:  # the near accounts listed: picked among them first
        return set_account(item["invoice_id"], answer, choices=proposed)
    if step == "link_tiers":
        return apply(item["invoice_id"], decision, answer, item["choices"])  # the tiers the question proposed
    if step in ("settle_exercice", "settle_warnings"):  # their words, and what the agent read in them
        return apply(item["invoice_id"], answer, decision)
    if step == "settle_withholding":  # a file sent with the answer may be the supplier's attestation
        return apply(item["invoice_id"], answer, answering=True)
    if step not in DECIDED:
        return apply(item["invoice_id"], answer)
    if yes_no(decision) is None and yes_no(answer) is None:
        return question(f"Réponse non comprise. {item['question']}")
    # A plain "oui" or "non" is read here when the agent left the decision empty (it chose another tool).
    return apply(item["invoice_id"], decision if yes_no(decision) is not None else answer)


def new_files(session_id):
    """Return how many files came in conversation ``session_id`` since its last run; an entry file (ENTRY_FILES: FEC,
    grand livre) counts only when it is Experio's Excel statement template."""
    with cabinet_db() as db:
        names = [name for (name,) in db.execute(Q.new_files_of_session, (session_id, session_id))]
    return sum(not name.lower().endswith(ENTRY_FILES) or canvas_file_named(session_id, name) for name in names)


def awaiting_split(session_id):
    """Return how many PDFs of the conversation wait for the client to say how to cut them (split_invoices asked): such
    an answer goes to the files, whatever else waits."""
    with cabinet_db() as db:
        return db.execute(Q.pdfs_awaiting_split, (session_id,)).fetchone()[0]


@tool
def route_message(session_id: str, mode: str = "files") -> dict:
    """Return where a message of conversation ``session_id`` goes in the processing workflow, from what the database
    knows, not from the tool the agent happened to call.

    ``mode``: what the agent called it for: "files", "answer" or "resume".

    Returns an ok result whose ``route`` is:
    - "resume" when ``mode`` is "resume" (a follow-up stays one);
    - "answer" when the conversation waits on the client and no file came since its last run (nor waits to be cut);
    - "both" (the answer, then the files) when it waits on the client and files came;
    - "files" when files came and nothing waits, or ``mode`` is none of the three;
    - else ``mode``: "files" or "answer"."""
    if mode == "resume":
        return ok("Suite du traitement.", route="resume")
    waiting = bool(waiting_on(session_id))
    fresh = new_files(session_id) > 0 or awaiting_split(session_id) > 0
    if waiting:
        route = "both" if fresh else "answer"
    elif fresh or mode not in ("files", "answer"):
        route = "files"
    else:
        route = mode
    return ok(f"Aiguillage : {route}.", route=route)


@tool
def answer_pending(
    session_id: str, answer: str, decision: str = "", about: str = "", corrections: list | None = None
) -> dict:
    """Apply the client's answer to what this conversation waits on: an invoice's question (tiers, account, TVA, retenue
    à la source, constatée d'avance, exercice, warnings), a statement's bank account or network, a TPE statement's card
    sales' client, or a bank journal's bank.

    Args:
        session_id: the conversation.
        answer: the client's answer, their own words.
        decision: what the agent read in that answer, by question:
            - oui / non question (TVA, constatée d'avance, an account to confirm, most warnings): "oui" or "non";
            - tiers: the id of the tiers chosen among those proposed, or the other name or ICE given;
            - exercice: "nouveau" or the open exercice chosen;
            - statement's bank account: the journal_id chosen;
            - TPE statement's network: the network chosen, as the question lists it;
            - TPE statement's client: the client's name as given;
            - bank journal's bank: the bank chosen among those proposed;
            - any other question: empty.
        about: with several questions waiting, the invoice's number or the statement's name the client answers.
        corrections: the accounts the client gives invoices in the same message, as the agent read them ([{facture,
            compte}], their words); each is checked against those words and the conversation's drafts
            (verified_corrections).

    Returns the result of applying the answer: its ``next_step`` says where the sequence resumes, and ``corrections``
    ([[invoice, account]]) holds the corrections kept. With nothing waiting, an ok result whose next_step,
    waiting_drafts, resumes the drafts Experio hadn't read yet. A question result when several questions wait and
    ``about`` names none of them. The email's sender must have the dossier, as for any work on it: otherwise
    check_sender's result."""
    answer = own_words(answer)
    waiting = waiting_on(session_id)
    # An answer about an invoice no question waits on (« IFS a le numéro 2503-0702 » beside SRM's question) is never
    # applied to another's question: its draft is taken up again instead.
    elsewhere = bool(waiting and about.strip() and not named_by(waiting, about))
    if not waiting or elsewhere:
        # Nothing asked: an account given here still goes to the invoice it names (none answered about).
        fixes, notes = verified_corrections(session_id, answer, corrections or [], None)
        result = ok(
            ("Aucune question en attente" + (f" pour « {about} »" if elsewhere else ""))
            + ". Je reprends les factures restées en cours."
            + "".join(f" {note}" for note in notes),
            next_step="waiting_drafts",
            step="answer_pending",
        )
        if fixes:
            result["corrections"] = as_pairs(fixes)
        return result
    # Bank journals asked together are answered together (answer_banks reads each journal's bank in the answer).
    journals = {(entry["step"], entry.get("dossier_id")) for entry in waiting}
    items = waiting[:1] if len(journals) == 1 and waiting[0]["step"] == "ask_banks" else meant_all(waiting, about)
    item = items[0] if items else None
    if not item:
        # An invoice's question already begins with its number and supplier when Experio gave them.
        lines = (  # bank journals asked together share one question: listed once
            f"- {entry['question']}"
            if entry.get("headed") or entry["step"] == "ask_banks"
            else f"- {entry['label']} : {entry['question']}"
            for entry in waiting
        )
        questions = "\n".join(dict.fromkeys(lines))
        return question(
            f"Plusieurs questions attendent votre réponse ; précisez la facture ou le relevé :\n{questions}",
            step="answer_pending",
        )
    sender = check_sender(session_id, dossier_id=item.get("dossier_id", 0), invoice_id=item.get("invoice_id", 0))
    if sender["status"] != "ok":  # recorded as check_sender's: the email workflow tells the manager, never the sender
        return {**sender, "step": "check_sender"}
    result = apply_answer(item, answer, decision)
    # The same supplier's other invoices asking the same question get the answer too, each to go on from its next step.
    more = [(other["invoice_id"], apply_answer(other, answer, decision)) for other in items[1:]]
    result["more"] = [[i, r["next_step"]] for i, r in more if r.get("status") == "ok" and r.get("next_step")]
    fixes, notes = verified_corrections(session_id, answer, corrections or [], item.get("invoice_id"))
    result["message"] = result.get("message", "") + "".join(f" {note}" for note in notes)
    if fixes:
        result["corrections"] = as_pairs(fixes)
    return result


def as_pairs(fixes):
    """Return {invoice: account} as [[invoice, account]], by invoice: a list, kept as it is through the workflow's
    state."""
    return [[invoice_id, account] for invoice_id, account in sorted(fixes.items())]


# How a client points at the invoice they answer about (normalized): « mettez-la », « celle-ci ».
THIS_ONE = {"", "la", "elle", "celle ci", "celle la", "cette facture", "cette facture ci", "la meme"}


def traceable(value, words):
    """True when ``value``, as the agent read it, is in the client's normalized ``words``: each of its numbers, and
    most of its words of four letters or more (« 61425000 Transport sur achats » for « en 61425000 transport »)."""
    tokens = normalized(value).split()
    numbers = [token for token in tokens if token.isdigit()]
    long_words = [token for token in tokens if not token.isdigit() and len(token) >= 4]
    if not numbers and not long_words:
        return said(normalized(value), words)
    found = sum(said(word, words) for word in long_words)
    return all(said(number, words) for number in numbers) and found * 2 >= len(long_words)


def verified_corrections(session_id, answer, corrections, answered):
    """Check the accounts the agent read in the client's answer; return ({invoice: account}, notes).

    Args:
        session_id: the conversation.
        answer: the client's own words.
        corrections: [{facture, compte}] as the agent read them.
        answered: the invoice the client answers about, or None.

    Each account must be in the client's own words (traceable). Its invoice is ``answered`` when the correction names
    none (« mettez-la »), else the one draft of the conversation it names (Tarik's number, the invoice's number, its
    supplier), on a dossier the sender has. ``notes``: what is reported to the client of the accounts left out for want
    of an invoice. Never a model's guess."""
    if not corrections:
        return {}, []
    words = normalized(answer)
    names = draft_names(session_id)
    fixes, notes = {}, []
    for correction in corrections:
        correction = correction if isinstance(correction, dict) else {}
        reference = str(correction.get("facture") or "").strip()
        account = str(correction.get("compte") or "").strip()
        if not account or not traceable(account, words):
            continue  # not the client's words: never applied
        if normalized(reference) in THIS_ONE:
            target = answered
        else:
            named = named_drafts(normalized(reference), names) if traceable(reference, words) else []
            target = named[0] if len(named) == 1 else None
        if not target:
            notes.append(f"Le compte « {account} » : précisez pour quelle facture (son numéro), je n'ai rien changé.")
            continue
        if target != answered and check_sender(session_id, invoice_id=target)["status"] != "ok":
            continue  # a dossier the sender may not work on: never applied
        fixes[target] = account
    return fixes, notes


def draft_names(session_id):
    """Return {draft id: [names a client may call it by]} (names_of) for the conversation's drafts: made from its files,
    asked about in it, or worked on in it."""
    with cabinet_db() as db:
        ids = {i for (i,) in db.execute(Q.drafts_of_session, (session_id,))}
        ids |= {row[0] for row in db.execute(Q.pending_of_session, (session_id,))}
        ids |= {row[0] for row in db.execute(Q.invoice_outcomes_of_session, (session_id,))}
    ids = sorted(ids)
    domain = [("id", "in", ids)]  # search_read: a draft deleted in Experio since is left out, never an error
    drafts = odoo_call("exp.invoice", "search_read", domain, fields=["invoice_number", "partner_id"]) if ids else []
    return {draft["id"]: names_of(draft) for draft in drafts}


def names_of(draft):
    """Return the names a client may call ``draft`` by (three letters or more): Tarik's number, the invoice's own
    number, its supplier's names."""
    names = [str(draft["id"]), normalized(draft["invoice_number"] or "")]
    if draft["partner_id"]:
        names += supplier_names(draft["partner_id"][1])
    return [name for name in names if len(name) >= 3]


def named_drafts(words, names):
    """Return the ids, sorted, of the drafts of ``names`` (draft_names) that ``words`` (normalized) name."""
    return sorted(i for i, found in names.items() if any(said(name, words) for name in found))


@tool
def save_files(session_id: str = "", files: list | None = None) -> dict:
    """Save a conversation's files (the agent's attachments); an ok result lists those saved (``files``: [{id,
    file_name}]).

    ``files``: [{id, file_name, mime_type, data (base64)}]; the id saves each one once however many times the sequence
    runs. A zip is saved as the files in it Tarik takes (engine.tools.core.archives), each under its archive's id and
    its own name; those skipped are named in the message."""
    saved, skipped = [], []
    with cabinet_db() as db:
        for file in files or []:
            found, left = unpacked([(file["file_name"], file["mime_type"], base64.b64decode(file["data"]))], FILE_TYPES)
            skipped += left
            for name, mime, data in found:
                key = file.get("id") if len(found) == 1 and name == file["file_name"] else f"{file.get('id')}:{name}"
                row = db.execute(Q.insert_agent_file, (session_id, name, mime, data, key)).fetchone()
                if row:
                    saved.append({"id": row[0], "file_name": name})
    left = f" Ignorés : {' ; '.join(skipped)}." if skipped else ""
    return ok(counted(len(saved), "{} fichier enregistré.", "{} fichiers enregistrés.") + left, files=saved)


def is_entries(file_id, name):
    """True when file ``name`` is a dossier's entries (FEC, grand livre) for the dossier workflow: a spreadsheet or
    text file, never Experio's Excel statement template (a statement, for this workflow)."""
    if not name.lower().endswith(ENTRY_FILES):
        return False
    with cabinet_db() as db:
        row = db.execute(Q.file_data, (file_id,)).fetchone()
    return not (row and is_canvas(name, row[0]))


def canvas_file_named(session_id, name):
    """True when a file of conversation ``session_id`` named ``name`` is Experio's Excel statement template."""
    with cabinet_db() as db:
        rows = db.execute(Q.files_named, (session_id, name)).fetchall()
    return any(is_canvas(name, data) for (data,) in rows)


@tool
def files_to_process(session_id: str, entries: bool = False, asked: bool = False, client_message: str = "") -> dict:
    """Return the conversation's files not processed yet (``files``: their ids): its invoices and statements, or with
    ``entries`` its FEC and grand livre files. Picked here, not by the agent, which never sees the files' numbers.

    ``asked``: the client's message asked for files to be processed; when none is found, the result reports to the
    client that the attachment did not arrive (NO_FILE).

    A file identify_document found to be neither an invoice nor a statement (a payment receipt) was refused once and
    said so: it is not listed again, which would re-run and re-refuse it at every message and keep the conversation's
    drafts from being taken up. It is listed again only when ``client_message`` names it (« c'est une facture »)."""
    with cabinet_db() as db:
        rows = db.execute(
            Q.files_to_process,
            (session_id,),
        ).fetchall()
    message = own_words(client_message)
    own = [
        i for i, name, kind in rows if is_entries(i, name) == entries and (kind != "other" or file_named(name, message))
    ]
    if asked and not own:
        return ok(NO_FILE, files=[], tell=True)
    return ok(counted(len(own), "{} fichier à traiter.", "{} fichiers à traiter."), files=own)


# The stops a client's "reprenez" retries: a dossier setting or an Experio check the cabinet may have fixed since
# (correct_invoice: no amount, the scan's nor Experio's, until the team types one in Experio).
# A duplicate, a type or a draft kept unposted (hold_posting) is the client's call, not a setting.
FIXABLE_STOPS = {
    "assign_account",
    "check_journal",
    "configure_cabinet",
    "correct_invoice",
    "settle_warnings",
    "post_invoice",
}


def resumable(step, status, stopped, sided=False):
    """True when a draft whose furthest outcome is ``status`` at ``step`` is taken up again: it waited or failed; or
    (``stopped``: the client asks to resume) a fixable setting stopped it (FIXABLE_STOPS); or (``sided``: the client now
    says which side it is) its classification stopped it."""
    if status in ("wait", "error"):
        return True
    return stopped and status == "stop" and (step in FIXABLE_STOPS or (sided and step == "classify_invoice"))


def latest_runs(rows):
    """Return, of ``rows`` (invoice_id, step, status, run_id, in the order recorded), each invoice's outcomes of the
    latest run that recorded any for it, as (invoice_id, step, status)."""
    last = {invoice_id: run_id for invoice_id, _, _, run_id in rows}
    return [(invoice_id, step, status) for invoice_id, step, status, run_id in rows if last[invoice_id] == run_id]


def furthest_outcomes(rows):
    """Return {invoice_id: (step, status)}: the furthest step each invoice of ``rows`` (invoice_id, step, status)
    reached, by the sequence's order, not the rows'."""
    furthest = {}
    for invoice_id, step, status in rows:
        if step in ORDER and (invoice_id not in furthest or position(step) >= position(furthest[invoice_id][0])):
            furthest[invoice_id] = (step, status)
    return furthest


@tool
def waiting_drafts(
    session_id: str,
    held: bool = False,
    dossier_id: int = 0,
    stopped: bool = False,
    run_id: str = "",
    invoice_type: str = "",
    client_message: str = "",
    compte: str = "",
) -> dict:
    """Return this conversation's drafts to take up again from Experio's reading on, as an ok result (``invoices``).

    A draft is taken up when the furthest step of its latest run ended waiting on Experio or failed (an Experio or model
    timeout), or when nothing was recorded for it (a run cut short by a server restart); every later step then runs
    again. A question or a stop is not resumed here, except what the client asks for. A draft deleted or posted in
    Experio since is left alone.

    Args:
        session_id: the conversation.
        held: the client now asks to post: also take the drafts they asked to leave unposted (hold_posting). With
            ``dossier_id``, a draft Tarik posted that was put back to draft in Experio since is posted again: Experio
            decides what is a draft.
        dossier_id: the dossier. With ``held`` or ``stopped``, its drafts from other conversations are taken too, only
            when the client's own words ask for the whole dossier or name them (asked_among); with ``held``, also its
            Facturation drafts (facturation_drafts).
        stopped: the client asks to resume: also take the drafts a fixable setting stopped (FIXABLE_STOPS: the cabinet
            has fixed it since).
        run_id: the run taking them up, which claims them: a draft another run is still working on is left to it.
        invoice_type: the side the client now gives (achat, vente): with ``stopped``, a draft its classification stopped
            (not an invoice, the other side) is classified again.
        client_message: the client's own words; never an automatic follow-up's.
        compte: an account the agent read with no file sent: a correction for the one draft of the conversation the
            client's words name (``corrections`` in the result), to be confirmed.

    Returns an ok result with ``invoices`` (the import folders' transit invoices last) and, when there are any,
    ``corrections``, ``facturation`` (Facturation drafts to post) and ``statements`` (statement files to take up again).
    When the client asks to post and nothing is to be, a stop result says why (nothing_to_post), or a routine result
    when this run's other branch already took them. Drafts other conversations left in the dossier are only counted,
    with how to ask for them (elsewhere_hint); when the client asks for the whole dossier, the drafts that stay are
    reported to the client (left_in_dossier)."""
    sided = stopped and invoice_type in SIDES  # their words, verified at the start of the run (verified_side)
    with cabinet_db() as db:
        rows = db.execute(
            Q.invoice_outcomes_of_session,
            (session_id,),
        ).fetchall()
        drafts = db.execute(
            Q.drafts_of_session,
            (session_id,),
        ).fetchall()
        elsewhere = []
        if dossier_id and stopped:
            elsewhere = db.execute(Q.outcomes_of_stopped_invoices).fetchall()
        kept, reposted = [], []
        if held:
            kept = db.execute(Q.held_not_released).fetchall()
        if held and dossier_id:
            reposted = db.execute(Q.outcomes_of_posted_invoices).fetchall()
    # Where each draft stands is where its latest run left it: an earlier run that took it further (a draft kept
    # unposted, taken up since and stopped at a setting) no longer says where it is.
    mine = furthest_outcomes(latest_runs(rows))
    waiting = [i for i, (step, status) in mine.items() if resumable(step, status, stopped, sided)]
    # shortcut: a draft of a run still going also has no outcome yet; one conversation runs one at a time
    waiting += [invoice_id for (invoice_id,) in drafts if invoice_id not in mine]
    # The conversation's own held drafts, always; the dossier's from other conversations (stopped by a setting, held,
    # or posted and put back to draft since) only when the client asks for them: the whole dossier, or by name.
    # (Its hold may be recorded under another conversation the same file came in: it is still this one's draft.)
    waiting += [invoice_id for invoice_id, owner in kept if owner == session_id or invoice_id in mine]
    others, by_people = set(), set()
    if dossier_id and stopped:
        others |= {i for i, (step, status) in furthest_outcomes(elsewhere).items() if resumable(step, status, True)}
    if dossier_id and held:
        others |= {invoice_id for invoice_id, owner in kept if owner != session_id}
        # Posted by Tarik, still a draft in Experio only when someone put it back (the domain below keeps the drafts).
        others |= {i for i, outcome in furthest_outcomes(reposted).items() if outcome == ("post_invoice", "ok")}
        # Only on the client's own words (an automatic follow-up writes none): no search of the dossier's drafts else.
        by_people = made_in_experio(dossier_id) if normalized(own_words(client_message)) else set()
        others |= by_people
    others -= set(waiting) | set(mine)
    if others:  # still drafts of this dossier, whatever conversation (or dossier) left them
        domain = [("id", "in", sorted(others)), ("state", "in", ["pending", "draft"]), ("company_id", "=", dossier_id)]
        others = set(odoo_call("exp.invoice", "search", domain))
    asked = asked_among(others, dossier_id, client_message) if others else []
    # Of the drafts people made in Experio, only so many at once: the rest is said, to be asked for again.
    cut = [i for i in asked if i in by_people][AT_ONCE:]
    asked = [i for i in asked if i not in cut]
    waiting += asked
    # A message naming invoices (Tarik's number, the invoice's own, its supplier) is about those only: nothing else of
    # the conversation is taken up with them, however long it has been waiting.
    named = []
    if dossier_id and waiting and not asks_whole(client_message, dossier_id):
        named = asked_among(waiting, dossier_id, client_message)
    # The dossier's drafts from Experio's own invoicing (Facturation), posted when the client asks for them: never
    # read nor checked by Tarik (no scan, accounts per line), they start at the dossier's settings.
    billed = facturation_drafts(dossier_id, client_message) if held and dossier_id and not named else []
    elsewhere_left = len(others) - len(asked)
    if waiting:
        domain = [("id", "in", sorted(set(waiting))), ("state", "in", ["pending", "draft"])]
        # What came from any conversation is limited to this dossier's.
        if dossier_id and (held or stopped):
            domain.append(("company_id", "in", [dossier_id]))
        waiting = sorted(odoo_call("exp.invoice", "search", domain))
    if named:
        waiting = [i for i in waiting if i in named]
    transit = odoo_call("exp.invoice", "search", [("id", "in", waiting), TRANSIT]) if waiting else []
    waiting = transit_last(waiting, set(transit))
    billed = [i for i in billed if i not in waiting]
    if (waiting or billed) and run_id:
        with cabinet_db() as db:
            claimed = {i for (i,) in db.execute(Q.claim_invoices, (sorted({*waiting, *billed}), run_id)).fetchall()}
        waiting, billed = [i for i in waiting if i in claimed], sorted(set(billed) & claimed)  # transit last kept
    corrections = drafts_corrected(session_id, client_message, compte)
    if corrections:
        return ok(
            counted(len(waiting), "{} facture à reprendre.", "{} factures à reprendre."),
            invoices=waiting,
            corrections=corrections,
            facturation=billed,
        )
    statements = [] if named else statements_to_resume(session_id, held)
    if billed or statements:
        count = len(waiting) + len(billed)
        return ok(
            counted(count, "{} facture", "{} factures")
            + counted(len(statements), " et {} relevé à reprendre.", " et {} relevés à reprendre."),
            invoices=waiting,
            facturation=billed,
            statements=statements,
        )
    whole = held and (cut or asks_whole(client_message, dossier_id))
    left = left_in_dossier(dossier_id, {*waiting, *billed}, cut) if whole else ""
    if left and waiting:
        return ok(left, invoices=waiting, tell=True)  # told in the reply, with what is posted
    hint = elsewhere_hint(elsewhere_left, client_message, held) if not waiting else ""
    if held and not waiting:
        if run_id and worked_on(run_id):  # this run's other branch took them (« comptabilisez » goes both ways)
            return routine("Rien d'autre à comptabiliser.", invoices=[])
        # Apart: nothing_to_post ends on its list, which the hint would otherwise run on from.
        told = [nothing_to_post(session_id, dossier_id), hint.strip(), left]
        return stop("\n\n".join(part for part in told if part), invoices=[])
    if hint:
        return ok("Rien à reprendre dans cette conversation." + hint, invoices=[], tell=True)
    return ok(counted(len(waiting), "{} facture à reprendre.", "{} factures à reprendre."), invoices=waiting)


def statements_to_resume(session_id, held):
    """Return the conversation's statement files (ids) to take up again: those whose pointing waited (Experio was still
    reading), and with ``held`` (the client asks to post) those whose statement, bank or TPE, is not posted, exported or
    cancelled yet. Of files merged into one statement, only the first stands for it."""
    with cabinet_db() as db:
        rows = db.execute(Q.statement_outcomes_of_session, (session_id,)).fetchall()
    latest, statements, seen = {}, {}, set()
    networks = {}
    for file_id, statement_id, network, step, status in rows:
        if statement_id in seen and file_id not in statements:
            continue  # merged with an earlier file of the conversation: that one stands for the statement
        seen.add(statement_id)
        statements[file_id] = statement_id
        networks[file_id] = network
        if step in ("settle_statement", "post_statement"):
            latest[file_id] = status
    waiting = [f for f, status in latest.items() if status == "wait"]
    for model, tpe_files in (("exp.bank.statement", False), (tpe.STATEMENT, True)):
        mine = {f: s for f, s in statements.items() if bool(networks[f]) == tpe_files}
        if not held or not mine:
            continue
        domain = [("id", "in", sorted(set(mine.values()))), ("state", "not in", ["post", "export", "cancel"])]
        open_ids = set(odoo_call(model, "search", domain))
        waiting += [f for f, statement_id in mine.items() if statement_id in open_ids and f not in waiting]
    return sorted(waiting)


# Drafts of other conversations or made in Experio taken in one message: a run goes one invoice at a time.
AT_ONCE = 25


def made_in_experio(dossier_id):
    """Return the dossier's drafts that people made in Experio (not Facturation's, which have their own path): none
    Tarik made, worked on or asked about. They are taken like another conversation's drafts: only when the client's
    own words ask for the whole dossier or name them (asked_among), then from Experio's reading on."""
    domain = [("company_id", "=", dossier_id), ("state", "in", ["pending", "draft"])]
    if "gst_invoice_id" in experio_form()["types"]:
        domain.append(("gst_invoice_id", "=", False))
    found = odoo_call("exp.invoice", "search", domain)
    if not found:
        return set()
    with cabinet_db() as db:
        return set(found) - {i for (i,) in db.execute(Q.invoices_known_to_tarik, (found, found, found))}


def facturation_drafts(dossier_id, client_message):
    """Return the dossier's drafts made by Experio's Facturation (gst_invoice_id set) that the client's own words ask
    for: the whole dossier, or named (asked_among). [] when the client wrote nothing, or the cabinet's Experio has no
    Facturation (no gst_invoice_id field)."""
    if not normalized(own_words(client_message)) or "gst_invoice_id" not in experio_form()["types"]:
        return []
    domain = [("company_id", "=", dossier_id), ("state", "=", "draft"), ("gst_invoice_id", "!=", False)]
    found = odoo_call("exp.invoice", "search", domain)
    return asked_among(found, dossier_id, client_message) if found else []


def worked_on(run_id):
    """True when run ``run_id`` already took any invoice or statement in hand."""
    with cabinet_db() as db:
        return bool(db.execute(Q.run_worked_on_invoices, (run_id,)).fetchone())


def elsewhere_hint(count, client_message, held):
    """Return what is reported to the client of the ``count`` drafts other conversations left in the dossier: how to ask
    for them too (whole_example, from ``client_message`` and ``held``). "" when there are none."""
    if not count:
        return ""
    example = whole_example(client_message, held)
    return counted(
        count,
        " {} brouillon d'une autre conversation ou créé dans Experio attend dans ce dossier. "
        "Pour le reprendre aussi, écrivez "
        f"« {example} » ou nommez-le (numéro de facture ou fournisseur).",
        " {} brouillons d'autres conversations ou créés dans Experio attendent dans ce dossier. "
        "Pour les reprendre aussi, écrivez "
        f"« {example} » ou nommez-les (numéro de facture ou fournisseur).",
    )


def drafts_corrected(session_id, client_message, compte):
    """Return an account the agent read with no file sent (the processing tool's ``compte``) as a correction, [[invoice,
    account]], for the one draft of the conversation the client's own words name, checked as any correction
    (verified_corrections); [] otherwise."""
    if not compte.strip():
        return []
    words = own_words(client_message)
    named = named_drafts(normalized(words), draft_names(session_id))
    if len(named) != 1:
        return []
    fixes, _ = verified_corrections(session_id, words, [{"facture": str(named[0]), "compte": compte}], None)
    return as_pairs(fixes)


# How a client asks for every draft of the dossier, whatever conversation left it (normalized words).
WHOLE = re.compile(  # « toutes les factures (du dossier) », never « les factures du dossier X » nor « … de MIDAS »
    r"(?<![a-z])(?:tout le dossier|(?:toutes|tous) (?:les|ses) (?:factures|brouillons)(?: du dossier)?"
    r"(?! (?:de|du|d|des) )|toutes celles du dossier|l ensemble (?:des factures|des brouillons|du dossier))(?![a-z])"
)
# « les factures en brouillon(s) de la société X »: the whole dossier only when X is the conversation's dossier.
DRAFTS_OF = re.compile(r"(?<![a-z])les factures en brouillons? (?:de la societe|du dossier) (.+)", re.DOTALL)
# The client's own verb, to say back how to ask for the whole dossier (normalized stem -> as written).
VERBS = {"comptabilis": "comptabilisez", "repren": "reprenez", "relanc": "relancez", "termin": "terminez"}


def whole_example(client_message, held):
    """Return « <verb> toutes les factures du dossier »: the verb the client wrote (comptabilisez, reprenez…), else «
    comptabilisez » with ``held`` (asking to post), « reprenez » otherwise."""
    words = normalized(own_words(client_message))
    verb = next((written for stem, written in VERBS.items() if re.search(rf"(?<![a-z]){stem}", words)), None)
    return f"{verb or ('comptabilisez' if held else 'reprenez')} toutes les factures du dossier"


# Words too common in suppliers' names to name one on their own (normalized).
GENERIC = {"maroc", "societe", "ste", "sarl", "services", "service", "international", "group", "groupe", "station"}


def supplier_names(name):
    """Return how a client may name supplier ``name``: whole, or by its first distinctive word (« VALADE » for « GR
    VALADE LOGISTIQUE »)."""
    whole = normalized(name.split(" - ")[0])
    first = next((word for word in whole.split() if len(word) >= 4 and word not in GENERIC), "")
    return [whole, first]


def dossier_name(dossier_id):
    """Return dossier ``dossier_id``'s name as Experio has it."""
    return odoo_call("res.company", "read", [dossier_id], fields=["name"])[0]["name"]


def asks_whole(client_message, dossier_id):
    """True when the client's own words ask for the whole dossier (« toutes les factures du dossier », or « les
    factures en brouillon de la société X » with X the dossier ``dossier_id``: a supplier is never taken for it)."""
    words = normalized(own_words(client_message))
    if WHOLE.search(words):
        return True
    named = DRAFTS_OF.search(words)
    return bool(named and dossier_id and named.group(1).startswith(normalized(dossier_name(dossier_id))))


def left_in_dossier(dossier_id, taken, cut=()):
    """When the client asks for the whole dossier: return the lines reporting how many of its drafts stay drafts,
    except those ``taken``, and why: a question waiting in their conversation, stopped by a step, or not imported by
    Tarik; ``cut`` are the drafts made in Experio left for the next message (AT_ONCE). "" when none stay or no
    ``dossier_id``."""
    if not dossier_id:
        return ""
    domain = [("id", "not in", sorted(taken)), ("company_id", "=", dossier_id), ("state", "in", ["pending", "draft"])]
    left = odoo_call("exp.invoice", "search", domain)
    if not left:
        return ""
    with cabinet_db() as db:
        asked = {i for i, _ in db.execute(Q.pending_of_invoices, (left,))}
        made = {i for (i,) in db.execute(Q.invoices_known_to_tarik, (left, left, left))}  # Tarik's, once worked on
    counts = [
        (
            len(asked),
            "{} facture attend votre réponse à sa question, dans sa conversation.",
            "{} factures attendent votre réponse à leur question, dans leur conversation.",
        ),
        (
            len(made - asked),
            "{} facture est arrêtée par un point à corriger.",
            "{} factures sont arrêtées par un point à corriger.",
        ),
        (
            len(set(left) & set(cut)),
            f"{{}} facture créée dans Experio n'a pas été reprise cette fois ({AT_ONCE} au plus à la fois) : "
            "redemandez-moi de la reprendre.",
            f"{{}} factures créées dans Experio n'ont pas été reprises cette fois ({AT_ONCE} au plus à la fois) : "
            "redemandez-moi de les reprendre.",
        ),
        (
            len(set(left) - made - asked - set(cut)),
            "{} facture n'a pas été importée par moi : comptabilisez-la dans Experio.",
            "{} factures n'ont pas été importées par moi : comptabilisez-les dans Experio.",
        ),
    ]
    return "Restent en brouillon dans ce dossier :\n" + "\n".join(
        f"- {counted(count, one, many)}" for count, one, many in counts if count
    )


def asked_among(ids, dossier_id, client_message):
    """Return, of the dossier's drafts ``ids`` other conversations left, those the client's own words ask for: all of
    them (« toutes les factures du dossier »), else those they name by Tarik's draft number, their invoice number or
    their supplier; [] otherwise. Never a model's reading: what was left elsewhere is the client's call."""
    words = normalized(own_words(client_message))
    if not words:
        return []
    if asks_whole(client_message, dossier_id):
        return sorted(ids)
    drafts = odoo_call(
        "exp.invoice",
        "search_read",
        [("id", "in", sorted(ids)), ("company_id", "=", dossier_id)],
        fields=["invoice_number", "partner_id"],
    )
    named = [draft["id"] for draft in drafts if any(said(name, words) for name in names_of(draft))]
    return sorted(named)


def nothing_to_post(session_id, dossier_id):
    """Return why nothing is posted when the client asks to post: each draft of this conversation and what keeps it from
    being posted (a question waiting for them, not imported by Tarik, another run at it, or where Tarik's sequence
    stopped it). Other conversations' drafts are only counted (waiting_drafts' hint), never listed here."""
    with cabinet_db() as db:
        mine = {i for (i,) in db.execute(Q.drafts_of_session, (session_id,))}
        mine |= {row[0] for row in db.execute(Q.invoice_outcomes_of_session, (session_id,))}
    domain = [("id", "in", sorted(mine)), ("state", "in", ["pending", "draft"])]
    if dossier_id:
        domain.append(("company_id", "=", dossier_id))
    drafts = odoo_call("exp.invoice", "search_read", domain, fields=["invoice_number"], order="id") if mine else []
    if not drafts:
        return "Aucune facture n'est restée en brouillon dans cette conversation. Il n'y a rien à comptabiliser."
    ids = [draft["id"] for draft in drafts]
    with cabinet_db() as db:
        made = {i for (i,) in db.execute(Q.invoices_made_by_tarik, (ids,))}
        asked = dict(db.execute(Q.pending_of_invoices, (ids,)))
        outcomes = db.execute(Q.outcomes_of_invoices, (ids,)).fetchall()
        working = dict(db.execute(Q.invoices_worked_on, (ids,)).fetchall())
    # An invoice a run is at: only that run's outcomes, an earlier run's stop no longer says where it is.
    outcomes = [row for row in outcomes if row[0] not in working or row[4] == working[row[0]]]
    rows = [(i, step, status) for i, step, status, _, _ in outcomes]
    messages = {(i, step): message for i, step, _, message, _ in outcomes}
    furthest = furthest_outcomes(rows)
    lines = []
    for draft in drafts:
        name = f"Facture {draft['id']} ({draft['invoice_number'] or 'sans numéro'})"
        if draft["id"] in asked:
            lines.append(f"- {name} attend votre réponse à cette question : {asked[draft['id']]}")
        elif draft["id"] not in made:
            lines.append(f"- {name} n'a pas été importée par moi : comptabilisez-la dans Experio.")
        elif draft["id"] in working and furthest.get(draft["id"], ("", "ok"))[1] in ("ok", "wait"):
            # Another run is at it, not stopped yet: its own reply will tell how it ends.
            lines.append(f"- {name} est en cours de traitement. Je vous écris dès qu'elle est prête.")
        elif draft["id"] in furthest:
            message = messages[(draft["id"], furthest[draft["id"]][0])].removeprefix(f"Facture {draft['id']} : ")
            lines.append(bullet(f"{name} : {message}"))
        else:
            lines.append(f"- {name} est encore en cours de traitement.")
    return "Rien n'a été comptabilisé. Les brouillons de cette conversation :\n" + "\n".join(lines)
