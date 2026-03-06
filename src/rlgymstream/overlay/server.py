"""FastAPI overlay web server for OBS Browser Sources."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from rlgymstream.overlay.state import OverlayState

logger = logging.getLogger(__name__)

OVERLAY_DIR = Path(__file__).parent
STATIC_DIR = OVERLAY_DIR / "static"
TEMPLATE_DIR = OVERLAY_DIR / "templates"


def create_overlay_app(state: OverlayState) -> FastAPI:
    """Create the FastAPI app wired to the shared overlay state."""
    app = FastAPI(title="RLGymStream Overlay")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    # Disable caching on all responses so OBS always gets fresh CSS/JS
    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    # ── Single overlay page (1920×1080 OBS Browser Source) ───────────

    @app.get("/", response_class=HTMLResponse)
    async def overlay(request: Request):
        return templates.TemplateResponse("overlay.html", {"request": request})

    # ── JSON API ─────────────────────────────────────────────────────

    @app.get("/api/state")
    async def api_state():
        return json.loads(state.to_json())

    @app.get("/api/logo/{bot_id}")
    async def bot_logo(bot_id: int):
        """Serve a bot's logo image from its filesystem path."""
        # Search current match teams + leaderboard for this bot's logo
        logo = _find_logo(state, bot_id)
        if logo and Path(logo).is_file():
            return FileResponse(logo)
        # 1×1 transparent PNG fallback
        return FileResponse(
            STATIC_DIR / "placeholder.png",
        ) if (STATIC_DIR / "placeholder.png").is_file() else HTMLResponse("", status_code=404)

    # ── SSE stream for live updates ──────────────────────────────────

    @app.get("/api/events")
    async def events(request: Request):
        async def event_generator():
            last_version = -1
            while True:
                if await request.is_disconnected():
                    break
                if state.version != last_version:
                    last_version = state.version
                    yield {"event": "state", "data": state.to_json()}
                await asyncio.sleep(0.5)

        return EventSourceResponse(event_generator())

    return app


def _find_logo(state: OverlayState, bot_id: int) -> str | None:
    """Find a bot's logo path from the current overlay state."""
    with state._lock:
        for b in state.match.team_blue + state.match.team_orange:
            if b.id == bot_id and b.logo_path:
                return b.logo_path
    return None

