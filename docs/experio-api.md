# Tarik's API for the Experio app

The Experio app tells Tarik when a cabinet is created, changed or closed. Tarik serves the cabinet (or stops) at
once, without a restart. Code: `app/api.py`.

## Access

- Every call sends the Experio app's key as a Bearer token: `Authorization: Bearer <key>`.
- The key is made once inside Vault by `vault/setup.sh` (`secret/tarik/orchestrator`, `EXPERIO_API_KEY`). Nobody types
  it; Experio's admin reads it from Vault and keeps it in the Experio app's own secrets.
- No key, a wrong key, or no key set in Vault: `401`. A session of Tarik's pages never opens the API.
- Call it over HTTPS only: the settings sent include passwords.

## The cabinet

A cabinet is named by its exact Experio database: lowercase letters, digits and `_`, starting with a letter
(`experio_cabinet_8`). Its settings are text values:

| Setting | Required | What |
|---|---|---|
| `ODOO_USER` | yes | Tarik's login in that cabinet's Experio |
| `ODOO_UID` | yes | Tarik's user id there (a positive number): checked at every connection |
| `ODOO_PASSWORD` | yes | its password (secret) |
| `ODOO_API_KEY` | no | an Experio API key, used instead of the password when set (secret) |
| `DRY_RUN` | no | `1` simulation (default for a new cabinet), `0` real writes in Experio |
| `IMAP_USER`, `IMAP_PASSWORD` | yes | Tarik's mailbox for this cabinet (reading) |
| `SMTP_USER`, `SMTP_PASSWORD` | yes | the account Tarik sends with |
| `SMTP_FROM` | no | the sender clients see, `Tarik <tarik@cabinet.ma>` |
| `MAIL_ALLOWED_RECIPIENTS` | no | who may receive Tarik's emails: addresses, `@domain`, patterns with `*`, comma-separated; empty: nobody |
| `MAIL_MANAGER` | no | told when an unknown sender writes, when Experio has no manager for the cabinet |

Values are checked as Tarik reads them; spaces around a value are removed, a secret is kept exactly. A secret is
never returned by any call, only whether it is set.

## Calls

All bodies and answers are JSON. An error is `{"error": "…"}` in plain French, with `400` (wrong value or missing
setting), `401` (key), `404` (no such cabinet) or `409` (already exists).

### `GET /api/cabinets`

Every cabinet Tarik knows: `[{"name": "experio_cabinet_8", "active": true}]`.

### `GET /api/cabinets/{name}`

`{"name", "active", "settings"}`, each secret only as `true` (set) or `false`.

### `POST /api/cabinets`

```json
{"name": "experio_cabinet_60", "settings": {"ODOO_USER": "tarik@experio.ma", "ODOO_UID": "431", "ODOO_PASSWORD": "…",
 "IMAP_USER": "tarik60@experio.ma", "IMAP_PASSWORD": "…", "SMTP_USER": "tarik60@experio.ma", "SMTP_PASSWORD": "…",
 "MAIL_ALLOWED_RECIPIENTS": "@experio.ma"}}
```

- A new cabinet: every required setting, `201`. It starts in simulation unless `DRY_RUN` is `"0"`.
- A cabinet deactivated earlier: reactivated, with the settings given changed and the others kept, `200`.
- One already active: `409` (change it with `PATCH`).
- Answer: `{"name", "active": true, "changed": [settings written], "mailbox_watched": true}`. Its data schema is
  made and its mailbox is watched straight away.

### `PATCH /api/cabinets/{name}`

`{"settings": {"DRY_RUN": "0"}}`: only the settings given change. Answer `{"name", "changed": [...]}` (empty when
nothing differed).

### `DELETE /api/cabinets/{name}`

Deactivates: Tarik stops serving it at once (its mailbox no longer read, its pages and runs refused). Nothing is
deleted: its settings, files, conversations and runs are kept, and a `POST` with the same name brings it back.
Answer `{"name", "active": false}`.

## Traces

Each call shows in Tarik's Journal (actor `experio-api`) and in the cabinet's settings history (which settings,
never a secret's value).
