"""What the workflow builders share: n8n nodes, their connections, and the MCP step.

Each workflow's JSON is built by its script (build_tarik_*.py): edit the script, never the JSON
(tests/style checks they match)."""

import json
import sys
import uuid
from pathlib import Path
from types import MappingProxyType
from typing import Final

# tarik-mcp serves each cabinet at its own address only (mcp/server.py).
MCP_ADDRESS = "http://tarik-mcp:8000/c/{}/mcp"
CALL: Final = "$('Appel').first().json"
RESULT: Final = "$json.content[0].text"
OK_ELSE: Final = f"{RESULT}.status === 'ok' ? 0 : 1"
# The native n8n agent's chat files: an "Appel" input, filled by n8n (n8n/patch.js), never by the model.
ATTACHMENTS: Final = MappingProxyType({"attachments": "string"})
# The cabinet a shared workflow works for: an "Appel" input, fixed by its caller (never chosen by a model).
CABINET_INPUT: Final = MappingProxyType({"cabinet": "string"})
# Each workflow's "Appel" inputs, in order: its builder declares them, and every caller fills them all.
TRAITEMENT_INPUTS: Final = MappingProxyType(
    {
        "mode": "string",
        "session_id": "string",
        "dossier_id": "number",
        "answer": "string",
        "decision": "string",  # what the agent read in the client's answer (answer_pending)
        "about": "string",  # with several questions waiting, the invoice or statement the client answers
        "invoice_type": "string",
        "client_message": "string",
        # What the client wrote with the invoices, for every invoice of the message (each empty when they didn't):
        "tva_non_recuperable": "string",  # oui / non, the agent's reading of "TVA non recuperable" / "recuperable"
        "retenue": "string",  # their words on the retenue a la source ("sans retenue", "10 et 75", "a la charge...")
        "repartir": "string",  # oui / non: charges constatees d'avance
        "compte": "string",  # the account they named (code or label)
        "mode_paiement": "string",  # especes, cheque, virement, effet, prelevement, compensation
        "exercice": "string",  # "nouveau" or the open exercice named, for an invoice outside the open ones
        **ATTACHMENTS,
        **CABINET_INPUT,
    }
)
DOSSIER_INPUTS: Final = MappingProxyType(
    {
        "mode": "string",
        "session_id": "string",
        "dossier_id": "number",
        "name": "string",
        "ice": "string",
        "if_number": "string",
        "folder_type": "string",
        "skip_ice_if_check": "boolean",
        "client_confirmed": "boolean",  # the agent read a clear yes to the dossier's last question
        "client_message": "string",
        "model_dossier_id": "number",
        "kind": "string",
        "finish": "boolean",  # the client said every setup file was sent: finish the onboarding
        # Mode person: a collaborateur or a client to create and put on the dossier.
        "person_kind": "string",
        "person_name": "string",
        "email": "string",
        "phone": "string",
        "profile": "string",
        # Mode update: the other identity fields a client may change (name, folder_type, ice, if_number above).
        "rc": "string",
        "patente": "string",
        "tsc": "string",
        "capital": "string",
        "cnss": "string",
        "street": "string",
        "street2": "string",
        "city": "string",
        "zip": "string",
        **ATTACHMENTS,
        **CABINET_INPUT,
    }
)
SAVE_ATTACHMENTS = "Pieces jointes de l'agent"
START: Final = "Debut"
RECORD = "Enregistrer"  # records each step's outcome; the run's summary is made from them
SENDER: Final = "Expediteur (check_sender)"
FILES: Final = "Fichiers du message (files_to_process)"
EACH_FILE: Final = "Un element par fichier"
END = "Fin des etapes"
NEVER = "Jamais execute"  # keeps "Fin des etapes" waiting: see record_and_summary


