"""The fixed processing sequence, run by the Traitement graph (engine/traitement.py): waiting for
Experio's reading, recording each step's outcome, pending questions and the client's answers."""

import base64
import json
import time

from tarik.core.app import ok, question, routine, step_tool, stop, tool, wait
from tarik.core.archives import unpacked
from tarik.core.cabinets import cabinet_db
from tarik.core.config import ENTRY_FILES, FILE_TYPES, READ_WAIT, READING, TEST_MODE
from tarik.core.conversation import chosen_among, own_words, yes_no
from tarik.core.experio import odoo_call, require_invoice
from tarik.core.queries import Q
from tarik.core.words import normalized
from tarik.dossiers.people import check_sender
from tarik.dossiers.setup import ask_banks, set_journal_bank
from tarik.invoices.accounts import set_account
from tarik.invoices.classify import client_side
from tarik.invoices.exercice import settle_exercice
from tarik.invoices.intake import client_kind, import_bank_statement, set_kind
from tarik.invoices.prepaid import settle_prepaid
from tarik.invoices.tiers import choose_tiers
from tarik.invoices.tva import settle_tva
from tarik.invoices.withholding import settle_withholding

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
    deadline = READ_DEADLINES.setdefault(run_id, now + READ_WAIT) if run_id else now + READ_WAIT
    while require_invoice(invoice_id, ["bluejay_state"])["bluejay_state"] in READING:
        if time.monotonic() >= deadline:
            return wait(f"Facture {invoice_id} : Experio la lit encore, elle sera reprise a votre prochain message.")
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
    """Record a step's outcome for this run. ``origin``: the step tool that ran, or what asked (dossier_questions:
    the step is the result's own); ``result``: that step's answer. A question becomes the invoice's pending question;
    an outcome at or after that step clears it."""
    step = origin if origin in STEPS else result.get("step") or origin
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


# The summary's sections, in the order the client reads them: what was done, what is still under way, the
# duplicates left aside, what failed or was stopped, then what they must answer. Empty ones are left out.
SECTIONS = {
    "done": "**Fait**",
    "wait": "**En cours**",
    "duplicate": "**Doublons, non traités**",
    "problem": "**À corriger**",
    "question": "**Questions**",
}
SECTION_OF = {"ok": "done", "wait": "wait", "stop": "problem", "question": "question"}  # any other status: a problem
QUESTION_MARK = "QUESTION : "  # what the agent recognizes a question by (its answer goes to Experio_repondre)


def section_of(step, status):
    return "duplicate" if step == "check_duplicate" and status == "stop" else SECTION_OF.get(status, "problem")


def summary_text(key, step, status, message):
    """One outcome's text in the summary, on invoice ``key`` (int, None or a negative file id)."""
    text = message.removeprefix(TEST_MODE)
    if key and key > 0 and str(key) not in text:  # an error Experio gave without it: which invoice
        text = f"Facture {key} : {text}"
    if status == "question":
        return QUESTION_MARK + text
    if status not in SECTION_OF:
        head, sep, rest = text.partition(" : ")
        problem = f"problème à l'étape {STEPS.get(step, step)}"
        return f"{head} : {problem} : {rest}" if sep and head.lower().startswith("facture") else f"{problem} : {text}"
    return text


def joined(key, texts):
    """An invoice's (or file's) outcomes as one sentence: the first whole, the next ones without the « Facture N : »
    they each start with."""
    head = f"facture {key} : "
    parts = [text if i == 0 or not text.lower().startswith(head) else text[len(head) :] for i, text in enumerate(texts)]
    return "; ".join(part.rstrip(". ") for part in parts) + "."


