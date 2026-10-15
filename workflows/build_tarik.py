"""Build workflows/tarik.json: the chat with Tarik, its agent, memory and tools.

Edit this script, not the JSON: python3 workflows/build_tarik.py
The agent only finds the dossier and hands the work to "Tarik - Traitement"
(build_tarik_traitement.py), which runs the fixed sequence."""

import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("tarik.json")
MCP_URL = "http://tarik-mcp:8000/mcp"
POSTGRES = {"postgres": {"id": "TarikPostgres001", "name": "Tarik Postgres"}}
SESSION = "={{ $('Message a Tarik').item.json.sessionId }}"
nodes, connections = [], {}


def node(id_, name, type_, version, position, parameters, **extra):
    """Node ids are fixed: n8n keeps them across imports."""
    nodes.append({"id": id_, "name": name, "type": type_, "typeVersion": version,
                  "position": position, "parameters": parameters, **extra})


def connect(source, target, kind="main"):
    connections.setdefault(source, {}).setdefault(kind, [[]])[0].append({"node": target, "type": kind, "index": 0})


# The chat page, and what comes with each message: its files, pending questions, drafts Experio hasn't read.
node("c1a0b5e2-0001-4000-8000-000000000001", "Message a Tarik", "@n8n/n8n-nodes-langchain.chatTrigger", 1.4, [0, 0], {
    "public": True, "mode": "hostedChat",
    "initialMessages": "Bonjour, je suis Tarik. Envoyez-moi vos factures (PDF ou photos), ou le FEC d'un dossier.",
    "options": {"allowFileUploads": True, "allowedFilesMimeTypes": "application/pdf,image/*,text/csv,text/plain,.csv,.txt,.xlsx", "title": "Tarik",
                "subtitle": "Assistant comptable", "inputPlaceholder": "Votre message..."},
}, webhookId="7304d10e-ea71-4883-be8d-dcd560c62f4e")
node("c1a0b5e2-0001-4000-8000-000000000006", "Fichiers recus", "n8n-nodes-base.code", 2, [220, 0], {"jsCode": (
    "// The uploaded files as one base64 payload: a single query parameter, free of the commas the Postgres node splits on.\n"
    "const message = $input.first().json;\n"
    "const files = [];\n"
    "for (const file of message.files || []) {\n"
    "  const buffer = await this.helpers.getBinaryDataBuffer(0, file.binaryKey);\n"
    "  files.push({ file_name: file.fileName, mime_type: file.mimeType, data: buffer.toString('base64') });\n"
    "}\n"
    "const payload = Buffer.from(JSON.stringify({ session_id: message.sessionId, files })).toString('base64');\n"
    "return [{ json: { payload } }];")})
node("c1a0b5e2-0001-4000-8000-000000000007", "Enregistrer fichiers", "n8n-nodes-base.postgres", 2.6, [440, 0], {
    "operation": "executeQuery",
    "query": ("with payload as (select convert_from(decode($1, 'base64'), 'UTF8')::jsonb as body),\n"
              "saved as (\n"
              "  insert into tarik_files (session_id, file_name, mime_type, data)\n"
              "  select body->>'session_id', file->>'file_name', file->>'mime_type', decode(file->>'data', 'base64')\n"
              "  from payload, jsonb_array_elements(body->'files') as file\n"
              "  returning id, file_name\n"
              ")\n"
              "select coalesce(json_agg(json_build_object('id', id, 'file_name', file_name) order by id), '[]'::json) as files from saved"),
    "options": {"queryReplacement": "={{ $json.payload }}"},
}, credentials=POSTGRES)
node("1a10c278-5986-5ed6-a2d4-7573e8d5078f", "Questions en attente", "n8n-nodes-base.postgres", 2.6, [580, 0], {
    "operation": "executeQuery",
    "query": ("select coalesce(json_agg(json_build_object('invoice_id', invoice_id, 'question', question) order by created_at), "
              "'[]'::json) as pending from tarik_pending where session_id = $1"),
    "options": {"queryReplacement": SESSION},
}, credentials=POSTGRES)
node("bea448c1-1139-55b3-b34b-6ac4a4252471", "Brouillons en attente", "@n8n/n8n-nodes-langchain.mcpClient", 1.1, [800, 0], {
    "endpointUrl": MCP_URL, "serverTransport": "httpStreamable",
    "tool": {"__rl": True, "mode": "id", "value": "waiting_drafts"}, "inputMode": "json",
    "jsonInput": "={{ JSON.stringify({ session_id: $('Message a Tarik').item.json.sessionId }) }}",
    "options": {"timeout": 60000},
}, onError="continueRegularOutput")  # the chat still answers when the check fails
for source, target in [("Message a Tarik", "Fichiers recus"), ("Fichiers recus", "Enregistrer fichiers"),
                       ("Enregistrer fichiers", "Questions en attente"), ("Questions en attente", "Brouillons en attente"),
                       ("Brouillons en attente", "Tarik")]:
    connect(source, target)

