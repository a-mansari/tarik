"""Build workflows/tarik-dossier.json: a dossier's creation, configuration and entries import, or (mode update) a change
to an existing dossier's identity fields.

Sequence: create the dossier (unless it exists) -> copy a model dossier's configuration
(when one is given) -> import each FEC or grand livre -> finish the onboarding, unless an
import was refused -> the dossier's state. n8n runs the lower branches last, so the
onboarding comes after the imports and the summary after everything. The onboarding waits on
Experio: when the run's time (n8n stops an agent's tool after 300 s) is too short, the dossier waits
in tarik_setups. Mode resume ("Tarik - Emails" following up, no agent, no time limit) finishes those.

Mode update: the email's sender must have the dossier (the agent's own chat has no sender), then update_dossier
changes only the fields given, and only to values the client wrote."""

from steps import CALL, DOSSIER_INPUTS, EACH_FILE, FILES, RESULT, SENDER, START, Workflow

CREATE = "Creation (create_dossier)"
COPY = "Configuration (copy_configuration)"
TARGET_NODE = "Dossier cible"
IMPORT = "Ecritures (import_entries)"
UPDATE = "Modification (update_dossier)"
IDENTITY = [
    "name",
    "folder_type",
    "ice",
    "if_number",
    "rc",
    "patente",
    "tsc",
    "capital",
    "cnss",
    "street",
    "street2",
    "city",
    "zip",
]
TARGET = f"$('{TARGET_NODE}').first().json.dossier_id"
CREATED = f"$('{CREATE}').first().json.content[0].text.dossier_id"

flow = Workflow("tarik-dossier")
connect, mcp, route = flow.connect, flow.mcp, flow.route

flow.trigger(**DOSSIER_INPUTS)
# The onboarding ends by BUDGET_MS from the start, leaving the run time to record and sum up.
BUDGET_MS = 260_000
SECONDS_LEFT = f"Math.max(1, Math.round(({BUDGET_MS} - (Date.now() - $('{START}').first().json.started)) / 1000))"
flow.start("Nouveau dossier ?")
route(
    "Nouveau dossier ?",
    f"{{ resume: 2, update: 3 }}[{CALL}.mode] ?? ({CALL}.dossier_id > 0 ? 1 : 0)",
    4,
    [220, 300],
)

# A new dossier: created, then its configuration copied from the model, before any import.
created = flow.checked(
    CREATE,
    "create_dossier",
    (
        f"{{ name: {CALL}.name, ice: {CALL}.ice || '', if_number: {CALL}.if_number || '', "
        f"folder_type: {CALL}.folder_type || 'company', skip_ice_if_check: {CALL}.skip_ice_if_check === true, "
        f"client_confirmed: {CALL}.client_confirmed === true, client_message: {CALL}.client_message || '', "
        f"session_id: {CALL}.session_id }}"
    ),
    [440, 200],
    [660, 200],
)
# A dossier reused from earlier in the conversation keeps its configuration.
route("Modele ?", f"{CALL}.model_dossier_id > 0 && !{RESULT}.reused ? 0 : 1", 2, [880, 200])
copied = flow.checked(
    COPY,
    "copy_configuration",
    (f"{{ dossier_id: {CREATED}, model_dossier_id: {CALL}.model_dossier_id }}"),
    [1100, 120],
    [1320, 120],
)
flow.code(
    TARGET_NODE,
    [1540, 300],
    (
        "// The dossier the rest works on: the one given, or the one just created.\n"
        f"const call = {CALL};\n"
        f"return [{{ json: {{ dossier_id: call.dossier_id > 0 ? call.dossier_id : {CREATED} }} }}];"
    ),
)
connect("Nouveau dossier ?", CREATE, 0)
# An existing dossier, to set up or to change: the email's sender must have it.
sender = flow.check_sender([440, 420], [660, 420])
connect("Nouveau dossier ?", SENDER, 1)
connect("Nouveau dossier ?", SENDER, 3)
route("Modifier ?", f"{CALL}.mode === 'update' ? 0 : 1", 2, [880, 420])
connect(sender, "Modifier ?", 0)
connect("Modifier ?", TARGET_NODE, 1)
identity = ", ".join(f"{field}: {CALL}.{field} ?? ''" for field in IDENTITY)
mcp(
    UPDATE,
    "update_dossier",
    (
        f"{{ dossier_id: {CALL}.dossier_id, {identity}, skip_ice_if_check: {CALL}.skip_ice_if_check === true, "
        f"client_confirmed: {CALL}.client_confirmed === true, client_message: {CALL}.client_message || '', "
        f"session_id: {CALL}.session_id }}"
    ),
    [1100, 520],
)
connect("Modifier ?", UPDATE, 0)
flow.to_record(UPDATE, 0, 1)
connect(created, "Modele ?", 0)
flow.to_record(created, 0, 1)
connect("Modele ?", COPY, 0)
connect("Modele ?", TARGET_NODE, 1)
connect(copied, TARGET_NODE, 0)
flow.to_record(copied, 0, 1)

