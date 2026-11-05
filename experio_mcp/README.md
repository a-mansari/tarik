# Experio MCP server

Read-only, standalone: Claude Code fetches a cabinet's Experio data through it and builds the dashboards and reports.

## Install
```bash
python3 -m venv experio_mcp/.venv && experio_mcp/.venv/bin/pip install "mcp>=1.2,<2"
```

## Cabinets
`~/.config/experio-mcp/cabinets.json` (chmod 600), one entry per cabinet, named by its Experio database:
```json
{"experio_cabinet_8": {"url": "https://experio.example.com", "user": "login", "key": "password or API key"}}
```

## Connect
```bash
claude mcp add experio -- "$PWD/experio_mcp/.venv/bin/python" "$PWD/experio_mcp/server.py"
```
Each cabinet is one Experio database; its client dossiers are companies in it, picked with `dossier_id` (`list_dossiers`).
None of the tools writes: `search_read`, `search_count`, `read`, `read_group`, `fields_get`, `name_search` are the only
calls the server can make.

- Discovery: `list_cabinets`, `whoami`, `list_models`, `describe_model`, `list_dossiers`, `list_fiscal_years`,
  `list_accounts`, `list_journals`, `list_partners`
- Any model: `count`, `search_read`, `get_records`, `name_search`, `read_group`, `export_csv` (big extracts to
  `~/experio-reports`, or `EXPERIO_MCP_OUT`)
- Reports: `trial_balance`, `monthly_totals`, `general_ledger`, `partner_balances`, `journal_totals`, `invoices`,
  `invoice_totals`, `bank_operations`
