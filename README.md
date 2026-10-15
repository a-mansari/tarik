# sarnec-n8n

Tarik, Experio's accounting assistant, rebuilt on n8n from the LangGraph version in
`tarik-master`, to compare speed, maintenance, scaling and adaptability.

A client chats with Tarik and sends invoices or bank statements, or asks for a new dossier
with its FEC. Tarik finds the dossier,
then a fixed sequence puts each document into Experio: draft, check against the scan,
correction, tiers, duplicate, account, TVA, journal, withholding, prepaid charges,
fiscal year and posting. When a step needs the client, Tarik asks, and the sequence
resumes from there with the answer.

## How it is built

| Part | What it does |
|------|--------------|
| `workflows/tarik.json` | The chat page and the agent (MiniMax-M3). The agent only finds the dossier, lists invoices and hands the work over. |
| `workflows/tarik-dossier.json` | A dossier's creation and setup: create it, copy a model dossier's configuration, import its FEC or grand livre, finish the onboarding. |
| `workflows/tarik-traitement.json` | The fixed sequence, one n8n node per step. Called by the agent in three modes: `files`, `answer`, `resume`. |
| `mcp/` | The steps themselves: Python tools served over MCP (`tarik-mcp` container), one module per phase. They use Experio's own wizards and onchanges, never a parallel logic. |
| `postgres/init.sql` | Tarik's own tables: received files, check results, step results, pending questions. The chat memory table is created by n8n. |

Rules the tools keep:

- Odoo, its URL, database and user are never shown to the agent or the client: it is "Experio".
- Every dossier tool needs a real dossier id and fails without one. The cabinet is locked
  by its exact database name and Tarik's user id (`ODOO_DB`, `ODOO_UID`).
- `DRY_RUN=1` (default): drafts and bank statements are created, but nothing else is written and nothing is posted.
  Replies then start with "MODE TEST".

## Setup

1. Copy `.env.example` to `.env` and fill it in (Experio access, Postgres password,
   MiniMax key and URL, n8n encryption key).
2. Start everything:

   ```bash
   docker compose up -d --build
   ```

3. Create the two n8n credentials the workflows use, with these exact ids. For example,
   from a JSON file copied into the n8n container, then deleted:

   ```json
   [
     {"id": "TarikPostgres001", "name": "Tarik Postgres", "type": "postgres",
      "data": {"host": "postgres", "port": 5432, "database": "<POSTGRES_DB>", "user": "<POSTGRES_USER>",
               "password": "<POSTGRES_PASSWORD>", "ssl": "disable"}},
     {"id": "MiniMaxAnthr0p1c", "name": "MiniMax (Anthropic-compatible)", "type": "anthropicApi",
      "data": {"apiKey": "<MODEL_API_KEY>", "url": "<MODEL_API_URL>"}}
   ]
   ```

   ```bash
   docker compose exec n8n n8n import:credentials --input=/tmp/credentials.json
   ```

4. Deploy both workflows (see below).
5. Open the chat: http://localhost:5678/webhook/7304d10e-ea71-4883-be8d-dcd560c62f4e/chat

## Changing a workflow

The workflow JSON files are generated. Edit the builder, not the JSON:

```bash
python3 workflows/build_tarik.py
```

```bash
python3 workflows/build_tarik_traitement.py
```

```bash
python3 workflows/build_tarik_dossier.py
```

`workflows/steps.py` holds what the builders share.

Then deploy. Importing a workflow unpublishes it, so publish it again and restart n8n:

```bash
docker compose cp workflows/tarik-traitement.json n8n:/tmp/tarik-traitement.json
docker compose exec n8n n8n import:workflow --input=/tmp/tarik-traitement.json
docker compose exec n8n n8n publish:workflow --id=TarikTraitement1
docker compose restart n8n
```

Same for `tarik.json` (id `Sca0Bh73nUct7ixj`) and `tarik-dossier.json` (id `TarikDossier0001`). To check the published version is the
new one, export the workflow (`n8n export:workflow --id=...`): `versionId` must equal
`activeVersionId`.

## Changing a tool

Edit the module in `mcp/`, then rebuild the container and run the checks:

```bash
docker compose up -d --build --force-recreate tarik-mcp
```

```bash
docker compose exec -T tarik-mcp python test_server.py
```

A new tool is registered by importing its module in `mcp/server.py`. To give it to the
agent, add it to the "Outils Experio" tools in `workflows/build_tarik.py`; to make it a
step of the sequence, add it to `CHAIN` in `workflows/build_tarik_traitement.py` and to
`STEPS` in `mcp/pipeline.py`.

## New dossiers

Tarik asks for the name, ICE and IF, and the model dossier whose configuration is copied, then
waits for the client's confirmation: Experio can't delete a dossier, only archive it. The model's
configuration is copied before the entries are imported, because the copy rewrites every setting
and would erase what the import detected. The import (FEC or grand livre, CSV or TXT) goes through
Experio's Importation screen, which creates the missing accounts, journals, tiers and fiscal years.
Finishing the onboarding hands the entries to Experio's processing.

## Real posting

Set `DRY_RUN=0` in `.env`, then recreate the tools container (it only reads `.env` when
recreated):

```bash
docker compose up -d --force-recreate tarik-mcp
```

A posted invoice can't simply be deleted in Experio: test on a test dossier.

## Following a run

- n8n, Executions: one run of "Tarik - Traitement" per batch of files or answer, one node per step.
- Postgres `tarik_results`: each step's outcome, by run and invoice. `tarik_pending`: the open questions.
- `docker compose logs tarik-mcp`: what Experio refused, in full (the agent only gets a short message).