@tool
def run_summary(run_id: str) -> dict:
    """What happened in one processing run, for the client, in sections: Fait, En cours, Doublons, À corriger,
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
    # In the sequence's order, whatever order the rows were recorded in (invoices of one run are interleaved).
    rows = sorted(rows, key=lambda row: position(row[1]))
    test = any(message.startswith(TEST_MODE) for _, _, _, message, _ in rows)
    outcomes, seen = {}, set()
    for key, step, status, message, unread in rows:
        if step in SETUP_STEPS:
            key = None  # a dossier's setup is one block, its imports in it in order
        if (key, step, message) not in seen:
            seen.add((key, step, message))
            outcomes.setdefault(key, []).append((step, status, message, unread))
    sections = {name: [] for name in SECTIONS}
    for key, found in outcomes.items():
        shown = [outcome for outcome in found if not outcome[3] or outcome is found[-1]]
        if key is None:  # a dossier's setup: each outcome in its own section, on its own line
            for step, status, message, _ in shown:
                sections[section_of(step, status)].append(summary_text(key, step, status, message))
            continue
        where = section_of(*found[-1][:2])
        texts = [summary_text(key, *outcome[:3]) for outcome in shown if section_of(*outcome[:2]) == where]
        sections[where] += texts if where == "question" else [joined(key, texts)]
    parts = []
    for name, lines in sections.items():
        if lines:
            marks = [f"{i}." if name == "question" else "-" for i in range(1, len(lines) + 1)]
            parts.append(
                SECTIONS[name] + "\n" + "\n".join(f"{mark} {line}" for mark, line in zip(marks, lines, strict=True))
            )
    return ok((TEST_MODE + "\n\n" if test else "") + "\n\n".join(parts))


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
    return (
        [
            {
                "invoice_id": i,
                "step": step,
                "question": text,
                "label": f"facture {i}",
                "names": {str(i)},
                "choices": choices,
            }
            for i, step, text, choices in invoices
            if i in drafts
        ]
        + [
            {
                "file_id": f,
                "dossier_id": d,
                "step": "import_bank_statement",
                "question": text,
                "label": f"relevé {name}",
                "file_name": name,
                "names": {normalized(name), normalized(name.rsplit(".", 1)[0])},
                "choices": choices,
            }
            for f, d, text, name, choices in statements
        ]
        + [
            {
                "dossier_id": d,
                "journal_id": j,
                "step": "ask_banks",
                "question": text,
                "label": f"journal {name}",
                "names": {normalized(name), f"journal {j}"},
                "choices": choices,
            }
            for d, j, text, choices in banks
            for name in [journal_name(j)]
            if name  # still without its bank
        ]
    )


@tool
def dossier_questions(session_id: str, dossier_id: int) -> dict:
    """The invoice questions other conversations left waiting on the dossier ``dossier_id``'s drafts, for the client
    who asks about that dossier in a new conversation (the workflow records each one as this run's question:
    record_step then keeps it as this conversation's pending question, so their answer finds it). Never the
    conversation's own, and none while one of its own questions or held drafts waits; a draft deleted or posted
    since, or of another dossier, is left out. Each: {invoice_id,
    step (the one it waits at), message, choices}."""
    with cabinet_db() as db:
        busy = db.execute(
            Q.session_busy,
            (session_id, session_id),
        ).fetchone()
        rows = (
            []
            if busy
            else db.execute(
                Q.pending_of_other_sessions,
                (session_id,),
            ).fetchall()
        )
    ids = [invoice_id for invoice_id, *_ in rows]
    here = (
        set(
            odoo_call(
                "exp.invoice", "search", [("id", "in", ids), ("state", "=", "draft"), ("company_id", "=", dossier_id)]
            )
        )
        if ids and dossier_id
        else set()
    )
    questions = [
        {"invoice_id": i, "step": step, "message": text, "choices": choices}
        for i, step, text, choices in rows
        if i in here
    ]
    return ok(f"{len(questions)} question(s) en attente sur ce dossier.", questions=questions)


def meant(waiting, about):
    """The question the client answers: the only one, or the one the agent read they meant (``about``: an invoice's
    number or a statement's name, as the questions are listed). None when unclear: the client is asked which."""
    if len(waiting) == 1:
        return waiting[0]
    about = normalized(str(about or "")).removeprefix("facture ").removeprefix("relevé ")
    named = [item for item in waiting if about and about in item["names"]]
    return named[0] if len(named) == 1 else None


def apply_answer(item, answer, decision):
    """Apply the client's ``answer`` (their own words) and the agent's ``decision`` (what it read in them) to waiting
    ``item`` (from waiting_on): the result of the call that takes it, with the step it answers and, when it went
    fine, the ``next_step`` the sequence resumes at. A oui / non question the decision doesn't settle is asked
    again."""
    if item["step"] == "import_bank_statement":
        if client_kind(own_words(answer)) == "invoice":  # « ce n'est pas un relevé, c'est une facture »
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
    if item["step"] == "ask_banks":
        bank = chosen_among(decision, item["choices"])
        if not bank:
            return {**ask_banks(item["dossier_id"]), "step": "ask_banks"}  # asked again, the same journal first
        return {**set_journal_bank(item["dossier_id"], item["journal_id"], bank), "step": "ask_banks"}
    apply, following = ANSWERS[item["step"]], ORDER[position(item["step"]) + 1]
    if item["step"] == "link_tiers":
        result = apply(item["invoice_id"], decision, answer, item["choices"])  # the tiers the question proposed
    elif item["step"] == "settle_exercice":  # the client's date, or the exercice chosen
        result = apply(item["invoice_id"], answer, decision)
    elif item["step"] in DECIDED and yes_no(decision) is None and yes_no(answer) is None:
        result = question(f"Réponse non comprise. {item['question']}")
    else:
        # A plain "oui" or "non" is read here when the agent left the decision empty (it chose another tool).
        settled = decision if yes_no(decision) is not None else answer
        result = apply(item["invoice_id"], settled if item["step"] in DECIDED else answer)
    if result["status"] == "ok":
        result["next_step"] = following
    return {**result, "invoice_id": item["invoice_id"], "step": item["step"]}


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
    waiting, fresh = bool(waiting_on(session_id)), new_files(session_id) > 0 or awaiting_split(session_id) > 0
    if waiting:
        route = "both" if fresh else "answer"
    else:
        route = "files" if fresh else mode if mode in ("files", "answer") else "files"
    return ok(f"Aiguillage : {route}.", route=route)


@tool
def answer_pending(session_id: str, answer: str, decision: str = "", about: str = "") -> dict:
    """Apply the client's answer (their own words) to what this conversation waits on: an invoice's question
    (tiers, account, TVA, withholding, prepaid, exercice) or a statement's bank account. ``decision`` is what
    the agent read in that answer: "oui" or "non" for a oui / non question, the id of the tiers chosen among
    those proposed (or the other name or ICE given) for a tiers question, "nouveau" or the open exercice chosen
    for an exercice question, the journal_id chosen for a bank account, else empty. ``about``: with several
    questions waiting, the invoice's number or the statement's name the client answers. ``next_step`` says
    where the sequence resumes; with nothing waiting, the drafts Experio hadn't read yet are resumed. The
    email's sender must have the dossier, as for any work on it."""
    answer = own_words(answer)
    waiting = waiting_on(session_id)
    if not waiting:
        return ok(
            "Aucune question en attente : les factures a reprendre le sont.",
            next_step="waiting_drafts",
            step="answer_pending",
        )
    item = meant(waiting, about)
    if not item:
        questions = "\n".join(f"- {entry['label']} : {entry['question']}" for entry in waiting)
        return question(
            f"Plusieurs questions attendent votre réponse ; precisez la facture ou le relevé :\n{questions}",
            step="answer_pending",
        )
    sender = check_sender(session_id, dossier_id=item.get("dossier_id", 0), invoice_id=item.get("invoice_id", 0))
    if sender["status"] != "ok":  # recorded as check_sender's: the email workflow tells the manager, never the sender
        return {**sender, "step": "check_sender"}
    return apply_answer(item, answer, decision)


@tool
def save_files(session_id: str = "", files: list | None = None) -> dict:
    """Save a conversation's files, the agent's attachments: [{ id, file_name, mime_type, data (base64) }], whose id
    saves each one once however many times the sequence runs. A zip is saved as the files in it Tarik takes
    (tarik.core.archives), each under its archive's id and its own name."""
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


# The stops a client's "reprenez" retries: a dossier setting or an Experio check the cabinet may have fixed since.
# A duplicate, a type or a draft kept unposted (hold_posting) is the client's call, not a setting.
FIXABLE_STOPS = {"assign_account", "check_journal", "configure_cabinet", "post_invoice"}


def resumable(step, status, stopped):
    """True when a draft whose furthest outcome is ``status`` at ``step`` is taken up again: it waited or failed, or
    (``stopped``, the client asked) a fixable setting stopped it."""
    return status in ("wait", "error") or (stopped and status == "stop" and step in FIXABLE_STOPS)


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
    session_id: str, held: bool = False, dossier_id: int = 0, stopped: bool = False, run_id: str = ""
) -> dict:
    """This conversation's drafts to take up again from Experio's reading: the furthest step each reached ended
    waiting on Experio (still reading) or failed (an Experio or model timeout), or nothing was recorded for it (a
    run cut short: the server restarted). Every later step runs again; a question or a stop is not resumed here,
    except what the client asks for. ``held``: the drafts they asked to leave unposted (they now ask to post).
    ``stopped``: the drafts a fixable setting stopped (FIXABLE_STOPS: the cabinet has fixed it since, they ask to
    resume). Both also reach the dossier ``dossier_id``'s drafts from any conversation, since the client may ask in
    a new email thread; never an automatic follow-up's. A draft deleted or posted in Experio since is left alone.
    Experio decides what is a draft: with ``held`` and a dossier, a draft Tarik posted that was put back to draft in
    Experio since is posted again. When they ask to post and nothing is to be, the answer says why (a stop).
    ``run_id``: the run taking them up, which claims them: a draft another run is still working on is left to it."""
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
    mine = furthest_outcomes(rows)
    waiting = [i for i, (step, status) in mine.items() if resumable(step, status, stopped)]
    # shortcut: a draft of a run still going also has no outcome yet; one conversation runs one at a time
    waiting += [invoice_id for (invoice_id,) in drafts if invoice_id not in mine]
    if dossier_id and stopped:
        found = furthest_outcomes(elsewhere).items()
        waiting += [i for i, (step, status) in found if resumable(step, status, True)]
    # The conversation's own held drafts; the dossier's from other conversations only when it has none ("postez-la"
    # in a thread is about that thread's invoice, never every invoice held in the dossier).
    own = [invoice_id for invoice_id, owner in kept if owner == session_id]
    waiting += own
    if not own and dossier_id:
        waiting += [invoice_id for invoice_id, owner in kept if owner != session_id]
        # Posted by Tarik, still a draft in Experio only when someone put it back (the domain below keeps the drafts).
        waiting += [i for i, outcome in furthest_outcomes(reposted).items() if outcome == ("post_invoice", "ok")]
    if waiting:
        domain = [("id", "in", sorted(set(waiting))), ("state", "in", ["pending", "draft"])]
        # What came from any conversation is limited to this dossier's.
        if dossier_id and (held or stopped):
            domain.append(("company_id", "in", [dossier_id]))
        waiting = sorted(odoo_call("exp.invoice", "search", domain))
    if waiting and run_id:
        with cabinet_db() as db:
            waiting = sorted(i for (i,) in db.execute(Q.claim_invoices, (waiting, run_id)).fetchall())
    if held and not waiting:
        return stop(nothing_to_post(session_id, dossier_id), invoices=[])
    return ok(f"{len(waiting)} facture(s) a reprendre.", invoices=waiting)


def nothing_to_post(session_id, dossier_id):
    """Why nothing is posted when the client asks to post: each draft of the dossier and what keeps it from being
    posted (a question waiting for them, a draft Tarik did not import, where Tarik's sequence stopped it)."""
    if not dossier_id:
        return "Aucune facture laissee en brouillon dans cette conversation : rien à comptabiliser."
    drafts = odoo_call(
        "exp.invoice",
        "search_read",
        [("company_id", "=", dossier_id), ("state", "in", ["pending", "draft"])],
        fields=["invoice_number"],
        order="id",
    )
    if not drafts:
        return "Aucun brouillon dans ce dossier : rien à comptabiliser."
    ids = [draft["id"] for draft in drafts]
    with cabinet_db() as db:
        made = {i for (i,) in db.execute(Q.invoices_made_by_tarik, (ids,))}
        asked = dict(db.execute(Q.pending_of_invoices, (ids,)))
        outcomes = db.execute(Q.outcomes_of_invoices, (ids,)).fetchall()
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
        elif draft["id"] in furthest:
            lines.append(f"- {name} : {messages[(draft['id'], furthest[draft['id']][0])]}")
        else:
            lines.append(f"- {name} : encore en cours de traitement")
    return "Rien n'a été comptabilisé. Les brouillons du dossier :\n" + "\n".join(lines)
