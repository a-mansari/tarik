"""Phase 0: can Tarik reach Experio?"""

import xmlrpc.client

from tarik.core.app import server
from tarik.core.experio import ODOO_DB, ODOO_URL, ODOO_USER, odoo_call


@server.tool()
def experio_connection() -> str:
    """Check that Tarik can reach the Experio system and log in. Call it when
    asked whether Experio works, or before anything else when in doubt."""
    # Connection details, and the fact that Experio runs on Odoo, stay in this log: the agent never sees them.
    try:
        version = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common").version()["server_version"]
    except Exception as error:
        print(f"experio_connection: {ODOO_URL} unreachable: {error}", flush=True)
        return "Experio est injoignable pour le moment."
    try:
        # A real read, not only the login: a password can log in yet be refused every call.
        odoo_call("res.company", "search_count", [])
    except Exception as error:
        print(f"experio_connection: {ODOO_USER} on {ODOO_DB} refused: {error}", flush=True)
        return "Experio repond, mais la connexion de Tarik est refusee."
    print(f"experio_connection: ok, Odoo {version}", flush=True)
    return "Connecte au systeme Experio."
