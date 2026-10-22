# Tarik

Tarik, Experio's accounting assistant. A client writes to Tarik and sends invoices or bank statements, or asks for a
new dossier with its FEC; Tarik finds the dossier, then a fixed sequence puts each document into Experio (draft,
check against the scan, correction, tiers, duplicate, account, TVA, journal, withholding, prepaid charges, fiscal
year, the dossier's settings), posted when the client asks. When a step needs the client, Tarik asks, and the
sequence resumes from there with the answer. What it can do, and how to write to it: [docs/using-tarik.md](docs/using-tarik.md).

Built on LangGraph's MIT-licensed parts, with n8n's strengths built on top: an agent core that runs every workflow
without n8n's limits, and a UI any colleague can read, follow and change without reading code.

Why: n8n's Sustainable Use License allows internal use only, and LangGraph's own server (`langgraph-api`, behind
`langgraph dev` and Studio) is Elastic-2.0. Tarik is to be offered to cabinets as a hosted service, so this stack uses
neither: only `langgraph`, `langgraph-checkpoint(-postgres)` and LangChain (MIT), starlette and uvicorn (BSD), and
psycopg (LGPL, as an unmodified library). Check the licence of every new dependency.

## A stack of its own

From n8n it takes only ideas (its canvas, runs step by step, sub-workflows, the prompt log, untrusted data); nothing
of the n8n stack runs in it:

- **Its tools** (`tarik/`): every Experio step, mail and file, forked from tarik-mcp (branch `main`, retired) and called in-process (no MCP
  hop), each call inside its cabinet (`using(cabinet)`): a run names its cabinet once, never the model. Free of n8n's
  workarounds: no 5-minute tool cap to dodge, steps recorded as they end.
- **Its content**, in its Postgres only (table `content_versions`): the agent's instructions and tools' descriptions,
  the skills, the tools' prompts, the UI's words. Edited on the Contenu page, with its history; exported and imported
  there (all of it, a group, or one section) to keep it or to fill another stack. No file holds it: back up the
  database, or keep an export. `docs/using-tarik.md` is for the cabinets.
- **Its Postgres** (service `db`): runs, conversations, layouts (schema `orchestrator`) and each cabinet's schema.
- **Its model key**: Vault `secret/tarik/orchestrator` (`MODEL_API_KEY`, `MODEL_API_URL` optional).
- **Its mail watcher** (`engine/mail.py`), for every cabinet with a mailbox of its own (`IMAP_USER`).
- **Its Vault** (service `vault`): read-only for Tarik, an AppRole on `secret/tarik/*`, and the Cabinets page's own,
  which may only update `secret/tarik/cabinets/*`; both set up by `vault/setup.sh`.

The n8n implementation it replaced (branch `main`) was retired on 2026-10-20: its stack stopped and removed, its data
backed up outside the repository. Only the cabinets' Experio is outside this stack.

## Layout

| Path | What |
|---|---|
| `app/server.py` (`python -m app`) | The HTTP API; checks every cabinet and makes its schema at start. |
| `docker-compose.yml` | The server, its Postgres and Vault. |
| `vault/` | Vault's configuration and setup. |
| `docs/using-tarik.md` | For the cabinets: what Tarik can do, and how to write to it. |
| `sql/` | Every SQL statement, named (`-- name: ...`), read as `Q.<name>` (`tarik/core/queries.py`); no SQL in the Python. |
| `tarik/` | The tools (`TOOLS`, registered by `@tool` / `@step_tool`), Experio, mail, the vision model. |
| `app/` | What the pages, the Experio API and `app/users.py` stand on, besides the workflows: accounts (`accounts.py`), the Journal (`audit.py`), each cabinet's settings in Vault (`cabinet_settings.py`), the canvases' layouts (`layouts.py`). `engine/` never imports it but to record a content change in the Journal. |
| `engine/tools.py` | One tool call: in a worker thread, in its cabinet, at most 10 at once per cabinet; a refusal's text kept for the client, a crash logged and told plainly; a step's outcome recorded for the run's summary. |
| `engine/live.py` | What changed (a run, a cabinet's runs, a conversation), for the pages to refresh near real time: each holds one event stream and fetches only when something it shows changed, never while its tab is hidden. One server process. |
| `engine/runs.py` | Every run and each of its steps, in Postgres (schema `orchestrator`); the agent's conversations next to them. |
| `engine/agent.py` | The agent: instructions and tools' descriptions from the content's `agent.json`, its skills (`skills/*.md`), tool output from Experio tagged as untrusted data; its model (MiniMax-M3, a 1024-token thinking budget, this stack's own key). |
| `engine/prompts.py` | Each model call's prompt saved in its cabinet's schema, read on the LLM Calls page (each call, its conversation and its run). |
| `engine/content.py` | What the team edits: every version of each content document (who, when, why), Postgres its only home, the latest in use at the next run; a change that would break its reader is refused. |
| `app/api.py` | The Experio app's API: cabinets created, changed or deactivated, served at once (guide: `docs/experio-api.md`). |
| `docs/tests.md` | Every test by category, and how to add a client situation to the live model tests. |
| `app/cabinet_settings.py`, `ui/settings_pages.py` | Each cabinet's own settings, read and changed on the Cabinets page, written to Vault with its own AppRole, each change kept. |
| `app/accounts.py`, `app/users.py` | The team's accounts (scrypt hashes), their role (admin or member) and sessions; made on the Comptes page (`ui/accounts_pages.py`, a one-time link to set the password) or by `app/users.py`. |
| `engine/mail.py` | The mailbox watcher: starts an Emails run when a thread waits. |
| `engine/traitement.py`, `dossier.py`, `emails.py`, `chat.py` | The workflows, as graphs (`chat.py`: the Discussions page's, and the asking shared with email threads). |
| `engine/graphs.py` | Each workflow's shape for the canvas, read from the compiled graphs (never drawn by hand). |
| `app/layouts.py` | How the team arranged each canvas (node places, edge bends and style), checked and kept in Postgres. |
| `ui/` | The team's pages (French): `pages.py`, `content_pages.py` (Contenu), `chat_pages.py` (Discussion), their templates, a stylesheet, scripts, no build step; `ui/static/vendor/` holds Cytoscape.js and dagre (MIT). |
| `tests/` | The tools' and the isolation tests (`python -m pytest`; the real model's with `-m llm`). |
| `check.py` | Every tool a graph calls exists; every step drawn is described. |
| `run.py` | Run a graph from a terminal and watch it step by step. |

## Run it

Its `.env` (never in git, from `.env.example`) gives `POSTGRES_PASSWORD` and `ODOO_URL`; `.env.example` also lists
every setting it reads from Vault.

```bash
docker compose up --build -d
```

| Call | What it does |
|---|---|
| `GET /ok` | The server is up, and its graphs. |
| `POST /runs/<graph>` | `{input, thread_id?, cabinet?, session_id?, wait?}`. Waits for the run by default; `wait: false` answers its `run_id` at once. |
| `GET /runs` | The latest runs (`?cabinet=`, `?graph=`, `?limit=`). |
| `GET /runs/<run_id>` | A run: status, input, output or error, and its steps (`?after=<step id>`: only the newer steps, and its status). |
| `GET /graphs/<graph>` | A workflow's shape for the canvas. |
| `GET` / `PUT` / `DELETE /graphs/<graph>/layout` | How the team arranged its canvas: read, saved, or forgotten. |

The team's pages, each cabinet picked in the sidebar (under its name in Experio, its database beside it) and
the pages showing that cabinet's: http://127.0.0.1:2024/ui/accueil (its dashboard: its last 30 days in four
numbers, and a message to Tarik that opens a conversation in place), /ui/executions (runs), /ui/discussion (Discussions: every conversation, the ones written there and the email threads, read-only), /ui/workflows
(workflows), /ui/llm-calls (LLM Calls: each model call, what it was sent), /ui/tarik-prompts (Tarik prompts: what Tarik is told, edited with its history),
/ui/cabinets (each cabinet's settings), /ui/comptes (the accounts), /ui/logs-serveur (the Logs serveur page, who did what: logins, accounts,
content, settings, layouts, runs started by hand; `app/audit.py`, never a password or a link). Each person logs in with their own account, made
by an admin on the Comptes page (a one-time link, 72 h, for the person to choose their password; a new link for a
forgotten one) or in the container: `python -m app.users add <email> "<name>"` (the password is asked; `disable` closes
one). The last admin can be neither made a member nor closed. A new account is a member: it
reads every page and writes to Tarik on Discussion. An admin (`python -m app.users role <email> admin`) also changes what
everyone's Tarik uses: the content, the cabinets' settings (read too), the canvases' layout, and starts runs by hand.
The server refuses these to a member (`admin_only` in `app/server.py`), whatever a page shows.

Every call but `/ok` and the login page needs a session, or for scripts `TARIK_GRAPH_TOKEN` as a bearer token. A
change made with a session must come from the pages themselves (Origin checked). Graphs: `agent`, `traitement`,
`dossier`, `emails`. In the container: `python run.py <graph> '<input json>' [--thread name]`, `python check.py`,
`python -m pytest`.

Before each commit, `.githooks/pre-commit` runs the style check (ruff, as `pyproject.toml` sets it) in the server
container. Enable it once per clone: `git config core.hooksPath .githooks`.

## Plan

What non-technical colleagues change is content, not logic: the agent's instructions and skills, the tools' prompts
and questions, cabinets' settings. Workflows stay Python, reviewed by developers, and the UI shows them as diagrams
with every run live, step by step.

1. **Engine and server** — done: the four graphs, the agent with its memory in Postgres, runs and steps kept and
   streamed live; parity with the n8n workflows checked step by step (2026-10-20), then the stack made its own.
2. **Runs in the UI** — done: the runs of each cabinet (refreshed on their own), each run step by step as it happens,
   in plain words (what was asked, what the client reads, each step's outcome, its whole output on demand), and for
   the agent the conversation with its thinking and the tools it used; the exact prompt of each model call (Prompts).
   What a run was asked reads in plain words too: each input under its name, a coded value in words (labels.json:
   inputs, input_values; `check.py` fails on an input without its name), an email thread as its sender and subject.
3. **Workflows in the UI** — done: each workflow drawn as a canvas, as n8n draws one (steps left to right, a file or
   an invoice as a framed group, conditional paths dashed; pan, zoom, "Au début", "Tout voir"); a run painted over it,
   live while it goes on (each step coloured by its outcome, ×n when it ran for several invoices); a click on a step
   opens what it did in that run. A sub-workflow (a file, an invoice, Traitement inside an email follow-up) shows as
   one node, as n8n shows one, coloured by the worst outcome inside it; double-click (or "Déplier") opens it in place.
   The canvas is a map on a 20 px grid: steps dragged snap to it, an edge takes bend points (dragged, snapped,
   removed with a double-click) and a route style (right angles, straight, curved), and "Enregistrer la disposition"
   keeps the arrangement in Postgres (table `layouts`, one per workflow, shared by the team); "Disposition
   automatique" forgets it. On each run's page and on each workflow's page with its recent runs. Each step explained
   in plain words in its panel.
4. **Login, then content in the UI, with history** — done: each person's account (`app/users.py`); the agent's
   instructions and tools' descriptions, its skills, the tools' prompts and the pages' words, each as a plain form;
   every change kept (who, when, why), each version's changes shown, any version brought back in one click; a change
   naming a tool Tarik lacks, emptying a text or reshaping a document is refused. Then each cabinet's own settings,
   on the Cabinets page (`app/cabinet_settings.py`): its Experio login, its mailbox, who may receive Tarik's emails,
   simulation or real writes (confirmed first). Vault stays their only home, written with the page's own AppRole
   (`tarik-settings`: read and update `secret/tarik/cabinets/*` only, never the shared settings nor a new cabinet);
   a secret is never shown, only replaced; a change is written over the version the page showed only, kept (who,
   when, why, which settings; never a secret's value: table `settings_changes`) and in use at once.
5. **Mail** — done: a cabinet switched over from the n8n stack; then the email test and `-m llm` again.
6. **Chat with file upload** — done: the Discussions page, for the team (logged in), per cabinet. Each message, with
   its files (the email's types, 10 per message, 20 MB each), is a run of the "Discussion" workflow: the files saved
   in the cabinet's schema first, then Tarik answers in the conversation's own thread, reminded of files left
   untaken as for an email. As in n8n's chat, each tool Tarik calls shows as it runs, then its reply (markdown, its
   raw HTML never kept), and each file sent can be downloaded again. No sender is checked: whoever is logged in
   writes.

## Lessons kept from the first port

- The model needs its thinking budget: without it the agent decides what the client did not (`-m llm`: 3 % wrong
  decisions without, 1 % with).
- An answer's outcome is recorded under the step it answers, or a question asked again can never be answered.
- A step's own arguments are positional: a tool may take a `run_id` of its own.
- Tool output from Experio reaches the model as untrusted data (n8n's guard, rebuilt).
- Two invoices of one message run one after another: side by side, both could create the same new tiers.
