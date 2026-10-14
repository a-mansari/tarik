"""Build workflows/tarik-traitement.json: the fixed processing sequence as n8n nodes.

Edit this script, not the JSON: python3 workflows/build_tarik_traitement.py
Node ids are derived from node names, so a rebuild only changes what changed."""

import json
import sys
import uuid
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("tarik-traitement.json")
MCP_URL = "http://tarik-mcp:8000/mcp"
nodes, connections = [], {}


def nid(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tarik-traitement/{name}"))


def node(name, type_, version, position, parameters, **extra):
    nodes.append({"id": nid(name), "name": name, "type": type_, "typeVersion": version,
                  "position": position, "parameters": parameters, **extra})
    return name


def connect(source, target, output=0):
    outputs = connections.setdefault(source, {}).setdefault("main", [])
    while len(outputs) <= output:
        outputs.append([])
    outputs[output].append({"node": target, "type": "main", "index": 0})


CALL = "$('Appel').first().json"
RESULT = "$json.content[0].text"


def mcp(name, tool, arguments, position):
    """One step: an MCP call. Output 0 = its answer, output 1 = a tool error."""
    return node(name, "@n8n/n8n-nodes-langchain.mcpClient", 1.1, position, {
        "endpointUrl": MCP_URL, "serverTransport": "httpStreamable",
        "tool": {"__rl": True, "mode": "id", "value": tool},
        "inputMode": "json", "jsonInput": "={{ JSON.stringify(" + arguments + ") }}",
        "options": {"timeout": 300000},
    }, onError="continueErrorOutput")


def route(name, expression, outputs, position):
    return node(name, "n8n-nodes-base.switch", 3.2, position,
                {"mode": "expression", "numberOutputs": outputs, "output": "={{ " + expression + " }}"})


RECORD = "Enregistrer"
OK_ELSE = f"{RESULT}.status === 'ok' ? 0 : 1"

node("Appel", "n8n-nodes-base.executeWorkflowTrigger", 1.1, [0, 300], {
    "inputSource": "workflowInputs",
    "workflowInputs": {"values": [{"name": "mode", "type": "string"}, {"name": "session_id", "type": "string"},
                                  {"name": "dossier_id", "type": "number"}, {"name": "file_ids", "type": "any"},
                                  {"name": "invoice_id", "type": "number"}, {"name": "answer", "type": "string"}]},
})
route("Fichiers ou reponse ?", "$json.mode === 'answer' ? 1 : ($json.mode === 'resume' ? 2 : 0)", 3, [220, 300])
mcp("Fichiers du message (files_to_process)", "files_to_process",
    f"{{ session_id: {CALL}.session_id, file_ids: {CALL}.file_ids ?? null }}", [330, 200])
node("Un element par fichier", "n8n-nodes-base.code", 2, [440, 200], {"jsCode": (
    "// One item per file to process (files_to_process already checked they belong to the conversation).\n"
    "return $input.first().json.content[0].text.files.map(file_id => ({ json: { file_id } }));")})
connect("Appel", "Fichiers ou reponse ?")
connect("Fichiers ou reponse ?", "Fichiers du message (files_to_process)", 0)
connect("Fichiers du message (files_to_process)", "Un element par fichier", 0)
connect("Fichiers du message (files_to_process)", RECORD, 1)

# Files: identify -> statement import, or split -> drafts.
mcp("Identification (identify_document)", "identify_document", "{ file_id: $json.file_id }", [660, 200])
route("Suite apres identify_document", f"{RESULT}.status !== 'ok' ? 2 : ({RESULT}.kind === 'statement' ? 0 : 1)", 3, [880, 200])
mcp("Releve bancaire (import_bank_statement)", "import_bank_statement",
    f"{{ file_id: {RESULT}.file_id, dossier_id: {CALL}.dossier_id }}", [1100, 40])
mcp("Decoupage (split_invoices)", "split_invoices", f"{{ file_id: {RESULT}.file_id }}", [1100, 240])
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
        mcp(name, tool, f"{{ invoice_id: {RESULT}.invoice_id }}", [x, 420])
    router = f"Suite apres {tool}"
    route(router, OK_ELSE, 2, [x + 110, 620])
    connect(name, router, 0)
    connect(name, RECORD, 1)  # tool error
    if index + 1 < len(CHAIN):
        connect(router, CHAIN[index + 1][0], 0)
        connect(router, RECORD, 0)  # every outcome is recorded, so the summary tells all of it
        connect(router, RECORD, 1)
    else:
        connect(router, RECORD, 0)  # end of the sequence: recorded as done
        connect(router, RECORD, 1)
    x += 240

for name in ("Identification (identify_document)", "Decoupage (split_invoices)"):
    connect(name, RECORD, 1)
connect("Suite apres identify_document", RECORD, 2)
connect("Suite apres split_invoices", RECORD, 1)
connect("Releve bancaire (import_bank_statement)", RECORD, 0)
connect("Releve bancaire (import_bank_statement)", RECORD, 1)

# Resume: the conversation's drafts Experio hadn't read yet go back to the reading step.
mcp("Brouillons en attente (waiting_drafts)", "waiting_drafts", f"{{ session_id: {CALL}.session_id }}", [440, 1000])
node("Une facture par brouillon", "n8n-nodes-base.code", 2, [660, 1000], {"jsCode": (
    "// Each waiting draft, shaped like a step's answer so the reading step takes its invoice_id.\n"
    "return $input.first().json.content[0].text.invoices.map(invoice_id => "
    "({ json: { content: [{ type: 'text', text: { status: 'ok', invoice_id } }] } }));")})
connect("Fichiers ou reponse ?", "Brouillons en attente (waiting_drafts)", 2)
connect("Brouillons en attente (waiting_drafts)", "Une facture par brouillon", 0)
connect("Brouillons en attente (waiting_drafts)", RECORD, 1)
connect("Une facture par brouillon", "Lecture Experio (wait_until_read)", 0)

# Answer: apply the client's answer to the pending question, then resume where it left off.
mcp("Reponse du client (answer_pending)", "answer_pending",
    f"{{ invoice_id: {CALL}.invoice_id, answer: {CALL}.answer }}", [440, 760])
route("Reprendre ou ?", (f"{RESULT}.status !== 'ok' ? 0 : ({{ check_duplicate: 1, settle_tva: 2, check_journal: 3, settle_withholding: 4, settle_prepaid: 5, settle_exercice: 6, post_invoice: 7 }}[{RESULT}.next_step] ?? 0)"),
      8, [660, 760])
connect("Fichiers ou reponse ?", "Reponse du client (answer_pending)", 1)
connect("Reponse du client (answer_pending)", "Reprendre ou ?", 0)
connect("Reponse du client (answer_pending)", RECORD, 1)
connect("Reprendre ou ?", RECORD, 0)
connect("Reprendre ou ?", "Doublon (check_duplicate)", 1)
connect("Reprendre ou ?", "TVA (settle_tva)", 2)
connect("Reprendre ou ?", "Journal (check_journal)", 3)
connect("Reprendre ou ?", "Retenue a la source (settle_withholding)", 4)
connect("Reprendre ou ?", "Constatee d'avance (settle_prepaid)", 5)
connect("Reprendre ou ?", "Exercice (settle_exercice)", 6)
connect("Reprendre ou ?", "Comptabilisation (post_invoice)", 7)

mcp(RECORD, "record_step", (
    f"{{ run_id: String($execution.id), session_id: {CALL}.session_id, origin: $prevNode.name, "
    f"result: $json.error ? {{ status: 'error', message: String($json.error.message ?? $json.error) }} : {RESULT} }}"),
    [x + 200, 640])

# Last (lowest branch, so n8n runs it after the whole sequence): the run's summary is the answer.
mcp("Resume", "run_summary", "{ run_id: String($execution.id) }", [220, 1250])
connect("Appel", "Resume")

workflow = {"id": "TarikTraitement1", "name": "Tarik - Traitement", "active": False,
            "settings": {"executionOrder": "v1", "callerPolicy": "workflowsFromSameOwner"},
            "nodes": nodes, "connections": connections}
OUT.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")
print(len(nodes), "nodes")