# The agent: short prompt, the steps themselves are in "Tarik - Traitement".
PROMPT = """Tu es Tarik, assistant comptable d'un cabinet marocain. Réponds dans la langue du client, en phrases courtes. Vouvoie-le, sans civilité.

Ce que tu fais, et seulement cela :
1. Factures et relevés reçus : trouve le dossier avec find_dossier, à partir du nom donné par le client. S'il n'a pas dit quel dossier, ou si plusieurs dossiers se ressemblent, demande-lui lequel. Puis appelle traiter_fichiers une seule fois, avec l'id du dossier et les numéros de tous les fichiers joints. Recopie son résultat mot pour mot, facture par facture, sans le reformuler (les numéros surtout).
2. Questions en attente : elles te sont données avec le message du client. S'il y répond, appelle repondre avec le numéro de la facture et sa réponse, puis recopie le résultat mot pour mot. Sinon, rappelle-lui les questions.
3. Factures en attente de lecture : elles te sont données avec le message du client. Appelle reprendre une seule fois, puis recopie son résultat mot pour mot.
4. Nouveau dossier : il faut son nom, et si possible son ICE et son IF. Vérifie avec find_dossier qu'il n'existe pas déjà. Demande de quel dossier existant copier la configuration (le dossier modèle, cherché avec find_dossier), ou aucun. Récapitule (nom, ICE, IF, modèle, fichiers) et attends que le client confirme : un dossier créé ne peut pas être supprimé. Puis appelle parametrer_dossier avec dossier_id 0, et les numéros des fichiers FEC ou grand livre joints. Recopie son résultat mot pour mot.
5. FEC ou grand livre (fichier d'écritures CSV, TXT ou Excel) pour un dossier existant : trouve le dossier avec find_dossier, puis appelle parametrer_dossier avec son id et les numéros des fichiers. Un FEC ou un grand livre ne va jamais à traiter_fichiers.
6. Le reste : experio_connection pour savoir si Experio fonctionne, list_dossiers pour la liste des dossiers, list_invoices pour les factures d'un dossier (trouve d'abord le dossier avec find_dossier ; filtre possible : en cours, brouillon, comptabilisee, payee). Recopie la liste telle quelle. Pour toute autre demande, dis simplement que tu ne peux pas encore le faire.

Règles :
- N'invente aucun résultat. Ne propose aucune action que tu ne peux pas faire. Ne dis jamais que tu transmets quelque chose au cabinet.
- Ne cite ni le nom de tes outils, ni d'autres numéros que ceux des factures."""
MESSAGE = (
    "={{ $('Message a Tarik').item.json.chatInput || '(pas de message)' }}"
    "{{ $('Enregistrer fichiers').item.json.files.length ? '\\n\\nFichiers joints : ' + $('Enregistrer fichiers').item.json.files"
    ".map(f => f.file_name + ' (fichier n° ' + f.id + ')').join(', ') : '' }}"
    "{{ $('Questions en attente').item.json.pending.length ? '\\n\\nQuestions en attente : ' + $('Questions en attente').item.json.pending"
    ".map(p => 'facture ' + p.invoice_id + ' : ' + p.question).join(' | ') : '' }}"
    "{{ $json.content?.[0]?.text?.invoices?.length ? '\\n\\nFactures en attente de lecture par Experio : ' + "
    "$json.content[0].text.invoices.join(', ') : '' }}")
node("c1a0b5e2-0001-4000-8000-000000000002", "Tarik", "@n8n/n8n-nodes-langchain.agent", 3.1, [880, 0], {
    "promptType": "define", "text": MESSAGE, "options": {"systemMessage": PROMPT, "passthroughBinaryImages": False},
}, retryOnFail=True, maxTries=3, waitBetweenTries=5000)
node("c1a0b5e2-0001-4000-8000-000000000003", "MiniMax-M3", "@n8n/n8n-nodes-langchain.lmChatAnthropic", 1.3, [800, 220], {
    "model": {"__rl": True, "mode": "id", "value": "MiniMax-M3"}, "options": {},
}, credentials={"anthropicApi": {"id": "MiniMaxAnthr0p1c", "name": "MiniMax (Anthropic-compatible)"}},
    notes="MiniMax, not Claude: n8n's Anthropic node type is used because MiniMax speaks the Anthropic message format.")
node("c1a0b5e2-0001-4000-8000-000000000004", "Memory", "@n8n/n8n-nodes-langchain.memoryPostgresChat", 1.4, [960, 220], {
    "tableName": "tarik_chat_histories", "sessionIdType": "customKey", "sessionKey": SESSION,
}, credentials=POSTGRES)
node("c1a0b5e2-0001-4000-8000-000000000005", "Outils Experio", "@n8n/n8n-nodes-langchain.mcpClientTool", 1.4, [1120, 220], {
    "endpointUrl": MCP_URL, "serverTransport": "httpStreamable", "include": "selected", "options": {},
    "includeTools": ["experio_connection", "list_dossiers", "find_dossier", "list_invoices"],
})
connect("MiniMax-M3", "Tarik", "ai_languageModel")
connect("Memory", "Tarik", "ai_memory")
connect("Outils Experio", "Tarik", "ai_tool")

