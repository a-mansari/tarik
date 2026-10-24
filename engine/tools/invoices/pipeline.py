"""The fixed processing sequence, run by the Traitement graph (engine/traitement.py): waiting for
Experio's reading, recording each step's outcome, pending questions and the client's answers."""

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
from engine.tools.invoices.accounts import confirm_account, set_account
from engine.tools.invoices.classify import client_side
from engine.tools.invoices.exercice import settle_exercice
from engine.tools.invoices.intake import client_kind, file_named, import_bank_statement, set_kind
from engine.tools.invoices.prepaid import settle_prepaid
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
    "create_dossier": "création du dossier",
    "update_dossier": "modification du dossier",
    "copy_configuration": "configuration",
    "import_entries": "écritures",
    "finish_setup": "intégration",
    "ask_banks": "banque",
    "setup_status": "état du dossier",
    "import_dossiers": "liste de dossiers",
    "create_person": "accès",
    "archive_dossier": "archivage",
    "files_to_process": "fichiers",
    "identify_document": "identification",
    "import_bank_statement": "relevé bancaire",
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
    """Where ``step`` (a step tool's name) stands in the sequence; after every step when it is not one."""
    return ORDER.index(step) if step in ORDER else len(ORDER)


READ_DEADLINES = {}  # run id -> when that run stops waiting on Experio's reading (time.monotonic)


@step_tool
def wait_until_read(invoice_id: int, run_id: str = "") -> dict:
    """Wait until Experio has finished reading a new draft: READ_WAIT seconds at most for all the drafts of run
    ``run_id`` together, so one stuck reading never holds the run; a draft still being read is resumed later. Once
    read, the drafts Experio made from the same document besides this one (``split_off``), for the run to take in
    hand too."""
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
    """Have Experio read a draft again (its own Re-send, action_resend) when it has waited on its reading for over
    STUCK and nothing touched it for as long (a resend touches it: once an hour at most): a phrase for the client."""
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
    """The drafts Experio made from the same scan as Tarik's draft ``invoice_id`` (one per bill of a document holding
    several: water and electricity), no step has taken in hand yet: their ids. None for a draft Tarik did not make
    itself, so a split-off draft never reports the others again."""
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
    """Stop an invoice at its verified draft unless the client explicitly asked to post it (``answer`` "oui"); "non"
    takes that back. The choice is kept per invoice, since later runs don't carry the answer."""
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
    """Record a step's outcome for this run. ``origin``: the step tool that ran, or what asked (the step is then
    the result's own); ``result``: that step's answer. A question becomes the invoice's pending question;
    an outcome at or after that step clears it."""
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
QUESTION_MARK = "QUESTION : "  # what the agent recognizes a question by (its answer goes to Experio_repondre)


def section_of(step, status, message=""):
    if status == "stop" and step == "check_duplicate":  # record_step records every duplicate's stop at this step
        return "duplicate"
    if step == "hold_posting" and status == "stop":  # verified, left as a draft until the client says to post it
        return "wait"
    if step == "waiting_drafts" and status in ("ok", "stop"):  # what waits: nothing to post, drafts elsewhere
        return "wait"
    return SECTION_OF.get(status, "problem")


def summary_text(key, step, status, message):
    """One outcome's text in the summary, on invoice ``key`` (int, None or a negative file id)."""
    text = message.removeprefix(TEST_MODE)
    if key and key > 0 and str(key) not in text:  # an error Experio gave without it: which invoice
        text = f"Facture {key} : {text}"
    if status == "question":
        return QUESTION_MARK + text
    if status not in SECTION_OF:
        return with_problem(step, text)
    return text


def with_problem(step, text):
    """``text`` saying at which step it failed: after its « Facture N : » when it has one, else first."""
    problem = f"problème à l'étape {STEPS.get(step, step)}"
    head, sep, rest = text.partition(" : ")
    if sep and head.lower().startswith("facture"):
        return f"{head} : {problem} : {rest}"
    return f"{problem} : {text}"


def joined(key, texts):
    """An invoice's (or file's) outcomes as one sentence: the first whole, the next ones without the « Facture N : »
    they each start with."""
    head = f"facture {key} : "
    parts = texts[:1]
    for text in texts[1:]:
        if text.lower().startswith(head):
            text = text[len(head) :]
        parts.append(text)
    return "; ".join(part.rstrip(". ") for part in parts) + "."


