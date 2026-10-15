"""Build workflows/tarik-traitement.json: the fixed processing sequence as n8n nodes.

Modes: files (the conversation's files, from identification to posting), answer (the client's answer
to a pending question, then the sequence resumes there), resume (drafts Experio hadn't read yet)."""

import sys
from pathlib import Path

from steps import ATTACHMENTS, CABINET_INPUT, CALL, OK_ELSE, RESULT, SAVE_ATTACHMENTS, Workflow

flow = Workflow("tarik-traitement")
node, connect, mcp, route = flow.node, flow.connect, flow.mcp, flow.route

flow.trigger([{"name": "mode", "type": "string"}, {"name": "session_id", "type": "string"},
              {"name": "dossier_id", "type": "number"}, {"name": "invoice_id", "type": "number"}, {"name": "answer", "type": "string"},
              {"name": "invoice_type", "type": "string"}, {"name": "client_message", "type": "string"}, ATTACHMENTS, CABINET_INPUT])
flow.save_attachments([110, 300])
route("Fichiers ou reponse ?", f"{CALL}.mode === 'answer' ? 1 : ({CALL}.mode === 'resume' ? 2 : 0)", 3, [220, 300])
mcp("Fichiers du message (files_to_process)", "files_to_process",
    f"{{ session_id: {CALL}.session_id }}", [330, 200])
node("Un element par fichier", "n8n-nodes-base.code", 2, [440, 200], {"jsCode": (
    "// One item per file to process (files_to_process picked the conversation's invoices and statements not processed yet).\n"
    "return $input.first().json.content[0].text.files.map(file_id => ({ json: { file_id } }));")})
# Work on an existing dossier: the email's sender must have it assigned in Experio (the agent's own chat has no sender).
sender = flow.checked("Expediteur (check_sender)", "check_sender", f"{{ session_id: {CALL}.session_id, dossier_id: {CALL}.dossier_id }}",
                      [260, 120], [300, 160])
connect("Appel", SAVE_ATTACHMENTS)
connect(SAVE_ATTACHMENTS, "Fichiers ou reponse ?", 0)
flow.to_record(SAVE_ATTACHMENTS, 1)
connect("Fichiers ou reponse ?", "Expediteur (check_sender)", 0)
connect(sender, "Fichiers du message (files_to_process)", 0)
flow.to_record(sender, 1)
connect("Fichiers du message (files_to_process)", "Un element par fichier", 0)
flow.to_record("Fichiers du message (files_to_process)", 1)

# Files: identify -> statement import, or split -> drafts.
mcp("Identification (identify_document)", "identify_document", "{ file_id: $json.file_id }", [660, 200])
route("Suite apres identify_document", f"{RESULT}.status !== 'ok' ? 2 : ({RESULT}.kind === 'statement' ? 0 : 1)", 3, [880, 200])
mcp("Releve bancaire (import_bank_statement)", "import_bank_statement",
    f"{{ file_id: {RESULT}.file_id, dossier_id: {CALL}.dossier_id }}", [1100, 40])
# The client says whether the invoices are purchases or sales; split_invoices asks them when they didn't.
mcp("Decoupage (split_invoices)", "split_invoices", (f"{{ file_id: {RESULT}.file_id, invoice_type: {CALL}.invoice_type || '', "
                                                    f"client_message: {CALL}.client_message || '' }}"), [1100, 240])
route("Suite apres split_invoices", OK_ELSE, 2, [1320, 240])
node("Une facture par fichier", "n8n-nodes-base.code", 2, [1540, 200], {"jsCode": (
    "// Each invoice found in the file becomes its own item.\n"
    "return $input.all().flatMap(item => item.json.content[0].text.files.map(file_id => ({ json: { file_id } })));")})
mcp("Brouillon (create_draft_invoice)", "create_draft_invoice",
    f"{{ file_id: $json.file_id, dossier_id: {CALL}.dossier_id }}", [1760, 200])
for source, target, output in [("Un element par fichier", "Identification (identify_document)", 0),
                               ("Identification (identify_document)", "Suite apres identify_document", 0),
                               ("Suite apres identify_document", "Releve bancaire (import_bank_statement)", 0),
                               ("Suite apres identify_document", "Decoupage (split_invoices)", 1),
                               ("Decoupage (split_invoices)", "Suite apres split_invoices", 0),
                               ("Suite apres split_invoices", "Une facture par fichier", 0),
                               ("Une facture par fichier", "Brouillon (create_draft_invoice)", 0)]:
    connect(source, target, output)

