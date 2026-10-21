"""Tarik's server, started with python -m app (the image's command): app/server.py's routes on uvicorn."""

import uvicorn

from app.server import app

uvicorn.run(app, host="0.0.0.0", port=2024, log_level="warning")
