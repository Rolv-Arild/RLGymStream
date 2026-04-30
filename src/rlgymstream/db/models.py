"""Data models (plain dataclasses mirroring DB tables)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Bot:
    id: int | None = None
    name: str = ""
    author: str = ""
    config_path: str = ""        # absolute path to bot.toml
    logo_path: str | None = None
    description: str = ""
    fun_fact: str = ""
    language: str = ""
    supported_modes: str = ""    # comma-separated: "1v1,2v2,3v3" (empty = all)
    enabled: bool = True

    def supports_mode(self, mode: str) -> bool:
        """Check if this bot supports the given mode (e.g. '1v1', '2v2', '3v3').

        The *mode* value can be a MatchMode value like ``"solo_2v2"`` — the
        underlying team size tag (``"2v2"``) is checked.  If *supported_modes*
        is empty the bot is assumed to support everything.
        """
        if not self.supported_modes:
            return True  # no tags → assume all modes
        tags = {t.strip() for t in self.supported_modes.split(",") if t.strip()}
        # Map MatchMode values to the tag that matters
        mode_to_tag = {
            "1v1": "1v1",
            "2v2": "2v2",
            "3v3": "3v3",
            "solo_2v2": "2v2",
            "solo_3v3": "3v3",
        }
        needed = mode_to_tag.get(mode, mode)
        return needed in tags


@dataclass
class Rating:
    bot_id: int = 0
    mode: str = ""       # MatchMode.value
    mu: float = 25.0                # OpenSkill default
    sigma: float = 8.333333333333334  # OpenSkill default (25/3)
    matches_played: int = 0


@dataclass
class Match:
    id: int | None = None
    mode: str = ""
    map_name: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    team_blue_ids: str = ""   # comma-separated bot ids
    team_orange_ids: str = ""
    score_blue: int = 0
    score_orange: int = 0
    winner: str = ""          # "blue", "orange", or "draw"
    duration_seconds: float = 0.0


@dataclass
class MatchPlayerStats:
    """Post-game stats for a single player in a match, from the Stats API."""
    id: int | None = None
    match_id: int = 0
    bot_id: int | None = None       # FK → bots (None if we can't map the name)
    player_name: str = ""           # name as reported by the Stats API
    team_num: int = 0               # 0 = Blue, 1 = Orange
    score: int = 0
    goals: int = 0
    shots: int = 0
    assists: int = 0
    saves: int = 0
    demos: int = 0
    touches: int = 0
    avg_boost: float = 0.0
    avg_speed: float = 0.0
    pct_supersonic: float = 0.0
    pct_ground: float = 0.0
    pct_wall: float = 0.0
    pct_air: float = 0.0
    pct_demolished: float = 0.0
