# Tarik

Tarik, Experio's accounting assistant. A client writes to Tarik and sends invoices or bank statements, or asks for a
new dossier with its FEC; Tarik finds the dossier, then a fixed sequence puts each document into Experio (draft,
check against the scan, correction, tiers, duplicate, account, TVA, journal, withholding, prepaid charges, fiscal
year, the dossier's settings), posted when the client asks. When a step needs the client, Tarik asks, and the
sequence resumes from there with the answer.

| Folder | What |
|---|---|
| [`orchestrator/`](orchestrator/README.md) | Tarik itself: the workflows and the agent on LangGraph's MIT-licensed parts, the tools, its own Postgres, and the team's pages (runs, workflows, prompts). Start there. |
| [`orchestrator/docs/using-tarik.md`](orchestrator/docs/using-tarik.md) | What Tarik can do, and how to write to it. |
| `vault/` | Vault's configuration and setup: Tarik's settings and secrets, read-only for Tarik. |
| `docker-compose.yml` | Vault, the one service shared outside the orchestrator's own stack. |

The first implementation, on n8n, lives on branch `main`; it keeps answering a cabinet's mailbox until that cabinet
is switched to the orchestrator (`MAIL_BY_ORCHESTRATOR`).

## Run it

```bash
docker compose up -d
```

```bash
docker compose -f orchestrator/docker-compose.yml --project-directory orchestrator up --build -d
```

Vault first (its setup: `vault/setup.sh`), then the orchestrator, whose `orchestrator/.env` (from
`orchestrator/.env.example`) gives its database password and Experio's address. Its settings in Vault:
`orchestrator/.env.example` lists them.
