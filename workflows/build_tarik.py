"""Build workflows/tarik.json (the chat) and workflows/tarik-agent.json (Tarik himself: agent,
memory and tools), which the chat and the emails ("Tarik - Emails") both call.

Edit this script, not the JSON: python3 workflows/build_tarik.py
The agent only finds the dossier and hands the work to "Tarik - Traitement"
(build_tarik_traitement.py), which runs the fixed sequence."""

import json
from pathlib import Path

from steps import LEGACY_CABINET, MCP_ADDRESS

HERE = Path(__file__).parent
MCP_URL = MCP_ADDRESS.format(LEGACY_CABINET)
POSTGRES = {"postgres": {"id": "TarikPostgres001", "name": "Tarik Postgres"}}
AGENT_ID = "TarikAgent000001"
CALL = "$('Appel').item.json"
SESSION = "={{ " + CALL + ".session_id }}"
chat, agent = ([], {}), ([], {})
nodes, connections = chat


def node(id_, name, type_, version, position, parameters, **extra):
    """Node ids are fixed: n8n keeps them across imports."""
    nodes.append({"id": id_, "name": name, "type": type_, "typeVersion": version,
                  "position": position, "parameters": parameters, **extra})


def connect(source, target, kind="main"):
    connections.setdefault(source, {}).setdefault(kind, [[]])[0].append({"node": target, "type": kind, "index": 0})


def inputs_schema(fields):
    return [{"id": key, "displayName": key, "required": False, "defaultMatch": False, "display": True,
             "canBeUsedToMatch": True, "type": kind, "removed": False} for key, kind in fields]


# The chat page: each message's files saved, then Tarik ("Tarik - Agent") answers.
node("c1a0b5e2-0001-4000-8000-000000000001", "Message a Tarik", "@n8n/n8n-nodes-langchain.chatTrigger", 1.4, [0, 0], {
    "public": True, "mode": "hostedChat",
    "initialMessages": "Bonjour, je suis Tarik. Envoyez-moi vos factures (PDF ou photos), ou le FEC d'un dossier.",
    "options": {"allowFileUploads": True, "allowedFilesMimeTypes": "application/pdf,image/*,text/csv,text/plain,.csv,.txt,.xlsx", "title": "Tarik",
                "subtitle": "Assistant comptable", "inputPlaceholder": "Votre message..."},
}, webhookId="7304d10e-ea71-4883-be8d-dcd560c62f4e")
node("c1a0b5e2-0001-4000-8000-000000000006", "Fichiers recus", "n8n-nodes-base.code", 2, [220, 0], {"jsCode": (
    "// The uploaded files as one base64 payload, saved by save_files in this cabinet's tarik_files.\n"
    "const message = $input.first().json;\n"
    "const files = [];\n"
    "for (const file of message.files || []) {\n"
    "  const buffer = await this.helpers.getBinaryDataBuffer(0, file.binaryKey);\n"
    "  files.push({ file_name: file.fileName, mime_type: file.mimeType, data: buffer.toString('base64') });\n"
    "}\n"
    "const payload = Buffer.from(JSON.stringify({ session_id: message.sessionId, files })).toString('base64');\n"
    "return [{ json: { payload } }];")})
node("c1a0b5e2-0001-4000-8000-000000000007", "Enregistrer fichiers", "@n8n/n8n-nodes-langchain.mcpClient", 1.1, [440, 0], {
    "endpointUrl": MCP_URL, "serverTransport": "httpStreamable",
    "tool": {"__rl": True, "mode": "id", "value": "save_files"}, "inputMode": "json",
    "jsonInput": "={{ JSON.stringify({ payload: $json.payload }) }}",
    "options": {"timeout": 300000},
})
AGENT_INPUTS = [("session_id", "string"), ("message", "string"), ("files", "array")]
node("c1a0b5e2-0001-4000-8000-000000000002", "Tarik", "n8n-nodes-base.executeWorkflow", 1.2, [660, 0], {
    "workflowId": {"__rl": True, "mode": "id", "value": AGENT_ID, "cachedResultName": "Tarik - Agent"},
    "workflowInputs": {"mappingMode": "defineBelow", "value": {
        "session_id": "={{ $('Message a Tarik').item.json.sessionId }}",
        "message": "={{ $('Message a Tarik').item.json.chatInput || '' }}",
        "files": "={{ $json.content[0].text.files.map(f => f.file_name) }}"},
        "matchingColumns": [], "schema": inputs_schema(AGENT_INPUTS), "attemptToConvertTypes": False, "convertFieldsToString": False},
    "options": {},
})
for source, target in [("Message a Tarik", "Fichiers recus"), ("Fichiers recus", "Enregistrer fichiers"), ("Enregistrer fichiers", "Tarik")]:
    connect(source, target)

