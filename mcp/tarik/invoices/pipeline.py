"""The fixed processing sequence, run by the n8n workflow "Tarik - Traitement": waiting for
Experio's reading, recording each step's outcome, pending questions and the client's answers."""

import base64
import json
import re
import time

from tarik.core.app import ok, question, server, wait, with_ids
from tarik.core.cabinets import cabinet_db
from tarik.core.config import ENTRY_FILES, READ_WAIT, READING, TEST_MODE
from tarik.core.conversation import own_words
from tarik.core.experio import odoo_call, require_invoice
from tarik.core.text import normalized, said, without_if
from tarik.dossiers.people import check_sender
from tarik.invoices.accounts import set_account
from tarik.invoices.exercice import settle_exercice
from tarik.invoices.intake import import_bank_statement, journal_meant
from tarik.invoices.prepaid import settle_prepaid
from tarik.invoices.tiers import set_tiers_by_name
from tarik.invoices.tva import settle_tva
from tarik.invoices.withholding import settle_withholding

# The sequence, in order: step tool -> what the client reads it as.
STEPS = {
    "answer_pending": "reponse",
    "check_sender": "expediteur",
    "create_dossier": "creation du dossier",
    "update_dossier": "modification du dossier",
    "copy_configuration": "configuration",
    "import_entries": "ecritures",
    "finish_setup": "integration",
    "setup_status": "etat du dossier",
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
    "post_invoice": "comptabilisation",
}
ORDER = list(STEPS)
SETUP_STEPS = {
    "create_dossier",
    "update_dossier",
    "copy_configuration",
    "import_entries",
    "finish_setup",
    "setup_status",
}
# A pending question, the call that applies the client's answer, and the step that follows.
ANSWERS = {
    "link_tiers": (set_tiers_by_name, "check_duplicate"),
    "assign_account": (set_account, "settle_tva"),
    "settle_tva": (settle_tva, "check_journal"),
    "settle_withholding": (settle_withholding, "settle_prepaid"),
    "settle_prepaid": (settle_prepaid, "settle_exercice"),
    "settle_exercice": (settle_exercice, "post_invoice"),
}
QUIET = {"wait_until_read", "check_sender"}  # outcomes the client doesn't need when they went fine


def position(step):
    """Where ``step`` (a step tool's name) stands in the sequence; after every step when it is not one."""
    return ORDER.index(step) if step in ORDER else len(ORDER)


READ_DEADLINES = {}  # n8n run id -> when that run stops waiting on Experio's reading (time.monotonic)


@server.tool()
@with_ids
def wait_until_read(invoice_id: int, run_id: str = "") -> dict:
    """Wait until Experio has finished reading a new draft: READ_WAIT seconds at most for all the drafts of run
    ``run_id`` together, so the run ends before n8n stops it; a draft still being read is resumed later."""
    now = time.monotonic()
    for old in [run for run, deadline in READ_DEADLINES.items() if deadline < now - 3600]:
        del READ_DEADLINES[old]
    deadline = READ_DEADLINES.setdefault(run_id, now + READ_WAIT) if run_id else now + READ_WAIT
    while require_invoice(invoice_id, ["bluejay_state"])["bluejay_state"] in READING:
        if time.monotonic() >= deadline:
            return wait(f"Facture {invoice_id} : Experio la lit encore, elle sera reprise a votre prochain message.")
        time.sleep(15)
    return ok(f"Facture {invoice_id} : lue par Experio.")


def readable(message):
    """A step's refusal (str) as the client may read it: without n8n's wrapper, nor the MCP library's own
    validation text, which names tools and their arguments."""
    message = re.sub(r"^Error executing tool \w+: ", "", message)  # n8n's wrapper, at the very start
    if "validation error for" in message:
        return "des informations manquent ou sont invalides pour cette etape."
    return message


