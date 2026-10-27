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
from engine.tools.core.conversation import chosen_among, client_messages, own_words, yes_no
from engine.tools.core.experio import _form as experio_form
from engine.tools.core.experio import dry_run, in_dossier, odoo_call, odoo_json, require_invoice
from engine.tools.core.queries import Q
from engine.tools.core.words import normalized, said
from engine.tools.dossiers.people import check_sender
from engine.tools.dossiers.setup import ask_banks, set_journal_bank
from engine.tools.invoices import tpe
from engine.tools.invoices.accounts import confirm_account, set_account
from engine.tools.invoices.classify import client_side
from engine.tools.invoices.exercice import settle_exercice
from engine.tools.invoices.intake import BANK_QUESTION, client_kind, file_named, import_bank_statement, set_kind
from engine.tools.invoices.prepaid import settle_prepaid
from engine.tools.invoices.statement import is_canvas
from engine.tools.invoices.tiers import choose_tiers
from engine.tools.invoices.tva import settle_tva
from engine.tools.invoices.warnings import answer_warnings
from engine.tools.invoices.withholding import settle_withholding

# The sequence, in order: step tool -> what the client reads it as.
STEPS = {
    "answer_pending": "réponse",
    "waiting_drafts": "comptabilisation demandée",
    "resolve_dossier": "dossier",
    "check_sender": "accès au dossier",
    "check_creator": "droit de créer des dossiers",
    "check_manager": "droit de gérer les accès",
    "create_dossier": "création du dossier",
    "update_dossier": "modification du dossier",
    "copy_configuration": "configuration",
    "import_entries": "écritures",
    "finish_setup": "intégration",
    "ask_banks": "banque",
    "setup_status": "état du dossier",
    "import_dossiers": "liste de dossiers",
    "create_person": "accès",
    "change_access": "accès",
    "manage_routine": "routine",
    "archive_dossier": "archivage",
    "files_to_process": "fichiers",
    "create_import_folder": "dossier d'import",
    "identify_document": "identification",
    "import_bank_statement": "relevé bancaire",
    "settle_statement": "pointage du relevé",
    "post_statement": "comptabilisation du relevé",
    "split_invoices": "découpage",
    "create_draft_invoice": "brouillon",
    "wait_until_read": "lecture par Experio",
    "check_invoice": "vérification",
    "correct_invoice": "correction",
    "classify_invoice": "classement",
    "link_tiers": "tiers",
    "check_duplicate": "doublon",
    "assign_account": "compte",
    "settle_tva": "TVA",
    "check_journal": "journal",
    "settle_withholding": "retenue à la source",
    "settle_prepaid": "constatée d'avance",
    "settle_exercice": "exercice",
    "configure_cabinet": "paramétrage",
    "settle_warnings": "avertissements",
    "hold_posting": "mise en attente",
    "post_invoice": "comptabilisation",
    "apply_credit_note": "imputation de l'avoir",
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
    "import_dossiers",
    "create_person",
    "change_access",
    "manage_routine",
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
    run.

    Returns a wait result when the deadline passes first (the draft is resumed at the client's next message; a reading
    stuck over an hour is sent again, resend_if_stuck). Once read: an ok result with ``split_off``, the other drafts
    Experio made from the same scan, for the run to take in hand too; a routine result when there are none."""
    now = time.monotonic()
    for old in [run for run, deadline in READ_DEADLINES.items() if deadline < now - 3600]:
        del READ_DEADLINES[old]
    deadline = now + READ_WAIT
    if run_id:
        deadline = READ_DEADLINES.setdefault(run_id, deadline)
    while require_invoice(invoice_id, ["bluejay_state"])["bluejay_state"] in READING:
        if time.monotonic() >= deadline:
            resent = resend_if_stuck(invoice_id)
            return wait(
                f"Facture {invoice_id} : Experio la lit encore{resent}, elle sera reprise a votre prochain message."
            )
        time.sleep(15)
    others = split_off(invoice_id)
    if others:
        listed = ", ".join(f"n° {other}" for other in others)
        return ok(
            f"Facture {invoice_id} : lue par Experio, qui a tire du même document la ou les factures {listed} "
            "(une par facture du document), traitees à la suite.",
            split_off=others,
        )
    return routine(f"Facture {invoice_id} : lue par Experio.")


STUCK = timedelta(minutes=60)  # Experio's own threshold for a reading gone wrong (get_pending_grouped_invoices_domain)


def resend_if_stuck(invoice_id):
    """Ask Experio to read draft ``invoice_id`` again (its own « Re-send » button, exp.invoice.action_resend) when it is
    still pending and both its creation and its last change are over STUCK ago (a resend changes it: once an hour at
    most).

    Returns the phrase for the client's message: "" when nothing was done; else that it was sent again or, when Experio
    refused (Tarik's Experio user lacks the right), that the cabinet can resend it in Experio."""
    invoice = require_invoice(invoice_id, ["state", "create_date", "write_date", "company_id"])
    now = datetime.now(UTC).replace(tzinfo=None)  # Experio's dates are UTC
    old = [now - datetime.fromisoformat(invoice[field]) > STUCK for field in ("create_date", "write_date")]
    if invoice["state"] != "pending" or not all(old) or dry_run():
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


@step_tool
def hold_posting(invoice_id: int, session_id: str, answer: str = "") -> dict:
    """Stop an invoice at its verified draft unless the client asked to post it.

    ``answer``: "oui" records the client's request to post it, "non" takes that back, empty keeps what was recorded (an
    invoice never asked about is held). The choice is kept per invoice, since later runs don't carry the answer.

    Returns a stop result while the invoice is held, else a routine result: go on to posting."""
    with cabinet_db() as db:
        if answer in ("oui", "non"):
            db.execute(
                Q.upsert_held_choice,
                (invoice_id, session_id, answer == "oui"),
            )
        else:
            db.execute(
                Q.insert_held,
                (invoice_id, session_id),
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
    with cabinet_db() as db:
        db.execute(
            Q.insert_result,
            (*row, choices, result.get("journal_id")),
        )
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
    return ok("enregistre", step=step, status_recorded=status)


# The summary's sections, in the order the client reads them: what was done, what waits (Experio still reading, a
# draft kept until the client says to post it), the duplicates left aside, what failed or was stopped, then what
# they must answer. Empty ones are left out.
SECTIONS = {
    "done": "**Fait**",
    "wait": "**En attente**",
    "duplicate": "**Doublons, non traités**",
    "problem": "**À corriger**",
    "question": "**Questions**",
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


def summary_text(key, step, status, message):
    """Return one outcome's text for the summary, about ``key`` (an invoice id, None for a dossier's setup, or a
    negative file id): « Facture N : » added when an invoice's message lacks its number, QUESTION_MARK before a
    question, the failing step named for an error."""
    text = message.removeprefix(TEST_MODE)
    if key and key > 0 and str(key) not in text:  # an error Experio gave without it: which invoice
        text = f"Facture {key} : {text}"
    if status == "question":
        return QUESTION_MARK + text
    if status not in SECTION_OF:
        return with_problem(step, text)
    return text


def with_problem(step, text):
    """Return ``text`` with the step it failed at: after its « Facture N : » when it has one, else first."""
    problem = f"problème à l'étape {STEPS.get(step, step)}"
    head, sep, rest = text.partition(" : ")
    if sep and head.lower().startswith("facture"):
        return f"{head} : {problem} : {rest}"
    return f"{problem} : {text}"


def joined(key, texts):
    """Return an invoice's (or file's) outcome ``texts`` as one sentence: the first whole, the next ones without the «
    Facture N : » they each start with."""
    head = f"facture {key} : "
    parts = texts[:1]
    for text in texts[1:]:
        if text.lower().startswith(head):
            text = text[len(head) :]
        parts.append(text)
    return "; ".join(part.rstrip(". ") for part in parts) + "."


def listed(texts):
    """Return an invoice's (or file's) outcome ``texts`` as summary lines: one "- " line each when there is one or they
    don't start with a name (« Facture N » or a `file`); else that name on a line, then each outcome indented below."""
    head, sep, _ = texts[0].partition(" : ")
    if len(texts) == 1 or not sep or not (head.lower().startswith("facture") or head.startswith("`")):
        return [f"- {text}" for text in texts]
    named = head.lower() + " : "
    return [f"- {head} :"] + [f"  - {text[len(named) :] if text.lower().startswith(named) else text}" for text in texts]


@tool
def run_summary(run_id: str) -> dict:
    """Return what happened in run ``run_id``, for the client, as an ok result in sections: Fait, En attente, Doublons,
    À corriger, Questions, empty ones left out (NOTHING when the run recorded nothing).

    Each invoice (or file) is in the section of its furthest outcome: when done, its outcomes that say something (what
    was set or changed); otherwise only what it waits on, the problem or the question. Routine outcomes are left out; a
    dossier's setup keeps one line per outcome. Also frees the run's invoices for other runs and, outside test mode,
    notes the run in each invoice's Experio history (note_in_history)."""
    with cabinet_db() as db:
        rows = db.execute(
            Q.run_outcomes,
            (run_id,),
        ).fetchall()
        db.execute(Q.release_invoices, (run_id,))  # the run is over: its invoices are free for another
    if not rows:
        return ok(NOTHING)
    test = any(message.startswith(TEST_MODE) for _, _, _, message, _ in rows)
    if not test and not dry_run():
        note_in_history(rows)
    text = summary_sections(sections_of(outcomes_by_key(rows)))
    return ok((TEST_MODE + "\n\n" if test else "") + text)


def note_in_history(rows):
    """Post what Tarik did on each invoice of ``rows`` (the run's outcomes) as one internal note in the invoice's
    history in Experio (message_post with subtype mail.mt_note, as its own actions are noted), written by Tarik's
    Experio user: what it set or changed, and where it left the invoice. Nothing for an invoice only routine steps
    touched. A refused note is logged, never a reason to fail the run's summary."""
    for key, found in outcomes_by_key(rows).items():
        if not key or key < 0:
            continue
        told = [
            summary_text(key, step, status, message) for step, status, message, is_routine in found if not is_routine
        ]
        if not told:
            continue
        # One line: Experio keeps a text body as one <span>, its line breaks unseen in the history. Its names in « »:
        # the history shows no Markdown, a `name`'s backticks would stay raw.
        body = "Tarik (traitement automatique) : " + CODE.sub(r"« \1 »", joined(key, told))
        try:
            odoo_json(
                "exp.invoice", "message_post", [key], body=body, message_type="comment", subtype_xmlid="mail.mt_note"
            )
        except Exception as error:
            log(f"note_in_history {key}: {error}")


def outcomes_by_key(rows):
    """Return {key: [(step, status, message, routine)]}: each invoice's (or file's) outcomes of ``rows``, in the
    sequence's order, each identical one once; a dossier's setup steps under the key None."""
    # In the sequence's order, whatever order the rows were recorded in (invoices of one run are interleaved).
    rows = sorted(rows, key=lambda row: position(row[1]))
    outcomes, seen = {}, set()
    for key, step, status, message, is_routine in rows:
        if step in SETUP_STEPS:
            key = None  # a dossier's setup is one block, its imports in it in order
        if (key, step, message) not in seen:
            seen.add((key, step, message))
            outcomes.setdefault(key, []).append((step, status, message, is_routine))
    return outcomes


def sections_of(outcomes):
    """Return {section: [lines]}: each invoice (or file) of ``outcomes`` in the section of its last outcome; a dossier's
    setup outcome by outcome. An invoice shows only its latest question, a file each of its questions."""
    sections = {name: [] for name in SECTIONS}
    for key, found in outcomes.items():
        shown = [outcome for outcome in found if not outcome[3] or outcome is found[-1]]
        if key is None:  # a dossier's setup: each outcome in its own section, on its own line
            for step, status, message, _ in shown:
                sections[section_of(step, status, message)].append("- " + summary_text(key, step, status, message))
            continue
        where = section_of(*found[-1][:3])
        texts = [summary_text(key, *outcome[:3]) for outcome in shown if section_of(*outcome[:3]) == where]
        # An invoice waits on one question, its latest (asked again in other words, it is still the one); a file may
        # wait on several different ones (how to cut it, which side): each is asked.
        if where != "question":
            sections[where] += listed(texts)
        elif key > 0:
            sections[where] += texts[-1:]
        else:
            sections[where] += texts
    return sections


def summary_sections(sections):
    """Return the non-empty ``sections`` as the client reads them: a heading, then its lines (questions numbered)."""
    parts = []
    for name, lines in sections.items():
        if not lines:
            continue
        # Questions numbered; the other lines are already listed (« - », an invoice's outcomes indented under it).
        items = [f"{i}. {line}" for i, line in enumerate(lines, start=1)] if name == "question" else lines
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
    those of invoices still in ``drafts``."""
    items = []
    for i, step, text, choices in invoices:
        if i in drafts:
            items.append(
                {
                    "invoice_id": i,
                    "step": step,
                    "question": text,
                    "label": f"facture {i}",
                    "names": {str(i)},
                    "choices": choices,
                }
            )
    return items


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


def meant(waiting, about):
    """Return the ``waiting`` item the client answers: the only one, or the one ``about`` names (an invoice's number or
    a statement's name, as the questions are listed). None when unclear: the client is asked which."""
    if len(waiting) == 1:
        return waiting[0]
    about = normalized(str(about or "")).removeprefix("facture ").removeprefix("relevé ")
    named = [item for item in waiting if about and about in item["names"]]
    return named[0] if len(named) == 1 else None


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
    if item["step"] == "ask_banks":
        bank = chosen_among(decision, item["choices"])
        if not bank:
            return {**ask_banks(item["dossier_id"]), "step": "ask_banks"}  # asked again, the same journal first
        return {**set_journal_bank(item["dossier_id"], item["journal_id"], bank), "step": "ask_banks"}
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
    if step == "assign_account" and len(proposed) == 1:  # an account to confirm (set_account, confirm)
        settled = yes_no(decision) if yes_no(decision) is not None else yes_no(answer)
        if settled is not None:
            return confirm_account(item["invoice_id"], int(next(iter(proposed))), settled)
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
    if not waiting:
        # Nothing asked: an account given here still goes to the invoice it names (none answered about).
        fixes, notes = verified_corrections(session_id, answer, corrections or [], None)
        result = ok(
            "Aucune question en attente : les factures a reprendre le sont." + "".join(f" {note}" for note in notes),
            next_step="waiting_drafts",
            step="answer_pending",
        )
        if fixes:
            result["corrections"] = as_pairs(fixes)
        return result
    item = meant(waiting, about)
    if not item:
        questions = "\n".join(f"- {entry['label']} : {entry['question']}" for entry in waiting)
        return question(
            f"Plusieurs questions attendent votre réponse ; précisez la facture ou le relevé :\n{questions}",
            step="answer_pending",
        )
    sender = check_sender(session_id, dossier_id=item.get("dossier_id", 0), invoice_id=item.get("invoice_id", 0))
    if sender["status"] != "ok":  # recorded as check_sender's: the email workflow tells the manager, never the sender
        return {**sender, "step": "check_sender"}
    result = apply_answer(item, answer, decision)
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
    left = f" Ignores : {' ; '.join(skipped)}." if skipped else ""
    return ok(f"{len(saved)} fichier(s) enregistre(s).{left}", files=saved)


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
def files_to_process(session_id: str, entries: bool = False, asked: bool = False) -> dict:
    """Return the conversation's files not processed yet (``files``: their ids): its invoices and statements, or with
    ``entries`` its FEC and grand livre files. Picked here, not by the agent, which never sees the files' numbers.

    ``asked``: the client's message asked for files to be processed; when none is found, the result reports to the
    client that the attachment did not arrive (NO_FILE)."""
    with cabinet_db() as db:
        rows = db.execute(
            Q.files_to_process,
            (session_id,),
        ).fetchall()
    own = [i for i, name in rows if is_entries(i, name) == entries]
    if asked and not own:
        return ok(NO_FILE, files=[], tell=True)
    return ok(f"{len(own)} fichier(s) a traiter.", files=own)


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
    sided = stopped and bool(client_side(invoice_type, " ".join(client_messages(session_id, ""))))  # their words
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
    others = set()
    if dossier_id and stopped:
        others |= {i for i, (step, status) in furthest_outcomes(elsewhere).items() if resumable(step, status, True)}
    if dossier_id and held:
        others |= {invoice_id for invoice_id, owner in kept if owner != session_id}
        # Posted by Tarik, still a draft in Experio only when someone put it back (the domain below keeps the drafts).
        others |= {i for i, outcome in furthest_outcomes(reposted).items() if outcome == ("post_invoice", "ok")}
    others -= set(waiting) | set(mine)
    if others:  # still drafts of this dossier, whatever conversation (or dossier) left them
        domain = [("id", "in", sorted(others)), ("state", "in", ["pending", "draft"]), ("company_id", "=", dossier_id)]
        others = set(odoo_call("exp.invoice", "search", domain))
    asked = asked_among(others, dossier_id, client_message) if others else []
    waiting += asked
    # The dossier's drafts from Experio's own invoicing (Facturation), posted when the client asks for them: never
    # read nor checked by Tarik (no scan, accounts per line), they start at the dossier's settings.
    billed = facturation_drafts(dossier_id, client_message) if held and dossier_id else []
    elsewhere_left = len(others) - len(asked)
    if waiting:
        domain = [("id", "in", sorted(set(waiting))), ("state", "in", ["pending", "draft"])]
        # What came from any conversation is limited to this dossier's.
        if dossier_id and (held or stopped):
            domain.append(("company_id", "in", [dossier_id]))
        waiting = sorted(odoo_call("exp.invoice", "search", domain))
    waiting = transit_last(waiting)
    billed = [i for i in billed if i not in waiting]
    if (waiting or billed) and run_id:
        with cabinet_db() as db:
            claimed = {i for (i,) in db.execute(Q.claim_invoices, (sorted({*waiting, *billed}), run_id)).fetchall()}
        waiting, billed = [i for i in waiting if i in claimed], sorted(set(billed) & claimed)  # transit last kept
    corrections = drafts_corrected(session_id, client_message, compte)
    if corrections:
        return ok(
            f"{len(waiting)} facture(s) a reprendre.", invoices=waiting, corrections=corrections, facturation=billed
        )
    statements = statements_to_resume(session_id, held)
    if billed or statements:
        count = len(waiting) + len(billed)
        return ok(
            f"{count} facture(s) et {len(statements)} relevé(s) a reprendre.",
            invoices=waiting,
            facturation=billed,
            statements=statements,
        )
    left = left_in_dossier(dossier_id, {*waiting, *billed}) if held and asks_whole(client_message) else ""
    if left and waiting:
        return ok(left.strip(), invoices=waiting, tell=True)  # told in the reply, with what is posted
    hint = elsewhere_hint(elsewhere_left, client_message, held) if not waiting else ""
    if held and not waiting:
        if run_id and worked_on(run_id):  # this run's other branch took them (« comptabilisez » goes both ways)
            return routine("Rien d'autre à comptabiliser.", invoices=[])
        return stop(nothing_to_post(session_id, dossier_id) + hint + left, invoices=[])
    if hint:
        return ok("Rien à reprendre dans cette conversation." + hint, invoices=[], tell=True)
    return ok(f"{len(waiting)} facture(s) a reprendre.", invoices=waiting)


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


def transit_last(ids):
    """Return ``ids`` with the import folders' transit invoices last: a transit invoice takes the folder's invoices it
    paid only once those are posted (Experio's _onchange_autofill_disbursement_invoices links posted or exported ones
    only, exp_invoice.py:2698)."""
    if not ids:
        return ids
    transit = set(
        odoo_call("exp.invoice", "search", [("id", "in", list(ids)), ("import_invoice_type", "=", "transit")])
    )
    return sorted(ids, key=lambda i: i in transit)


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
    return (
        f" {count} brouillon(s) d'autres conversations attendent dans ce dossier : écrivez "
        f"« {whole_example(client_message, held)} », ou nommez-les (numéro de facture ou fournisseur), pour les "
        "reprendre aussi."
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


def asks_whole(client_message):
    """True when the client's own words ask for the whole dossier (« toutes les factures du dossier »)."""
    return bool(WHOLE.search(normalized(own_words(client_message))))


def left_in_dossier(dossier_id, taken):
    """When the client asks for the whole dossier: return the sentence reporting how many of its drafts stay drafts,
    except those ``taken``, and why: a question waiting in their conversation, stopped by a step, or not imported by
    Tarik. "" when none stay or no ``dossier_id``."""
    if not dossier_id:
        return ""
    domain = [("id", "not in", sorted(taken)), ("company_id", "=", dossier_id), ("state", "in", ["pending", "draft"])]
    left = odoo_call("exp.invoice", "search", domain)
    if not left:
        return ""
    with cabinet_db() as db:
        asked = {i for i, _ in db.execute(Q.pending_of_invoices, (left,))}
        made = {i for (i,) in db.execute(Q.invoices_made_by_tarik, (left,))}
    counts = [
        (len(asked), "attend(ent) une réponse à sa question dans sa conversation"),
        (len(made - asked), "arrêtée(s) par une étape, à corriger"),
        (len(set(left) - made - asked), "pas importée(s) par Tarik, à comptabiliser dans Experio"),
    ]
    said = ", ".join(f"{count} {words}" for count, words in counts if count)
    return f" Restent en brouillon dans ce dossier : {said}."


def asked_among(ids, dossier_id, client_message):
    """Return, of the dossier's drafts ``ids`` other conversations left, those the client's own words ask for: all of
    them (« toutes les factures du dossier »), else those they name by Tarik's draft number, their invoice number or
    their supplier; [] otherwise. Never a model's reading: what was left elsewhere is the client's call."""
    words = normalized(own_words(client_message))
    if not words:
        return []
    if WHOLE.search(words):
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
        return "Aucune facture laissée en brouillon dans cette conversation : rien à comptabiliser."
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
        name = f"facture {draft['id']} ({draft['invoice_number'] or 'sans numéro'})"
        if draft["id"] in asked:
            lines.append(f"- {name} : attend votre réponse à sa question : {asked[draft['id']]}")
        elif draft["id"] not in made:
            lines.append(f"- {name} : pas importée par Tarik, à comptabiliser dans Experio")
        elif draft["id"] in working and furthest.get(draft["id"], ("", "ok"))[1] in ("ok", "wait"):
            # Another run is at it, not stopped yet: its own reply will tell how it ends.
            lines.append(f"- {name} : en cours de traitement, je vous écris dès qu'elle est prête")
        elif draft["id"] in furthest:
            lines.append(f"- {name} : {messages[(draft['id'], furthest[draft['id']][0])]}")
        else:
            lines.append(f"- {name} : encore en cours de traitement")
    return "Rien n'a été comptabilisé. Les brouillons de cette conversation :\n" + "\n".join(lines)
