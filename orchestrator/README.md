# Tarik's orchestrator

Tarik rebuilt on LangGraph's MIT-licensed parts, with n8n's strengths built on top: an agent core that runs every
workflow without n8n's limits, and a UI any colleague can read, follow and change without reading code.

Why: n8n's Sustainable Use License allows internal use only, and LangGraph's own server (`langgraph-api`, behind
`langgraph dev` and Studio) is Elastic-2.0. Tarik is to be offered to cabinets as a hosted service, so this stack uses
neither: only `langgraph`, `langgraph-checkpoint(-postgres)` and LangChain (MIT), starlette and uvicorn (BSD), and
psycopg (LGPL, as an unmodified library). Check the licence of every new dependency.

## A stack of its own

From n8n it takes only ideas (its canvas, runs step by step, sub-workflows, the prompt log, untrusted data); nothing
of the n8n stack runs in it:

- **Its tools** (`tarik/`): every Experio step, mail and file, forked from tarik-mcp (branch `main`) and called in-process (no MCP
  hop), each call inside its cabinet (`using(cabinet)`): a run names its cabinet once, never the model. Free of n8n's
  workarounds: no 5-minute tool cap to dodge, steps recorded as they end.
- **Its content** (`content/`): the agent's instructions and tools' descriptions, the skills, the tools' prompts, the
  UI's words; `docs/using-tarik.md` for the cabinets.
- **Its Postgres** (service `db`): runs, conversations, layouts (schema `orchestrator`) and each cabinet's schema.
- **Its model key**: Vault `secret/tarik/orchestrator` (`MODEL_API_KEY`, `MODEL_API_URL` optional).
- **Its mail watcher** (`engine/mail.py`), for the cabinets listed in `MAIL_BY_ORCHESTRATOR` (same Vault path) only:
  a mailbox is answered by one stack, never both.

Shared with the n8n stack: Vault (read-only AppRole on `secret/tarik/*`, reached on its Docker network) and the
cabinets' Experio. The n8n implementation (branch `main`) keeps running until a cabinet is switched over.

## Layout

| Path | What |
|---|---|
| `server.py` | The HTTP API; checks every cabinet and makes its schema at start. |
| `tarik/` | The tools (`TOOLS`, registered by `@tool` / `@step_tool`), Experio, mail, the vision model, `cabinet.sql`. |
| `engine/tools.py` | One tool call: in a worker thread, in its cabinet, at most 10 at once per cabinet; a refusal's text kept for the client, a crash logged and told plainly; a step's outcome recorded for the run's summary. |
| `engine/runs.py` | Every run and each of its steps, in Postgres (schema `orchestrator`); the agent's conversations next to them. |
| `engine/agent.py` | The agent: instructions and tools' descriptions from `content/agent.json`, skills from `content/skills/`, tool output from Experio tagged as untrusted data. |
| `engine/model.py` | MiniMax-M3 with a 1024-token thinking budget, with this stack's own key. |
| `engine/prompts.py` | Each model call's prompt saved in its cabinet's schema, read on the Prompts page. |
| `engine/mail.py` | The mailbox watcher: starts an Emails run when a thread waits. |
| `engine/traitement.py`, `dossier.py`, `emails.py` | The workflows, as graphs. |
| `content/` | What the agent is told and the UI's plain words (`labels.json`: names and descriptions of each step). |
| `engine/graphs.py` | Each workflow's shape for the canvas, read from the compiled graphs (never drawn by hand). |
| `engine/layouts.py` | How the team arranged each canvas (node places, edge bends and style), checked and kept in Postgres. |
| `ui/` | The team's pages (French): `pages.py`, its templates, a stylesheet, scripts, no build step; `ui/static/vendor/` holds Cytoscape.js and dagre (MIT). |
| `tests/` | The tools' and the isolation tests (`python -m pytest`; the real model's with `-m llm`). |
| `check.py` | Every tool a graph calls exists; every step drawn is described. |
| `run.py` | Run a graph from a terminal and watch it step by step. |

## Run it

From this folder (its `.env`, never in git, from `.env.example`, gives `POSTGRES_PASSWORD` and `ODOO_URL`; Vault
up first, from the repository's root):

```bash
docker compose up --build -d
```

| Call | What it does |
|---|---|
| `GET /ok` | The server is up, and its graphs. |
| `POST /runs/<graph>` | `{input, thread_id?, cabinet?, session_id?, wait?}`. Waits for the run by default; `wait: false` answers its `run_id` at once. |
| `GET /runs` | The latest runs (`?cabinet=`, `?graph=`, `?limit=`). |
| `GET /runs/<run_id>` | A run: status, input, output or error, and its steps. |
| `GET /runs/<run_id>/events` | Its steps as each one ends (server-sent events). |
| `GET /graphs/<graph>` | A workflow's shape for the canvas. |
| `GET` / `PUT` / `DELETE /graphs/<graph>/layout` | How the team arranged its canvas: read, saved, or forgotten. |

The team's pages: http://127.0.0.1:2024/ui/runs (runs), /ui/workflows (workflows), /ui/prompts (what the model
was sent).

Graphs: `agent`, `traitement`, `dossier`, `emails`. `TARIK_GRAPH_TOKEN` set: every call but `/ok` needs it as a
bearer token. In the container: `python run.py <graph> '<input json>' [--thread name]`, `python check.py`,
`python -m pytest`.

## Plan

What non-technical colleagues change is content, not logic: the agent's instructions and skills, the tools' prompts
and questions, cabinets' settings. Workflows stay Python, reviewed by developers, and the UI shows them as diagrams
with every run live, step by step.

1. **Engine and server** — done: the four graphs, the agent with its memory in Postgres, runs and steps kept and
   streamed live; parity with the n8n workflows checked step by step (2026-10-20), then the stack made its own.
2. **Runs in the UI** — done: the runs of each cabinet (refreshed on their own), each run step by step as it happens,
   in plain words (what was asked, what the client reads, each step's outcome, its whole output on demand), and for
   the agent the conversation with its thinking and the tools it used; the exact prompt of each model call (Prompts).
   Still to come: the inputs' names in plain words.
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
4. **Content in the UI, with history**: the agent's instructions and skills, the tools' prompts, cabinets' settings;
   every change kept (who, when), and undone in one click.
5. **Mail**: a cabinet switched over (`MAIL_BY_ORCHESTRATOR`, the n8n watcher stopped for it); then the email test
   and `-m llm` again.
6. **Chat with file upload**, per cabinet, if cabinets need more than email.

## Lessons kept from the first port

- The model needs its thinking budget: without it the agent decides what the client did not (`-m llm`: 3 % wrong
  decisions without, 1 % with).
- An answer's outcome is recorded under the step it answers, or a question asked again can never be answered.
- A step's own arguments are positional: a tool may take a `run_id` of its own.
- Tool output from Experio reaches the model as untrusted data (n8n's guard, rebuilt).
- Two invoices of one message run one after another: side by side, both could create the same new tiers.
