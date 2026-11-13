# Deploying Tarik

For the DevOps team. Tarik runs as **four containers** (a fifth, the memory, is optional) from one compose file, `docker-compose.prod.yml`, and needs no
manual setup of Vault or Postgres. No address and no secret is written in the code: what every cabinet shares is in `.env`
(`.env.example` is the complete list), and each cabinet's own settings (its Experio login, its mailbox) are typed by the
Tarik team on the **Ajouter un cabinet** page, which keeps them in Vault.

## The short version

You provide a host with Docker and a route on Experio's reverse proxy; the rest is the team's, from the pages.

1. `cp .env.example .env` and fill it (section 2): the Experio address, the database password, the proxy's address, the
   mail server, the model's login. Every other line has a working default or can stay empty.
2. `docker compose -f docker-compose.prod.yml up -d --build`, then wait for `server` to be healthy.
3. On the proxy, route the public host name to the machine's port 2024 (section 3).
4. Hand over to the team: they make the first admin, load the content (section 4), and add every cabinet on the
   **Ajouter un cabinet** page, in `DRY_RUN`. Nothing more is asked of DevOps.

## 1. The containers

| Container | Image | What it does | Data |
|---|---|---|---|
| `server` | built from `Dockerfile` | One Python process: the workflows, the agent and the tools (`engine/`), the HTTP routes and the team's pages (`server/`), the mail watcher. Port 2024. | none |
| `db` | `pgvector/pgvector:pg17` | Runs, conversations, content versions, accounts, one schema per cabinet. | volume `db_data` |
| `vault` | `hashicorp/vault:1.18.3` | Secrets and each cabinet's settings. Reachable on the compose network only (no published port). Unsealed again at each start. | volumes `vault_data`, `vault_keys` |
| `vault-init` | the same Vault image | One-shot at each start (`up`): initialises and unseals Vault the first time, makes the two AppRole logins. Exits. | `vault_approle`, `vault_settings_approle` |
| `hindsight` | built from `deploy/hindsight/Dockerfile` | Tarik's memory (`engine/memory.py`), only with `--profile memory`: one bank per cabinet, in the schema `hindsight` of `db`. Its fact extraction runs on a Claude Code subscription token. | `db` |

`server` starts after `vault-init` has succeeded and `db` is up. Engine and server are not separate containers.

| Network | Detail |
|---|---|
| Outbound | `ODOO_URL` (Experio: HTTPS, XML-RPC and JSON-RPC); the model providers (Claude's CLI, MiniMax's `MODEL_API_URL`: HTTPS); the mail server (`IMAP_HOST`, `SMTP_HOST` in `.env`, ports 993 and 465 or 587); Gladden if `GLADDEN_URL` is set. At image build only: package registries and, unless `INSTALL_CLAUDE_CODE=0`, the Claude Code installer. |
| Inbound | The team's browsers to `/ui/`; the Experio app to `/api/` (its key only); Gladden's callback to `/gladden/<token>` when Gladden is used. Publish port 2024 through Experio's reverse proxy; nothing else (section 3). |
| Replicas | **One `server`.** The pages' live updates are kept in the process's memory (`engine/live.py`): a second replica would show stale pages. Scale up the machine, not the count. |

## 2. What to provide

One file, `.env` (copy `.env.example`: every line is explained there). It is never committed and never sent to the image
build; protect it like a secret, it holds the keys below. The compose file refuses to start when a required line is
missing.

- **Required**: `ODOO_URL` (Experio), `POSTGRES_PASSWORD` (`openssl rand -hex 24`), `TARIK_TRUSTED_PROXIES` (the proxy's
  address).