@tool
def run_summary(run_id: str) -> dict:
    """What happened in one processing run, for the client, in sections: Fait, En attente, Doublons, À corriger,
    Questions (empty ones left out). Each invoice (or file) is one line, in the section of where it stands (its
    furthest outcome): its outcomes that say something (what was set or changed) joined when it is done, else only
    what it waits on, the problem or the question. Routine ones (nothing to do, the expected setting) are left out; a
    dossier's setup keeps one line per outcome."""
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
    """What Tarik did on each invoice in this run, as one internal note in the invoice's history in Experio (its
    message_post, the way its own actions are noted), written by Tarik's Experio user: what it set or changed, and
    where it left the invoice. Nothing for an invoice only routine steps touched. A note refused is logged, never a
    reason to fail the run's summary."""
    for key, found in outcomes_by_key(rows).items():
        if not key or key < 0:
            continue
        told = [
            summary_text(key, step, status, message) for step, status, message, is_routine in found if not is_routine
        ]
        if not told:
            continue
        # One line: Experio keeps a text body as one <span>, its line breaks unseen in the history.
        body = "Tarik (traitement automatique) : " + joined(key, told)
        try:
            odoo_json(
                "exp.invoice", "message_post", [key], body=body, message_type="comment", subtype_xmlid="mail.mt_note"
            )
        except Exception as error:
            log(f"note_in_history {key}: {error}")


def outcomes_by_key(rows):
    """{key: [(step, status, message, routine)]}: each invoice's (or file's) outcomes of ``rows``, in the sequence's
    order, each said once; a dossier's setup under the key None."""
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
    """{section: [lines]}: each invoice (or file) of ``outcomes`` as a line in the section of where it stands."""
    sections = {name: [] for name in SECTIONS}
    for key, found in outcomes.items():
        shown = [outcome for outcome in found if not outcome[3] or outcome is found[-1]]
        if key is None:  # a dossier's setup: each outcome in its own section, on its own line
            for step, status, message, _ in shown:
                sections[section_of(step, status, message)].append(summary_text(key, step, status, message))
            continue
        where = section_of(*found[-1][:3])
        texts = [summary_text(key, *outcome[:3]) for outcome in shown if section_of(*outcome[:3]) == where]
        # An invoice waits on one question, its latest (asked again in other words, it is still the one); a file may
        # wait on several different ones (how to cut it, which side): each is asked.
        if where != "question":
            sections[where].append(joined(key, texts))
        elif key > 0:
            sections[where] += texts[-1:]
        else:
            sections[where] += texts
    return sections


def summary_sections(sections):
    """The non-empty ``sections`` as the client reads them: a heading, then its lines (questions numbered)."""
    parts = []
    for name, lines in sections.items():
        if not lines:
            continue
        if name == "question":
            items = [f"{i}. {line}" for i, line in enumerate(lines, start=1)]
        else:
            items = [f"- {line}" for line in lines]
        parts.append(SECTIONS[name] + "\n" + "\n".join(items))
    return "\n\n".join(parts)


def journal_name(journal_id):
    """The bank journal's name while it still lacks its bank, else None."""
    found = odoo_call("exp.journal", "search_read", [("id", "=", journal_id), ("bank_id", "=", False)], fields=["name"])
    return found[0]["name"] if found else None


def waiting_on(session_id):
    """What this conversation waits on the client for, oldest first: each draft invoice's question, each bank
    statement whose last import asked for its bank account, and each dossier whose bank journal was asked its
    bank. A question on an invoice deleted or posted since, or on a journal given its bank since, is dropped:
    nothing can answer it any more. Each item: {invoice_id, or file_id and dossier_id, or dossier_id and
    journal_id; step, question, label (how the questions are listed: "facture 12", "releve mars.pdf", "journal
    BQ"), names (what ``about`` may say), choices ({id: name} the question proposed, if any)}."""
    with cabinet_db() as db:
        invoices = db.execute(
            Q.pending_of_session,
            (session_id,),
        ).fetchall()
        statements = db.execute(Q.statements_asked, (session_id,)).fetchall()
        banks = db.execute(Q.banks_asked, (session_id,)).fetchall()
        ids = [i for i, _, _, _ in invoices]
        drafts = set(odoo_call("exp.invoice", "search", [("id", "in", ids), ("state", "=", "draft")])) if ids else set()
        gone = [i for i in ids if i not in drafts]
        if gone:
            db.execute(Q.delete_pending_of_invoices, (gone,))
    return invoice_questions(invoices, drafts) + statement_questions(statements) + bank_questions(banks)


