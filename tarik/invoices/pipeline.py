"""The fixed processing sequence, run by the Traitement graph (engine/traitement.py): waiting for
Experio's reading, recording each step's outcome, pending questions and the client's answers."""

import base64
import json
import time

from tarik.core.app import CRASHED, ok, question, routine, step_tool, stop, tool, wait
from tarik.core.cabinets import cabinet_db
from tarik.core.config import ENTRY_FILES, READ_WAIT, READING, TEST_MODE
from tarik.core.conversation import chosen_among, own_words, yes_no
from tarik.core.experio import odoo_call, require_invoice
from tarik.core.words import normalized
from tarik.dossiers.people import check_sender
from tarik.dossiers.setup import ask_banks, set_journal_bank
from tarik.invoices.accounts import set_account
from tarik.invoices.exercice import settle_exercice
from tarik.invoices.intake import import_bank_statement
from tarik.invoices.prepaid import settle_prepaid
from tarik.invoices.tiers import choose_tiers
from tarik.invoices.tva import settle_tva
from tarik.invoices.withholding import settle_withholding

# The sequence, in order: step tool -> what the client reads it as.
STEPS = {
    "answer_pending": "reponse",
    "resolve_dossier": "dossier",
    "check_sender": "expediteur",
    "create_dossier": "creation du dossier",
    "update_dossier": "modification du dossier",
    "copy_configuration": "configuration",
    "import_entries": "ecritures",
    "finish_setup": "integration",
    "ask_banks": "banque",
    "setup_status": "etat du dossier",
    "import_dossiers": "liste de dossiers",
    "create_person": "acces",
    "archive_dossier": "archivage",
    "identify_document": "identification",
    "import_bank_statement": "releve bancaire",
    "split_invoices": "decoupage",
    "create_draft_invoice": "brouillon",
    "wait_until_read": "lecture par Experio",
    "check_invoice": "verification",
    "correct_invoice": "correction",
    "classify_invoice": "classement",
    "link_tiers": "tiers",
    "check_duplicate": "doublon",
    "assign_account": "compte",
    "settle_tva": "TVA",
    "check_journal": "journal",
    "settle_withholding": "retenue a la source",
    "settle_prepaid": "constatee d'avance",
    "settle_exercice": "exercice",
    "configure_cabinet": "parametrage",
    "hold_posting": "mise en attente",
    "post_invoice": "comptabilisation",
}
ORDER = list(STEPS)
INVOICE_CHAIN = ORDER[ORDER.index("create_draft_invoice") :]  # an invoice's own steps, from its draft (Traitement)
NOTHING = "Rien n'a ete traite."  # a run's summary when it did nothing (the Emails follow-up leaves it out)
SETUP_STEPS = {
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
    ``run_id`` together, so one stuck reading never holds the run; a draft still being read is resumed later."""
    now = time.monotonic()
    for old in [run for run, deadline in READ_DEADLINES.items() if deadline < now - 3600]:
        del READ_DEADLINES[old]
    deadline = READ_DEADLINES.setdefault(run_id, now + READ_WAIT) if run_id else now + READ_WAIT
    while require_invoice(invoice_id, ["bluejay_state"])["bluejay_state"] in READING:
        if time.monotonic() >= deadline:
            return wait(f"Facture {invoice_id} : Experio la lit encore, elle sera reprise a votre prochain message.")
        time.sleep(15)
    return routine(f"Facture {invoice_id} : lue par Experio.")


@step_tool
def hold_posting(invoice_id: int, session_id: str, answer: str = "") -> dict:
    """Stop an invoice at its verified draft unless the client explicitly asked to post it (``answer`` "oui"); "non"
    takes that back. The choice is kept per invoice, since later runs don't carry the answer."""
    with cabinet_db() as db:
        if answer in ("oui", "non"):
            db.execute(
                "insert into tarik_held (invoice_id, session_id, released) values (%s, %s, %s) "
                "on conflict (invoice_id) do update set released = excluded.released",
                (invoice_id, session_id, answer == "oui"),
            )
        else:
            db.execute(
                "insert into tarik_held (invoice_id, session_id) values (%s, %s) on conflict do nothing",
                (invoice_id, session_id),
            )
        released = db.execute("select released from tarik_held where invoice_id = %s", (invoice_id,)).fetchone()[0]
    if not released:
        return stop(
            f"Facture {invoice_id} : importee et verifiee, laissee en brouillon. Dites-moi quand la comptabiliser."
        )
    return routine(f"Facture {invoice_id} : a comptabiliser.")


def readable(message):
    """A step's refusal (str) as the client may read it: never a technical text naming tools and their arguments."""
    return message or CRASHED


@tool
def record_step(run_id: str, session_id: str, origin: str, result: dict) -> dict:
    """Record a step's outcome for this run. ``origin``: the step tool that ran, or what asked (dossier_questions:
    the step is the result's own); ``result``: that step's answer. A question becomes the invoice's pending question;
    an outcome at or after that step clears it."""
    step = origin if origin in STEPS else result.get("step") or origin
    status = result.get("status", "error")
    message = readable(result.get("message") or result.get("error") or str(result))
    invoice_id, file_id, dossier_id = result.get("invoice_id"), result.get("file_id"), result.get("dossier_id")
    choices = json.dumps(result["choices"]) if result.get("choices") else None  # {id: name} a question proposed
    row = (run_id, session_id, file_id, invoice_id, dossier_id, step, status, message, result.get("routine") is True)
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_results (run_id, session_id, file_id, invoice_id, dossier_id, step, status, message, "
            "routine, choices, journal_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (*row, choices, result.get("journal_id")),
        )
        if invoice_id and status == "question":
            db.execute(
                "insert into tarik_pending (invoice_id, session_id, step, question, choices) "
                "values (%s, %s, %s, %s, %s) on conflict (invoice_id) do update set step = excluded.step, "
                "session_id = excluded.session_id, question = excluded.question, choices = excluded.choices, "
                "created_at = now()",
                (invoice_id, session_id, step, message, choices),
            )
        elif invoice_id:
            # Only an outcome at or after the pending step settles it: an earlier step's outcome never does.
            pending = db.execute("select step from tarik_pending where invoice_id = %s", (invoice_id,)).fetchone()
            if pending and (pending[0] not in ORDER or position(step) >= position(pending[0])):
                db.execute("delete from tarik_pending where invoice_id = %s", (invoice_id,))
    return ok("enregistre", step=step, status_recorded=status)


def summary_line(key, step, status, message):
    """One line of the run's summary: a step's outcome on invoice ``key`` (int, None or a negative file id)."""
    text = message.removeprefix(TEST_MODE)
    if key and key > 0 and str(key) not in text:  # an error Experio gave without it: which invoice
        text = f"facture {key} : {text}"
    if status == "question":
        text = f"QUESTION : {text}"
    elif status not in ("ok", "stop", "wait"):
        text = f"Probleme a l'etape {STEPS.get(step, step)} : {text}"
    return f"- {STEPS.get(step, step)} : {text}"


@tool
def run_summary(run_id: str) -> dict:
    """What happened in one processing run, invoice by invoice, for the client: each invoice's (or file's) outcomes
    that say something (a question, a stop, a problem, what was set or changed), and where it stands, its furthest
    outcome. Routine ones (nothing to do, the expected setting) are left out; a dossier's setup is shown whole."""
    with cabinet_db() as db:
        rows = db.execute(
            "select coalesce(invoice_id, -file_id), step, status, message, routine from tarik_results "
            "where run_id = %s order by id",
            (run_id,),
        ).fetchall()
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
    blocks = [
        [summary_line(key, *outcome[:3]) for outcome in found if not outcome[3] or outcome is found[-1]]
        for key, found in outcomes.items()
    ]
    summary = "\n\n".join("\n".join(lines) for lines in blocks)
    return ok((TEST_MODE + "\n\n" if test else "") + summary)


# Each bank statement of a conversation not imported yet whose last import asked for its bank account.
STATEMENTS_ASKED = (
    "select file_id, dossier_id, message, file_name, choices from (select distinct on (r.file_id) r.file_id, "
    "r.dossier_id, r.status, r.message, r.choices, f.file_name from tarik_results r join tarik_files f "
    "on f.id = r.file_id "
    "where r.session_id = %s and r.step = 'import_bank_statement' and f.statement_id is null "
    "order by r.file_id, r.id desc) latest where status = 'question'"
)


# Each dossier of a conversation whose last bank question (ask_banks) is still a question.
BANKS_ASKED = (
    "select dossier_id, journal_id, message, choices from (select distinct on (dossier_id) dossier_id, journal_id, "
    "status, message, choices from tarik_results where session_id = %s and step = 'ask_banks' "
    "order by dossier_id, id desc) latest where status = 'question'"
)


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
            "select invoice_id, step, question, choices from tarik_pending where session_id = %s order by created_at",
            (session_id,),
        ).fetchall()
        statements = db.execute(STATEMENTS_ASKED, (session_id,)).fetchall()
        banks = db.execute(BANKS_ASKED, (session_id,)).fetchall()
        ids = [i for i, _, _, _ in invoices]
        drafts = set(odoo_call("exp.invoice", "search", [("id", "in", ids), ("state", "=", "draft")])) if ids else set()
        gone = [i for i in ids if i not in drafts]
        if gone:
            db.execute("delete from tarik_pending where invoice_id = any(%s)", (gone,))
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
                "label": f"releve {name}",
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
    conversation's own; a draft deleted or posted since, or of another dossier, is left out. Each: {invoice_id,
    step (the one it waits at), message, choices}."""
    with cabinet_db() as db:
        rows = db.execute(
            "select invoice_id, step, question, choices from tarik_pending where session_id <> %s order by created_at",
            (session_id,),
        ).fetchall()
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
    about = normalized(str(about or "")).removeprefix("facture ").removeprefix("releve ")
    named = [item for item in waiting if about and about in item["names"]]
    return named[0] if len(named) == 1 else None


def apply_answer(item, answer, decision):
    """Apply the client's ``answer`` (their own words) and the agent's ``decision`` (what it read in them) to waiting
    ``item`` (from waiting_on): the result of the call that takes it, with the step it answers and, when it went
    fine, the ``next_step`` the sequence resumes at. A oui / non question the decision doesn't settle is asked
    again."""
    if item["step"] == "import_bank_statement":
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
        result = question(f"Reponse non comprise. {item['question']}")
    else:
        # A plain "oui" or "non" is read here when the agent left the decision empty (it chose another tool).
        settled = decision if yes_no(decision) is not None else answer
        result = apply(item["invoice_id"], settled if item["step"] in DECIDED else answer)
    if result["status"] == "ok":
        result["next_step"] = following
    return {**result, "invoice_id": item["invoice_id"], "step": item["step"]}


# The files that came since the conversation's last run recorded anything: not yet taken by any step, not cut
# parts, not a dossier's entries (those go to the dossier workflow).
NEW_FILES = (
    "select file_name from tarik_files where session_id = %s and invoice_id is null and statement_id is null "
    "and parent_id is null and kind is distinct from 'entries' and created_at > "
    "coalesce((select max(created_at) from tarik_results where session_id = %s), '-infinity')"
)


def new_files(session_id):
    """How many files (int) came in this conversation since its last run, for the processing sequence."""
    with cabinet_db() as db:
        names = [name for (name,) in db.execute(NEW_FILES, (session_id, session_id))]
    return sum(not name.lower().endswith(ENTRY_FILES) for name in names)


@tool
def route_message(session_id: str, mode: str = "files") -> dict:
    """Where a message of this conversation goes in the processing workflow, from what the database knows, not
    from the tool the agent happened to call (``mode``: files, answer or resume). ``route``: answer when the
    conversation waits on the client and no file came since its last run; files when files came and nothing
    waits; both, the answer and the files, when both; else ``mode``, files or answer. A follow-up (resume)
    stays one."""
    if mode == "resume":
        return ok("Suite du traitement.", route="resume")
    waiting, fresh = bool(waiting_on(session_id)), new_files(session_id) > 0
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
            f"Plusieurs questions attendent votre reponse ; precisez la facture ou le releve :\n{questions}",
            step="answer_pending",
        )
    sender = check_sender(session_id, dossier_id=item.get("dossier_id", 0), invoice_id=item.get("invoice_id", 0))
    if sender["status"] != "ok":  # recorded as check_sender's: the email workflow tells the manager, never the sender
        return {**sender, "step": "check_sender"}
    return apply_answer(item, answer, decision)


@tool
def save_files(session_id: str = "", files: list | None = None) -> dict:
    """Save a conversation's files, the agent's attachments: [{ id, file_name, mime_type, data (base64) }], whose id
    saves each one once however many times the sequence runs."""
    saved = []
    with cabinet_db() as db:
        for file in files or []:
            row = db.execute(
                "insert into tarik_files (session_id, file_name, mime_type, data, source_id) "
                "values (%s, %s, %s, %s, %s) on conflict (session_id, source_id) do nothing returning id",
                (session_id, file["file_name"], file["mime_type"], base64.b64decode(file["data"]), file.get("id")),
            ).fetchone()
            if row:
                saved.append({"id": row[0], "file_name": file["file_name"]})
    return ok(f"{len(saved)} fichier(s) enregistre(s).", files=saved)


@tool
def files_to_process(session_id: str, entries: bool = False) -> dict:
    """The conversation's files not processed yet: its invoices and statements, or with
    ``entries`` its FEC and grand livre files. Picked here, not by the agent: it never
    sees the files' numbers, so it can't show them to the client."""
    with cabinet_db() as db:
        # Whatever has no draft or statement yet, until it has: a file left uncut (its side wasn't given), one invoice
        # cut out of a PDF whose draft failed, a statement waiting for its bank account. A cut PDF is its parts.
        rows = db.execute(
            "select id, file_name from tarik_files f where session_id = %s and invoice_id is null "
            "and statement_id is null and kind is distinct from 'entries' "
            "and not exists (select 1 from tarik_files part where part.parent_id = f.id) order by id",
            (session_id,),
        ).fetchall()
    own = [i for i, name in rows if name.lower().endswith(ENTRY_FILES) == entries]
    return ok(f"{len(own)} fichier(s) a traiter.", files=own)


# The stops a client's "reprenez" retries: a dossier setting or an Experio check the cabinet may have fixed since.
# A duplicate, a type or a draft kept unposted (hold_posting) is the client's call, not a setting.
FIXABLE_STOPS = {"assign_account", "check_journal", "post_invoice"}


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
def waiting_drafts(session_id: str, held: bool = False, dossier_id: int = 0, stopped: bool = False) -> dict:
    """This conversation's drafts to take up again from Experio's reading: the furthest step each reached ended
    waiting on Experio (still reading) or failed (an Experio or model timeout), or nothing was recorded for it (a
    run cut short: the server restarted). Every later step runs again; a question or a stop is not resumed here,
    except what the client asks for. ``held``: the drafts they asked to leave unposted (they now ask to post).
    ``stopped``: the drafts a fixable setting stopped (FIXABLE_STOPS: the cabinet has fixed it since, they ask to
    resume). Both also reach the dossier ``dossier_id``'s drafts from any conversation, since the client may ask in
    a new email thread; never an automatic follow-up's. A draft deleted or posted in Experio since is left alone."""
    with cabinet_db() as db:
        rows = db.execute(
            "select invoice_id, step, status from tarik_results where session_id = %s and invoice_id is not null "
            "order by id",
            (session_id,),
        ).fetchall()
        drafts = db.execute(
            "select invoice_id from tarik_files where session_id = %s and invoice_id is not null order by id",
            (session_id,),
        ).fetchall()
        elsewhere = []
        if dossier_id and stopped:
            elsewhere = db.execute(
                "select invoice_id, step, status from tarik_results where invoice_id in "
                "(select invoice_id from tarik_results where status = 'stop' and invoice_id is not null) order by id"
            ).fetchall()
        kept = []
        if held:
            kept = db.execute("select invoice_id, session_id from tarik_held where not released").fetchall()
    mine = furthest_outcomes(rows)
    waiting = [i for i, (step, status) in mine.items() if resumable(step, status, stopped)]
    # shortcut: a draft of a run still going also has no outcome yet; one conversation runs one at a time
    waiting += [invoice_id for (invoice_id,) in drafts if invoice_id not in mine]
    if dossier_id and stopped:
        found = furthest_outcomes(elsewhere).items()
        waiting += [i for i, (step, status) in found if resumable(step, status, True)]
    # The conversation's own held drafts, and the dossier's from any other.
    waiting += [invoice_id for invoice_id, owner in kept if owner == session_id or dossier_id]
    if waiting:
        domain = [("id", "in", sorted(set(waiting))), ("state", "in", ["pending", "draft"])]
        # What came from any conversation is limited to this dossier's.
        if dossier_id and (held or stopped):
            domain.append(("company_id", "in", [dossier_id]))
        waiting = sorted(odoo_call("exp.invoice", "search", domain))
    return ok(f"{len(waiting)} facture(s) a reprendre.", invoices=waiting)
