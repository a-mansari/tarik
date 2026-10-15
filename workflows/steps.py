"""What the workflow builders share: n8n nodes, their connections, and the MCP step."""

import uuid

# tarik-mcp serves each cabinet at its own address only (mcp/server.py).
MCP_ADDRESS = "http://tarik-mcp:8000/c/{}/mcp"
# The cabinet whose mailbox "Tarik - Emails" reads (its replies come from that cabinet's own agent).
MAIL_CABINET = "experio_cabinet_8"
CALL = "$('Appel').first().json"
RESULT = "$json.content[0].text"
OK_ELSE = f"{RESULT}.status === 'ok' ? 0 : 1"
# The native n8n agent's chat files: an "Appel" input, filled by n8n (n8n/patch.js), never by the model.
ATTACHMENTS = {"name": "attachments", "type": "string"}
# The cabinet a shared workflow works for: an "Appel" input, fixed by its caller (never chosen by a model).
CABINET = {"name": "cabinet", "type": "string"}
SAVE_ATTACHMENTS = "Pieces jointes de l'agent"


class Workflow:
    def __init__(self, prefix, cabinet=None):
        """``cabinet``: the cabinet's name, or None for a shared workflow, whose MCP steps go to the
        cabinet its "Appel" was given (no cabinet: the steps fail, never reach another cabinet)."""
        self.prefix, self.nodes, self.connections = prefix, [], {}
        self.mcp_url = MCP_ADDRESS.format(cabinet) if cabinet else "=" + MCP_ADDRESS.format("{{ " + CALL + ".cabinet }}")

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
