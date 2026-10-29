"""Serve each page's data as JSON under /ui/api/, shown by the Vue app in server/web/.

runs.py: the runs, the workflows, the LLM calls and what every page shares; chat.py: the Discussion and the
dashboard; content.py: Tarik's capabilities; settings.py: each cabinet's settings; behaviour.py: how Tarik behaves
for each; routines.py: each cabinet's routines; accounts.py: the accounts and invitations; journal.py: the Logs
serveur page."""

from server.api import (  # noqa: F401 -- reached as pages.<name>
    accounts,
    behaviour,
    chat,
    content,
    journal,
    routines,
    runs,
    settings,
)
