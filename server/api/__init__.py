"""Each page's data, as JSON under /ui/api/ (the Vue app in server/web/ shows it): runs.py the runs, the workflows, the
LLM calls and what every page shares; chat.py the Discussion and the dashboard; content.py the Tarik prompts;
settings.py each cabinet's settings; accounts.py the accounts and invitations; journal.py the Logs serveur."""

from server.api import accounts, chat, content, journal, runs, settings  # noqa: F401 -- reached as pages.<name>