# Tarik himself, called with a conversation's session, the client's message and the files that came with it.
nodes, connections = agent
node("2b6f0c1e-7d3a-5a40-9e21-4c8b7f1d2e01", "Appel", "n8n-nodes-base.executeWorkflowTrigger", 1.1, [0, 0], {
    "inputSource": "workflowInputs", "workflowInputs": {"values": [{"name": key, "type": kind} for key, kind in AGENT_INPUTS]}})
node("1a10c278-5986-5ed6-a2d4-7573e8d5078f", "Questions en attente", "@n8n/n8n-nodes-langchain.mcpClient", 1.1, [580, 0], {
    "endpointUrl": MCP_URL, "serverTransport": "httpStreamable",
    "tool": {"__rl": True, "mode": "id", "value": "pending_questions"}, "inputMode": "json",
    "jsonInput": "={{ JSON.stringify({ session_id: " + CALL + ".session_id }) }}",
    "options": {"timeout": 60000},
})
node("bea448c1-1139-55b3-b34b-6ac4a4252471", "Brouillons en attente", "@n8n/n8n-nodes-langchain.mcpClient", 1.1, [800, 0], {
    "endpointUrl": MCP_URL, "serverTransport": "httpStreamable",
    "tool": {"__rl": True, "mode": "id", "value": "waiting_drafts"}, "inputMode": "json",
    "jsonInput": "={{ JSON.stringify({ session_id: " + CALL + ".session_id }) }}",
    "options": {"timeout": 60000},
}, onError="continueRegularOutput")  # Tarik still answers when the check fails
for source, target in [("Appel", "Questions en attente"), ("Questions en attente", "Brouillons en attente"),
                       ("Brouillons en attente", "Tarik")]:
    connect(source, target)

