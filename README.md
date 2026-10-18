# sarnec-n8n

Tarik, Experio's accounting assistant, built on n8n.

A client writes to Tarik (each cabinet's own n8n agent, by chat or by email) and sends invoices
or bank statements, or asks for a new dossier with its FEC. Tarik finds the dossier,
then a fixed sequence puts each document into Experio: draft, check against the scan,
correction, tiers, duplicate, account, TVA, journal, withholding, prepaid charges,
fiscal year and posting. When a step needs the client, Tarik asks, and the sequence
resumes from there with the answer.

Documentation: [architecture](docs/architecture.md), [tools and workflows](docs/tools-and-workflows.md),
[using Tarik](docs/using-tarik.md) (what it can do, and how to write to it).

## How it is built

| Part | What it does |
|------|--------------|
| n8n Agents section | Tarik: one native agent per cabinet, "Tarik - <cabinet's name>" (MiniMax-M3). It only finds the dossier, lists invoices and hands the work over to the workflows below. All are made from `workflows/tarik-agent.json` and the skills in `instructions/skills/` by `workflows/build_tarik_agents.py`, which lists the cabinets: edit those, not the agents in n8n. |
| `workflows/tarik-emails.json` | Every cabinet's mailbox, woken per cabinet by `tarik-mail-watch`: each email thread is answered by that cabinet's agent (`ask_agent`, through n8n's MCP `call_agent`, in the thread's own conversation). |
| `workflows/tarik-dossier.json` | A dossier's creation and setup: create it, copy a model dossier's configuration, import its FEC or grand livre, finish the onboarding. |
| `workflows/tarik-modification.json` | Changing an existing dossier's identity fields (name, type, ICE, IF, RC, patente, T.S.C, capital, CNSS, address), only to values the client wrote. |
| `workflows/tarik-traitement.json` | The fixed sequence, one n8n node per step. Called by the agent (`traiter_fichiers`, mode `files`; `repondre`, mode `answer`) and by "Tarik - Emails" (mode `resume`). |
| `mcp/` | The steps themselves: Python tools served over MCP (`tarik-mcp` container), in the `tarik` package (see below). They use Experio's own wizards and onchanges, never a parallel logic. |
| `n8n/` | n8n 2.43.5 patched (`patch.js`): an agent's workflow tool may run `TARIK_AGENT_TOOL_TIMEOUT_MS` (5 minutes, not n8n's fixed 2); a native agent's workflow tool input fixed to `{{threadId}}` gets the conversation's id, one fixed to `{{attachments}}` its attached files, and one fixed to `{{clientMessage}}` the client's latest message word for word (the dossier tools take only values from the client's own words), from n8n, not from the model. "Tarik - Traitement" and "Tarik - Dossier" save those files (`save_files`, once each by `source_id`). n8n's MCP `call_agent` also takes `newSession`, so an email thread's conversation has the thread's own id. The build fails if an n8n upgrade moves a patched line. |
| `mcp/tarik/core/cabinet.sql` | One cabinet's tables (received files, check results, step results, pending questions, dossiers left waiting, mail, the client's messages), created in that cabinet's own Postgres schema. |

Rules the tools keep:

- Odoo, its URL, database and user are never shown to the agent or the client: it is "Experio".
- Every dossier tool needs a real dossier id and fails without one. Each cabinet is locked
  by its exact database name and Tarik's user id there (its `ODOO_UID`).
- A cabinet's `DRY_RUN=1` (default): drafts and bank statements are created, but nothing else is written and nothing is posted.
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
  `tarik/core/cabinets.py`): one in the wrong place, or also in the container's environment, stops the server. No setting
  is ever shown to the agent.
- **its data**: a Postgres schema of the same name. A tool's connection only sees that schema.

Workflows and agents pass the cabinet as a fixed value, never chosen by a model: "Tarik - Traitement",
"Tarik - Dossier" and "Tarik - Modification" take it as their `cabinet` input; each cabinet's native agent has its own
MCP address and its own fixed `cabinet` tool input.

Each cabinet has its own Tarik mailbox (its `IMAP_USER`, `IMAP_PASSWORD`, `SMTP_*` in Vault).
`tarik-mail-watch` watches every cabinet that has one: the moment an email arrives (IMAP IDLE), and every
15 minutes in case one was missed, it stores the new emails in their threads, tells the manager of refused
senders, and wakes "Tarik - Emails" for that cabinet when a thread waits for an answer; the webhook only
answers its `X-Tarik-Webhook` header, the secret both share (`MAIL_WEBHOOK_SECRET` in Vault, the n8n
credential "Tarik mail webhook"; change both together). The workflow is
one for all cabinets: each run works in the cabinet the watcher named, every step at its address,
and the reply comes from that cabinet's own agent. An email thread's conversation id includes its
cabinet, as n8n's conversation ids are shared by every agent. Every cabinet's
`MAIL_ALLOWED_RECIPIENTS` is `@experio.ma,@sarnec.com,achraf.mansari.1@gmail.com`: Tarik writes to
those addresses only, and only answers senders among them.