# The agent's ways into "Tarik - Traitement", one per mode.
INPUTS = [("mode", "string"), ("session_id", "string"), ("dossier_id", "number"), ("file_ids", "any"),
          ("invoice_id", "number"), ("answer", "string")]


def processing_tool(id_, name, position, description, values):
    node(id_, name, "@n8n/n8n-nodes-langchain.toolWorkflow", 2.2, position, {
        "description": description,
        "workflowId": {"__rl": True, "mode": "id", "value": "TarikTraitement1", "cachedResultName": "Tarik - Traitement"},
        "workflowInputs": {
            "mappingMode": "defineBelow", "value": {"session_id": SESSION, "dossier_id": 0, "file_ids": "={{ [] }}",
                                                    "invoice_id": 0, "answer": "", **values},
            "matchingColumns": [],
            "schema": [{"id": key, "displayName": key, "required": False, "defaultMatch": False, "display": True,
                        "canBeUsedToMatch": True, "type": kind, "removed": False} for key, kind in INPUTS],
            "attemptToConvertTypes": False, "convertFieldsToString": False},
    })
    connect(name, "Tarik", "ai_tool")


node("6f1d3a52-dc4e-5b7e-9a1c-7d2e4b9c0a11", "parametrer_dossier", "@n8n/n8n-nodes-langchain.toolWorkflow", 2.2, [1260, 380], {
    "description": ("Cree un dossier (dossier_id 0) ou reprend un dossier existant, dans l'ordre : creation, copie de la "
                    "configuration du dossier modele, import des FEC ou grands livres joints (Experio cree les comptes, "
                    "journaux, tiers et exercices manquants), fin de l'integration, etat du dossier. Renvoie le resultat."),
    "workflowId": {"__rl": True, "mode": "id", "value": "TarikDossier0001", "cachedResultName": "Tarik - Dossier"},
    "workflowInputs": {
        "mappingMode": "defineBelow",
        "value": {
            "session_id": SESSION,
            "dossier_id": "={{ $fromAI('dossier_id', 'L id du dossier existant (find_dossier), 0 pour un nouveau dossier', 'number') }}",
            "name": "={{ $fromAI('name', 'Le nom du nouveau dossier, vide pour un dossier existant', 'string') }}",
            "ice": "={{ $fromAI('ice', 'L ICE du nouveau dossier, vide si inconnu', 'string') }}",
            "if_number": "={{ $fromAI('if_number', 'L IF du nouveau dossier, vide si inconnu', 'string') }}",
            "folder_type": "={{ $fromAI('folder_type', 'company (societe, par defaut) ou person (personne physique)', 'string') }}",
            "model_dossier_id": "={{ $fromAI('model_dossier_id', 'L id du dossier modele (find_dossier), 0 si aucun', 'number') }}",
            "file_ids": "={{ $fromAI('file_ids', 'Les numeros des fichiers FEC ou grand livre joints, ex. [12], [] si aucun', 'json') }}",
            "kind": "={{ $fromAI('kind', 'fec ou grand livre si le client l a dit, sinon vide', 'string') }}"},
        "matchingColumns": [],
        "schema": [{"id": key, "displayName": key, "required": False, "defaultMatch": False, "display": True,
                    "canBeUsedToMatch": True, "type": kind, "removed": False}
                   for key, kind in [("session_id", "string"), ("dossier_id", "number"), ("name", "string"), ("ice", "string"),
                                     ("if_number", "string"), ("folder_type", "string"), ("model_dossier_id", "number"),
                                     ("file_ids", "any"), ("kind", "string")]],
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
    "file_ids": "={{ $fromAI('file_ids', 'Les numeros de tous les fichiers joints au message, ex. [12, 13]', 'json') }}"})
processing_tool("5c58fd2a-7a23-5608-b249-21bc975ace65", "repondre", [860, 380], (
    "Applique la reponse du client a la question en attente d'une facture, puis reprend le traitement de cette facture "
    "la ou il s'etait arrete. Renvoie le resultat."), {
    "mode": "answer",
    "invoice_id": "={{ $fromAI('invoice_id', 'Le numero de la facture concernee par la question', 'number') }}",
    "answer": "={{ $fromAI('answer', 'La reponse du client, avec ses mots', 'string') }}"})
processing_tool("094759d5-3ec6-5be8-a71a-e6bb1377ccb6", "reprendre", [1060, 380], (
    "Reprend les factures en attente de lecture par Experio, a l'etape de lecture, puis la suite du traitement. "
    "Renvoie le resultat facture par facture."), {"mode": "resume"})

workflow = {"id": "Sca0Bh73nUct7ixj", "name": "Tarik", "active": False, "settings": {"executionOrder": "v1"},
            "nodes": nodes, "connections": connections}
OUT.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")
print(len(nodes), "nodes")
