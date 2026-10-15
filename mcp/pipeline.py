"""The fixed processing sequence, run by the n8n workflow "Tarik - Traitement": waiting for
Experio's reading, recording each step's outcome, pending questions and the client's answers."""

import re
import time

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from accounts import set_account
from app import ok, server, stop, wait, with_ids
from experio import POSTGRES, odoo_call, require_invoice
from tiers import set_tiers_by_name
from tva import settle_tva
from exercice import settle_exercice
from prepaid import settle_prepaid
from withholding import settle_withholding

# The sequence, in order: step tool -> what the client reads it as.
STEPS = {
    "create_dossier": "creation du dossier", "copy_configuration": "configuration", "import_entries": "ecritures",
    "finish_setup": "integration", "setup_status": "etat du dossier",
    "identify_document": "identification", "import_bank_statement": "releve bancaire", "split_invoices": "decoupage",
    "create_draft_invoice": "brouillon", "wait_until_read": "lecture par Experio", "check_invoice": "verification",
    "correct_invoice": "correction", "classify_invoice": "classement", "link_tiers": "tiers",
    "check_duplicate": "doublon", "assign_account": "compte", "settle_tva": "TVA", "check_journal": "journal", "settle_withholding": "retenue a la source", "settle_prepaid": "constatee d'avance", "settle_exercice": "exercice", "post_invoice": "comptabilisation",
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


@server.tool()
@with_ids
def wait_until_read(invoice_id: int) -> dict:
    """Wait (up to 3 minutes) until Experio has finished reading a new draft."""
    for _ in range(12):
        state = require_invoice(invoice_id, ["bluejay_state"])["bluejay_state"]
        if state not in ("pending", "sent"):
            return ok(f"Facture {invoice_id} : lue par Experio.")
        time.sleep(15)
    return wait(f"Facture {invoice_id} : Experio la lit encore, elle sera reprise a votre prochain message.")


@server.tool()
def record_step(run_id: str, session_id: str, origin: str, result: dict) -> dict:
    """Record a step's outcome for this run. ``origin``: the n8n node that routed here,
    whose name holds the step tool; ``result``: that step's answer. A question becomes
    the invoice's pending question; an outcome at or after that step clears it."""
    step = next((tool for tool in STEPS if tool in origin), result.get("step") or origin)
    status, message = result.get("status", "error"), result.get("message") or result.get("error") or str(result)
    invoice_id, file_id = result.get("invoice_id"), result.get("file_id")
    with psycopg.connect(**POSTGRES) as db:
        db.execute("insert into tarik_results (run_id, session_id, file_id, invoice_id, step, status, message) "
                   "values (%s, %s, %s, %s, %s, %s, %s)", (run_id, session_id, file_id, invoice_id, step, status, message))
        if invoice_id and status == "question":
            db.execute("insert into tarik_pending (invoice_id, session_id, step, question) values (%s, %s, %s, %s) "
                       "on conflict (invoice_id) do update set step = excluded.step, question = excluded.question, created_at = now()",
                       (invoice_id, session_id, step, message))
        elif invoice_id:
            # Only an outcome at or after the pending step settles it: n8n records the earlier steps after the question.
            pending = db.execute("select step from tarik_pending where invoice_id = %s", (invoice_id,)).fetchone()
            order = list(STEPS)
            if pending and (pending[0] not in order or step not in order or order.index(step) >= order.index(pending[0])):
                db.execute("delete from tarik_pending where invoice_id = %s", (invoice_id,))
    return ok("enregistre", step=step, status_recorded=status)


TEST_PREFIX = "MODE TEST : aucune modification enregistree dans Experio. "
QUIET = {"wait_until_read"}  # outcomes the client doesn't need when they went fine


@server.tool()
def run_summary(run_id: str) -> dict:
    """What happened in one processing run, invoice by invoice and step by step, for the client."""
    with psycopg.connect(**POSTGRES) as db:
        rows = db.execute("select coalesce(invoice_id, -file_id), step, status, message from tarik_results "
                          "where run_id = %s order by id", (run_id,)).fetchall()
    if not rows:
        return ok("Rien n'a ete traite.")
    # n8n records a step after running the next ones, so rows come in reverse: sort by the sequence instead.
    order = list(STEPS)
    rows = sorted(rows, key=lambda row: order.index(row[1]) if row[1] in order else len(order))
    test = any(message.startswith(TEST_PREFIX) for *_, message in rows)
    blocks, seen = {}, set()
    for key, step, status, message in rows:
        if (key, step, message) in seen or (status == "ok" and step in QUIET):
            continue
        seen.add((key, step, message))
        text = message.removeprefix(TEST_PREFIX)
        if status == "question":
            text = f"QUESTION : {text}"
        elif status not in ("ok", "stop", "wait"):
            text = f"Probleme a l'etape {STEPS.get(step, step)} : {text}"
        blocks.setdefault(key, []).append(f"- {STEPS.get(step, step)} : {text}")
    summary = "\n\n".join("\n".join(lines) for lines in blocks.values())
    return ok((TEST_PREFIX + "\n\n" if test else "") + summary)


@server.tool()
@with_ids
def answer_pending(invoice_id: int, answer: str) -> dict:
    """Apply the client's answer to the invoice's pending question. ``next_step``
    says where the sequence resumes (none when the question is still open)."""
    with psycopg.connect(**POSTGRES) as db:
        row = db.execute("select step, question from tarik_pending where invoice_id = %s", (invoice_id,)).fetchone()
    if not row:
        return stop(f"Facture {invoice_id} : aucune question en attente.")
    apply, following = ANSWERS.get(row[0], (None, None))
    if not apply:
        raise ToolError(f"Facture {invoice_id} : la question en attente ({row[0]}) ne se regle pas par une reponse.")
    result = apply(invoice_id, answer, row[1]) if row[0] == "link_tiers" else apply(invoice_id, answer)
    if result["status"] == "ok":
        result["next_step"] = following or "done"
    result["step"] = row[0]
    return result


@server.tool()
def files_to_process(session_id: str, file_ids: list | dict | str | int | None = None) -> dict:
    """The files a run works on: the numbers the agent passed when they are this
    conversation's files, else the conversation's files not processed yet (the model
    sometimes passes none, or garbles them)."""
    asked = [int(n) for n in re.findall(r"\d+", str(file_ids))] if file_ids is not None else []
    with psycopg.connect(**POSTGRES) as db:
        own = [i for (i,) in db.execute("select id from tarik_files where session_id = %s and id = any(%s)", (session_id, asked))]
        if not own:
            own = [i for (i,) in db.execute(
                "select id from tarik_files where session_id = %s and parent_id is null and kind is null "
                "and invoice_id is null and statement_id is null order by id", (session_id,))]
    return ok(f"{len(own)} fichier(s) a traiter.", files=own)


@server.tool()
def waiting_drafts(session_id: str) -> dict:
    """This conversation's drafts still waiting on Experio's reading, to resume from
    there: the last time, wait_until_read gave up and no later step ran."""
    with psycopg.connect(**POSTGRES) as db:
        waiting = [i for (i,) in db.execute(
            "select distinct invoice_id from tarik_results r where session_id = %s and step = 'wait_until_read' "
            "and status = 'wait' and not exists (select 1 from tarik_results later where later.invoice_id = r.invoice_id "
            "and (later.step not in ('create_draft_invoice', 'wait_until_read') "
            "or (later.step = 'wait_until_read' and later.status <> 'wait'))) order by invoice_id", (session_id,))]
    if waiting:  # a draft deleted in Experio since then is not resumed
        waiting = sorted(odoo_call("exp.invoice", "search", [("id", "in", waiting), ("state", "in", ["pending", "draft"])]))
    return ok(f"{len(waiting)} facture(s) en attente de lecture.", invoices=waiting)