# The agent: short prompt, the steps themselves are in "Tarik - Traitement".
PROMPT = """Tu es Tarik, assistant comptable d'un cabinet marocain. Réponds dans la langue du client, en phrases courtes. Vouvoie-le, sans civilité.

Ce que tu fais, et seulement cela :
1. Factures et relevés reçus : trouve le dossier avec find_dossier, à partir du nom donné par le client. S'il n'a pas dit quel dossier, ou si plusieurs dossiers se ressemblent, demande-lui lequel. Puis appelle traiter_fichiers une seule fois, avec l'id du dossier : il prend les fichiers joints lui-même. Recopie son résultat mot pour mot, facture par facture, sans le reformuler (les numéros surtout).
2. Questions en attente : elles te sont données avec le message du client. S'il y répond, appelle repondre avec le numéro de la facture et sa réponse, puis recopie le résultat mot pour mot. Sinon, rappelle-lui les questions.
3. Factures en attente de lecture : elles te sont données avec le message du client. Appelle reprendre une seule fois, puis recopie son résultat mot pour mot.
4. Nouveau dossier : il faut son nom, et si possible son ICE et son IF. Appelle directement parametrer_dossier avec dossier_id 0, sans récapitulatif ni demande de confirmation : il prend les fichiers FEC ou grand livre joints lui-même, et vérifie lui-même qu'aucun dossier n'a le même ICE, le même IF ou un nom très proche. S'il pose une question (dossier au nom très proche), transmets-la ; si le client répond oui, rappelle parametrer_dossier avec les mêmes informations. Experio vérifie l'ICE et l'IF auprès du registre officiel : s'il les refuse, transmets son refus et demande au client de vérifier les numéros. Propose alors de vérifier les numéros, ou de créer le dossier sans vérification s'il l'écrit lui-même (« créez-le sans vérification ») : ce n'est qu'alors que tu mets skip_ice_if_check à true, jamais de toi-même. Recopie son résultat mot pour mot.
5. FEC ou grand livre (fichier d'écritures CSV, TXT ou Excel) pour un dossier existant, y compris un dossier que tu viens de créer : trouve le dossier avec find_dossier, puis appelle parametrer_dossier avec son id, jamais avec dossier_id 0. Si le client dit avoir joint un fichier mais qu'aucun n'est arrivé, dis-le-lui. Un FEC ou un grand livre ne va jamais à traiter_fichiers.
6. Si le client demande lui-même de reprendre la configuration d'un dossier existant pour son nouveau dossier, cherche ce dossier avec find_dossier, ajoute-le au récapitulatif, et passe son id en model_dossier_id.
7. Le reste : experio_connection pour savoir si Experio fonctionne, list_dossiers pour la liste des dossiers, list_collaborators pour les collaborateurs du cabinet (rôle, manager ou non, nombre de dossiers), list_invoices pour les factures d'un dossier (trouve d'abord le dossier avec find_dossier ; filtre possible : en cours, brouillon, comptabilisee, payee). Recopie la liste telle quelle. Pour toute autre demande, dis simplement que tu ne peux pas encore le faire.

Règles :
- N'invente aucun résultat. Ne propose aucune action que tu ne peux pas faire. Ne dis jamais que tu transmets quelque chose au cabinet.
- Ne cite ni le nom de tes outils, ni l'id d'un dossier, ni d'autres numéros que ceux des factures : un dossier se nomme par son nom."""
MESSAGE = (
    "={{ " + CALL + ".message || '(pas de message)' }}"
    "{{ (" + CALL + ".files || []).length ? '\\n\\nFichiers joints : ' + " + CALL + ".files.join(', ') : '' }}"
    # Said, not guessed: MiniMax took "ci-joint notre FEC" for a file that never came.
    "{{ !(" + CALL + ".files || []).length && /joint|attach|fichier|fec|pi[eè]ce/i.test(" + CALL + ".message || '') "
    "? '\\n\\nFichiers joints : aucun, rien n est arrive avec ce message.' : '' }}"
    "{{ $('Questions en attente').item.json.content[0].text.pending.length ? '\\n\\nQuestions en attente : ' + $('Questions en attente').item.json.content[0].text.pending"
    ".map(p => 'facture ' + p.invoice_id + ' : ' + p.question).join(' | ') : '' }}"
    "{{ $json.content?.[0]?.text?.invoices?.length ? '\\n\\nFactures en attente de lecture par Experio : ' + "
    "$json.content[0].text.invoices.join(', ') : '' }}")
node("7e3c9a41-2d5b-5f86-b0a7-3c1e8d4f6a92", "Tarik", "@n8n/n8n-nodes-langchain.agent", 3.1, [880, 0], {
    "promptType": "define", "text": MESSAGE, "options": {"systemMessage": PROMPT, "passthroughBinaryImages": False},
}, retryOnFail=True, maxTries=3, waitBetweenTries=5000)
node("8c3e0b7a-4f51-5d2e-b6a9-1e7c2d9f4a30", "Reponse au client", "n8n-nodes-base.code", 2, [1100, 0], {"jsCode": (
    "// The agent needs the dossiers' ids for its tools, and MiniMax sometimes writes them: never shown to the client.\n"
    "const text = $input.first().json.output || '';\n"
    "let output = text\n"
    "  .replace(/\\s*\\((?:dossier\\s+)?id\\s*:?\\s*\\d+\\)/gi, '')\n"
    "  .replace(/,?\\s*\\b(?:dossier\\s+)?id\\s*:?\\s*\\d+\\b/gi, '')\n"
    "  .trim();\n"
    "// MiniMax sometimes quotes a result it copies, the opening quote taking the first line's dash.\n"
    "const quoted = output.match(/^[«\"“]\\s*([\\s\\S]*?)\\s*[»\"”]$/);\n"
    "if (quoted) output = quoted[1];\n"
    "const lines = output.split('\\n');\n"
    "const step = /^[a-zàâçéèêëîïôûù' ]+ : \\S/;  // a summary line: \"integration : Dossier ...\"\n"
    "if (lines.length > 1 && step.test(lines[0]) && lines.slice(1).some(line => line.startsWith('- '))) lines[0] = '- ' + lines[0];\n"
    "return [{ json: { output: lines.join('\\n') } }];")})
