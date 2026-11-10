"""What the operator sets: which cabinets are served (and where each one's Experio is) and the limits. Nothing secret:
the clients bring their own Experio login or API key with every request, and the server keeps none."""

import json
import os
import re
from pathlib import Path
from typing import Final

CABINET_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")  # an Experio database name (Tarik's own rule)
CABINETS_FILE: Final = Path(os.environ.get("EXPERIO_CLIENT_MCP_CABINETS", "/etc/experio-client-mcp/cabinets.json"))


def number(name, default):
    return int(os.environ.get(f"EXPERIO_CLIENT_MCP_{name}", default))


RATE_PER_MINUTE: Final = number("RATE_PER_MINUTE", 60)  # tool calls of one credential
CONCURRENT: Final = number("CONCURRENT", 3)  # calls of one credential at once
FAILED_MAX: Final = number("FAILED_MAX", 5)  # wrong credentials before a pause, for one login and for one address
FAILED_WINDOW: Final = number("FAILED_WINDOW", 600)  # seconds a failure is remembered
PAUSE: Final = number("PAUSE", 900)  # seconds a login or an address is refused after too many failures
LOGIN_TTL: Final = number("LOGIN_TTL", 300)  # seconds a successful login is remembered
TIMEOUT: Final = number("TIMEOUT", 30)  # seconds, per Experio call
MAX_CHARS: Final = number("MAX_CHARS", 100_000)  # an answer longer than this is refused
PUBLIC_HOSTS: Final = [
    h for h in os.environ.get("EXPERIO_CLIENT_MCP_HOSTS", "").split(",") if h.strip()
]  # Host headers
TRUST_PROXY: Final = os.environ.get("EXPERIO_CLIENT_MCP_TRUST_PROXY") == "1"  # X-Forwarded-For is the proxy's own
ALLOW_INSECURE: Final = os.environ.get("EXPERIO_CLIENT_MCP_ALLOW_INSECURE") == "1"  # http Experio: tests only


def load_cabinets(path=None):
    """Return {cabinet: Experio's address} from the cabinets file, checked: a database name and an https address.

    Raises ValueError, naming the entry, for anything else: the server does not start with a doubtful list."""
    path = Path(path or CABINETS_FILE)
    try:
        found = json.loads(path.read_text())
    except FileNotFoundError:
        raise ValueError(f"no cabinets file at {path}: see cabinets.example.json") from None
    except json.JSONDecodeError:
        raise ValueError(f"{path} is not valid JSON") from None
    if not isinstance(found, dict) or not found:
        raise ValueError(f"{path}: an object of cabinets is expected, with at least one")
    cabinets = {}
    for name, entry in found.items():
        url = entry.get("url", "") if isinstance(entry, dict) else ""
        if not CABINET_NAME.fullmatch(name):
            raise ValueError(f"{name!r}: not an Experio database name (lowercase letters, digits, _)")
        if not (url.startswith("https://") or (ALLOW_INSECURE and url.startswith("http://"))):
            raise ValueError(f"{name}: url must be https")
        cabinets[name] = url.rstrip("/")
    return cabinets
