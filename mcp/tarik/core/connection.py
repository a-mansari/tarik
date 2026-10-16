"""Phase 0: can Tarik reach Experio?"""

from tarik.core import experio
from tarik.core.app import log, server
from tarik.core.experio import account, odoo_call


@server.tool()
def experio_connection() -> str:
    """Check that Tarik can reach the Experio system and log in. Call it when
    asked whether Experio works, or before anything else when in doubt."""
    # Connection details, and the fact that Experio runs on Odoo, stay in this log: the agent never sees them.
    try:
        version = experio.rpc("/xmlrpc/2/common").version()["server_version"]
    except Exception as error:
        log(f"experio_connection: {experio.ODOO_URL} unreachable: {error}")
        return "Experio est injoignable pour le moment."
    try:
        # A real read, not only the login: a password can log in yet be refused every call.
        odoo_call("res.company", "search_count", [])
    except Exception as error:
        database, _, user, _ = account()
        log(f"experio_connection: {user} on {database} refused: {error}")
        return "Experio repond, mais ma connexion y est refusee."
    log(f"experio_connection: ok, Odoo {version}")
    return "Connecte au systeme Experio."
