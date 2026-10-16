"""Build workflows/tarik-emails.json: Tarik answering his mailbox.

One workflow for every cabinet: tarik-mail-watch (mcp/watcher.py) reads the cabinet's mailbox, stores
its new emails in their threads and tells the manager of refused senders, then calls this webhook when a
thread waits for an answer. The run works in that cabinet only, every step at its address: for each
waiting thread, the cabinet's own n8n agent answers its client's emails (ask_agent), and the reply goes out
in the thread, to whitelisted addresses only. A thread whose run stops midway is taken again 30 minutes
later; after 3 such runs it is set aside in error."""

import sys
from pathlib import Path

from steps import Workflow

# The cabinet tarik-mail-watch named: every MCP step goes to its address (an unknown one: 404, nothing done).
flow = Workflow("tarik-emails", "$('Nouvel email').first().json.body.cabinet")
node, connect, mcp = flow.node, flow.connect, flow.mcp

THREAD = "$('Un element par fil').item.json"

node("Nouvel email", "n8n-nodes-base.webhook", 2, [0, 300],
     {"httpMethod": "POST", "path": "tarik-nouvel-email", "responseMode": "onReceived", "options": {},
      # Only tarik-mail-watch: its X-Tarik-Webhook header carries the secret both share (Vault MAIL_WEBHOOK_SECRET).
      "authentication": "headerAuth"},
     webhookId="5d0f6c2a-8e4b-4f17-9a3c-2b7e1d6f8a40",
     credentials={"httpHeaderAuth": {"id": "TarikMailWebhook1", "name": "Tarik mail webhook"}})
mcp("Fils en attente (claim_threads)", "claim_threads", "{}", [220, 300])
node("Un element par fil", "n8n-nodes-base.code", 2, [440, 300], {"jsCode": (
    "// One item per thread waiting for an answer (claim_threads locked them for this run).\n"
    "// None when tarik-mcp answered without them (restarting): the next wake-up takes them.\n"
    "return ($input.first().json.content?.[0]?.text?.threads ?? []).map(thread => ({ json: thread }));")})
# The cabinet's own agent answers, in the thread's conversation (ask_agent: n8n's call_agent).
# Longer than ask_agent's own 600 s wait: n8n never gives up on an answer still coming.
mcp("Tarik", "ask_agent", "{ session_id: $json.session_id, message: $json.message, files: $json.files }", [660, 300], timeout=660)
mcp("Reponse (send_email)", "send_email", (
    f"{{ to: {THREAD}.to, subject: {THREAD}.subject, body: $('Tarik').item.json.content[0].text.output, "
    f"in_reply_to: {THREAD}.in_reply_to, references: {THREAD}.references }}"), [880, 300])
mcp("Fil traite (finish_thread)", "finish_thread", (
    f"{{ thread_id: {THREAD}.thread_id, message_ids: {THREAD}.message_ids, sent: !$json.error, "
    "reason: $json.error ? String($json.error.message ?? $json.error).replace(/^Error executing tool \\w+: /, '') : '' }"),
    [1100, 300], retryOnFail=True, maxTries=3, waitBetweenTries=5000)  # the reply is out: its thread must be closed, or it's sent twice
connect("Nouvel email", "Fils en attente (claim_threads)")
connect("Fils en attente (claim_threads)", "Un element par fil", 0)
connect("Un element par fil", "Tarik")
# Refused for the sender's rights on the dossier: the sender gets nothing, the manager is told.
mcp("Refus de droits ? (thread_refusal)", "thread_refusal", f"{{ thread_id: {THREAD}.thread_id }}", [770, 300])
flow.route("Refuse ?", "$json.content[0].text.refused ? 1 : 0", 2, [820, 360])
mcp("Manager prevenu (notify_manager)", "notify_manager",
    "{ sender: $json.content[0].text.sender, subject: $json.content[0].text.subject, reason: $json.content[0].text.reason }",
    [880, 480])
connect("Tarik", "Refus de droits ? (thread_refusal)", 0)
connect("Refus de droits ? (thread_refusal)", "Fil traite (finish_thread)", 1)  # not known whether refused: set aside, nothing sent
connect("Refus de droits ? (thread_refusal)", "Refuse ?", 0)
connect("Refuse ?", "Reponse (send_email)", 0)
connect("Refuse ?", "Manager prevenu (notify_manager)", 1)

connect("Reponse (send_email)", "Fil traite (finish_thread)", 0)
connect("Reponse (send_email)", "Fil traite (finish_thread)", 1)  # not sent (whitelist, SMTP): set aside, with why
connect("Tarik", "Fil traite (finish_thread)", 1)  # no reply (n8n or the model failing): set aside, with why, never asked twice

flow.save(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("tarik-emails.json"),
          "TarikEmails00001", "Tarik - Emails", {"executionOrder": "v1", "saveExecutionProgress": True})
