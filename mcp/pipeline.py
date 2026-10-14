"""The fixed processing sequence, run by the n8n workflow "Tarik - Traitement": waiting for
Experio's reading, recording each step's outcome, pending questions and the client's answers."""

import time

import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from accounts import set_account
from app import ok, server, stop, wait, with_ids
from experio import POSTGRES, require_invoice
from tiers import set_tiers_by_name
from tva import settle_tva

# The sequence, in order: step tool -> what the client reads it as.
STEPS = {
    "identify_document": "identification", "import_bank_statement": "releve bancaire", "split_invoices": "decoupage",
    "create_draft_invoice": "brouillon", "wait_until_read": "lecture par Experio", "check_invoice": "verification",
    "correct_invoice": "correction", "classify_invoice": "classement", "link_tiers": "tiers",
    "check_duplicate": "doublon", "assign_account": "compte", "settle_tva": "TVA",
}
# A pending question, the call that applies the client's answer, and the step that follows.
ANSWERS = {
    "link_tiers": (set_tiers_by_name, "check_duplicate"),
    "assign_account": (set_account, "settle_tva"),
    "settle_tva": (settle_tva, None),
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
    return wait(f"Facture {invoice_id} : Experio la lit encore, elle sera traitee a votre prochain message.")


@server.tool()
def record_step(run_id: str, session_id: str, origin: str, result: dict) -> dict:
    """Record a step's outcome for this run. ``origin``: the n8n node that routed here,
    whose name holds the step tool; ``result``: that step's answer. A question becomes
    the invoice's pending question; any other outcome clears it."""
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
