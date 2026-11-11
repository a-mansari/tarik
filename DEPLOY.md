# Deploying Tarik

For the DevOps team. Tarik runs as **four containers** (a fifth, the memory, is optional) from one compose file, `docker-compose.prod.yml`, and needs no
manual setup of Vault or Postgres. No address and no secret is written in the code: they are in `.env` (starting
configuration) and in the seed files (secrets, loaded into Vault once). `.env.example` is the complete list.

## The short version

You provide a host with Docker and a route on Experio's reverse proxy; the rest is the team's, from the pages.

1. `cp .env.example .env` and fill the four blank lines: `ODOO_URL`, `POSTGRES_PASSWORD` (`openssl rand -hex 24`),
   `TARIK_DEFAULT_CABINET`, plus `TARIK_TRUSTED_PROXIES` (the proxy's address). Every other line has a
   working default (the Claude Code installer's address and version included).
2. `cp -r deploy/seed.example deploy/seed` and fill it with what the Experio and Tarik teams hand over (section 2): the
   model's key, the mail servers, **one** cabinet's login and mailbox. Keep `DRY_RUN=1`.
3. `docker compose -f docker-compose.prod.yml up -d --build`, then wait for `server` to be healthy.
4. On the proxy, route the public host name to the machine's port 2024 (section 3).
5. Hand over to the team: they make the first admin, load the content (section 4), and add every other cabinet on the
   **Ajouter un cabinet** page. Nothing more is asked of DevOps.

## 1. The containers

| Container | Image | What it does | Data |
|---|---|---|---|
| `server` | built from `Dockerfile` | One Python process: the workflows, the agent and the tools (`engine/`), the HTTP routes and the team's pages (`server/`), the mail watcher. Port 2024. | none |
| `db` | `pgvector/pgvector:pg17` | Runs, conversations, content versions, accounts, one schema per cabinet. | volume `db_data` |
| `vault` | `hashicorp/vault:1.18.3` | Secrets and each cabinet's settings. Reachable on the compose network only (no published port). Unsealed again at each start. | volumes `vault_data`, `vault_keys` |
| `vault-init` | the same Vault image | One-shot at each start (`up`): initialises and unseals Vault the first time, makes the two AppRole logins, loads the seed files. Exits. | `vault_approle`, `vault_settings_approle` |
| `hindsight` | built from `deploy/hindsight/Dockerfile` | Tarik's memory (`engine/memory.py`), only with `--profile memory`: one bank per cabinet, in the schema `hindsight` of `db`. Its fact extraction runs on a Claude Code subscription token. | `db` |

`server` starts after `vault-init` has succeeded and `db` is up. Engine and server are not separate containers.

| Network | Detail |
|---|---|
| Outbound | `ODOO_URL` (Experio: HTTPS, XML-RPC and JSON-RPC); the model providers (Claude's CLI, MiniMax's `MODEL_API_URL`: HTTPS); each cabinet's IMAP and SMTP hosts (set in the seed, ports 993 and 465 or 587); Gladden if `GLADDEN_URL` is set. At image build only: package registries and, unless `INSTALL_CLAUDE_CODE=0`, the Claude Code installer. |
| Inbound | The team's browsers to `/ui/`; the Experio app to `/api/` (its key only); Gladden's callback to `/gladden/<token>` when Gladden is used. Publish port 2024 through Experio's reverse proxy; nothing else (section 3). |
| Replicas | **One `server`.** The pages' live updates are kept in the process's memory (`engine/live.py`): a second replica would show stale pages. Scale up the machine, not the count. |

## 2. What to provide

1. **`.env`**: copy `.env.example` and fill the **Required** block's blank lines: `ODOO_URL`, `POSTGRES_PASSWORD`,
   `TARIK_DEFAULT_CABINET`, `TARIK_TRUSTED_PROXIES`. The compose file refuses to start when one is
   missing. Every other line has its default in the file (`VAULT_ADDR`, `TARIK_SEED_DIR`, the Claude Code installer's
   address and version included: they are the compose file's). `.env` is never committed and never sent
   to the image build.
2. **The seed folder** (`TARIK_SEED_DIR`, `./deploy/seed` unless set): the template is `deploy/seed.example/` (see its `README.md`). It holds
   the secrets Vault starts with, one JSON file per Vault path:
   - `orchestrator.json`: `MODEL_API_KEY` and `MODEL_API_URL` (the MiniMax LLM's; Claude needs neither), and `EXPERIO_API_KEY` (the key the Experio app sends to `/api/`; leave it
     out and Vault makes a random one, which only Vault shows).
   - `shared.json`: the mail servers (`IMAP_HOST`, `IMAP_PORT`, `IMAP_FOLDER`, `SMTP_HOST`, `SMTP_PORT`,
     `MAIL_CERT_SHA256`), optionally `GLADDEN_URL` and `GLADDEN_CALLBACK_URL`, `PUBLIC_URL` (this server as clients reach it: a new dossier's form link, and the host Sparrow calls Tarik on for WhatsApp (`/sparrow/handler` must be reachable there from outside);
     without it Tarik asks the settings by email). A form's link is a one-time key in the address (`/formulaire/<cabinet>/<token>`): keep that path out of long-lived proxy or funnel logs, and, for the WhatsApp channel, `SPARROW_URL`,
     `SPARROW_SENDER_ID` and `SPARROW_TOKEN` (Sparrow's address, Experio's one WhatsApp sender, its master token).
   - `cabinets/<cabinet's exact Experio database name>.json`, **one** is enough (the server does not start without
     any); the team adds the others on the **Ajouter un cabinet** page (or the Experio app through the API):
     the cabinet's Experio login (`ODOO_USER`, `ODOO_PASSWORD` or `ODOO_API_KEY`; `ODOO_UID` is optional: the login's own uid when empty), its mailbox
     (`IMAP_USER`, `IMAP_PASSWORD`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`), `MAIL_ALLOWED_RECIPIENTS`,
     `MAIL_MANAGER`, `DRY_RUN`. **Keep `DRY_RUN=1`** until a cabinet has been checked: Tarik then reads and reports,
     and posts nothing to Experio.

   Those values come from the Experio and Tarik teams, not from DevOps. The seed folder is secret: keep it out of git
   (`deploy/seed/` is ignored), with the same protection as `.env`.

A path is written into Vault only when it does not exist yet. Later, cabinets are created and changed through the Experio
API (`docs/experio-api.md`) or the Cabinets page, and those changes are kept at every restart. To push a changed seed
file into existing paths, run the stack once with `VAULT_SEED_OVERWRITE=1`: it merges `orchestrator.json` and `shared.json`
only, never a cabinet's file (a cabinet's edited settings, `DRY_RUN` included, are never reverted). `vault-init` stops with
a message when a seed file still holds `CHANGE_ME`, and when Vault ends with no cabinet.

## 3. Build and run

```bash
cp .env.example .env                    # fill the Required block
cp -r deploy/seed.example deploy/seed   # fill it
docker compose -f docker-compose.prod.yml up -d --build
```

- The image holds the code; the server mounts only the two AppRole volumes. `docker-compose.yml` is the development
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

Then give the public address as `PUBLIC_URL`: in `shared.json` before the first `up` (the seed is loaded only into paths that do not exist yet), or on the Paramètres page afterwards. The form links Tarik sends use it.

## 4. First start

1. `docker compose -f docker-compose.prod.yml logs vault-init` ends with "vault ready", and `server` becomes healthy.
2. **Load the content** (the prompts, skills and labels live in Postgres, not in the image): on the Contenu page (Tarik
   prompts > Import) or by script with the bearer token (`TARIK_GRAPH_TOKEN` in `.env`):
   `curl -X POST -H "authorization: Bearer $TARIK_GRAPH_TOKEN" -H "content-type: application/json" --data @deploy/tarik-contenu-complet-20261110.json http://127.0.0.1:2024/ui/api/tarik-prompts-import`
   (the file is a full export of the current content: the Tarik team refreshes it with each release).
   `python check.py` in the server container then says every step is described.
3. Make the first admin: `docker compose -f docker-compose.prod.yml exec server python -m server.users add <email> "<name>"`
   (the password is asked), then `... role <email> admin`. Other people are made on the Comptes page.
4. Add the other cabinets on the **Ajouter un cabinet** page (an admin's): Tarik logs in to the cabinet's Experio first, serves it
   only when that works, and leaves it deactivated, with the reason, when the login fails. Then send a test email or write on the
   Discussion page, still in `DRY_RUN`.

## 5. Operating

- **Backup**: the volumes `db_data` (runs, conversations, the content's versions, the accounts, each cabinet's schema),
  `vault_data` **and `vault_keys`** (Vault's data is unreadable without its unseal key; both are needed to restore it).
  Tarik keeps no other file.
- **Restart**: any container can restart on its own. After a restart Vault unseals itself, and `server` keeps the last
  secrets it read while Vault is away.
- **Upgrade**: build the new image, `up -d` (`vault-init` runs again and changes nothing). The server ends in error
  the runs the stop cut off; the follow-up on their email thread or conversation takes them up again. A change made
  in the database by hand (content) is read only at start: restart.
- **Limits**: `TARIK_CALLS_PER_CABINET`, `TARIK_CALLS_TOTAL` and `TARIK_MODEL_CALLS` cap the model calls at once.
- **A secret changed**: through the Cabinets page or the Experio API for a cabinet; for the shared or the model's, edit
  the seed file and run once with `VAULT_SEED_OVERWRITE=1` (`orchestrator.json` and `shared.json` only).

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
Vault initialised and unsealed itself, the roles and seed paths were made, the server read them, created its schema,
reported healthy and started the mail watcher; Vault was restarted and unsealed itself; a second `up` changed
nothing. Not tested: a real Experio and mailbox, the content import on a fresh stack, a managed Postgres, TLS and a
KMS seal.
