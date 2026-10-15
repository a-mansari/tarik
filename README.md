# sarnec-n8n

Tarik, Experio's accounting assistant, rebuilt on n8n from the LangGraph version in
`tarik-master`, to compare speed, maintenance, scaling and adaptability.

A client writes to Tarik (each cabinet's own n8n agent, by chat or by email) and sends invoices
or bank statements, or asks for a new dossier with its FEC. Tarik finds the dossier,
then a fixed sequence puts each document into Experio: draft, check against the scan,
correction, tiers, duplicate, account, TVA, journal, withholding, prepaid charges,
fiscal year and posting. When a step needs the client, Tarik asks, and the sequence
resumes from there with the answer.

## How it is built

| Part | What it does |
|------|--------------|
| n8n Agents section | Tarik: one native agent per cabinet, "Tarik - <cabinet's name>" (MiniMax-M3). It only finds the dossier, lists invoices and hands the work over to the workflows below. All are made from `workflows/tarik-agent.json` and the skills in `workflows/skills/` by `workflows/build_tarik_agents.py`, which lists the cabinets: edit those, not the agents in n8n. |
| `workflows/tarik-emails.json` | Every cabinet's mailbox, woken per cabinet by `tarik-mail-watch`: each email thread is answered by that cabinet's agent (`ask_agent`, through n8n's MCP `call_agent`, in the thread's own conversation). |
| `workflows/tarik-dossier.json` | A dossier's creation and setup: create it, copy a model dossier's configuration, import its FEC or grand livre, finish the onboarding. |
| `workflows/tarik-traitement.json` | The fixed sequence, one n8n node per step. Called by the agent (`traiter_fichiers`, mode `files`; the workflow also has `answer` and `resume`). |
| `mcp/` | The steps themselves: Python tools served over MCP (`tarik-mcp` container), in the `tarik` package (see below). They use Experio's own wizards and onchanges, never a parallel logic. |
| `n8n/` | n8n 2.43.5 patched (`patch.js`): a native agent's workflow tool input fixed to `{{threadId}}` gets the conversation's id, one fixed to `{{attachments}}` its attached files, and one fixed to `{{clientMessage}}` the client's latest message word for word ("Tarik - Dossier" needs the client's own words to skip the ICE/IF check), from n8n, not from the model. "Tarik - Traitement" and "Tarik - Dossier" save those files (`save_files`, once each by `source_id`). n8n's MCP `call_agent` also takes `newSession`, so an email thread's conversation has the thread's own id. The build fails if an n8n upgrade moves a patched line. |
| `mcp/tarik/core/cabinet.sql` | One cabinet's tables (received files, check results, step results, pending questions, mail), created in that cabinet's own Postgres schema. |

Rules the tools keep:

- Odoo, its URL, database and user are never shown to the agent or the client: it is "Experio".
- Every dossier tool needs a real dossier id and fails without one. Each cabinet is locked
  by its exact database name and Tarik's user id there (`<CABINET>__ODOO_UID`).
- `<CABINET>__DRY_RUN=1` (default): drafts and bank statements are created, but nothing else is written and nothing is posted.
  Replies then start with "MODE TEST".

## Cabinets

One Tarik serves several cabinets, and never mixes them. A cabinet is named by its exact
Experio database (`experio_cabinet_8`): its Vault path, `secret/tarik/cabinets/<cabinet>`. That name is also:

- **its address**: `tarik-mcp` answers at `http://tarik-mcp:8000/c/<cabinet>/mcp` only, and every
  call runs for that cabinet (`mcp/server.py`). There is no address without a cabinet, and the
  server is stateless, so no session carries one cabinet's call into another's.
