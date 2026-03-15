"""Shared overlay state – updated by the orchestrator, read by the web server."""

from __future__ import annotations

import json
import time
import threading
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class OverlayBotInfo:
    id: int = 0
    name: str = ""
    author: str = ""
    description: str = ""
    fun_fact: str = ""
    language: str = ""
    mmr: int = 600          # 20 * mu + 100
    mu: float = 25.0
    sigma: float = 8.333
    matches_played: int = 0
    wins: int = 0
    losses: int = 0
    logo_path: str | None = None
    anchored: bool = False


@dataclass
class OverlayMatchState:
    phase: str = "idle"  # idle, pregame, countdown, live, replay, postgame
    mode: str = ""
    mode_display: str = ""
    map_name: str = ""
    team_blue: list[OverlayBotInfo] = field(default_factory=list)
    team_orange: list[OverlayBotInfo] = field(default_factory=list)
    score_blue: int = 0
    score_orange: int = 0
    winner: str = ""
    match_number: int = 0
    win_probabilities: list[float] = field(default_factory=list)  # [p_blue, p_orange]
    mmr_deltas: dict[int, int] = field(default_factory=dict)  # bot_id → MMR change
    updated_at: float = field(default_factory=time.time)


@dataclass
class OverlayLeaderboardEntry:
    rank: int = 0
    bot_name: str = ""
    author: str = ""
    mmr: int = 600
    mu: float = 0.0
    sigma: float = 0.0
    matches_played: int = 0
    anchored: bool = False


@dataclass
class OverlayState:
    """Central state object shared between orchestrator and overlay server."""

    match: OverlayMatchState = field(default_factory=OverlayMatchState)
    leaderboards: dict[str, list[OverlayLeaderboardEntry]] = field(
        default_factory=dict
    )
    recent_results: list[dict[str, Any]] = field(default_factory=list)
    head_to_head: dict[str, Any] = field(default_factory=dict)
    total_matches: int = 0
    session_matches: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _version: int = 0

    @property
    def version(self) -> int:
        return self._version

    def update_match(self, match_state: OverlayMatchState) -> None:
        with self._lock:
            self.match = match_state
            self.match.updated_at = time.time()
            self._version += 1

    def update_leaderboards(
        self, boards: dict[str, list[OverlayLeaderboardEntry]]
    ) -> None:
        with self._lock:
            self.leaderboards = boards
            self._version += 1

    def update_head_to_head(self, h2h: dict[str, Any]) -> None:
        with self._lock:
            self.head_to_head = h2h
            self._version += 1

    def add_recent_result(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.recent_results.insert(0, result)
            self.recent_results = self.recent_results[:20]
            self.total_matches += 1
            self.session_matches += 1
            self._version += 1

    def to_json(self) -> str:
        with self._lock:
            data = {
                "match": asdict(self.match),
                "leaderboards": {
                    k: [asdict(e) for e in v]
                    for k, v in self.leaderboards.items()
                },
                "recent_results": self.recent_results,
                "head_to_head": self.head_to_head,
                "total_matches": self.total_matches,
                "version": self._version,
            }
            return json.dumps(data)

