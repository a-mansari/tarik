"""Tarik's Experio tools, served over MCP for the n8n workflows. Importing each module
registers its tools on tarik.core.app.server."""

import tarik.core.connection  # noqa: F401
import tarik.dossiers.onboarding  # noqa: F401
import tarik.dossiers.people  # noqa: F401
import tarik.dossiers.search  # noqa: F401
import tarik.emails.inbox  # noqa: F401
import tarik.emails.sending  # noqa: F401
import tarik.invoices.accounts  # noqa: F401
import tarik.invoices.check  # noqa: F401
import tarik.invoices.classify  # noqa: F401
import tarik.invoices.correct  # noqa: F401
import tarik.invoices.duplicates  # noqa: F401
import tarik.invoices.exercice  # noqa: F401
import tarik.invoices.intake  # noqa: F401
import tarik.invoices.journal  # noqa: F401
import tarik.invoices.listing  # noqa: F401
import tarik.invoices.pipeline  # noqa: F401
import tarik.invoices.posting  # noqa: F401
import tarik.invoices.prepaid  # noqa: F401
import tarik.invoices.tiers  # noqa: F401
import tarik.invoices.tva  # noqa: F401
import tarik.invoices.withholding  # noqa: F401
from tarik.core.app import server

if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)
