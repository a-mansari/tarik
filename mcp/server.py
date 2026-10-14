"""Tarik's Odoo tools, served over MCP for the n8n agent. One tool per step.

Ported from tarik-master/tarik/odoo.py as each step needs it.
"""

import os
import xmlrpc.client

from mcp.server.mcpserver import MCPServer

ODOO_URL = os.environ["ODOO_URL"].rstrip("/")
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USER = os.environ["ODOO_USER"]
ODOO_KEY = os.getenv("ODOO_API_KEY") or os.environ["ODOO_PASSWORD"]

server = MCPServer("tarik")


@server.tool()
def odoo_connection() -> str:
    """Check that Tarik can reach Experio (Odoo) and log in. Call it when asked
    whether Experio works, or before anything else when in doubt."""
    # Connection details stay in this server's log, never in the agent's answer.
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        version = common.version()["server_version"]
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_KEY, {})
    except Exception as error:
        print(f"odoo_connection: {ODOO_URL} unreachable: {error}", flush=True)
        return "Experio est injoignable pour le moment."
    if not uid:
        print(f"odoo_connection: login refused for {ODOO_USER} on {ODOO_DB}", flush=True)
        return "Experio repond, mais la connexion de Tarik est refusee."
    return f"Connecte a Experio (Odoo {version})."


if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)
