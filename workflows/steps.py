"""What the workflow builders share: n8n nodes, their connections, and the MCP step.

Each workflow's JSON is built by its script (build_tarik_*.py): edit the script, never the JSON
(tests/style checks they match)."""

import json
import uuid

# tarik-mcp serves each cabinet at its own address only (mcp/server.py).
MCP_ADDRESS = "http://tarik-mcp:8000/c/{}/mcp"
CALL = "$('Appel').first().json"
RESULT = "$json.content[0].text"
OK_ELSE = f"{RESULT}.status === 'ok' ? 0 : 1"
# The native n8n agent's chat files: an "Appel" input, filled by n8n (n8n/patch.js), never by the model.
ATTACHMENTS = {"name": "attachments", "type": "string"}
# The cabinet a shared workflow works for: an "Appel" input, fixed by its caller (never chosen by a model).
CABINET_INPUT = {"name": "cabinet", "type": "string"}
SAVE_ATTACHMENTS = "Pieces jointes de l'agent"
RECORD = "Enregistrer"  # records each step's outcome; the run's summary is made from them


class Workflow:
    def __init__(self, prefix, cabinet=CALL + ".cabinet"):
        """``cabinet``: where the workflow's MCP steps read their cabinet, by default its "Appel" input,
        fixed by its caller (no cabinet: the steps fail, never reach another cabinet)."""
        self.prefix, self.nodes, self.connections = prefix, [], {}
        self.mcp_url = "=" + MCP_ADDRESS.format("{{ " + cabinet + " }}")

    def node(self, name, type_, version, position, parameters, **extra):
        """Node ids are derived from node names, so a rebuild only changes what changed."""
        self.nodes.append({"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.prefix}/{name}")), "name": name, "type": type_,
                           "typeVersion": version, "position": position, "parameters": parameters, **extra})
        return name

    def connect(self, source, target, output=0):
        outputs = self.connections.setdefault(source, {}).setdefault("main", [])
        while len(outputs) <= output:
            outputs.append([])
        outputs[output].append({"node": target, "type": "main", "index": 0})

    def mcp(self, name, tool, arguments, position):
        """One step: an MCP call. Output 0 = its answer, output 1 = a tool error."""
        return self.node(name, "@n8n/n8n-nodes-langchain.mcpClient", 1.1, position, {
            "endpointUrl": self.mcp_url, "serverTransport": "httpStreamable",
            "tool": {"__rl": True, "mode": "id", "value": tool},
            "inputMode": "json", "jsonInput": "={{ JSON.stringify(" + arguments + ") }}",
            "options": {"timeout": 300000},
        }, onError="continueErrorOutput")

    def route(self, name, expression, outputs, position):
        return self.node(name, "n8n-nodes-base.switch", 3.2, position,
                         {"mode": "expression", "numberOutputs": outputs, "output": "={{ " + expression + " }}"})

    def save_attachments(self, position):
        """The native agent's chat files (base64 of { thread_id, files }), saved once each in the
        cabinet's tarik_files. Emails save theirs as they arrive, and send none."""
        return self.mcp(SAVE_ATTACHMENTS, "save_files", "{ payload: " + CALL + ".attachments || '' }", position)

    def trigger(self, inputs):
        """"Appel": the workflow called as a tool, with these inputs (fixed by the caller, or filled by the model)."""
        return self.node("Appel", "n8n-nodes-base.executeWorkflowTrigger", 1.1, [0, 300], {
            "inputSource": "workflowInputs", "workflowInputs": {"values": inputs}})

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

    def record_and_summary(self, record_position, summary_position):
        """The "Enregistrer" step, then, last (lowest branch: n8n runs it after everything), the run's
        summary, which is the workflow's answer."""
        self.mcp(RECORD, "record_step", (
            f"{{ run_id: String($execution.id), session_id: {CALL}.session_id, origin: $prevNode.name, "
            f"result: $json.error ? {{ status: 'error', message: String($json.error.message ?? $json.error) }} : {RESULT} }}"),
            record_position)
        self.mcp("Resume", "run_summary", "{ run_id: String($execution.id) }", summary_position)
        self.connect("Appel", "Resume")

    def save(self, path, workflow_id, name, settings=None):
        settings = settings or {"executionOrder": "v1", "callerPolicy": "workflowsFromSameOwner", "saveExecutionProgress": True}
        workflow = {"id": workflow_id, "name": name, "active": False, "settings": settings,
                    "nodes": self.nodes, "connections": self.connections}
        path.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")
        print(len(self.nodes), "nodes")