@server.tool()
def record_step(run_id: str, session_id: str, origin: str, result: dict) -> dict:
    """Record a step's outcome for this run. ``origin``: the n8n node that routed here,
    whose name holds the step tool; ``result``: that step's answer. A question becomes
    the invoice's pending question; an outcome at or after that step clears it."""
    step = next((tool for tool in STEPS if tool in origin), result.get("step") or origin)
    status = result.get("status", "error")
    message = readable(result.get("message") or result.get("error") or str(result))
    invoice_id, file_id, dossier_id = result.get("invoice_id"), result.get("file_id"), result.get("dossier_id")
    with cabinet_db() as db:
        db.execute(
            "insert into tarik_results (run_id, session_id, file_id, invoice_id, dossier_id, step, status, message) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (run_id, session_id, file_id, invoice_id, dossier_id, step, status, message),
        )
        if invoice_id and status == "question":
            db.execute(
                "insert into tarik_pending (invoice_id, session_id, step, question) values (%s, %s, %s, %s) "
                "on conflict (invoice_id) do update set step = excluded.step, question = excluded.question, "
                "created_at = now()",
                (invoice_id, session_id, step, message),
            )
        elif invoice_id:
            # Only an outcome at or after the pending step settles it: n8n records the earlier steps after the question.
            pending = db.execute("select step from tarik_pending where invoice_id = %s", (invoice_id,)).fetchone()
            if pending and (pending[0] not in ORDER or step not in ORDER or position(step) >= position(pending[0])):
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


@server.tool()
def run_summary(run_id: str) -> dict:
    """What happened in one processing run, invoice by invoice and step by step, for the client."""
    with cabinet_db() as db:
        rows = db.execute(
            "select coalesce(invoice_id, -file_id), step, status, message from tarik_results "
            "where run_id = %s order by id",
            (run_id,),
        ).fetchall()
    if not rows:
        return ok("Rien n'a ete traite.")
    # n8n records a step after running the next ones, so rows come in reverse: sort by the sequence instead.
    rows = sorted(rows, key=lambda row: position(row[1]))
    test = any(message.startswith(TEST_MODE) for *_, message in rows)
    blocks, seen = {}, set()
    for key, step, status, message in rows:
        if step in SETUP_STEPS:
            key = None  # a dossier's setup is one block, its imports in it in order
        if (key, step, message) in seen or (status == "ok" and step in QUIET):
            continue
        seen.add((key, step, message))
        blocks.setdefault(key, []).append(summary_line(key, step, status, message))
    summary = "\n\n".join("\n".join(lines) for lines in blocks.values())
    return ok((TEST_MODE + "\n\n" if test else "") + summary)


# Each bank statement of a conversation not imported yet whose last import asked for its bank account.
STATEMENTS_ASKED = (
    "select file_id, dossier_id, message, file_name from (select distinct on (r.file_id) r.file_id, r.dossier_id, "
    "r.status, r.message, f.file_name from tarik_results r join tarik_files f on f.id = r.file_id "
    "where r.session_id = %s and r.step = 'import_bank_statement' and f.statement_id is null "
    "order by r.file_id, r.id desc) latest where status = 'question'"
)


def invoice_names(ids):
    """How the client may name each invoice of ``ids`` besides "facture <id>": {id: [its own number, its tiers]}."""
    if not ids:
        return {}
    invoices = odoo_call("exp.invoice", "read", ids, fields=["invoice_number", "partner_id"])
    return {
        invoice["id"]: [invoice["invoice_number"], invoice["partner_id"] and without_if(invoice["partner_id"][1])]
        for invoice in invoices
    }


def waiting_on(session_id):
    """What this conversation waits on the client for, oldest first: each draft invoice's question, and each bank
    statement whose last import asked for its bank account. A question on an invoice deleted or posted since is
    dropped: nothing can answer it any more. Each item: {invoice_id or file_id and dossier_id, step, question,
    names (how the client may name it)}."""
    with cabinet_db() as db:
        invoices = db.execute(
            "select invoice_id, step, question from tarik_pending where session_id = %s order by created_at",
            (session_id,),
        ).fetchall()
        statements = db.execute(STATEMENTS_ASKED, (session_id,)).fetchall()
        ids = [i for i, _, _ in invoices]
        drafts = set(odoo_call("exp.invoice", "search", [("id", "in", ids), ("state", "=", "draft")])) if ids else set()
        gone = [i for i in ids if i not in drafts]
        if gone:
            db.execute("delete from tarik_pending where invoice_id = any(%s)", (gone,))
    invoices = [row for row in invoices if row[0] in drafts]
    named = invoice_names([i for i, _, _ in invoices])
    return [
        {"invoice_id": i, "step": step, "question": text, "names": [f"facture {i}", f"n {i}", *named.get(i, [])]}
        for i, step, text in invoices
    ] + [
        {
            "file_id": f,
            "dossier_id": d,
            "step": "import_bank_statement",
            "question": text,
            "names": [name.rsplit(".", 1)[0]],
        }
        for f, d, text, name in statements
    ]


