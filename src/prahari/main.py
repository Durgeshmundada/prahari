"""ASGI entry point: ``uv run uvicorn prahari.main:app --app-dir src``."""

from prahari.api import create_app

app = create_app()
