# Experio MCP server

Read-only and standalone: Claude Code fetches a cabinet's Experio data through it and builds the dashboards and
reports. Nothing it offers writes to Experio.

## Install
```bash
python3 -m venv experio_mcp/.venv && experio_mcp/.venv/bin/pip install "mcp>=1.2,<2"
```

## Cabinets and the login
`~/.config/experio-mcp/cabinets.json`, one entry per cabinet, named by its Experio database:
```json
{"experio_cabinet_8": {"url": "https://experio.example.com", "user": "login", "key": "API key"}}
```
- **Make the file yours alone:** `chmod 600 ~/.config/experio-mcp/cabinets.json`. The server refuses a file others can
  read or write, and an `http://` url.
- **Use a dedicated Experio user** with read access to the dossiers Claude may see, never an administrator, and an **API
  key** you can revoke rather than your password.
- The generic tools refuse system models (settings, users, mail servers, attachments, bank accounts) and fields named
  like a secret, but they can read everything else the login can: what the login may not read, Claude cannot either.
- Claude's own file tools run as you and could read that file: add `Read(~/.config/experio-mcp/**)` to the permission
  deny rules of Claude Code.

## Connect
```bash
claude mcp add experio -- "$PWD/experio_mcp/.venv/bin/python" "$PWD/experio_mcp/server.py"
```

## What to know
- Each cabinet is one Experio database; its client dossiers are companies in it, picked with `dossier_id`
  (`list_dossiers` lists the ones the login can open).
- Reads name their columns (`describe_model` lists them): an empty list is refused, one invoice has 284 columns. An answer
  over 150 KB is refused; `export_csv` writes big extracts to `~/experio-reports` (or `EXPERIO_MCP_OUT`), 600 rights,
  never over an existing file.
- The report rules (posted entries, closure entries left out, open balances from Experio's residual, credit notes
  apart) are copies of Tarik's own (`engine/tools/data/`): change one, change the other.