connect("Tarik", "Reponse au client")
node("c1a0b5e2-0001-4000-8000-000000000003", "MiniMax-M3", "@n8n/n8n-nodes-langchain.lmChatAnthropic", 1.3, [800, 220], {
    "model": {"__rl": True, "mode": "id", "value": "MiniMax-M3"}, "options": {},
}, credentials={"anthropicApi": {"id": "MiniMaxAnthr0p1c", "name": "MiniMax (Anthropic-compatible)"}},
    notes="MiniMax, not Claude: n8n's Anthropic node type is used because MiniMax speaks the Anthropic message format.")
node("c1a0b5e2-0001-4000-8000-000000000004", "Memory", "@n8n/n8n-nodes-langchain.memoryPostgresChat", 1.4, [960, 220], {
    "tableName": "tarik_chat_histories", "sessionIdType": "customKey", "sessionKey": SESSION,
}, credentials=POSTGRES)
node("c1a0b5e2-0001-4000-8000-000000000005", "Outils Experio", "@n8n/n8n-nodes-langchain.mcpClientTool", 1.4, [1120, 220], {
    "endpointUrl": MCP_URL, "serverTransport": "httpStreamable", "include": "selected", "options": {},
    "includeTools": ["experio_connection", "list_dossiers", "find_dossier", "list_invoices", "list_collaborators"],
})
connect("MiniMax-M3", "Tarik", "ai_languageModel")
connect("Memory", "Tarik", "ai_memory")
connect("Outils Experio", "Tarik", "ai_tool")

# The agent's ways into "Tarik - Traitement", one per mode.
INPUTS = [("mode", "string"), ("session_id", "string"), ("dossier_id", "number"),
          ("invoice_id", "number"), ("answer", "string"), ("cabinet", "string")]


def processing_tool(id_, name, position, description, values):
    node(id_, name, "@n8n/n8n-nodes-langchain.toolWorkflow", 2.2, position, {
        "description": description,
        "workflowId": {"__rl": True, "mode": "id", "value": "TarikTraitement1", "cachedResultName": "Tarik - Traitement"},
        "workflowInputs": {
            "mappingMode": "defineBelow", "value": {"session_id": SESSION, "dossier_id": 0,
                                                    "invoice_id": 0, "answer": "", "cabinet": LEGACY_CABINET, **values},
            "matchingColumns": [],
            "schema": [{"id": key, "displayName": key, "required": False, "defaultMatch": False, "display": True,
                        "canBeUsedToMatch": True, "type": kind, "removed": False} for key, kind in INPUTS],
            "attemptToConvertTypes": False, "convertFieldsToString": False},
    })
    connect(name, "Tarik", "ai_tool")