def invoice_questions(invoices, drafts):
    """waiting_on's items for the invoices' pending questions, those still ``drafts`` only."""
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
    """waiting_on's items for the bank statements asked their bank account."""
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


def bank_questions(banks):
    """waiting_on's items for the bank journals asked their bank, those still without it only."""
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
    """The question the client answers: the only one, or the one the agent read they meant (``about``: an invoice's
    number or a statement's name, as the questions are listed). None when unclear: the client is asked which."""
    if len(waiting) == 1:
        return waiting[0]
    about = normalized(str(about or "")).removeprefix("facture ").removeprefix("relevé ")
    named = [item for item in waiting if about and about in item["names"]]
    return named[0] if len(named) == 1 else None


def about_this_file(item, answer):
    """True when the client's ``answer`` is about the statement ``item`` asks for: it names that file, or no other
    file of its conversation (« douane.pdf n'est pas un relevé » is not about `releve.pdf`)."""
    if file_named(item["file_name"], answer):
        return True
    with cabinet_db() as db:
        names = [name for (name,) in db.execute(Q.other_file_names, (item["file_id"], item["file_id"]))]
    return not any(file_named(name, answer) for name in names)


def apply_answer(item, answer, decision):
    """Apply the client's ``answer`` (their own words) and the agent's ``decision`` (what it read in them) to waiting
    ``item`` (from waiting_on): the result of the call that takes it, with the step it answers and, when it went
    fine, the ``next_step`` the sequence resumes at. A oui / non question the decision doesn't settle is asked
    again."""
    if item["step"] == "import_bank_statement":
        return answer_statement(item, answer, decision)
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
    """apply_answer for a statement asked its bank account: the file is an invoice after all when the client says
    so, else the statement is imported into the account chosen (or the question asked again)."""
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
    imported = import_bank_statement(item["file_id"], item["dossier_id"], chosen)
    return {**imported, "step": "import_bank_statement"}


def answer_invoice(item, answer, decision):
    """apply_answer for an invoice's question: the result of the call that takes the answer (ANSWERS)."""
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
    """How many files (int) came in this conversation since its last run, for the processing sequence."""
    with cabinet_db() as db:
        names = [name for (name,) in db.execute(Q.new_files_of_session, (session_id, session_id))]
    return sum(not name.lower().endswith(ENTRY_FILES) for name in names)


def awaiting_split(session_id):
    """How many PDFs of the conversation wait for the client to say how to cut them (split_invoices asks): their
    answer goes to the files, whatever else waits."""
    with cabinet_db() as db:
        return db.execute(Q.pdfs_awaiting_split, (session_id,)).fetchone()[0]


@tool
def route_message(session_id: str, mode: str = "files") -> dict:
    """Where a message of this conversation goes in the processing workflow, from what the database knows, not
    from the tool the agent happened to call (``mode``: files, answer or resume). ``route``: answer when the
    conversation waits on the client and no file came since its last run; files when files came and nothing
    waits; both, the answer and the files, when both; else ``mode``, files or answer. A follow-up (resume)
    stays one."""
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
    """Apply the client's answer (their own words) to what this conversation waits on: an invoice's question
    (tiers, account, TVA, withholding, prepaid, exercice) or a statement's bank account. ``decision`` is what
    the agent read in that answer: "oui" or "non" for a oui / non question, the id of the tiers chosen among
    those proposed (or the other name or ICE given) for a tiers question, "nouveau" or the open exercice chosen
    for an exercice question, the journal_id chosen for a bank account, else empty. ``about``: with several
    questions waiting, the invoice's number or the statement's name the client answers. ``next_step`` says
    where the sequence resumes; with nothing waiting, the drafts Experio hadn't read yet are resumed. The
    email's sender must have the dossier, as for any work on it. ``corrections``: the accounts the client gives
    invoices in the same message, as the agent read them ([{facture, compte}], their words), each checked against
    those words and the conversation's drafts (verified_corrections)."""
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
    """{invoice: account} as [[invoice, account]], by invoice: a list, kept as it is through the workflow's state."""
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
    """The accounts the agent read in the client's answer ([{facture, compte}]), checked: ({invoice: account}, [what
    the client is told of those left out]). Each must be in the client's own words; its invoice is the one they
    answer about when it names none (« mettez-la »), else the one draft of the conversation it names (Tarik's
    number, the invoice's number, its supplier), on a dossier the sender has. Never a model's guess."""
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
    """{draft id: [names a client may call it by]}: Tarik's number, the invoice's own number, its supplier. The
    conversation's drafts: made from its files, asked about in it, or worked on in it."""
    with cabinet_db() as db:
        ids = {i for (i,) in db.execute(Q.drafts_of_session, (session_id,))}
        ids |= {row[0] for row in db.execute(Q.pending_of_session, (session_id,))}
        ids |= {row[0] for row in db.execute(Q.invoice_outcomes_of_session, (session_id,))}
    ids = sorted(ids)
    domain = [("id", "in", ids)]  # search_read: a draft deleted in Experio since is left out, never an error
    drafts = odoo_call("exp.invoice", "search_read", domain, fields=["invoice_number", "partner_id"]) if ids else []
    return {draft["id"]: names_of(draft) for draft in drafts}


