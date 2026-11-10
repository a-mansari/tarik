# Experio MCP server for clients

A **read-only** MCP server that gives an Experio client's own agent (Claude, Gemini, Grok, ChatGPT, any MCP client)
safe access to its books. The cabinet's provider hosts one server for all its cabinets; each client signs in with
**its own Experio login or API key** and sees only what Experio lets that user see.

Not the same as `experio_mcp/` (the maintainers' personal server, one local process with one credential file): this one
is multi-client, hosted, and keeps no credential.

## How it keeps clients apart

- **The cabinet comes first.** The address is `https://<host>/<cabinet>/mcp`. A cabinet that is not served is a plain
  404, and the credentials are never read for it. A client's login is tried in **that cabinet's Experio database
  only**, so changing the url to another cabinet does not get a client in: their login does not exist there.
- **The client reads as themself.** Every Experio call carries the client's own login and key: Experio's own rights
  decide, and this server holds no credential of its own. A dossier that is not one of the user's is refused in the
  same words as one that does not exist.
- **Only reads, only the registry.** Five read methods are the whole of what can be called. A general query reaches
  only the models and columns listed in `registry.py`: no user, password, bank account, payroll per person,
  attachment or setting, no "or", no path through another model.
- **Nothing is kept.** Stateless: no session, no stored key. A successful login is remembered five minutes by a
  fingerprint (never the key). The audit trail (`audit.py`, JSON lines on the standard output) names who (a short
  fingerprint), what and how it ended, never a value, a filter's value, a login or a key.
- **No oracle.** Wrong credentials are counted per login and per address, then paused: this is no way to guess an
  Experio password, nor to lock a client's account. Each credential has a rate (60 requests a minute) and 3 calls at
  once. Answers are capped (100 000 characters), invisible and bidirectional characters are cleaned out of what
  third parties typed, and every answer is marked as data, not instructions.
- **Refusals tell nothing internal**: no model, SQL, trace or path; Experio's own message never reaches the client.

## For the operator

```bash
cp experio_client_mcp/cabinets.example.json experio_client_mcp/cabinets.json   # the cabinets served and their Experio
export MCP_PUBLIC_HOST=mcp.example.com
docker compose -f experio_client_mcp/docker-compose.yml up -d --build
```

`cabinets.json` maps each served cabinet (its exact Experio database name) to its Experio address, `https` only. It holds
no secret. The server refuses to start with a doubtful list.

**Put a reverse proxy in front** that terminates TLS and sets `X-Forwarded-For` and `X-Forwarded-Proto`. With
`EXPERIO_CLIENT_MCP_TRUST_PROXY=1` (the compose file sets it) the server refuses a request that came over plain http.
Caddy, for example:

```
mcp.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Settings (environment, prefix `EXPERIO_CLIENT_MCP_`): `CABINETS` (path of the cabinets file, default
`/etc/experio-client-mcp/cabinets.json`), `HOSTS` (the host names clients use: any other Host or Origin is refused),
`TRUST_PROXY` (1 behind the proxy: its `X-Forwarded-For` and `-Proto` are used; without it every client shares the
proxy's address and the server warns at start), `AUDIT_SECRET` (keys the user fingerprints of the audit trail: set it,
or they change at each start). The plain variables `HOST` (0.0.0.0) and `PORT` (8080) set where it listens: publish the
port to the proxy only, never to the internet.

Limits (same prefix): `RATE_PER_MINUTE` (60) and `CONCURRENT` (3), per **user** (cabinet and login: a second API key
brings no second allowance); `FAILED_MAX` (5), `FAILED_WINDOW` (600 s), `PAUSE` (900 s) for wrong credentials, counted
before the next try and ignored for a login already remembered; `LOGIN_TTL` (300 s); `TIMEOUT` (30 s per Experio call)
and `DEADLINE` (90 s per tool call); `THREADS` (30) and `CABINET_THREADS` (8) bound the calls to Experio in all and per
cabinet (a full cabinet is refused at once); `AUTH_CONCURRENT` (8) and `AUTH_WAITING` (64) bound the logins tried at
once; `MAX_BODY` (262 144 bytes); `MAX_CHARS` (100 000).

**One process.** The throttles are in memory, so run one container; to scale out, add the same limits at the proxy.
`GET /healthz` answers `{"status": "ok"}` and nothing else.

## For a client

1. Ask your cabinet for your **address**: `https://mcp.example.com/<cabinet>/mcp`.
2. Make your **token** (it asks for your Experio password or API key without showing it):

   ```bash
   python -m experio_client_mcp token your-experio-login
   ```

   Or by hand: `printf 'login:key' | base64`. Use a **dedicated Experio user** with read access and an **API key you can
   revoke** rather than your own password: the key travels to the agent provider's servers with each request.
3. Connect your agent with `Authorization: Bearer <token>`:

   | Agent | Setting |
   |---|---|
   | Claude Code | `claude mcp add --transport http experio https://mcp.example.com/<cabinet>/mcp --header "Authorization: Bearer <token>"` |
   | Gemini CLI | `~/.gemini/settings.json`: `{"mcpServers": {"experio": {"httpUrl": "https://mcp.example.com/<cabinet>/mcp", "headers": {"Authorization": "Bearer $EXPERIO_TOKEN"}}}}` |
   | Grok (xAI API) | a `tools` entry `{"type": "mcp", "server_label": "experio", "server_url": "https://mcp.example.com/<cabinet>/mcp", "authorization": "<token>"}` |
   | OpenAI Responses API | a `tools` entry `{"type": "mcp", "server_label": "experio", "server_url": "...", "headers": {"Authorization": "Bearer <token>"}, "require_approval": "never"}` |

   Agents that only accept OAuth sign-in (some chat apps' "connectors") cannot use this yet: see below.
4. Ask: *"List my dossiers"*, *"Give me the accounts report of the 2026 exercice for dossier 35"*, *"Which clients
   still owe us the most?"*

**Tools:** `whoami`, `list_dossiers`, `list_fiscal_years`, `invoices`, `invoice_totals`, `bank_operations`,
`trial_balance`, `monthly_totals`, `partner_balances`, `account_ledger`, `accounts_report`, `describe_data`,
`query_data`. Every report ends with its notes: an exercice not closed is not a final result, and the points
d'attention are automatic rules to be checked, not a balance sheet nor advice.

## Not built yet

- **OAuth sign-in** (the flow some hosted chat apps require for remote servers). A static key in a header works with
  Claude Code, Gemini CLI and the Grok and OpenAI APIs.
- **A shared limit across processes** (one container for now).
- The rules and the registry are **copies of Tarik's** (`engine/tools/data/`, `engine/tools/reports/accounts.py`): a
  rule changed there is changed here too. A shared package would remove the copy.

## Tests

They need the `mcp` package, which Tarik's image does not have, so they are not part of Tarik's own run:

```bash
python3 -m venv .venv && .venv/bin/pip install "mcp>=1.30,<2" httpx pytest
.venv/bin/python -m pytest experio_client_mcp/tests
```

They run the whole application against a fake Experio over real XML-RPC (two cabinets, users with their own keys).
