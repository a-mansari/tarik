"""Build workflows/tarik-dossier.json: a dossier's creation, configuration and entries import.

Sequence: create the dossier (unless it exists) -> copy a model dossier's configuration
(when one is given) -> import each FEC or grand livre -> finish the onboarding, unless an
import was refused -> the dossier's state. n8n runs the lower branches last, so the
onboarding comes after the imports and the summary after everything."""

import sys
from pathlib import Path

from steps import ATTACHMENTS, CABINET_INPUT, CALL, RESULT, SAVE_ATTACHMENTS, Workflow

CREATE = "Creation (create_dossier)"
COPY = "Configuration (copy_configuration)"
SENDER = "Expediteur (check_sender)"
TARGET_NODE = "Dossier cible"
IMPORT = "Ecritures (import_entries)"
TARGET = f"$('{TARGET_NODE}').first().json.dossier_id"
CREATED = f"$('{CREATE}').first().json.content[0].text.dossier_id"

flow = Workflow("tarik-dossier")
node, connect, mcp, route = flow.node, flow.connect, flow.mcp, flow.route

flow.trigger([{"name": "session_id", "type": "string"}, {"name": "dossier_id", "type": "number"},
              {"name": "name", "type": "string"}, {"name": "ice", "type": "string"},
              {"name": "if_number", "type": "string"}, {"name": "folder_type", "type": "string"},
              {"name": "skip_ice_if_check", "type": "boolean"}, {"name": "client_message", "type": "string"},
              {"name": "model_dossier_id", "type": "number"}, {"name": "kind", "type": "string"}, ATTACHMENTS, CABINET_INPUT])
flow.save_attachments([110, 300])
route("Nouveau dossier ?", f"{CALL}.dossier_id > 0 ? 1 : 0", 2, [220, 300])
connect("Appel", SAVE_ATTACHMENTS)
connect(SAVE_ATTACHMENTS, "Nouveau dossier ?", 0)
flow.to_record(SAVE_ATTACHMENTS, 1)

# A new dossier: created, then its configuration copied from the model, before any import.
created = flow.checked(CREATE, "create_dossier", (
    f"{{ name: {CALL}.name, ice: {CALL}.ice || '', if_number: {CALL}.if_number || '', "
    f"folder_type: {CALL}.folder_type || 'company', skip_ice_if_check: {CALL}.skip_ice_if_check === true, "
    f"client_message: {CALL}.client_message || '', session_id: {CALL}.session_id }}"), [440, 200], [660, 200])
# A dossier reused from earlier in the conversation keeps its configuration.
route("Modele ?", f"{CALL}.model_dossier_id > 0 && !{RESULT}.reused ? 0 : 1", 2, [880, 200])
copied = flow.checked(COPY, "copy_configuration", (
    f"{{ dossier_id: {CREATED}, model_dossier_id: {CALL}.model_dossier_id }}"), [1100, 120], [1320, 120])
node(TARGET_NODE, "n8n-nodes-base.code", 2, [1540, 300], {"jsCode": (
    "// The dossier the rest works on: the one given, or the one just created.\n"
    f"const call = {CALL};\n"
    f"return [{{ json: {{ dossier_id: call.dossier_id > 0 ? call.dossier_id : {CREATED} }} }}];")})
connect("Nouveau dossier ?", CREATE, 0)
# An existing dossier: the email's sender must have it assigned in Experio (the agent's own chat has no sender).
sender = flow.checked(SENDER, "check_sender", f"{{ session_id: {CALL}.session_id, dossier_id: {CALL}.dossier_id }}",
                      [440, 420], [660, 420])
connect("Nouveau dossier ?", SENDER, 1)
connect(sender, TARGET_NODE, 0)
flow.to_record(sender, 1)
connect(created, "Modele ?", 0)
flow.to_record(created, 0, 1)
connect("Modele ?", COPY, 0)
connect("Modele ?", TARGET_NODE, 1)
connect(copied, TARGET_NODE, 0)
flow.to_record(copied, 0, 1)

# Upper branch, run first: each file of the message imported.
mcp("Fichiers du message (files_to_process)", "files_to_process",
    f"{{ session_id: {CALL}.session_id, entries: true }}", [1760, 200])
node("Un element par fichier", "n8n-nodes-base.code", 2, [1980, 200], {"jsCode": (
    "// One item per file to import (files_to_process picked the conversation's FEC and grand livre files not imported yet).\n"
    "return $input.first().json.content[0].text.files.map(file_id => ({ json: { file_id } }));")})
mcp(IMPORT, "import_entries", f"{{ dossier_id: {TARGET}, file_id: $json.file_id, kind: {CALL}.kind || '' }}", [2200, 200])
connect(TARGET_NODE, "Fichiers du message (files_to_process)")
connect("Fichiers du message (files_to_process)", "Un element par fichier", 0)
flow.to_record("Fichiers du message (files_to_process)", 1)
connect("Un element par fichier", IMPORT)
flow.to_record(IMPORT, 0, 1)

# Lower branch, run after the imports: the onboarding, unless an import was refused.
node("Imports acceptes ?", "n8n-nodes-base.code", 2, [1760, 460], {"jsCode": (
    "// Finishing hands the entries to Experio: not with a refused file, which the client sends again.\n"
    f"const imports = $('{IMPORT}').isExecuted ? [...$('{IMPORT}').all(0), ...$('{IMPORT}').all(1)] : [];  // answers and errors\n"
    "const refused = imports.some(item => item.json.error || item.json.content?.[0]?.text?.status !== 'ok');\n"
    "return refused ? [] : [{ json: {} }];")})
mcp("Integration (finish_setup)", "finish_setup", f"{{ dossier_id: {TARGET} }}", [1980, 460])
mcp("Etat du dossier (setup_status)", "setup_status", f"{{ dossier_id: {TARGET} }}", [2200, 560])
connect(TARGET_NODE, "Imports acceptes ?")
connect("Imports acceptes ?", "Integration (finish_setup)")
connect("Integration (finish_setup)", "Etat du dossier (setup_status)", 0)
flow.to_record("Integration (finish_setup)", 0, 1)
flow.to_record("Etat du dossier (setup_status)", 0, 1)

flow.record_and_summary([2500, 400], [220, 800])
flow.save(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("tarik-dossier.json"),
          "TarikDossier0001", "Tarik - Dossier")