def meant(waiting, answer):
    """The question the client answers: the only one, or the one their answer names (whole words). A bare number
    names no invoice: "oui, TVA 20 %" is not about invoice 20."""
    if len(waiting) == 1:
        return waiting[0]
    words = normalized(answer)
    named = [item for item in waiting if any(said(normalized(name), words) for name in item["names"] if name)]
    return named[0] if len(named) == 1 else None


def apply_answer(item, answer):
    """Apply the client's ``answer`` to waiting ``item`` (from waiting_on): the result of the call that takes it,
    with the step it answers and, when it went fine, the ``next_step`` the sequence resumes at."""
    if item["step"] == "import_bank_statement":
        imported = import_bank_statement(item["file_id"], item["dossier_id"], journal_meant(item["dossier_id"], answer))
        return {**imported, "step": "import_bank_statement"}
    apply, following = ANSWERS[item["step"]]
    asked = (item["question"],) if item["step"] == "link_tiers" else ()  # the tiers it proposed, by id
    result = apply(item["invoice_id"], answer, *asked)
    if result["status"] == "ok":
        result["next_step"] = following
    return {**result, "invoice_id": item["invoice_id"], "step": item["step"]}


@server.tool()
def answer_pending(session_id: str, answer: str) -> dict:
    """Apply the client's answer (their own words) to what this conversation waits on: an invoice's question
    (tiers, account, TVA, withholding, prepaid, exercice) or a statement's bank account. ``next_step`` says
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
    item = meant(waiting, answer)
    if not item:
        questions = "\n".join(f"- {entry['question']}" for entry in waiting)
        return question(
            f"Plusieurs questions attendent votre reponse ; precisez la facture ou le releve :\n{questions}",
            step="answer_pending",
        )
    sender = check_sender(session_id, dossier_id=item.get("dossier_id", 0), invoice_id=item.get("invoice_id", 0))
    if sender["status"] != "ok":  # recorded as check_sender's: the email workflow tells the manager, never the sender
        return {**sender, "step": "check_sender"}
    return apply_answer(item, answer)


@server.tool()
def save_files(payload: str = "") -> dict:
    """Save a conversation's files, sent by n8n as base64 of { session_id (or thread_id), files:
    [{ id?, file_name, mime_type, data }] }: the native agent's attachments
    (n8n/patch.js), whose id saves each one once however many times the sequence runs."""
    body = json.loads(base64.b64decode(payload or "e30=")) or {}  # "e30=" is base64 of "{}"
    session_id = body.get("session_id") or body.get("thread_id") or ""
    saved = []
    with cabinet_db() as db:
        for file in body.get("files") or []:
            row = db.execute(
                "insert into tarik_files (session_id, file_name, mime_type, data, source_id) "
                "values (%s, %s, %s, %s, %s) on conflict (session_id, source_id) do nothing returning id",
                (session_id, file["file_name"], file["mime_type"], base64.b64decode(file["data"]), file.get("id")),
            ).fetchone()
            if row:
                saved.append({"id": row[0], "file_name": file["file_name"]})
    return ok(f"{len(saved)} fichier(s) enregistre(s).", files=saved)


@server.tool()
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


@server.tool()
def waiting_drafts(session_id: str) -> dict:
    """This conversation's drafts to take up again from Experio's reading: the furthest step each reached ended
    waiting on Experio (still reading) or failed (an Experio or model timeout), or nothing was recorded for it:
    n8n records a run's steps last, so a run cut short (n8n stops an agent's tool after 5 minutes) leaves its
    drafts with no outcome. Every later step runs again; a question or a stop is not resumed here. A draft
    deleted or posted in Experio since is left alone."""
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
    furthest = {}  # by the sequence's order, not the rows': n8n records a step after the ones it led to
    for invoice_id, step, status in rows:
        if step in ORDER and (invoice_id not in furthest or position(step) >= position(furthest[invoice_id][0])):
            furthest[invoice_id] = (step, status)
    waiting = [invoice_id for invoice_id, (_, status) in furthest.items() if status in ("wait", "error")]
    # shortcut: a draft of a run still going also has no outcome yet; one conversation runs one at a time
    waiting += [invoice_id for (invoice_id,) in drafts if invoice_id not in furthest]
    if waiting:
        waiting = sorted(
            odoo_call("exp.invoice", "search", [("id", "in", waiting), ("state", "in", ["pending", "draft"])])
        )
    return ok(f"{len(waiting)} facture(s) a reprendre.", invoices=waiting)