- **The mail server**: `IMAP_HOST` and `SMTP_HOST` (every cabinet's mailbox is on it; the ports, the folder and a
  self-signed certificate's fingerprint have their own lines). The stack starts without them, but no cabinet can be added.
- **The model**: `CLAUDE_CODE_OAUTH_TOKEN` (the default LLM, from `claude setup-token`: use a token made for this stack)
  and/or `MODEL_API_URL` with `MODEL_API_KEY` (MiniMax). With neither, Tarik has no model.
- **The Experio app's key**: `EXPERIO_API_KEY` (`openssl rand -hex 32`), which the Experio app sends to `/api/`; empty:
  that API accepts nothing.
- **Optional**: `PUBLIC_URL` (this server as clients reach it: the form links Tarik sends and WhatsApp's callback; without
  it Tarik asks the settings by email; a form link holds a one-time key in its path, keep `/formulaire/` out of
  long-lived proxy logs), `GLADDEN_URL` and `GLADDEN_CALLBACK_URL`, `SPARROW_URL`, `SPARROW_SENDER_ID` and `SPARROW_TOKEN`
  (WhatsApp), `MEMORY_ENABLED`, the limits.

The values come from the Experio and Tarik teams, not from DevOps. A change to `.env` applies at the next start
(`docker compose -f docker-compose.prod.yml up -d`); the Paramètres page shows these settings and cannot change them.

**Each cabinet's own settings** are not DevOps's: its Experio login (`ODOO_USER`, `ODOO_PASSWORD` or `ODOO_API_KEY`), its
mailbox (`IMAP_USER`, `IMAP_PASSWORD`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`), `MAIL_ALLOWED_RECIPIENTS`,
`MAIL_MANAGER`, `DRY_RUN`. An admin of the Tarik team types them on the **Ajouter un cabinet** page (or the Experio app
sends them through the API, `docs/experio-api.md`); they are kept in Vault, and later changes are on the Cabinets page.
**A cabinet starts in `DRY_RUN=1`**: Tarik then reads and reports, and posts nothing to Experio, until it has been
checked. There is no seed file: Vault starts empty and the server starts with no cabinet.

## 3. Build and run

```bash
cp .env.example .env                    # fill it (section 2)
docker compose -f docker-compose.prod.yml up -d --build
```

- The image holds the code; the server mounts only the two AppRole volumes and takes the shared settings from `.env`. `docker-compose.yml` is the development
  file (the working copy mounted): not for a deployment.
- With a managed Postgres instead of the `db` container, set `POSTGRES_HOST` and remove the `db` service and its
  `depends_on` from the compose file. The user must be allowed to create schemas.
- The memory (off by default): set `HINDSIGHT_URL`, `HINDSIGHT_LLM_TOKEN` in `.env` (`.env.example`), then
  `docker compose --profile memory up -d`. `db` runs the pgvector image (Debian) where it ran `postgres:17-alpine`: the
  data files are the same, but Alpine and Debian sort text differently, so on a stack that already has data run once
  after the swap: `docker compose exec db psql -U tarik -c 'REINDEX DATABASE tarik'`. A managed Postgres needs the
  pgvector extension, and a user allowed to `CREATE EXTENSION vector` (Hindsight makes it).
- The memory's web UI (read and delete what Tarik remembers, a bank per cabinet) is on `http://127.0.0.1:9999` of the
  host, never the network: it has no login. From another machine: `ssh -L 9999:127.0.0.1:9999 <host>`.
- Without the Claude Code CLI (only a cabinet whose model is Claude uses it): `INSTALL_CLAUDE_CODE=0` in `.env`.
- Health: `GET /ok` (no login); the compose file's healthcheck uses it.
- Logs go to each container's output (warnings and errors only for the server); who did what is on the Logs serveur page.

**The reverse proxy** (Experio's own, on experioservices.com). The server speaks plain HTTP on port 2024; the proxy needs:

- a route from the public host name to `<machine>:2024`, TLS terminated at the proxy;
- `X-Forwarded-For` and `X-Forwarded-Proto` forwarded (the login cookie is `Secure` only when it sees `https`, and the
  login throttle keys on the client address);
- no response buffering and a long read timeout (an hour or more) on `/ui/live`: it is a server-sent events stream the
  pages keep open;
- request bodies of at least 200 MB (a message from the Discussion page holds up to 10 files of 20 MB);
- `TARIK_TRUSTED_PROXIES` in `.env` = the proxy's address (`*` trusts any sender: fit only when nothing but the proxy
  can reach the port). Set `TARIK_BIND` to the interface the proxy reaches (default `127.0.0.1`: for a proxy on another
  machine, the machine's private address), never the open network.

Then give the public address as `PUBLIC_URL` in `.env` and restart: the form links Tarik sends use it.

## 4. First start

1. `docker compose -f docker-compose.prod.yml logs vault-init` ends with "vault ready", and `server` becomes healthy (with no cabinet yet: that is normal).
2. **Load the content** (the prompts, skills and labels live in Postgres, not in the image): on the Contenu page (Tarik
   prompts > Import) or by script with the bearer token (`TARIK_GRAPH_TOKEN` in `.env`):
   `curl -X POST -H "authorization: Bearer $TARIK_GRAPH_TOKEN" -H "content-type: application/json" --data @deploy/tarik-contenu-complet-20261110.json http://127.0.0.1:2024/ui/api/tarik-prompts-import`
   (the file is a full export of the current content: the Tarik team refreshes it with each release).
   `python check.py` in the server container then says every step is described.
3. Make the first admin: `docker compose -f docker-compose.prod.yml exec server python -m server.users add <email> "<name>"`
   (the password is asked), then `... role <email> admin`. Other people are made on the Comptes page.
4. Add every cabinet on the **Ajouter un cabinet** page (an admin's): Tarik logs in to the cabinet's Experio first, serves it
   only when that works, and leaves it deactivated, with the reason, when the login fails. Then send a test email or write on the
   Discussion page, still in `DRY_RUN`.

## 5. Operating

- **Backup**: `.env` and the volumes `db_data` (runs, conversations, the content's versions, the accounts, each cabinet's schema),
  `vault_data` **and `vault_keys`** (Vault's data is unreadable without its unseal key; both are needed to restore it).
  Tarik keeps no other file.
- **Restart**: any container can restart on its own. After a restart Vault unseals itself, and `server` keeps the last
  secrets it read while Vault is away.
- **Upgrade**: build the new image, `up -d` (`vault-init` runs again and changes nothing). The server ends in error
  the runs the stop cut off; the follow-up on their email thread or conversation takes them up again. A change made
  in the database by hand (content) is read only at start: restart.
- **A stack that kept the shared settings in Vault** (`secret/tarik/shared`, `secret/tarik/orchestrator`, before this
  change): put each of their keys in `.env` first (`.env.example` lists them), then `up -d --build`. Tarik no longer
  reads those two paths; delete them from Vault once the stack works. The cabinets, and the extra LLMs of the
  Paramètres page, stay in Vault untouched. **Read `EXPERIO_API_KEY` from Vault first**: the old setup made it there, and
  nobody typed it. Put it in `.env` (or make a new one with `openssl rand -hex 32` and give it to the Experio app),
  else the Experio app gets 401 on `/api/`.
- **Limits**: `TARIK_CALLS_PER_CABINET`, `TARIK_CALLS_TOTAL` and `TARIK_MODEL_CALLS` cap the model calls at once.
- **A secret changed**: through the Cabinets page or the Experio API for a cabinet; for a shared one or the model's, edit
  `.env` and run `up -d` (the server restarts).

## 6. For the security review

- No secret or address is committed: a scan of the tracked files found no key, token or private address (the emails in
  `tests/` are fixtures). The git history was not scanned: do it before the repository is shared.
- **Vault's seal.** Vault keeps its unseal key and root token on the `vault_keys` volume, so it restarts without anyone.
  Whoever can read the host's Docker volumes can read both. Vault listens in plain HTTP on the compose network only, and
  publishes no port. To harden it, add a KMS `seal` stanza to `vault/config.hcl` (auto-unseal) and TLS on the listener;
  the server and `vault-init` then need `VAULT_ADDR` as an `https` address.
- The two AppRoles have no `secret_id` expiry (`secret_id_ttl=0`). Rotation is not scripted here: it means making a new
  `secret_id` for each role in Vault and replacing the file in its volume.
- Every route but `/ok`, the login page, the invitation pages (a one-time link) and `/gladden/<token>` (a one-time
  token) needs a session, the script token, or (under `/api/`) the Experio key. A member cannot change content,
  settings or layouts. A change made with a session must come from the pages themselves (Origin checked).
- Cabinets are isolated: one schema each, and each one's own Vault path.
- `requirements.txt` also installs `pytest` and `ruff` (the tests run in the container). The image can drop them by
  splitting that file; nothing in the server imports them.
- The tests and docs are not in the image.

## 7. What was tested

The stack was run end to end from empty volumes on one machine, with dummy secrets and an unreachable Experio address:
Vault initialised and unsealed itself, the roles were made, the server read them, created its schema,
reported healthy and started the mail watcher; Vault was restarted and unsealed itself; a second `up` changed
nothing. Not tested: a real Experio and mailbox, the content import on a fresh stack, a managed Postgres, TLS and a
KMS seal.
