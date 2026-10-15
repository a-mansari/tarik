"""What the workflow builders share: n8n nodes, their connections, and the MCP step."""

import uuid

MCP_URL = "http://tarik-mcp:8000/mcp"
CALL = "$('Appel').first().json"
RESULT = "$json.content[0].text"
OK_ELSE = f"{RESULT}.status === 'ok' ? 0 : 1"
# The native n8n agent's chat files: an "Appel" input, filled by n8n (n8n/patch.js), never by the model.
ATTACHMENTS = {"name": "attachments", "type": "string"}
SAVE_ATTACHMENTS = "Pieces jointes de l'agent"


class Workflow:
    def __init__(self, prefix):
        self.prefix, self.nodes, self.connections = prefix, [], {}

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
            "endpointUrl": MCP_URL, "serverTransport": "httpStreamable",
            "tool": {"__rl": True, "mode": "id", "value": tool},
            "inputMode": "json", "jsonInput": "={{ JSON.stringify(" + arguments + ") }}",
            "options": {"timeout": 300000},
        }, onError="continueErrorOutput")

    def route(self, name, expression, outputs, position):
        return self.node(name, "n8n-nodes-base.switch", 3.2, position,
                         {"mode": "expression", "numberOutputs": outputs, "output": "={{ " + expression + " }}"})

    def save_attachments(self, position):
        """The native agent's chat files (base64 of { thread_id, files }), saved once each in tarik_files
        like the chat's. The chat and the emails save theirs before calling, and send none."""
        return self.node(SAVE_ATTACHMENTS, "n8n-nodes-base.postgres", 2.6, position, {
            "operation": "executeQuery",
            "query": ("with payload as (select convert_from(decode($1, 'base64'), 'UTF8')::jsonb as body),\n"
                      "saved as (\n"
                      "  insert into tarik_files (session_id, file_name, mime_type, data, source_id)\n"
                      "  select body->>'thread_id', file->>'file_name', file->>'mime_type', decode(file->>'data', 'base64'), file->>'id'\n"
                      "  from payload, jsonb_array_elements(coalesce(body->'files', '[]'::jsonb)) as file\n"
                      "  on conflict (session_id, source_id) do nothing\n"
                      "  returning id\n"
                      ")\n"
                      "select count(*) as saved from saved"),
            "options": {"queryReplacement": "={{ " + CALL + ".attachments || 'e30=' }}"},  # e30= is {}
        }, credentials={"postgres": {"id": "TarikPostgres001", "name": "Tarik Postgres"}})
