"""Tarik's Experio tools, served over MCP for the n8n agent. One module per phase,
ported from tarik-master as each step needs it."""

import check  # noqa: F401 -- each phase module registers its tools on app.server
import classify  # noqa: F401
import connection  # noqa: F401
import correct  # noqa: F401
import dossiers  # noqa: F401
import intake  # noqa: F401
import tiers  # noqa: F401
from app import server

if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)