# Invoices: the fixed sequence, each step only when the previous one says ok.
CHAIN = [("Brouillon (create_draft_invoice)", "create_draft_invoice"),
         ("Lecture Experio (wait_until_read)", "wait_until_read"),
         ("Verification (check_invoice)", "check_invoice"),
         ("Correction (correct_invoice)", "correct_invoice"),
         ("Classement (classify_invoice)", "classify_invoice"),
         ("Tiers (link_tiers)", "link_tiers"),
         ("Doublon (check_duplicate)", "check_duplicate"),
         ("Compte (assign_account)", "assign_account"),
         ("TVA (settle_tva)", "settle_tva"),
         ("Journal (check_journal)", "check_journal"),
         ("Retenue a la source (settle_withholding)", "settle_withholding"),
         ("Constatee d'avance (settle_prepaid)", "settle_prepaid"),
         ("Exercice (settle_exercice)", "settle_exercice"),
         ("Comptabilisation (post_invoice)", "post_invoice")]
x = 1760
for index, (name, tool) in enumerate(CHAIN):
    if index:
        extra = f", invoice_type: {CALL}.invoice_type || ''" if tool == "classify_invoice" else ""
        mcp(name, tool, f"{{ invoice_id: {RESULT}.invoice_id{extra} }}", [x, 420])
    router = f"Suite apres {tool}"
    route(router, OK_ELSE, 2, [x + 110, 620])
    connect(name, router, 0)
    flow.to_record(name, 1)  # tool error
    if index + 1 < len(CHAIN):
        connect(router, CHAIN[index + 1][0], 0)
    flow.to_record(router, 0, 1)  # every outcome is recorded, so the summary tells all of it
    x += 240

for name in ("Identification (identify_document)", "Decoupage (split_invoices)"):
    flow.to_record(name, 1)
flow.to_record("Suite apres identify_document", 2)
flow.to_record("Suite apres split_invoices", 1)
flow.to_record("Releve bancaire (import_bank_statement)", 0, 1)

# Resume: the conversation's drafts Experio hadn't read yet go back to the reading step.
mcp("Brouillons en attente (waiting_drafts)", "waiting_drafts", f"{{ session_id: {CALL}.session_id }}", [440, 1000])
node("Une facture par brouillon", "n8n-nodes-base.code", 2, [660, 1000], {"jsCode": (
    "// Each waiting draft, shaped like a step's answer so the reading step takes its invoice_id.\n"
    "return $input.first().json.content[0].text.invoices.map(invoice_id => "
    "({ json: { content: [{ type: 'text', text: { status: 'ok', invoice_id } }] } }));")})
connect("Fichiers ou reponse ?", "Brouillons en attente (waiting_drafts)", 2)
connect("Brouillons en attente (waiting_drafts)", "Une facture par brouillon", 0)
flow.to_record("Brouillons en attente (waiting_drafts)", 1)
connect("Une facture par brouillon", "Lecture Experio (wait_until_read)", 0)

# Answer: apply the client's answer to the pending question, then resume at the step after it
# (answer_pending's next_step; output 0, anything else, is only recorded).
RESUMED = ["check_duplicate", "settle_tva", "check_journal", "settle_withholding", "settle_prepaid", "settle_exercice", "post_invoice"]
mcp("Reponse du client (answer_pending)", "answer_pending",
    f"{{ invoice_id: {CALL}.invoice_id, answer: {CALL}.answer }}", [440, 760])
outputs = ", ".join(f"{tool}: {output}" for output, tool in enumerate(RESUMED, 1))
route("Reprendre ou ?", f"{RESULT}.status !== 'ok' ? 0 : ({{ {outputs} }}[{RESULT}.next_step] ?? 0)", len(RESUMED) + 1, [660, 760])
mcp("Expediteur de la reponse (check_sender)", "check_sender",
    f"{{ session_id: {CALL}.session_id, invoice_id: {CALL}.invoice_id }}", [330, 760])
route("Suite apres check_sender de la reponse", OK_ELSE, 2, [380, 800])
connect("Fichiers ou reponse ?", "Expediteur de la reponse (check_sender)", 1)
connect("Expediteur de la reponse (check_sender)", "Suite apres check_sender de la reponse", 0)
flow.to_record("Expediteur de la reponse (check_sender)", 1)
connect("Suite apres check_sender de la reponse", "Reponse du client (answer_pending)", 0)
flow.to_record("Suite apres check_sender de la reponse", 1)
connect("Reponse du client (answer_pending)", "Reprendre ou ?", 0)
flow.to_record("Reponse du client (answer_pending)", 1)
flow.to_record("Reprendre ou ?", 0)
step_node = {tool: name for name, tool in CHAIN}
for output, tool in enumerate(RESUMED, 1):
    connect("Reprendre ou ?", step_node[tool], output)

flow.record_and_summary([x + 200, 640], [220, 1250])
flow.save(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("tarik-traitement.json"),
          "TarikTraitement1", "Tarik - Traitement")