n8n stops an agent's workflow tool after 300 s (`TARIK_AGENT_TOOL_TIMEOUT_MS`, patched; n8n's own is 120 s),
so a processing run stops itself after 240 s: the invoices still going wait at the step they reached. A new
dossier's onboarding waits until its imported lines are a minute old (Experio's rule): when the run's 260 s
would not cover that, the dossier is left waiting
(`tarik_setups`). When Tarik's reply leaves drafts or a dossier waiting, the thread is followed up for 30
minutes: once Experio has read the drafts (a dossier: at the next tick, every 15 minutes), the watcher's tick wakes
"Tarik - Emails", which takes them up again without the agent ("Tarik - Traitement", then "Tarik - Dossier",
each in mode `resume`, no time limit) and sends the outcome in the thread.

To add a cabinet: give Tarik a user in that cabinet's Experio, add the cabinet to `CABINETS` in
`workflows/build_tarik_agents.py` and run it (`python3 workflows/build_tarik_agents.py`: it creates
the cabinet's agent, updates the others, and prints each `AGENT_ID`), write its settings to Vault
(`AGENT_ID` and `MAIL_ALLOWED_RECIPIENTS` included, see Vault below), then
`docker compose up -d --force-recreate tarik-mcp tarik-mail-watch`: the watcher picks up its mailbox. Its schema is created at start;
a cabinet missing a setting stops the server instead of starting half configured.

Tool calls wait their turn: at most `TARIK_CALLS_PER_CABINET` at once for one cabinet (10), so one
cabinet's long batch never holds up the others, and `TARIK_CALLS_TOTAL` in all (40).

`mcp/tests/isolation/` checks the isolation against the real Postgres and over HTTP,
with two throwaway cabinets: names, scopes, settings, threads, schemas, addresses, 200 overlapping
calls, the call caps, and every tool's answers, with Experio unreachable, empty, refusing or
logging in the wrong user, for any setting. `mcp/tests/n8n/test_agent_memory.py` checks, on the
running n8n with the real model, that two agents never share memory nor conversations; it needs
n8n's MCP server on (Settings > n8n MCP) and its token in Vault (`N8N_MCP_API_TOKEN`), and skips without.
`mcp/tests/n8n/test_at_the_end.py` runs "Tarik - Test Merge" (`TarikMergeTest01`, imported like the
other workflows) to check n8n still runs "Résumé" after every step: run it after each n8n upgrade.
These live tests run only when asked:

```bash
python3 mcp/tests/run.py -m live
```

`mcp/tests/llm/test_relay.py` compares, on the same model, the reply a client gets from a ten-invoice run's summary
as it was (every step) and as it is now: every fact must reach them, and the reply's length, time and tokens are
printed. `mcp/tests/llm/` also measures the decisions the agent makes from the client's replies on the real model (Vault's
`MODEL_API_*`, the agents' MiniMax-M3), shown Tarik's instructions, skills and tools as the agents have them: what
it reads as oui or non, which proposed tiers the client chose, a clear yes to a confirmation, the invoices' filter.
Each kind must be right on average (90 %) and almost never decide what the client did not (5 %, never for a
confirmation). The tool calls are only read: nothing reaches n8n or Experio. Run them after changing the agent's
instructions or skills, `REPEAT` times each case for a steadier average (`-s` shows each miss). The model thinks,
as the agents' does; `THINKING=off` measures it without (2026-10-17, REPEAT=3: 3 decisions the client did not make
without thinking, none with it):

```bash
REPEAT=3 python3 mcp/tests/run.py -m llm -s
```

## Instructions

Everything the model is told is in `instructions/`, nowhere else: `skills/` for the agent (below), `tools/` for
the prompts the Python tools give the model (one file per prompt, front matter `used_by`: the tool and the file
reading it, then the prompt, sent word for word). The container mounts `instructions/tools/`, so a prompt
changed there applies on the next restart, no rebuild. A style test checks each file is read by the file it names.

## Skills

n8n keeps an agent's skills (instructions it loads when a request matches) per agent. They are
written once, one file per skill in `instructions/skills/` (front matter `name`, `description`, then
the instructions), and `workflows/build_tarik_agents.py` gives every agent the same copy, found by
name, updated in place, and removed from all agents when its file goes.