node("6f1d3a52-dc4e-5b7e-9a1c-7d2e4b9c0a11", "parametrer_dossier", "@n8n/n8n-nodes-langchain.toolWorkflow", 2.2, [1260, 380], {
    "description": ("Cree un dossier (dossier_id 0) ou reprend un dossier existant, dans l'ordre : creation, import des FEC ou grands livres joints (Experio cree les comptes, "
                    "journaux, tiers et exercices manquants), fin de l'integration, etat du dossier. Renvoie le resultat."),
    "workflowId": {"__rl": True, "mode": "id", "value": "TarikDossier0001", "cachedResultName": "Tarik - Dossier"},
    "workflowInputs": {
        "mappingMode": "defineBelow",
        "value": {
            "session_id": SESSION,
            "cabinet": LEGACY_CABINET,
            "client_message": "={{ " + CALL + ".message }}",  # the client's words, not the agent's
            "dossier_id": "={{ $fromAI('dossier_id', 'L id du dossier existant (find_dossier), 0 pour un nouveau dossier', 'number') }}",
            "name": "={{ $fromAI('name', 'Le nom du nouveau dossier, vide pour un dossier existant', 'string') }}",
            "ice": "={{ $fromAI('ice', 'L ICE du nouveau dossier, vide si inconnu', 'string') }}",
            "if_number": "={{ $fromAI('if_number', 'L IF du nouveau dossier, vide si inconnu', 'string') }}",
            "folder_type": "={{ $fromAI('folder_type', 'company (societe, par defaut) ou person (personne physique)', 'string') }}",
            "skip_ice_if_check": "={{ $fromAI('skip_ice_if_check', 'true seulement si le client a accepte de creer le dossier sans verification de l ICE et de l IF, sinon false', 'boolean') }}",
            "model_dossier_id": "={{ $fromAI('model_dossier_id', '0 (voir la regle 6)', 'number') }}",
            "kind": "={{ $fromAI('kind', 'fec ou grand livre si le client l a dit, sinon vide', 'string') }}"},
        "matchingColumns": [],
        "schema": [{"id": key, "displayName": key, "required": False, "defaultMatch": False, "display": True,
                    "canBeUsedToMatch": True, "type": kind, "removed": False}
                   for key, kind in [("session_id", "string"), ("cabinet", "string"), ("client_message", "string"), ("dossier_id", "number"), ("name", "string"), ("ice", "string"),
                                     ("if_number", "string"), ("folder_type", "string"), ("skip_ice_if_check", "boolean"), ("model_dossier_id", "number"),
                                     ("kind", "string")]],
        "attemptToConvertTypes": False, "convertFieldsToString": False},
})
connect("parametrer_dossier", "Tarik", "ai_tool")

processing_tool("2dc18984-0bf1-5c55-99a0-3353d48845d2", "traiter_fichiers", [700, 380], (
    "Traite les fichiers recus pour un dossier, toujours dans le meme ordre : identification (facture ou releve bancaire), "
    "import du releve ou decoupage des factures, brouillon, lecture par Experio, verification, correction, classement, tiers, "
    "doublon, compte, TVA, journal, retenue a la source, charges constatees d'avance, exercice, comptabilisation. "
    "Renvoie le resultat facture par facture et les questions a poser au client."), {
    "mode": "files",
    "dossier_id": "={{ $fromAI('dossier_id', 'L id du dossier, donne par find_dossier', 'number') }}",
})
processing_tool("5c58fd2a-7a23-5608-b249-21bc975ace65", "repondre", [860, 380], (
    "Applique la reponse du client a la question en attente d'une facture, puis reprend le traitement de cette facture "
    "la ou il s'etait arrete. Renvoie le resultat."), {
    "mode": "answer",
    "invoice_id": "={{ $fromAI('invoice_id', 'Le numero de la facture concernee par la question', 'number') }}",
    "answer": "={{ $fromAI('answer', 'La reponse du client, avec ses mots', 'string') }}"})
processing_tool("094759d5-3ec6-5be8-a71a-e6bb1377ccb6", "reprendre", [1060, 380], (
    "Reprend les factures en attente de lecture par Experio, a l'etape de lecture, puis la suite du traitement. "
    "Renvoie le resultat facture par facture."), {"mode": "resume"})

SETTINGS = {"executionOrder": "v1", "saveExecutionProgress": True}  # each node saved as it ends: runs show live
for file_name, workflow in (("tarik.json", {"id": "Sca0Bh73nUct7ixj", "name": "Tarik", "settings": SETTINGS, "graph": chat}),
                            ("tarik-agent.json", {"id": AGENT_ID, "name": "Tarik - Agent",
                                                  "settings": {**SETTINGS, "callerPolicy": "workflowsFromSameOwner"}, "graph": agent})):
    graph_nodes, graph_connections = workflow.pop("graph")
    (HERE / file_name).write_text(json.dumps({**workflow, "active": False, "nodes": graph_nodes, "connections": graph_connections},
                                             indent=2, ensure_ascii=False) + "\n")
    print(file_name, len(graph_nodes), "nodes")