def names_of(draft):
    """The names a client may call ``draft`` by (of three letters or more): Tarik's number, the invoice's own
    number, its supplier's."""
    names = [str(draft["id"]), normalized(draft["invoice_number"] or "")]
    if draft["partner_id"]:
        names += supplier_names(draft["partner_id"][1])
    return [name for name in names if len(name) >= 3]


def named_drafts(words, names):
    """The drafts ``words`` (normalized) name."""
    return sorted(i for i, found in names.items() if any(said(name, words) for name in found))


@tool
def save_files(session_id: str = "", files: list | None = None) -> dict:
    """Save a conversation's files, the agent's attachments: [{ id, file_name, mime_type, data (base64) }], whose id
    saves each one once however many times the sequence runs. A zip is saved as the files in it Tarik takes
    (engine.tools.core.archives), each under its archive's id and its own name."""
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


@tool
def files_to_process(session_id: str, entries: bool = False, asked: bool = False) -> dict:
    """The conversation's files not processed yet: its invoices and statements, or with
    ``entries`` its FEC and grand livre files. Picked here, not by the agent: it never
    sees the files' numbers, so it can't show them to the client. ``asked``: the client's
    message asked for them, so none is said to the client (an attachment lost on the way)."""
    with cabinet_db() as db:
        rows = db.execute(
            Q.files_to_process,
            (session_id,),
        ).fetchall()
    own = [i for i, name in rows if name.lower().endswith(ENTRY_FILES) == entries]
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
    """True when a draft whose furthest outcome is ``status`` at ``step`` is taken up again: it waited or failed, or
    (``stopped``, the client asked) a fixable setting stopped it, or (``sided``: the client now says which side it
    is) its classification did."""
    if status in ("wait", "error"):
        return True
    return stopped and status == "stop" and (step in FIXABLE_STOPS or (sided and step == "classify_invoice"))


def latest_runs(rows):
    """Of ``rows`` (invoice_id, step, status, run_id, in the order recorded), each invoice's outcomes of the latest run
    that recorded any for it, as (invoice_id, step, status)."""
    last = {invoice_id: run_id for invoice_id, _, _, run_id in rows}
    return [(invoice_id, step, status) for invoice_id, step, status, run_id in rows if last[invoice_id] == run_id]


def furthest_outcomes(rows):
    """{invoice_id: (step, status)}: the furthest step each invoice of ``rows`` (invoice_id, step, status) reached, by
    the sequence's order, not the rows'."""
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
    """This conversation's drafts to take up again from Experio's reading: the furthest step each reached ended
    waiting on Experio (still reading) or failed (an Experio or model timeout), or nothing was recorded for it (a
    run cut short: the server restarted). Every later step runs again; a question or a stop is not resumed here,
    except what the client asks for. ``held``: the drafts they asked to leave unposted (they now ask to post).
    ``stopped``: the drafts a fixable setting stopped (FIXABLE_STOPS: the cabinet has fixed it since, they ask to
    resume). Both reach the dossier ``dossier_id``'s drafts from other conversations only when the client's own
    words (``client_message``) ask for the whole dossier or name those drafts (asked_among); never an automatic
    follow-up's. A draft deleted or posted in Experio since is left alone.
    Experio decides what is a draft: with ``held`` and a dossier, a draft Tarik posted that was put back to draft in
    Experio since is posted again. When they ask to post and nothing is to be, the answer says why (a stop).
    ``run_id``: the run taking them up, which claims them: a draft another run is still working on is left to it.
    ``invoice_type``: the side the client now gives (achat, vente): a draft its classification stopped (not an
    invoice, the other side) is classified again with it. ``compte``: an account the agent read with no file sent: a
    correction for the one draft of the conversation the client's words name (``corrections``), to be confirmed."""
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
    waiting += [invoice_id for invoice_id, owner in kept if owner == session_id]
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
    billed = [i for i in billed if i not in waiting]
    if (waiting or billed) and run_id:
        with cabinet_db() as db:
            claimed = {i for (i,) in db.execute(Q.claim_invoices, (sorted({*waiting, *billed}), run_id)).fetchall()}
        waiting, billed = sorted(set(waiting) & claimed), sorted(set(billed) & claimed)
    corrections = drafts_corrected(session_id, client_message, compte)
    if corrections:
        return ok(
            f"{len(waiting)} facture(s) a reprendre.", invoices=waiting, corrections=corrections, facturation=billed
        )
    if billed:
        return ok(f"{len(waiting) + len(billed)} facture(s) a reprendre.", invoices=waiting, facturation=billed)
    hint = elsewhere_hint(elsewhere_left, client_message, held) if not waiting else ""
    if held and not waiting:
        if run_id and worked_on(run_id):  # this run's other branch took them (« comptabilisez » goes both ways)
            return routine("Rien d'autre à comptabiliser.", invoices=[])
        return stop(nothing_to_post(session_id, dossier_id) + hint, invoices=[])
    if hint:
        return ok("Rien à reprendre dans cette conversation." + hint, invoices=[], tell=True)
    return ok(f"{len(waiting)} facture(s) a reprendre.", invoices=waiting)