class Workflow:
    """One workflow being built: its nodes and connections, saved as n8n's JSON."""

    def __init__(self, prefix, cabinet=CALL + ".cabinet"):
        """``prefix``: the workflow's file name without .json, also the seed of its node ids. ``cabinet``: where
        its MCP steps read their cabinet, by default its "Appel" input, fixed by its caller (no cabinet: the
        steps fail, never reach another cabinet)."""
        self.prefix, self.nodes, self.connections = prefix, [], {}
        self.mcp_url = "=" + MCP_ADDRESS.format("{{ " + cabinet + " }}")

    def node(self, name, type_, version, position, parameters, **extra):
        """Add a node; returns its name. Node ids are derived from node names, so a rebuild only changes what
        changed."""
        node_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.prefix}/{name}"))
        self.nodes.append(
            {
                "id": node_id,
                "name": name,
                "type": type_,
                "typeVersion": version,
                "position": position,
                "parameters": parameters,
                **extra,
            }
        )
        return name

    def connect(self, source, target, output=0):
        """Connect output ``output`` of node ``source`` to node ``target``."""
        outputs = self.connections.setdefault(source, {}).setdefault("main", [])
        while len(outputs) <= output:
            outputs.append([])
        outputs[output].append({"node": target, "type": "main", "index": 0})

    def mcp(self, name, tool, arguments, position, timeout=300, **extra):
        """One step: an MCP call of ``tool`` with ``arguments`` (a JS object expression), given up after
        ``timeout`` seconds. Output 0 = its answer, output 1 = a tool error. ``extra``: n8n's node settings
        (retryOnFail...). Returns its name."""
        parameters = {
            "endpointUrl": self.mcp_url,
            "serverTransport": "httpStreamable",
            "tool": {"__rl": True, "mode": "id", "value": tool},
            "inputMode": "json",
            "jsonInput": "={{ JSON.stringify(" + arguments + ") }}",
            "options": {"timeout": timeout * 1000},
        }
        return self.node(
            name,
            "@n8n/n8n-nodes-langchain.mcpClient",
            1.1,
            position,
            parameters,
            onError="continueErrorOutput",
            **extra,
        )

    def route(self, name, expression, outputs, position):
        """A switch node sending each item to output number ``expression`` (a JS expression), of ``outputs``."""
        parameters = {"mode": "expression", "numberOutputs": outputs, "output": "={{ " + expression + " }}"}
        return self.node(name, "n8n-nodes-base.switch", 3.2, position, parameters)

    def code(self, name, position, js):
        """A code node running ``js``; returns its name."""
        return self.node(name, "n8n-nodes-base.code", 2, position, {"jsCode": js})

    def start(self, router):
        """The run's start, then ``router``: "Debut" notes when it started (for its time budget), then the native
        agent's chat files (base64 of { thread_id, files }) are saved once each in the cabinet's tarik_files
        (emails save theirs as they arrive, and send none). A failed save is recorded."""
        started = "// When the run started, for its time budget.\nreturn [{ json: { started: Date.now() } }];"
        self.code(START, [60, 300], started)
        self.mcp(SAVE_ATTACHMENTS, "save_files", "{ payload: " + CALL + ".attachments || '' }", [110, 300])
        self.connect("Appel", START)
        self.connect(START, SAVE_ATTACHMENTS)
        self.connect(SAVE_ATTACHMENTS, router, 0)
        self.to_record(SAVE_ATTACHMENTS, 1)

    def trigger(self, **inputs):
        """ "Appel": the workflow called as a tool, with these inputs (name="type", in order; fixed by the caller,
        or filled by the model)."""
        values = [{"name": name, "type": type_} for name, type_ in inputs.items()]
        parameters = {"inputSource": "workflowInputs", "workflowInputs": {"values": values}}
        return self.node("Appel", "n8n-nodes-base.executeWorkflowTrigger", 1.1, [0, 300], parameters)

    def to_record(self, name, *outputs):
        """Each of these outputs of ``name`` (0: its answer, 1: a tool error) recorded."""
        for output in outputs:
            self.connect(name, RECORD, output)

    def checked(self, name, tool, arguments, position, router_position):
        """An MCP step whose answer goes on only when ok: its router's output 0 is ok, 1 anything else.
        A tool error is recorded. Returns the router's name."""
        router = f"Suite apres {tool}"
        self.mcp(name, tool, arguments, position)
        self.route(router, OK_ELSE, 2, router_position)
        self.connect(name, router, 0)
        self.to_record(name, 1)
        return router

    def check_sender(self, position, router_position):
        """The email's sender must have the called dossier assigned in Experio (the agent's own chat has no
        sender): a checked step, a refusal recorded. Returns its router's name."""
        arguments = f"{{ session_id: {CALL}.session_id, dossier_id: {CALL}.dossier_id }}"
        router = self.checked(SENDER, "check_sender", arguments, position, router_position)
        self.to_record(router, 1)
        return router

    def files_to_process(self, arguments, comment, position, split_position):
        """The conversation's files to work on (files_to_process with ``arguments``), then one item per file,
        ``comment`` saying which. Not connected to each other."""
        self.mcp(FILES, "files_to_process", arguments, position)
        each = "return $input.first().json.content[0].text.files.map(file_id => ({ json: { file_id } }));"
        self.code(EACH_FILE, split_position, f"// {comment}\n{each}")

    def record_and_summary(self, record_position, summary_position):
        """The "Enregistrer" step, then, once every step has ended, the run's summary, which is the workflow's
        answer."""
        # A failed step's error item carries the item it was given (n8n merges it in): its invoice or file.
        given = "$json.content?.[0]?.text"
        arguments = (
            f"{{ run_id: String($execution.id), session_id: {CALL}.session_id, origin: $prevNode.name, "
            f"result: $json.error ? {{ status: 'error', message: String($json.error.message ?? $json.error), "
            f"invoice_id: {given}?.invoice_id, file_id: {given}?.file_id ?? $json.file_id }} : {RESULT} }}"
        )
        self.mcp(RECORD, "record_step", arguments, record_position)
        x, y = summary_position
        self.mcp("Résumé", "run_summary", "{ run_id: String($execution.id) }", [x + 220, y])
        self.at_the_end("Résumé", [x, y])

    def at_the_end(self, target, position):
        """``target`` run once every other node has ended, wherever it sits in the editor: "Appel", through
        "Fin des etapes". tests/n8n/test_at_the_end.py checks n8n still does it (tarik-merge-test.json)."""
        # n8n holds a node with two inputs that got data on one only, and runs it once nothing else is left to run
        # (workflow-execute.js, executionOrder v1). The Merge gets "Appel" on input 0; input 1 comes from a node
        # nothing runs.
        x, y = position
        self.node(END, "n8n-nodes-base.merge", 3.2, [x, y], {})
        self.node(NEVER, "n8n-nodes-base.noOp", 1, [x - 220, y + 100], {})
        self.connect("Appel", END)
        self.connections[NEVER] = {"main": [[{"node": END, "type": "main", "index": 1}]]}
        self.connect(END, target)

    def save(self, workflow_id, name, settings=None):
        """Write the workflow to the path given on the command line, else to <prefix>.json next to this file."""
        path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name(f"{self.prefix}.json")
        # The layout arranged in n8n's editor (save_layout.py), over the positions written here.
        layout_file = Path(__file__).with_name("layout.json")
        layout = json.loads(layout_file.read_text()).get(workflow_id, {}) if layout_file.exists() else {}
        for node in self.nodes:
            node["position"] = layout.get(node["name"], node["position"])
        settings = settings or {
            "executionOrder": "v1",
            "callerPolicy": "workflowsFromSameOwner",
            "saveExecutionProgress": True,
        }
        workflow = {
            "id": workflow_id,
            "name": name,
            "active": False,
            "settings": settings,
            "nodes": self.nodes,
            "connections": self.connections,
        }
        path.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")
        print(len(self.nodes), "nodes")
