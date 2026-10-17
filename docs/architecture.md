# Architecture

Tarik is Experio's accounting assistant: one agent per accounting firm (cabinet), reachable by chat and
by email, that finds dossiers, processes invoices and bank statements, and creates or modifies dossiers
in Experio (Odoo). This page is the map; [tools-and-workflows.md](tools-and-workflows.md) lists every
piece, and [using-tarik.md](using-tarik.md) explains what Tarik can do and how to write to it.

## The pieces

```
 Client (chat, Agents section)                    Client (email, the cabinet's own Tarik mailbox)
        │                                                     │
        ▼                                                     ▼
┌──────────────────── n8n 2.43.5, patched (n8n/) ──────────────────┐   ┌─ tarik-mail-watch ──────────────┐
│ Agents: "Tarik - <cabinet>" ×8 (MiniMax-M3, reasoning low)       │◄──│ IMAP IDLE + 15-minute tick,      │
│   skills: Nouveau dossier · Traitement des documents ·           │   │ one per cabinet mailbox          │
│           Modification de dossier · Réponse à une question       │   │ → webhook {cabinet}              │
│   tools:  Experio_* MCP tools (read) at /c/<cabinet>/mcp         │   │   + X-Tarik-Webhook secret       │
│           Experio_traiter_fichiers   → "Tarik - Traitement"      │   └──────────────────────────────────┘
│           Experio_repondre           → "Tarik - Traitement"      │
│           Experio_parametrer_dossier → "Tarik - Dossier"         │
│           Experio_modifier_dossier   → "Tarik - Modification"    │
│ "Tarik - Emails" (webhook) ── ask_agent ──► n8n MCP call_agent    │
└───────────────┬────────────────────────────────────┬─────────────┘
                │ every workflow step → /c/<cabinet>/mcp   │ n8n's own data
                ▼                                    ▼
┌──────── tarik-mcp (Python, MCP) ───────┐   ┌──── Postgres ──────────────────────────────┐
│ /c/<cabinet>/mcp only, stateless       │──►│ db tarik: one schema per cabinet            │
│ 10 calls at once per cabinet, 40 total │   │   (files, step results, questions, mail,    │
│ settings through setting() ◄── Vault   │   │    client messages)                         │
└───────┬───────────────┬────────────────┘   │ db n8n: workflows, agents, conversations,   │
        ▼               ▼                    │   executions                                │
  Experio (Odoo)   MiniMax vision            └─────────────────────────────────────────────┘
  one database      (scans, answers)         ┌──── Vault (local; a real one in production) ┐
  per cabinet                                │ secret/tarik/shared                          │
                                             │ secret/tarik/cabinets/<cabinet>              │
                                             └──────────────────────────────────────────────┘
```

| Container | Role |
|---|---|
| `n8n` | The agents, their chats and memory, the workflows. Patched (`n8n/patch.js`), its data in Postgres. |
| `tarik-mcp` | Every Experio step, as Python tools served over MCP (`mcp/tarik/`). The only piece that talks to Experio. |
| `tarik-mail-watch` | Reads each cabinet's mailbox as emails arrive, stores them, and wakes "Tarik - Emails" for that cabinet when a thread waits. |
| `postgres` | Tarik's data (`tarik`, one schema per cabinet) and n8n's (`n8n`). |
| `vault` | Secrets and per-cabinet settings. Locally unsealed at start; on the public server, a real Vault. |

## How a request flows

