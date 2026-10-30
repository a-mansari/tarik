"""Start Tarik's server (python -m server, the image's command): server/server.py's routes on uvicorn, port 2024.

The container always listens on 0.0.0.0:2024; the compose files publish it (TARIK_BIND, TARIK_PORT in .env)."""

import os

import uvicorn

from server.server import app

# Which proxies' X-Forwarded-Proto and -For uvicorn believes: the browser's https and real address, which the login
# throttle keys on. "*" trusts any sender, fit only when nothing but a proxy that sets both itself (Tailscale Funnel, a
# reverse proxy) can reach the port: a client reaching it directly could pick its own address and dodge the throttle.
# docker-compose.prod.yml requires TARIK_TRUSTED_PROXIES (the proxy's address, comma-separated); uvicorn takes
# X-Forwarded-For's leftmost entry.
uvicorn.run(
    app, host="0.0.0.0", port=2024, log_level="warning", forwarded_allow_ips=os.getenv("TARIK_TRUSTED_PROXIES", "*")
)