- **its settings**, in that Vault path: its own Experio login (`ODOO_UID`, `ODOO_USER`, `ODOO_PASSWORD`),
  `DRY_RUN`, its mailbox's credentials and addresses (`IMAP_USER`, `IMAP_PASSWORD`, `SMTP_USER`,
  `SMTP_PASSWORD`, `SMTP_FROM`, `MAIL_MANAGER`, `MAIL_ALLOWED_RECIPIENTS`), its agent (`AGENT_ID`).
  What every cabinet shares is in `secret/tarik/shared`: the mail server (`IMAP_HOST`, `IMAP_PORT`,
  `IMAP_FOLDER`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_CERT_SHA256`), n8n's MCP token (`N8N_MCP_API_TOKEN`),
  the model (`MODEL_API_*`). Each setting is one or the other (`CABINET_SETTINGS`, `SHARED_SETTINGS` in
  `tarik/core/cabinets.py`): one in the wrong place, or still in `.env`, stops the server. No setting
  is ever shown to the agent.
- **its data**: a Postgres schema of the same name. A tool's connection only sees that schema.

Workflows and agents pass the cabinet as a fixed value, never chosen by a model: "Tarik - Traitement"
and "Tarik - Dossier" take it as their `cabinet` input; each cabinet's native agent has its own
MCP address and its own fixed `cabinet` tool input.

Each cabinet has its own Tarik mailbox (its `IMAP_USER`, `IMAP_PASSWORD`, `SMTP_*` in Vault).
`tarik-mail-watch` watches every cabinet that has one, and wakes "Tarik - Emails" for that cabinet
the moment an email arrives (IMAP IDLE) and every 2 minutes in case one was missed; the webhook only
answers its `X-Tarik-Webhook` header, the secret both share (`MAIL_WEBHOOK_SECRET` in Vault, the n8n
credential "Tarik mail webhook"; change both together). The workflow is
one for all cabinets: each run works in the cabinet the watcher named, every step at its address,
and the reply comes from that cabinet's own agent. An email thread's conversation id includes its
cabinet, as n8n's conversation ids are shared by every agent. Every cabinet's
`MAIL_ALLOWED_RECIPIENTS` is `@experio.ma,@sarnec.com,achraf.mansari.1@gmail.com`: Tarik writes to
those addresses only, and only answers senders among them.

To add a cabinet: give Tarik a user in that cabinet's Experio, add the cabinet to `CABINETS` in
`workflows/build_tarik_agents.py` and run it (`python3 workflows/build_tarik_agents.py`: it creates
the cabinet's agent, updates the others, and prints each `AGENT_ID`), write its settings to Vault
(`AGENT_ID` and `MAIL_ALLOWED_RECIPIENTS` included, see Vault below), then
`docker compose up -d --force-recreate tarik-mcp tarik-mail-watch`: the watcher picks up its mailbox. Its schema is created at start;
a cabinet missing a setting stops the server instead of starting half configured.

Tool calls wait their turn: at most `TARIK_CALLS_PER_CABINET` at once for one cabinet (10), so one
cabinet's long batch never holds up the others, and `TARIK_CALLS_TOTAL` in all (40).

`mcp/tests/core/test_cabinets.py` checks the isolation against the real Postgres and over HTTP,
with two throwaway cabinets: names, scopes, settings, threads, schemas, addresses, 200 overlapping
calls, the call caps, and every tool's answers, with Experio unreachable, empty, refusing or
logging in the wrong user, for any setting. `mcp/tests/n8n/test_agent_memory.py` checks, on the
running n8n with the real model, that two agents never share memory nor conversations; it needs
n8n's MCP server on (Settings > n8n MCP) and its token in Vault (`N8N_MCP_API_TOKEN`), and skips without:

```bash
docker compose exec -T tarik-mcp python -m pytest tests/n8n
```

## Skills

n8n keeps an agent's skills (instructions it loads when a request matches) per agent. They are
written once, one file per skill in `workflows/skills/` (front matter `name`, `description`, then
the instructions), and `workflows/build_tarik_agents.py` gives every agent the same copy, found by
name, updated in place, and removed from all agents when its file goes.

There is none for now. A "Nouveau dossier" skill was tested (2026-10-15, 10 new-dossier requests
each way, the tools' fields listed in their descriptions both times): with it, MiniMax loaded it
10 times in 10 and made 4 fully right calls; without it, 2. Both are poor for the same reasons,
which the skill does not change: calls without their fields (`{}`), and ICE or IF sent as numbers,
which loses their leading zeros. `allowedTools` in a skill is
not enforced by n8n 2.43: a skill never restricts tools; the workflows and MCP tools do.

## Vault

Tarik's secrets and per-cabinet settings are in Vault (`vault/`), not in `.env`. Locally, a Vault
container (`vault` in `docker-compose.yml`, file storage in the `vault_data` volume) unseals itself
at start with the key `vault/setup.sh` keeps in `vault/.local/` (git-ignored, this machine only).
`tarik-mcp` logs in with its AppRole (`vault/.local/approle/`, mounted read-only), whose policy only
reads `secret/tarik/*`; it reads Vault at start and every 5 minutes, so a changed secret applies
without a restart, and carries on with the last values while Vault is out.

Once, on a new machine:

```bash
docker compose up -d vault
docker compose exec -T -e HOST_UID=$(id -u) vault sh /vault/config/setup.sh
```

Then write the settings (or move them from an older `.env`: `python3 vault/migrate_env.py`):

```bash
docker compose exec -e VAULT_TOKEN="$(cat vault/.local/unseal/root_token)" vault vault kv put secret/tarik/shared IMAP_HOST=... N8N_MCP_API_TOKEN=...
docker compose exec -e VAULT_TOKEN="$(cat vault/.local/unseal/root_token)" vault vault kv put secret/tarik/cabinets/experio_cabinet_8 ODOO_UID=... AGENT_ID=...
```

`kv patch` changes one setting and keeps the others. Vault's own page is at http://127.0.0.1:8200
(the root token logs in). On the public server, use a real Vault instead: TLS, its own unseal
(operators' key shares or a KMS, never a key file next to it), and the same paths, policy and role.

## Setup

1. Copy `.env.example` to `.env` and fill it in (Postgres password, n8n encryption key, Experio's
   URL), and set up Vault with Tarik's settings (see Vault). n8n keeps its own data in Postgres too
   (database `N8N_POSTGRES_DB`), created once by:

   ```bash
   docker compose up -d postgres
   docker compose exec postgres sh -c 'createdb -U "$POSTGRES_USER" "$N8N_POSTGRES_DB"'
   ```
2. Start everything:

   ```bash
   docker compose up -d --build
   ```

3. Create the n8n credential the agents use, with this exact id. For example, from a JSON file
   copied into the n8n container, then deleted:

   ```json
   [
     {"id": "MiniMaxAnthr0p1c", "name": "MiniMax (Anthropic-compatible)", "type": "anthropicApi",
      "data": {"apiKey": "<MODEL_API_KEY>", "url": "<MODEL_API_URL>"}}
   ]
   ```

   ```bash
   docker compose exec n8n n8n import:credentials --input=/tmp/credentials.json
   ```

4. Deploy the three workflows (see below).
5. Turn on n8n's MCP server (Settings > n8n MCP), put its token in Vault (`N8N_MCP_API_TOKEN` in
   `secret/tarik/shared`), and create each cabinet's agent (see Cabinets). Its chat is in the Agents section.

## Changing a workflow

The workflow JSON files are generated. Edit the builder, not the JSON:

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

Same for `tarik-dossier.json` (id `TarikDossier0001`) and `tarik-emails.json` (id `TarikEmails00001`). To check the published version is the
new one, export the workflow (`n8n export:workflow --id=...`): `versionId` must equal
`activeVersionId`.

## Changing a tool

The tools live in `mcp/tarik/`, one package per kind of work, and their tests in
`mcp/tests/`, in the same tree:

| Package | What it holds |
|---------|---------------|
| `tarik/core/` | The MCP server and step results (`app`), the cabinets and the current one (`cabinets`), Experio calls and guards (`experio`), the vision model, documents, the connection check. |
| `tarik/invoices/` | "Tarik - Traitement": one module per step, in sequence order, `pipeline` for its bookkeeping, `listing` for `list_invoices`. |
| `tarik/dossiers/` | Finding dossiers (`search`), "Tarik - Dossier" (`onboarding`), collaborators and who may work on which dossier (`people`). |
| `tarik/emails/` | "Tarik - Emails": reading the mailbox (`inbox`), the agent's reply (`agent`), sending replies (`sending`). |

`mcp/server.py` and `mcp/watcher.py` are the two entry points (the MCP server, the IMAP
IDLE watcher of one cabinet's mailbox: `python watcher.py <cabinet>`). Edit a module, then rebuild the containers and run the tests:

```bash
docker compose up -d --build --force-recreate tarik-mcp tarik-mail-watch
```

```bash
docker compose exec -T tarik-mcp python -m pytest
```

A new tool is registered by importing its module in `mcp/server.py`. To give it to the
agent, add it to the Experio MCP server's tool list in the agent's configuration; to make it a
step of the sequence, add it to `CHAIN` in `workflows/build_tarik_traitement.py` and to
`STEPS` in `mcp/tarik/invoices/pipeline.py`. Its tests go in the matching file under `mcp/tests/`.

## New dossiers

Tarik asks for the name, ICE and IF, and the model dossier whose configuration is copied, then
waits for the client's confirmation: Experio can't delete a dossier, only archive it. The model's
configuration is copied before the entries are imported, because the copy rewrites every setting
and would erase what the import detected. The import (FEC or grand livre, CSV or TXT) goes through
Experio's Importation screen, which creates the missing accounts, journals, tiers and fiscal years.
Finishing the onboarding hands the entries to Experio's processing.

## Real posting

Set that cabinet's `DRY_RUN` to `0` in Vault; it applies within 5 minutes, or at once after a restart:

```bash
docker compose exec -e VAULT_TOKEN="$(cat vault/.local/unseal/root_token)" vault vault kv patch secret/tarik/cabinets/experio_cabinet_8 DRY_RUN=0
docker compose restart tarik-mcp
```

A posted invoice can't simply be deleted in Experio: test on a test dossier.

## Following a run

- n8n, Executions: one run of "Tarik - Traitement" per batch of files or answer, one node per step.
- Postgres `tarik_results`: each step's outcome, by run and invoice. `tarik_pending`: the open questions.
- `docker compose logs tarik-mcp`: what Experio refused, in full (the agent only gets a short message).
