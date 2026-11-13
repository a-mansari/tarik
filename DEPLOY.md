# Deploying Tarik

For the DevOps team. You provide a host with Docker and a route on Experio's reverse proxy. Everything else is the Tarik
team's, from Tarik's pages.

## Steps

1. `cp .env.example .env` and fill it (section 2).
2. `docker compose -f docker-compose.prod.yml up -d --build`. Wait for `server` to be healthy (`GET /ok`).
3. Route the public host name to the machine's port 2024 on the proxy (section 3).
4. Hand over to the Tarik team (section 4): first admin, content import, then every cabinet on the **Ajouter un cabinet**
   page.

## 1. What runs

| Container | What it does | Data |
|---|---|---|
| `server` | One Python process: workflows, agent, tools, HTTP routes, the team's pages, the mail watcher. Port 2024. | none |
| `db` | Postgres 17 (pgvector): runs, conversations, content, accounts, one schema per cabinet. | volume `db_data` |
| `vault` | Each cabinet's own settings (its Experio login, its mailbox). Compose network only, no published port, unseals itself at each start. | volumes `vault_data`, `vault_keys` |
| `vault-init` | One-shot at each `up`: initialises and unseals Vault the first time, makes the two AppRole logins. Exits. | `vault_approle`, `vault_settings_approle` |

- **One `server` only.** The pages' live updates live in its memory: a second replica shows stale pages. Scale the
  machine, not the count.
- **Outbound:** `ODOO_URL` (HTTPS, XML-RPC and JSON-RPC), the model providers (HTTPS), the mail server (`IMAP_HOST` 993,
  `SMTP_HOST` 465 or 587), Gladden if set. The new host's outbound IP must be allowed by Experio's RPC. At image build:
  package registries and the Claude Code installer (`INSTALL_CLAUDE_CODE=0` skips it).
- **Inbound:** the team's browsers (`/ui/`), the Experio app (`/api/`, its key only), Gladden's callback
  (`/gladden/<token>`). Publish port 2024 through the proxy, nothing else.

## 2. `.env`

Copy `.env.example`: every line is explained there. `.env` holds secrets: never commit it, protect it like a secret. The
compose file refuses to start when a required line is missing. The values come from the Experio and Tarik teams.

- **Required:** `ODOO_URL`, `POSTGRES_PASSWORD` (`openssl rand -hex 24`), `TARIK_TRUSTED_PROXIES` (the proxy's address).
- **Mail server:** `IMAP_HOST`, `SMTP_HOST` (ports, folder and a self-signed certificate's fingerprint have their own
  lines). The stack starts without them, but no cabinet can be added.
- **Model:** `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`; a token made for this stack) and/or `MODEL_API_URL` +
  `MODEL_API_KEY` (MiniMax). With neither, Tarik has no model.
- **Experio app's key:** `EXPERIO_API_KEY` (`openssl rand -hex 32`), sent by the Experio app to `/api/`. Empty: the API
  accepts nothing.
- **Optional:** `PUBLIC_URL` (this server as clients reach it: form links and WhatsApp's callback), `GLADDEN_URL`,
  `GLADDEN_CALLBACK_URL`, `SPARROW_URL`, `SPARROW_SENDER_ID`, `SPARROW_TOKEN`.

A change to `.env` applies at the next `up -d`. The Paramètres page shows these settings and cannot change them.

**Cabinets are not in `.env`.** An admin of the Tarik team types each cabinet's login, mailbox and recipients on the
**Ajouter un cabinet** page (or the Experio app sends them through the API, `docs/experio-api.md`). They go to Vault and
start in `DRY_RUN=1` (Tarik reads and reports, posts nothing to Experio) until checked. The server starts with no cabinet.

## 3. Reverse proxy

The server speaks plain HTTP on port 2024. The proxy needs:

- a route from the public host name to `<machine>:2024`, TLS ended at the proxy;
- `X-Forwarded-For` and `X-Forwarded-Proto` forwarded (the login cookie is `Secure` only with `https`; the login
  throttle keys on the client address);
- no response buffering and a read timeout of an hour or more on `/ui/live` (a server-sent events stream);
- request bodies of at least 200 MB (a Discussion message holds up to 10 files of 20 MB);
- `TARIK_TRUSTED_PROXIES` = the proxy's address (`*` only when nothing else can reach the port), and `TARIK_BIND` = the
  interface the proxy reaches (default `127.0.0.1`; a private address for a proxy on another machine, never the open
  network).

Keep `/formulaire/` out of long-lived proxy logs: a form link carries a one-time key in its path.

## 4. First start (the Tarik team)

1. `docker compose -f docker-compose.prod.yml logs vault-init` ends with "vault ready".
2. **Load the content** (prompts, skills and labels live in Postgres): Contenu page, Tarik prompts > Import.
3. **First admin:** `docker compose -f docker-compose.prod.yml exec server python -m server.users add <email> "<name>"`
   (asks the password), then `... role <email> admin`. Others are made on the Comptes page.
4. **Cabinets:** Ajouter un cabinet. Tarik logs in to the cabinet's Experio first and leaves it deactivated, with the
   reason, when that fails. Then test with an email or the Discussion page, still in `DRY_RUN`.

## 5. Operating

- **Backup:** `.env`, and the volumes `db_data`, `vault_data` **and `vault_keys`** (Vault is unreadable without its
  unseal key).
- **Upgrade:** build the new image, `up -d`. Runs the stop cut off end in error; the follow-up on their email thread
  or conversation takes them up again. Content changed by hand in the database is read only at start: restart.
- **Restart:** any container can restart alone; Vault unseals itself.
- **A secret changed:** a cabinet's on the Cabinets page or through the Experio API; a shared one in `.env`, then `up -d`.
- **Health and logs:** `GET /ok` (no login). Logs go to each container's output; who did what is on the Logs serveur page.

## 6. For the security review

- No secret or address is committed (the emails in `tests/` are fixtures). The git history was not scanned: do it before
  the repository is shared.
- **Vault's seal:** its unseal key and root token sit on the `vault_keys` volume, so it restarts alone. Whoever reads the
  host's Docker volumes reads both. Vault speaks plain HTTP on the compose network only.
- Every route but `/ok`, the login page, the invitation pages and `/gladden/<token>` needs a session, the script token or
  (under `/api/`) the Experio key. Changes with a session must come from the pages (Origin checked). Members cannot
  change content, settings or layouts.
- Cabinets are isolated: one schema each, and each one's own Vault path.
- `.env` secrets are not passed to the `claude` CLI process.
