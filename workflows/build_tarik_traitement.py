"""Build workflows/tarik-traitement.json: the fixed processing sequence as n8n nodes.

Modes: files (the conversation's new files, from identification to posting; with none, the drafts left
waiting or cut short are resumed), resume (those drafts only: "Tarik - Emails" following up, no agent),
answer (the client's answer to what the conversation waits on, then the sequence resumes there; with nothing
waiting, the drafts Experio hadn't read yet are resumed)."""

from steps import (
    CALL,
    EACH_FILE,
    FILES,
    OK_ELSE,
    RESULT,
    SAVE_ATTACHMENTS,
    SENDER,
    TRAITEMENT_INPUTS,
    Workflow,
)

flow = Workflow("tarik-traitement")
connect, mcp, route = flow.connect, flow.mcp, flow.route

flow.trigger(**TRAITEMENT_INPUTS)
# n8n stops an agent's tool after 300 s: past BUDGET_MS, the invoices still going wait for the next run instead,
# leaving the step in flight time to end. A follow-up (mode resume) is no agent's tool: it has no budget.
BUDGET_MS = 240_000
START = "Debut"
flow.code(
    START, [60, 300], "// When the run started, for its time budget.\nreturn [{ json: { started: Date.now() } }];"
)
LATE = f"({CALL}.mode !== 'resume' && Date.now() - $('{START}').first().json.started > {BUDGET_MS})"
flow.save_attachments([110, 300])
route("Fichiers ou reponse ?", f"{{ answer: 1, resume: 2 }}[{CALL}.mode] ?? 0", 3, [220, 300])
flow.files_to_process(
    f"{{ session_id: {CALL}.session_id }}",
    "One item per file to process (files_to_process picked the conversation's invoices and statements "
    "not processed yet).",
    [330, 200],
    [440, 200],
)
# Work on an existing dossier: the email's sender must have it.
sender = flow.check_sender([260, 120], [300, 160])
connect("Appel", START)
connect(START, SAVE_ATTACHMENTS)
connect(SAVE_ATTACHMENTS, "Fichiers ou reponse ?", 0)
flow.to_record(SAVE_ATTACHMENTS, 1)
connect("Fichiers ou reponse ?", SENDER, 0)
connect(sender, FILES, 0)
flow.to_record(sender, 1)
# No new file ("relancez le traitement"): the conversation's drafts left waiting or cut short are resumed.
route("Nouveaux fichiers ?", f"{RESULT}.files.length ? 0 : 1", 2, [440, 120])
connect(FILES, "Nouveaux fichiers ?", 0)
connect("Nouveaux fichiers ?", EACH_FILE, 0)
flow.to_record(FILES, 1)

# Files: identify -> statement import, or split -> drafts.
mcp("Identification (identify_document)", "identify_document", "{ file_id: $json.file_id }", [660, 200])
route(
    "Suite apres identify_document",
    f"{RESULT}.status !== 'ok' ? 2 : ({RESULT}.kind === 'statement' ? 0 : 1)",
    3,
    [880, 200],
)
mcp(
    "Releve bancaire (import_bank_statement)",
    "import_bank_statement",
    f"{{ file_id: {RESULT}.file_id, dossier_id: {CALL}.dossier_id }}",
    [1100, 40],
)
# The client says whether the invoices are purchases or sales; split_invoices asks them when they didn't.
mcp(
    "Decoupage (split_invoices)",
    "split_invoices",
    (
        f"{{ file_id: {RESULT}.file_id, invoice_type: {CALL}.invoice_type || '', "
        f"client_message: {CALL}.client_message || '' }}"
    ),
    [1100, 240],
)
route("Suite apres split_invoices", OK_ELSE, 2, [1320, 240])
flow.code(
    "Une facture par fichier",
    [1540, 200],
    (
        "// Each invoice found in the file becomes its own item.\n"
        "return $input.all().flatMap(item => item.json.content[0].text.files.map(file_id => ({ json: { file_id } })));"
    ),
)
mcp(
    "Brouillon (create_draft_invoice)",
    "create_draft_invoice",
    f"{{ file_id: $json.file_id, dossier_id: {CALL}.dossier_id }}",
    [1760, 200],
)
for source, target, output in [
    (EACH_FILE, "Identification (identify_document)", 0),
    ("Identification (identify_document)", "Suite apres identify_document", 0),
    ("Suite apres identify_document", "Releve bancaire (import_bank_statement)", 0),
    ("Suite apres identify_document", "Decoupage (split_invoices)", 1),
    ("Decoupage (split_invoices)", "Suite apres split_invoices", 0),
    ("Suite apres split_invoices", "Une facture par fichier", 0),
    ("Une facture par fichier", "Brouillon (create_draft_invoice)", 0),
]:
    connect(source, target, output)