# Upper branch, run first: each file of the message imported.
flow.files_to_process(
    f"{{ session_id: {CALL}.session_id, entries: true }}",
    "One item per file to import (files_to_process picked the conversation's FEC and grand livre files "
    "not imported yet).",
    [1760, 200],
    [1980, 200],
)
mcp(
    IMPORT,
    "import_entries",
    f"{{ dossier_id: {TARGET}, file_id: $json.file_id, kind: {CALL}.kind || '' }}",
    [2200, 200],
)
connect(TARGET_NODE, FILES)
connect(FILES, EACH_FILE, 0)
flow.to_record(FILES, 1)
connect(EACH_FILE, IMPORT)
flow.to_record(IMPORT, 0, 1)

# Lower branch, run after the imports: the onboarding, unless an import was refused.
flow.code(
    "Imports acceptes ?",
    [1760, 460],
    (
        "// Finishing hands the entries to Experio: not with a refused file, which the client sends again.\n"
        f"const imports = $('{IMPORT}').isExecuted ? [...$('{IMPORT}').all(0), ...$('{IMPORT}').all(1)] : [];"
        "  // answers and errors\n"
        "const refused = imports.some(item => item.json.error || item.json.content?.[0]?.text?.status !== 'ok');\n"
        "return refused ? [] : [{ json: {} }];"
    ),
)
mcp(
    "Integration (finish_setup)",
    "finish_setup",
    f"{{ dossier_id: {TARGET}, seconds_left: {SECONDS_LEFT}, finish: {CALL}.finish === true }}",
    [1980, 460],
)
# The bank of each bank journal, asked after the imports (a FEC or a journals file names none).
mcp("Banques (ask_banks)", "ask_banks", f"{{ dossier_id: {TARGET} }}", [1980, 340])
connect("Imports acceptes ?", "Banques (ask_banks)")
flow.to_record("Banques (ask_banks)", 0, 1)
# Its state once finished; a dossier left waiting gets it with its follow-up.
route("Integration terminee ?", f"{RESULT}.status === 'ok' ? 0 : 1", 2, [2090, 560])
mcp("Etat du dossier (setup_status)", "setup_status", f"{{ dossier_id: {TARGET} }}", [2200, 560])
connect(TARGET_NODE, "Imports acceptes ?")
connect("Imports acceptes ?", "Integration (finish_setup)")
connect("Integration (finish_setup)", "Integration terminee ?", 0)
connect("Integration terminee ?", "Etat du dossier (setup_status)", 0)
flow.to_record("Integration (finish_setup)", 0, 1)
flow.to_record("Etat du dossier (setup_status)", 0, 1)

# Mode resume: the conversation's dossiers left waiting, each finished (no time limit) then its state.
# A follow-up: the sender was checked when their entries were imported.
mcp("Integrations en attente (waiting_setups)", "waiting_setups", f"{{ session_id: {CALL}.session_id }}", [440, 640])
flow.code(
    "Un element par dossier",
    [660, 640],
    "// One item per dossier left waiting.\n"
    "return $input.first().json.content[0].text.dossiers.map(dossier_id => ({ json: { dossier_id } }));",
)
mcp("Integration reprise (finish_setup)", "finish_setup", "{ dossier_id: $json.dossier_id }", [880, 640])
route("Reprise terminee ?", f"{RESULT}.status === 'ok' ? 0 : 1", 2, [990, 740])
mcp("Etat du dossier repris (setup_status)", "setup_status", f"{{ dossier_id: {RESULT}.dossier_id }}", [1100, 740])
connect("Nouveau dossier ?", "Integrations en attente (waiting_setups)", 2)
connect("Integrations en attente (waiting_setups)", "Un element par dossier", 0)
flow.to_record("Integrations en attente (waiting_setups)", 1)
connect("Un element par dossier", "Integration reprise (finish_setup)")
connect("Integration reprise (finish_setup)", "Reprise terminee ?", 0)
connect("Reprise terminee ?", "Etat du dossier repris (setup_status)", 0)
flow.to_record("Integration reprise (finish_setup)", 0, 1)
flow.to_record("Etat du dossier repris (setup_status)", 0, 1)

flow.record_and_summary([2500, 400], [220, 800])
flow.save("TarikDossier0001", "Tarik - Dossier")
