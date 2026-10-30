"""Start Tarik's server (python -m server, the image's command): server/server.py's routes on uvicorn.

The address and port the server listens on, and the proxies it trusts, come from the environment (.env.example)."""

import os

import uvicorn

from server.server import app

# Which proxies' X-Forwarded-Proto and -For uvicorn believes: the browser's https and real address, which the login
# throttle keys on. "*" (the default) trusts any sender, fit when only a proxy that sets both itself (Tailscale
# Funnel, a reverse proxy) can reach the port; behind a proxy that appends to X-Forwarded-For instead, a client could
# pick its own address: set TARIK_TRUSTED_PROXIES to that proxy's address (comma-separated). uvicorn takes
# X-Forwarded-For's leftmost entry.
uvicorn.run(
    app,
    host=os.getenv("TARIK_HOST", "0.0.0.0"),
    port=int(os.getenv("TARIK_PORT", "2024")),
    log_level="warning",
    forwarded_allow_ips=os.getenv("TARIK_TRUSTED_PROXIES", "*"),
)
