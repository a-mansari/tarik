"""Start Tarik's server (python -m server, the image's command): server/server.py's routes on uvicorn, port 2024."""

import uvicorn

from server.server import app

uvicorn.run(app, host="0.0.0.0", port=2024, log_level="warning")
