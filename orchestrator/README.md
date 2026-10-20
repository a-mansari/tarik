# Tarik's orchestrator

Tarik rebuilt on LangGraph's MIT-licensed parts, with n8n's strengths built on top: an agent core that runs every
workflow without n8n's limits, and a UI any colleague can read, follow and change without reading code.

Why: n8n's Sustainable Use License allows internal use only, and LangGraph's own server (`langgraph-api`, behind
`langgraph dev` and Studio) is Elastic-2.0. Tarik is to be offered to cabinets as a hosted service, so this stack uses
neither: only `langgraph`, `langgraph-checkpoint(-postgres)` and LangChain (MIT), starlette and uvicorn (BSD), and
psycopg (LGPL, as an unmodified library). Check the licence of every new dependency.

## What it reuses, unchanged

- **tarik-mcp** (`mcp/`): every Experio step, mail and file, as MCP tools, one address per cabinet. The isolation
  between cabinets lives there: a run names its cabinet once, never the model.
- **`instructions/`**: the skills and the tools' prompts, shared with the n8n stack.
- **Postgres and Vault**, on the n8n stack's Docker network.

n8n keeps running untouched until a cabinet is switched over.

## Layout

| Path | What |
|---|---|
| `server.py` | The HTTP API. |
| `engine/runs.py` | Every run and each of its steps, in Postgres (schema `orchestrator`); the agent's conversations next to them. |
| `engine/agent.py` | The agent: instructions and tools' descriptions from `content/agent.json`, skills from `instructions/skills/`, tool output from Experio tagged as untrusted data. |
| `engine/model.py` | MiniMax-M3 with a 1024-token thinking budget; this stack's own key (Vault `secret/tarik/langgraph`) or n8n's "Tarik - Model". |
| `engine/traitement.py`, `dossier.py`, `emails.py` | The workflows, as graphs. |
| `engine/tools.py` | One tarik-mcp call; a step's outcome recorded for the run's summary; retried after a dropped connection only when nothing can be written twice. |
| `content/agent.json` | What the agent is told: kept out of the code so the UI can show and edit it. |
| `check.py` | Parity with the n8n workflows: same tools, same inputs. |
| `run.py` | Run a graph from a terminal and watch it step by step. |

## Run it

From the repository's root (its `.env` gives the container only the `POSTGRES_*` values):

```bash
docker compose -f orchestrator/docker-compose.yml --env-file .env up --build -d
```

| Call | What it does |
|---|---|
| `GET /ok` | The server is up, and its graphs. |
| `POST /runs/<graph>` | `{input, thread_id?, cabinet?, session_id?, wait?}`. Waits for the run by default; `wait: false` answers its `run_id` at once. |
| `GET /runs` | The latest runs (`?cabinet=`, `?graph=`, `?limit=`). |
| `GET /runs/<run_id>` | A run: status, input, output or error, and its steps. |
| `GET /runs/<run_id>/events` | Its steps as each one ends (server-sent events). |

Graphs: `agent`, `traitement`, `dossier`, `emails`. `TARIK_GRAPH_TOKEN` set: every call but `/ok` needs it as a
bearer token. In the container: `python run.py <graph> '<input json>' [--thread name]`, `python check.py`.

## Plan

What non-technical colleagues change is content, not logic: the agent's instructions and skills, the tools' prompts
and questions, cabinets' settings. Workflows stay Python, reviewed by developers, and the UI shows them as diagrams
with every run live, step by step.

1. **Engine and server** — done: the four graphs, the agent with its memory in Postgres, runs and steps kept and
   streamed live, parity with n8n (`check.py`).
2. **Runs in the UI**: the runs of each cabinet, each run step by step as it happens (what was asked, what each step
   did, the questions asked), the prompts sent to the model, the conversations.
3. **Workflows in the UI**: each graph as a diagram, each step with what it does in plain words.
4. **Content in the UI, with history**: the agent's instructions and skills, the tools' prompts, cabinets' settings;
   every change kept (who, when), and undone in one click.
5. **Mail**: the watcher wakes this stack for a cabinet switched to it; then the email test and `-m llm` again.
6. **Chat with file upload**, per cabinet, if cabinets need more than email.

## Lessons kept from the first port

- The model needs its thinking budget: without it the agent decides what the client did not (`-m llm`: 3 % wrong
  decisions without, 1 % with).
- An answer's outcome is recorded under the step it answers, or a question asked again can never be answered.
- A step's own arguments are positional: a tool may take a `run_id` of its own.
- After a dropped connection, only a tool a second call answers the same is called again.
- Tool output from Experio reaches the model as untrusted data, as n8n's agents do.
