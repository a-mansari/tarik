"""Build workflows/tarik-emails.json: Tarik answering his mailbox.

Edit this script, not the JSON: python3 workflows/build_tarik_emails.py
Every 2 minutes: read the new emails (the mailbox is not changed), then for each thread
waiting for an answer, Tarik ("Tarik - Agent", as in the chat) answers its client's emails,
and the reply goes out in the thread, to whitelisted addresses only. A thread whose run
fails is picked up again 30 minutes later; after 3 failures it is set aside in error."""

import json
import sys
from pathlib import Path

from steps import Workflow

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("tarik-emails.json")
flow = Workflow("tarik-emails")
node, connect, mcp = flow.node, flow.connect, flow.mcp

THREAD = "$('Un element par fil').item.json"
SIGNATURE = "\\n\\n--\\nTarik, assistant comptable"

node("Toutes les 2 minutes", "n8n-nodes-base.scheduleTrigger", 1.2, [0, 300],
     {"rule": {"interval": [{"field": "minutes", "minutesInterval": 2}]}})
mcp("Nouveaux emails (fetch_emails)", "fetch_emails", "{}", [220, 300])
node("Un element par fil", "n8n-nodes-base.code", 2, [440, 300], {"jsCode": (
    "// One item per thread waiting for an answer (fetch_emails locked them for this run).\n"
    "return $input.first().json.content[0].text.threads.map(thread => ({ json: thread }));")})
node("Tarik", "n8n-nodes-base.executeWorkflow", 1.2, [660, 300], {
    "workflowId": {"__rl": True, "mode": "id", "value": "TarikAgent000001", "cachedResultName": "Tarik - Agent"},
    "mode": "each",
    "workflowInputs": {"mappingMode": "defineBelow", "value": {
        "session_id": "={{ $json.session_id }}", "message": "={{ $json.message }}", "files": "={{ $json.files }}"},
        "matchingColumns": [],
        "schema": [{"id": key, "displayName": key, "required": False, "defaultMatch": False, "display": True,
                    "canBeUsedToMatch": True, "type": kind, "removed": False}
                   for key, kind in (("session_id", "string"), ("message", "string"), ("files", "array"))],
        "attemptToConvertTypes": False, "convertFieldsToString": False},
    "options": {},
}, onError="continueErrorOutput")  # a failed run leaves the thread locked: fetch_emails takes it again later
mcp("Reponse (send_email)", "send_email", (
    f"{{ to: {THREAD}.to, subject: {THREAD}.subject, body: $json.output + '{SIGNATURE}', "
    f"in_reply_to: {THREAD}.in_reply_to, references: {THREAD}.references }}"), [880, 300])
mcp("Fil traite (finish_thread)", "finish_thread", (
    f"{{ thread_id: {THREAD}.thread_id, message_ids: {THREAD}.message_ids, sent: !$json.error, "
    "reason: $json.error ? String($json.error.message ?? $json.error).replace(/^Error executing tool \\w+: /, '') : '' }"),
    [1100, 300])
connect("Toutes les 2 minutes", "Nouveaux emails (fetch_emails)")
connect("Nouveaux emails (fetch_emails)", "Un element par fil", 0)
connect("Un element par fil", "Tarik")
connect("Tarik", "Reponse (send_email)", 0)
connect("Reponse (send_email)", "Fil traite (finish_thread)", 0)
connect("Reponse (send_email)", "Fil traite (finish_thread)", 1)  # not sent (whitelist, SMTP): set aside, with why

workflow = {"id": "TarikEmails00001", "name": "Tarik - Emails", "active": False,
            "settings": {"executionOrder": "v1", "saveExecutionProgress": True},
            "nodes": flow.nodes, "connections": flow.connections}
OUT.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")
print(len(flow.nodes), "nodes")
