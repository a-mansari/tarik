"""Build workflows/tarik-modification.json: changing an existing dossier's fields.

Edit this script, not the JSON: python3 workflows/build_tarik_modification.py
The email's sender must have the dossier (the agent's own chat has no sender), then update_dossier
changes only the fields given, and only to values the client wrote. The run's summary is the answer."""

import json
import sys
from pathlib import Path

from steps import CABINET, CALL, OK_ELSE, RESULT, Workflow

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("tarik-modification.json")
flow = Workflow("tarik-modification")
node, connect, mcp, route = flow.node, flow.connect, flow.mcp, flow.route

RECORD = "Enregistrer"
FIELDS = ["name", "folder_type", "ice", "if_number", "rc", "patente", "tsc", "capital", "cnss", "street", "street2", "city", "zip"]

node("Appel", "n8n-nodes-base.executeWorkflowTrigger", 1.1, [0, 300], {
    "inputSource": "workflowInputs",
    "workflowInputs": {"values": [{"name": "session_id", "type": "string"}, {"name": "dossier_id", "type": "number"},
                                  *({"name": field, "type": "string"} for field in FIELDS),
                                  {"name": "skip_ice_if_check", "type": "boolean"}, {"name": "client_message", "type": "string"},
                                  CABINET]},
})
mcp("Expediteur (check_sender)", "check_sender", f"{{ session_id: {CALL}.session_id, dossier_id: {CALL}.dossier_id }}", [220, 300])
route("Suite apres check_sender", OK_ELSE, 2, [440, 300])
arguments = ", ".join(f"{field}: {CALL}.{field} ?? ''" for field in FIELDS)
mcp("Modification (update_dossier)", "update_dossier",
    (f"{{ dossier_id: {CALL}.dossier_id, {arguments}, skip_ice_if_check: {CALL}.skip_ice_if_check === true, "
     f"client_message: {CALL}.client_message || '', session_id: {CALL}.session_id }}"), [660, 300])
connect("Appel", "Expediteur (check_sender)")
connect("Expediteur (check_sender)", "Suite apres check_sender", 0)
connect("Expediteur (check_sender)", RECORD, 1)
connect("Suite apres check_sender", "Modification (update_dossier)", 0)
connect("Suite apres check_sender", RECORD, 1)
connect("Modification (update_dossier)", RECORD, 0)
connect("Modification (update_dossier)", RECORD, 1)

mcp(RECORD, "record_step", (
    f"{{ run_id: String($execution.id), session_id: {CALL}.session_id, origin: $prevNode.name, "
    f"result: $json.error ? {{ status: 'error', message: String($json.error.message ?? $json.error) }} : {RESULT} }}"),
    [900, 300])

# Last (lowest branch): the run's summary is the answer.
mcp("Resume", "run_summary", "{ run_id: String($execution.id) }", [220, 600])
connect("Appel", "Resume")

workflow = {"id": "TarikModif000001", "name": "Tarik - Modification", "active": False,
            "settings": {"executionOrder": "v1", "callerPolicy": "workflowsFromSameOwner", "saveExecutionProgress": True},
            "nodes": flow.nodes, "connections": flow.connections}
OUT.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")
print(len(flow.nodes), "nodes")
