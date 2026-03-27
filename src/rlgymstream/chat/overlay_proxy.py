"""Proxy that mirrors OverlayState by polling the overlay HTTP API.

Used by the chatbot subprocess so it can read live match/leaderboard data
without sharing memory with the main process.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from rlgymstream.overlay.state import (
    OverlayBotInfo,
    OverlayLeaderboardEntry,
    OverlayMatchState,
)

logger = logging.getLogger("rlgymstream.chat.proxy")

# How often to poll the overlay API (seconds)
_POLL_INTERVAL = 1.0


@dataclass
class OverlayStateProxy:
    """Read-only mirror of OverlayState, populated via HTTP polling.

    Provides the same attribute interface that ChatCommands expects:
    ``match``, ``leaderboards``, ``session_matches``, ``_lock``.
    """

    match: OverlayMatchState = field(default_factory=OverlayMatchState)
    leaderboards: dict[str, list[OverlayLeaderboardEntry]] = field(
        default_factory=dict
    )
    session_matches: int = 0
    total_matches: int = 0
    head_to_head: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    _base_url: str = ""
    _task: asyncio.Task | None = field(default=None, repr=False)
    _session: aiohttp.ClientSession | None = field(default=None, repr=False)

    def start(self, base_url: str) -> None:
        """Begin polling in the background (call from an async context)."""
        self._base_url = base_url.rstrip("/")
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    # ── internal ─────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        self._session = aiohttp.ClientSession()
        url = f"{self._base_url}/api/state"
        consecutive_errors = 0

        while True:
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._apply(data)
                        consecutive_errors = 0
                    else:
                        consecutive_errors += 1
                        if consecutive_errors <= 3 or consecutive_errors % 30 == 0:
                            logger.warning(
                                "Overlay API returned %d (error #%d)",
                                resp.status, consecutive_errors,
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_errors += 1
                if consecutive_errors <= 3 or consecutive_errors % 30 == 0:
                    logger.warning(
                        "Failed to reach overlay API at %s (error #%d)",
                        url, consecutive_errors,
                        exc_info=(consecutive_errors == 1),
                    )

            await asyncio.sleep(_POLL_INTERVAL)

    def _apply(self, data: dict) -> None:
        """Parse the JSON blob from /api/state into our dataclass fields."""
        with self._lock:
            # Match state
            md = data.get("match", {})
            self.match = OverlayMatchState(
                phase=md.get("phase", "idle"),
                mode=md.get("mode", ""),
                mode_display=md.get("mode_display", ""),
                map_name=md.get("map_name", ""),
                team_blue=[_parse_bot_info(b) for b in md.get("team_blue", [])],
                team_orange=[_parse_bot_info(b) for b in md.get("team_orange", [])],
                score_blue=md.get("score_blue", 0),
                score_orange=md.get("score_orange", 0),
                winner=md.get("winner", ""),
                match_number=md.get("match_number", 0),
                win_probabilities=md.get("win_probabilities", []),
                mmr_deltas={
                    int(k): v
                    for k, v in md.get("mmr_deltas", {}).items()
                },
            )

            # Leaderboards
            self.leaderboards = {}
            for mode, entries in data.get("leaderboards", {}).items():
                self.leaderboards[mode] = [
                    OverlayLeaderboardEntry(
                        rank=e.get("rank", 0),
                        bot_name=e.get("bot_name", ""),
                        author=e.get("author", ""),
                        mmr=e.get("mmr", 0),
                        mu=e.get("mu", 0.0),
                        sigma=e.get("sigma", 0.0),
                        matches_played=e.get("matches_played", 0),
                        anchored=e.get("anchored", False),
                    )
                    for e in entries
                ]

            self.total_matches = data.get("total_matches", 0)
            self.session_matches = data.get("session_matches", 0)
            self.head_to_head = data.get("head_to_head", {})


def _parse_bot_info(d: dict) -> OverlayBotInfo:
    return OverlayBotInfo(
        id=d.get("id", 0),
        name=d.get("name", ""),
        author=d.get("author", ""),
        description=d.get("description", ""),
        fun_fact=d.get("fun_fact", ""),
        language=d.get("language", ""),
        mmr=d.get("mmr", 0),
        mu=d.get("mu", 0.0),
        sigma=d.get("sigma", 0.0),
        matches_played=d.get("matches_played", 0),
        wins=d.get("wins", 0),
        losses=d.get("losses", 0),
        logo_path=d.get("logo_path"),
        anchored=d.get("anchored", False),
    )


