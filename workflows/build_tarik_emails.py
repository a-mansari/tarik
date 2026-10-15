"""Build workflows/tarik-emails.json: Tarik answering his mailbox.

Edit this script, not the JSON: python3 workflows/build_tarik_emails.py
As soon as an email arrives (tarik-mail-watch calls its webhook), and every 2 minutes in
case it missed one: read the new emails (the mailbox is not changed), then for each thread
waiting for an answer, the cabinet's own n8n agent answers its client's emails (ask_agent),
and the reply goes out in the thread, to whitelisted addresses only. A thread whose run
fails is picked up again 30 minutes later; after 3 failures it is set aside in error."""

import json
import sys
from pathlib import Path

from steps import MAIL_CABINET, Workflow

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("tarik-emails.json")
flow = Workflow("tarik-emails", MAIL_CABINET)
node, connect, mcp = flow.node, flow.connect, flow.mcp

THREAD = "$('Un element par fil').item.json"

node("Toutes les 2 minutes", "n8n-nodes-base.scheduleTrigger", 1.2, [0, 300],
     {"rule": {"interval": [{"field": "minutes", "minutesInterval": 2}]}})
# Called by tarik-mail-watch (mcp/watcher.py) the moment an email arrives; the schedule stays as the fallback.
node("Nouvel email", "n8n-nodes-base.webhook", 2, [0, 120],
     {"httpMethod": "POST", "path": "tarik-nouvel-email", "responseMode": "onReceived", "options": {}},
     webhookId="5d0f6c2a-8e4b-4f17-9a3c-2b7e1d6f8a40")
mcp("Nouveaux emails (fetch_emails)", "fetch_emails", "{}", [220, 300])
node("Un element par fil", "n8n-nodes-base.code", 2, [440, 300], {"jsCode": (
    "// One item per thread waiting for an answer (fetch_emails locked them for this run).\n"
    "return $input.first().json.content[0].text.threads.map(thread => ({ json: thread }));")})
# The cabinet's own agent answers, in the thread's conversation (ask_agent: n8n's call_agent).
mcp("Tarik", "ask_agent", "{ session_id: $json.session_id, message: $json.message, files: $json.files }", [660, 300])
# No reply (n8n or the model failing): the thread stays locked, and fetch_emails takes it again 30 minutes later.
mcp("Reponse (send_email)", "send_email", (
    f"{{ to: {THREAD}.to, subject: {THREAD}.subject, body: $('Tarik').item.json.content[0].text.output, "
    f"in_reply_to: {THREAD}.in_reply_to, references: {THREAD}.references }}"), [880, 300])
mcp("Fil traite (finish_thread)", "finish_thread", (
    f"{{ thread_id: {THREAD}.thread_id, message_ids: {THREAD}.message_ids, sent: !$json.error, "
    "reason: $json.error ? String($json.error.message ?? $json.error).replace(/^Error executing tool \\w+: /, '') : '' }"),
    [1100, 300])
connect("Toutes les 2 minutes", "Nouveaux emails (fetch_emails)")
connect("Nouvel email", "Nouveaux emails (fetch_emails)")
connect("Nouveaux emails (fetch_emails)", "Un element par fil", 0)
connect("Un element par fil", "Tarik")
# Refused for the sender's rights on the dossier: the sender gets nothing, the manager is told.
mcp("Refus de droits ? (thread_refusal)", "thread_refusal", f"{{ thread_id: {THREAD}.thread_id }}", [770, 300])
flow.route("Refuse ?", "$json.content[0].text.refused ? 1 : 0", 2, [820, 360])
mcp("Manager prevenu (notify_manager)", "notify_manager",
    "{ sender: $json.content[0].text.sender, subject: $json.content[0].text.subject, reason: $json.content[0].text.reason }",
    [880, 480])
connect("Tarik", "Refus de droits ? (thread_refusal)", 0)
connect("Refus de droits ? (thread_refusal)", "Refuse ?", 0)
connect("Refuse ?", "Reponse (send_email)", 0)
connect("Refuse ?", "Manager prevenu (notify_manager)", 1)

# Senders who may not use Tarik (not collaborators, or off the whitelist): the manager is told, the sender gets nothing.
node("Une alerte par refus", "n8n-nodes-base.code", 2, [440, 520], {"jsCode": (
    "// One item per email whose sender fetch_emails refused.\n"
    "return $input.first().json.content[0].text.alerts.map(alert => ({ json: alert }));")})
mcp("Manager prevenu de l'expediteur (notify_manager)", "notify_manager",
    "{ sender: $json.sender, subject: $json.subject, reason: $json.reason }", [660, 520])
connect("Nouveaux emails (fetch_emails)", "Une alerte par refus", 0)
connect("Une alerte par refus", "Manager prevenu de l'expediteur (notify_manager)")
connect("Reponse (send_email)", "Fil traite (finish_thread)", 0)
connect("Reponse (send_email)", "Fil traite (finish_thread)", 1)  # not sent (whitelist, SMTP): set aside, with why

workflow = {"id": "TarikEmails00001", "name": "Tarik - Emails", "active": False,
            "settings": {"executionOrder": "v1", "saveExecutionProgress": True},
            "nodes": flow.nodes, "connections": flow.connections}
OUT.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")
print(len(flow.nodes), "nodes")