# Invoices: the fixed sequence, each step only when the previous one says ok.
CHAIN = [
    ("Brouillon (create_draft_invoice)", "create_draft_invoice"),
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
    ("Comptabilisation (post_invoice)", "post_invoice"),
]
# What a step is given besides its invoice: the run (one reading deadline for all its drafts), the client's type.
EXTRA = {
    "wait_until_read": ", run_id: String($execution.id)",
    "classify_invoice": f", invoice_type: {CALL}.invoice_type || ''",
}
x = 1760
for index, (name, tool) in enumerate(CHAIN):
    if index:
        mcp(name, tool, f"{{ invoice_id: {RESULT}.invoice_id{EXTRA.get(tool, '')} }}", [x, 420])
    router = f"Suite apres {tool}"
    last = index + 1 == len(CHAIN)
    route(
        router, OK_ELSE if last else f"{RESULT}.status !== 'ok' ? 1 : {LATE} ? 2 : 0", 2 if last else 3, [x + 110, 620]
    )
    connect(name, router, 0)
    flow.to_record(name, 1)  # tool error
    if not last:
        connect(router, CHAIN[index + 1][0], 0)
        # Out of time: recorded as waiting at this step (its name in the node's), so waiting_drafts resumes it.
        late = flow.code(
            f"Hors delai ({tool})",
            [x + 110, 800],
            (
                "// The run's time is up: the invoice waits for the next run.\n"
                "return $input.all().map(item => { const invoice_id = item.json.content[0].text.invoice_id; "
                "return { json: { content: [{ type: 'text', text: { status: 'wait', invoice_id, message: "
                "`Facture ${invoice_id} : la suite de son traitement reprendra a votre prochain message.` } }] } }; });"
            ),
        )
        connect(router, late, 2)
        flow.to_record(late, 0)
    flow.to_record(router, 0, 1)  # every outcome is recorded, so the summary tells all of it
    x += 240

for name in ("Identification (identify_document)", "Decoupage (split_invoices)"):
    flow.to_record(name, 1)
flow.to_record("Suite apres identify_document", 2)
flow.to_record("Suite apres split_invoices", 1)
flow.to_record("Releve bancaire (import_bank_statement)", 0, 1)

# Nothing waiting on the client: the conversation's drafts Experio hadn't read yet go back to the reading step.
mcp("Brouillons en attente (waiting_drafts)", "waiting_drafts", f"{{ session_id: {CALL}.session_id }}", [440, 1000])
flow.code(
    "Une facture par brouillon",
    [660, 1000],
    (
        "// Each waiting draft, shaped like a step's answer so the reading step takes its invoice_id.\n"
        "return $input.first().json.content[0].text.invoices.map(invoice_id => "
        "({ json: { content: [{ type: 'text', text: { status: 'ok', invoice_id } }] } }));"
    ),
)
connect("Nouveaux fichiers ?", "Brouillons en attente (waiting_drafts)", 1)
# A follow-up: the sender was checked when these drafts were made.
connect("Fichiers ou reponse ?", "Brouillons en attente (waiting_drafts)", 2)
connect("Brouillons en attente (waiting_drafts)", "Une facture par brouillon", 0)
flow.to_record("Brouillons en attente (waiting_drafts)", 1)
connect("Une facture par brouillon", "Lecture Experio (wait_until_read)", 0)

# Answer: answer_pending finds what the client answers (and checks the email's sender), applies it, then the
# sequence resumes at its next_step; output 0, anything else, is only recorded.
RESUMED = [
    "check_duplicate",
    "settle_tva",
    "check_journal",
    "settle_withholding",
    "settle_prepaid",
    "settle_exercice",
    "post_invoice",
]
ANSWER = "Reponse du client (answer_pending)"
mcp(ANSWER, "answer_pending", f"{{ session_id: {CALL}.session_id, answer: {CALL}.answer }}", [440, 760])
outputs = ", ".join(f"{tool}: {output}" for output, tool in enumerate([*RESUMED, "waiting_drafts"], 1))
route(
    "Reprendre ou ?",
    f"{RESULT}.status !== 'ok' ? 0 : ({{ {outputs} }}[{RESULT}.next_step] ?? 0)",
    len(RESUMED) + 2,
    [660, 760],
)
connect("Fichiers ou reponse ?", ANSWER, 1)
connect(ANSWER, "Reprendre ou ?", 0)
flow.to_record(ANSWER, 1)
flow.to_record("Reprendre ou ?", *range(len(RESUMED) + 2))  # the answer's own outcome too, under its step
step_node = {tool: name for name, tool in CHAIN}
for output, tool in enumerate(RESUMED, 1):
    connect("Reprendre ou ?", step_node[tool], output)
connect("Reprendre ou ?", "Brouillons en attente (waiting_drafts)", len(RESUMED) + 1)

flow.record_and_summary([x + 200, 640], [220, 1250])
flow.save("TarikTraitement1", "Tarik - Traitement")