**Chat.** The client writes in their cabinet's agent. The agent reads its base instructions, loads the
skill that matches (a new dossier, documents, a modification), and calls tools by the names it sees.
Read-only questions (Experio's status, dossiers, invoices, collaborators) go straight to the MCP tools.
Actions go to a workflow: n8n runs it with the model's fields plus values the model never sets.

**Email.** `tarik-mail-watch` reads each cabinet's mailbox when an email arrives (and every 15 minutes):
it stores the new emails in their threads (the mailbox is never changed, attachments are saved in the
cabinet's schema), tells the manager of refused senders, and wakes "Tarik - Emails" with the cabinet's
name when a thread waits. The workflow claims the waiting threads (`claim_threads`), asks the cabinet's
agent for the reply (`ask_agent`, through n8n's MCP `call_agent`, in the thread's own
conversation), and sends it (`send_email`, whitelisted addresses only).

**A workflow run.** Each step is one MCP call to `/c/<cabinet>/mcp`; each outcome is recorded
(`record_step`), and the run's summary (`run_summary`) is the answer the agent passes on word for word.

## Keeping cabinets apart

The cabinet's name is its exact Experio database (`experio_cabinet_8`), and the same name is used
everywhere, so nothing ever chooses a cabinet by guessing:

| What | Per cabinet |
|---|---|
| Agent | "Tarik - <name>": its own memory, conversations and attachments (tested on the live n8n). |
| MCP address | `/c/<cabinet>/mcp`; any other address gets 404, and the server is stateless. |
| Data | A Postgres schema of the same name; a tool's connection sees only that schema. |
| Settings | Vault `secret/tarik/cabinets/<cabinet>`: Experio login, mailbox, agent id, whitelist, `DRY_RUN`. |
| Workflow input | A fixed `cabinet` value in each agent's tools, never chosen by the model. |

## What the model never decides

The model chooses which tool to call and fills the fields it is allowed to. Everything that must be
right every time is decided by n8n or by code:

- **The conversation, its attachments and the client's own words**: n8n fills tool inputs fixed to
  `{{threadId}}`, `{{attachments}}` and `{{clientMessage}}` (patches in `n8n/patch.js`).
- **That a question was asked before it is answered**: skipping Experio's ICE/IF check, or creating a
  dossier whose name is very close to another's, needs Tarik's own question about that dossier first;
  every modified value, and a new dossier's ICE and IF, must appear in the client's own messages (whole
  words, amounts as numbers), checked in Python.
- **Which answers count**: a oui / non question takes exactly "oui" or "non", anything else asks it again;
  a tiers question takes only a tiers it proposed (or an ICE that names one of the dossier's tiers); an
  exercice question takes "nouveau" or one of the open exercices it listed; a bank account question one of
  the accounts it listed; with several questions waiting, the answer goes to the one named among them; an
  invoice filter is one of the four statuses.
- **ICE and IF exactly as written**: a number that lost its leading zeros is restored from the
  client's message.
- **Who wrote an email**: its From address alone; Reply-To is only where the reply goes, and only when
  it is on the whitelist. A colleague's reply in someone else's thread is their own thread, with their
  own rights.
- **Who may work on a dossier by email**: the sender must be a collaborator of the cabinet, or one of Experio's
  own staff, with that dossier assigned.
- **Who may receive an email**: the cabinet's whitelist (`MAIL_ALLOWED_RECIPIENTS`).
- **Amounts**: compared as numbers (100.5 and 1005 differ), the model's own figures read as numbers or
  asked again, never guessed.
- **What never reaches the agent**: settings, secrets, the database name and Experio's own error text
  (a test calls every tool with marker values to check it).

What the agent does decide, from the client's reply and guided by its skills: whether it said oui or
non (`decision`), which proposed tiers, open exercice (or "nouveau") or bank account it chose, which waiting
question the reply answers (`about`), a clear yes to Tarik's confirmation question (`client_confirmed`), the
invoices' filter, and copying a model dossier's configuration. An unclear reply
leaves the decision empty, and the question is asked again.

## The agents and their skills

All 8 agents are made from one template, `workflows/tarik-agent.json`, and the skills in
`workflows/skills/`, by `workflows/build_tarik_agents.py`. The base instructions hold the rules that
always apply; each process lives in a skill the agent loads when a request matches it. A skill names
tools exactly as the model sees them (`Experio_…`), and the build refuses a skill, instruction or tool
description naming a tool the agents do not have.

## Where things live

| Path | What |
|---|---|
| `mcp/` | The Python tools (`tarik/core`, `invoices`, `dossiers`, `emails`), `server.py`, `watcher.py`, tests. |
| `workflows/` | Workflow builders (`build_tarik_*.py`) and their JSON, the agent template, its skills, the agents' build script. |
| `n8n/` | The patched n8n image. |
| `vault/` | Vault's config, start and setup scripts (`vault/.local/`: keys, never in git). |
| `.env` | What the services need to start; everything else is in Vault. |
| `README.md` | Setup, deployment and maintenance. |