def facturation_drafts(dossier_id, client_message):
    """The dossier's drafts made by Experio's Facturation (gst_invoice_id) the client's own words ask for: the whole
    dossier, or named (asked_among); none when the cabinet's Experio has no Facturation."""
    if not normalized(own_words(client_message)) or "gst_invoice_id" not in experio_form()["types"]:
        return []
    domain = [("company_id", "=", dossier_id), ("state", "=", "draft"), ("gst_invoice_id", "!=", False)]
    found = odoo_call("exp.invoice", "search", domain)
    return asked_among(found, dossier_id, client_message) if found else []


def worked_on(run_id):
    """Whether run ``run_id`` already took any invoice in hand."""
    with cabinet_db() as db:
        return bool(db.execute(Q.run_worked_on_invoices, (run_id,)).fetchone())


def elsewhere_hint(count, client_message, held):
    """What the client is told of the ``count`` drafts other conversations left in the dossier: how to ask for them
    too. Empty when there are none."""
    if not count:
        return ""
    return (
        f" {count} brouillon(s) d'autres conversations attendent dans ce dossier : écrivez "
        f"« {whole_example(client_message, held)} », ou nommez-les (numéro de facture ou fournisseur), pour les "
        "reprendre aussi."
    )


def drafts_corrected(session_id, client_message, compte):
    """An account the agent read with no file sent (processing tool's compte), as a correction: [[invoice, account]]
    for the one draft of the conversation the client's own words name, checked as any correction; else []."""
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
    """« <their verb> toutes les factures du dossier »: the verb the client wrote (comptabilisez, reprenez…), else
    the one their request means."""
    words = normalized(own_words(client_message))
    verb = next((written for stem, written in VERBS.items() if re.search(rf"(?<![a-z]){stem}", words)), None)
    return f"{verb or ('comptabilisez' if held else 'reprenez')} toutes les factures du dossier"


# Words too common in suppliers' names to name one on their own (normalized).
GENERIC = {"maroc", "societe", "ste", "sarl", "services", "service", "international", "group", "groupe", "station"}


def supplier_names(name):
    """How a client may name supplier ``name``: whole, or by its first distinctive word (« VALADE » for « GR VALADE
    LOGISTIQUE »)."""
    whole = normalized(name.split(" - ")[0])
    first = next((word for word in whole.split() if len(word) >= 4 and word not in GENERIC), "")
    return [whole, first]


def asked_among(ids, dossier_id, client_message):
    """Of the dossier's drafts ``ids`` other conversations left, those the client's own words ask for: all of them
    (« toutes les factures du dossier »), else those they name by Tarik's draft number, their invoice number or
    their supplier; none otherwise. Never a model's reading: what was left elsewhere is the client's call."""
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
    """Why nothing is posted when the client asks to post: each draft of this conversation and what keeps it from
    being posted (a question waiting for them, where Tarik's sequence stopped it). Other conversations' drafts are
    only counted (waiting_drafts' hint), never listed here."""
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
        working = {i for (i,) in db.execute(Q.invoices_worked_on, (ids,))}
    rows = [(i, step, status) for i, step, status, _ in outcomes]
    messages = {(i, step): message for i, step, _, message in outcomes}
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
