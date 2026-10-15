"""Tarik's Experio tools, served over MCP for the n8n workflows and agents. Importing each module
registers its tools on tarik.core.app.server.

Each cabinet has its own address, /c/<cabinet>/mcp (<cabinet>: one of TARIK_CABINETS), and every
call runs for the cabinet of its address only. There is no address without a cabinet. Stateless
HTTP: no session outlives its request, so none can carry one cabinet's call into another's.

Calls wait their turn: at most TARIK_CALLS_PER_CABINET at once for one cabinet (default 10), so a
cabinet's long batch (wait_until_read holds its call up to 3 minutes) never takes every slot, and at
most TARIK_CALLS_TOTAL at once in all (default 40), the tools' thread pool sized to match."""

import os
import re

import anyio
import anyio.to_thread
import uvicorn

from tarik.core import connection
from tarik.dossiers import identity, people, search, setup
from tarik.emails import agent, inbox, sending
from tarik.invoices import (accounts, check, classify, correct, duplicates, exercice, intake, journal, listing, pipeline,
                            posting, prepaid, tiers, tva, withholding)
from tarik.core.app import server
from tarik.core import vault
from tarik.core.cabinets import CABINETS, create_schemas, misplaced_in_vault, using
from tarik.core.experio import account

# Imported for their tools: each module registers its own on tarik.core.app.server.
TOOL_MODULES = (connection, identity, people, search, setup, agent, inbox, sending, accounts, check, classify, correct,
                duplicates, exercice, intake, journal, listing, pipeline, posting, prepaid, tiers, tva, withholding)
ADDRESS = re.compile(r"/c/([^/]+)/mcp/?")
PER_CABINET = int(os.getenv("TARIK_CALLS_PER_CABINET", "10"))
TOTAL = int(os.getenv("TARIK_CALLS_TOTAL", "40"))


def by_cabinet(app, per_cabinet=PER_CABINET, total=TOTAL):
    """The MCP app, reached only at a cabinet's address: the call runs for that cabinet, once
    its cabinet and the whole server each have a free slot."""
    slots, everyone = {}, []  # made on the first call: anyio's semaphores belong to the running event loop

    async def route(scope, receive, send):
        if scope["type"] != "http":  # lifespan
            return await app(scope, receive, send)
        found = ADDRESS.fullmatch(scope["path"])
        if not found or found.group(1) not in CABINETS:
            await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain")]})
            return await send({"type": "http.response.body", "body": b"unknown cabinet"})
        name = found.group(1)
        if not everyone:
            slots.update({cabinet: anyio.Semaphore(per_cabinet) for cabinet in CABINETS})
            everyone.append(anyio.Semaphore(total))
            anyio.to_thread.current_default_thread_limiter().total_tokens = total  # sync tools run in these threads
        async with slots[name], everyone[0]:  # the cabinet's slot first: waiting for the server holds no other's
            with using(name):
                await app(dict(scope, path="/mcp"), receive, send)
    return route


def app():
    return by_cabinet(server.streamable_http_app(stateless_http=True, host="0.0.0.0"))


def check_cabinets():
    """At start: every cabinet fully configured and its schema ready, or nothing is served."""
    if not vault.ENABLED:
        raise SystemExit("VAULT_ADDR is not set: Tarik's settings are read from Vault only")
    if not CABINETS:
        raise SystemExit("no cabinet in Vault (secret/tarik/cabinets/<cabinet>): no cabinet to serve")
    misplaced = misplaced_in_vault(*vault.settings())
    if misplaced:
        raise SystemExit(f"settings not where they belong (see tarik/core/cabinets.py): {', '.join(misplaced)}")
    for name in sorted(CABINETS):
        with using(name):
            account()  # exact database name and Tarik's user id, or SystemExit
    create_schemas()


if __name__ == "__main__":
    check_cabinets()
    uvicorn.run(app(), host="0.0.0.0", port=8000)