Four skills, each a whole process in plain decisions ("If ... → do ..."), its steps and a short
"never" list: "Nouveau dossier" (a new dossier, or a FEC or grand livre for one), "Modification de
dossier", "Traitement des documents" (invoices and bank statements) and "Réponse à une question" (the
client's answer to a question Tarik asked while processing them). The base instructions keep the rules that always hold,
and send each case to its skill: "charge la compétence ... et suis-la, dans ce même tour".

A skill names tools by the agent's own tool names (`Experio_parametrer_dossier`, `Experio_traiter_fichiers`,
`Experio_find_dossier`), never by a workflow's id or name: the agent's configuration maps each tool to its
workflow, with the values the model never sets (cabinet, conversation, client's message). The build
refuses a skill naming a tool or field the agents do not have.

Tested on GECAFISC (2026-10-15, dry run, MiniMax-M3): 10 new-dossier requests and 10 document
requests all ended right (the dossier set up with every field right; the right dossier processed
once; the client asked when the dossier was unclear), and 2 unrelated requests loaded no skill.
`allowedTools` in a skill is not enforced by n8n 2.43: a skill never restricts tools; the workflows
and MCP tools do.

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

Then write the settings:

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
     {"id": "MiniMaxNative001", "name": "MiniMax", "type": "minimaxApi",
      "data": {"apiKey": "<MODEL_API_KEY>", "region": "international"}}
   ]
   ```

   ```bash
   docker compose exec n8n n8n import:credentials --input=/tmp/credentials.json
   ```

4. Deploy the workflows (see below).
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

Same for `tarik-dossier.json` (id `TarikDossier0001`), `tarik-modification.json` (id `TarikModif000001`) and `tarik-emails.json` (id `TarikEmails00001`). To check the published version is the
new one, export the workflow (`n8n export:workflow --id=...`): `versionId` must equal
`activeVersionId`.

## Changing a tool

The tools live in `mcp/tarik/`, one package per kind of work, and their tests in
`mcp/tests/`, in the same tree:

| Package | What it holds |
|---------|---------------|
| `tarik/core/` | The values shared by every module: services, timeouts, Experio's codes, thresholds (`config`), the MCP server, step results and the log (`app`), comparing names and words (`text`), the cabinets and the current one (`cabinets`), Experio calls, guards and test mode (`experio`), the client's own words in a conversation (`conversation`), numbers as clients write them (`numbers`), the settings in Vault (`vault`), the vision model, documents, the connection check. |
| `tarik/invoices/` | "Tarik - Traitement": one module per step, in sequence order, `pipeline` for its bookkeeping, `listing` for `list_invoices`. |
| `tarik/dossiers/` | Finding dossiers (`search`), creating and modifying one (`identity`), its setup: configuration, entries, onboarding (`setup`), collaborators and who may work on which dossier (`people`). |
| `tarik/emails/` | "Tarik - Emails": reading the mailbox (`inbox`), the agent's reply (`agent`), sending replies (`sending`). |

`mcp/server.py` and `mcp/watcher.py` are the two entry points (the MCP server, the IMAP
IDLE watcher of every cabinet's mailbox: `python watcher.py`). Edit a module, run the tests that
cover what you changed (in a throwaway container on your working copy), then rebuild the containers:

```bash
python3 mcp/tests/run.py
```

```bash
docker compose up -d --build --force-recreate tarik-mcp tarik-mail-watch
```

The Python code has one style: ruff's formatter and linter for Python 3.14, lines of 120 at most
(`pyproject.toml`; `tests/style` checks it). To apply it:

```bash
docker compose run --rm --no-deps --user "$(id -u):$(id -g)" -v "$PWD:/repo" -w /repo tarik-mcp sh -c "ruff check --no-cache --fix . && ruff format --no-cache ."
```

`run.py` picks the tests that import the changed files; `--all` runs every test, `-m <category>` one
category. Each test has one: `security`, `functionality`, `communication`, `technical`, `style` (lint,
workflows rebuilt from their scripts, tool names), `live` (the real n8n and model, only with `-m live`), `llm`
(the real model's decisions, only with `-m llm`).

A new tool is registered by importing its module in `mcp/server.py` (`TOOL_MODULES`). To give it to the
agent, add it to the Experio MCP server's tool list in the agent's configuration; to make it a
step of the sequence, add it to `CHAIN` in `workflows/build_tarik_traitement.py` and to
`STEPS` in `mcp/tarik/invoices/pipeline.py`. Its tests go in the matching file under `mcp/tests/`.

## New dossiers

The name is enough: Tarik creates the dossier at once, with the ICE and IF only when the client wrote
them, and asks first only when a dossier has a very similar name, or before skipping Experio's ICE/IF
check (Experio can't delete a dossier, only archive it). A model dossier's configuration, when the client
names one, is copied before the entries are imported, because the copy rewrites every setting and would
erase what the import detected. The import (FEC or grand livre, CSV, TXT or Excel) goes through
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
