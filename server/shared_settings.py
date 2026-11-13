"""The settings every cabinet shares, as the general settings page (an admin's) shows them, read only.

They are environment variables (.env, .env.example), read at start and never written by Tarik: to change one, edit .env
and restart. A secret is never shown, only whether it is set."""

from engine.tools.core import cabinets

# Each setting the page shows, with what it holds (cabinet_settings.checked's kinds).
FIELDS = {
    "MEMORY_ENABLED": "toggle",
    "IMAP_HOST": "text",
    "IMAP_PORT": "integer",
    "IMAP_FOLDER": "text",
    "SMTP_HOST": "text",
    "SMTP_PORT": "integer",
    "MAIL_CERT_SHA256": "sha256",
    "MODEL_API_URL": "web",
    "MODEL_API_KEY": "secret",
    "CLAUDE_CODE_OAUTH_TOKEN": "secret",
    "EXPERIO_API_KEY": "secret",
    "PUBLIC_URL": "web",
    "GLADDEN_URL": "web",
    "GLADDEN_CALLBACK_URL": "web",
    "SPARROW_URL": "web",
    "SPARROW_SENDER_ID": "text",
    "SPARROW_TOKEN": "secret",
}
OPTIONAL = {
    "MEMORY_ENABLED",
    "MAIL_CERT_SHA256",
    "MODEL_API_URL",
    "MODEL_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "PUBLIC_URL",
    "GLADDEN_URL",
    "GLADDEN_CALLBACK_URL",
    "SPARROW_URL",
    "SPARROW_SENDER_ID",
    "SPARROW_TOKEN",
}
assert set(FIELDS) == cabinets.SHARED_SETTINGS


def current():
    """Return the shared settings as the page shows them: {key: value}, a secret only True (set) or False."""
    return {
        key: bool(cabinets.shared(key)) if kind == "secret" else cabinets.shared(key) for key, kind in FIELDS.items()
    }
