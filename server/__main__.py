"""Start Tarik's server (python -m server, the image's command): server/server.py's routes on uvicorn, port 2024."""

import uvicorn

from server.server import app

# any proxy's X-Forwarded-Proto: only 127.0.0.1 is published, and behind Tailscale Funnel the browser is on https
uvicorn.run(app, host="0.0.0.0", port=2024, log_level="warning", forwarded_allow_ips="*")
