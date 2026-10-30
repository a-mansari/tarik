"""Start Tarik's server (python -m server, the image's command): server/server.py's routes on uvicorn, port 2024."""

import uvicorn

from server.server import app

# any proxy's X-Forwarded-Proto and -For: only 127.0.0.1 is published, and Tailscale Funnel sets both itself (the
# browser's https, its real address; a forged X-Forwarded-For is replaced), which the login throttle keys on
uvicorn.run(app, host="0.0.0.0", port=2024, log_level="warning", forwarded_allow_ips="*")
