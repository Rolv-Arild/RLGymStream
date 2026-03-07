"""Application-level configuration."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from enum import Enum


class MatchMode(str, Enum):
    ONES = "1v1"
    TWOS = "2v2"
    THREES = "3v3"
    SOLO_TWOS = "solo_2v2"
    SOLO_THREES = "solo_3v3"

    @property
    def team_size(self) -> int:
        return {"1v1": 1, "2v2": 2, "3v3": 3, "solo_2v2": 2, "solo_3v3": 3}[self.value]

    @property
    def is_solo_queue(self) -> bool:
        return self.value.startswith("solo_")

    @property
    def display_name(self) -> str:
        labels = {
            "1v1": "1v1",
            "2v2": "2v2",
            "3v3": "3v3",
            "solo_2v2": "Solo Queue 2v2",
            "solo_3v3": "Solo Queue 3v3",
        }
        return labels[self.value]

    @property
    def min_bots_required(self) -> int:
        """Minimum distinct bots needed.

        Standard modes use full teams of one bot, so only 2 distinct bots
        are needed.  Solo-queue allows dupes, so just 1.
        """
        if self.is_solo_queue:
            return 1
        return 2


@dataclass
class BotSource:
    """A directory containing bot.toml files.

    *bots* lists relative paths to specific bot.toml files (or their parent
    directories — ``bot.toml`` is appended automatically).  If *bots* is
    empty **every** ``bot.toml`` found recursively under *path* is used.

    *exclude* lists relative paths (or simple ``fnmatch`` patterns) to skip.
    """
    path: Path
    bots: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    bot_sources: list[BotSource] = field(default_factory=list)
    db_path: Path = field(default_factory=lambda: Path("data/rlgymstream.db"))
    overlay_host: str = "127.0.0.1"
    overlay_port: int = 8080
    mode_rotation: list[MatchMode] = field(
        default_factory=lambda: list(MatchMode)
    )
    post_match_delay: float = 15.0  # seconds to show in-game scoreboard
    pre_match_delay: float = 15.0   # seconds to show pre-match screen
    leaderboard_delay: float = 15.0 # seconds to show leaderboard between matches
    sigma_priority_chance: float = 0.1  # chance to force highest-sigma bot into a match
    mercy_goal_diff: int = 8            # end match early if goal diff reaches this

    @classmethod
    def from_toml(cls, path: Path | str = "rlgymstream.toml") -> "AppConfig":
        """Load configuration from a TOML file.

        Example rlgymstream.toml::

            overlay_port = 8080
            mode_rotation = ["1v1", "2v2", "3v3", "solo_2v2", "solo_3v3"]

            [[bot_sources]]
            path = "C:/repos/RLGymPack"
            bots = [
                "necto",                         # folder containing bot.toml
                "nexto/bot.toml",                # explicit path to bot.toml
                "Byte/bob_build/Byte",
                "ripple/v1.1",
            ]

            [[bot_sources]]
            path = "C:/repos/my-bots"
            # empty bots list → use every bot.toml found recursively
            exclude = ["broken_bot"]
        """
        path = Path(path)
        cfg = cls()

        if path.exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)

            if "db_path" in data:
                cfg.db_path = Path(data["db_path"])
            if "overlay_host" in data:
                cfg.overlay_host = data["overlay_host"]
            if "overlay_port" in data:
                cfg.overlay_port = int(data["overlay_port"])
            if "post_match_delay" in data:
                cfg.post_match_delay = float(data["post_match_delay"])
            if "pre_match_delay" in data:
                cfg.pre_match_delay = float(data["pre_match_delay"])
            if "leaderboard_delay" in data:
                cfg.leaderboard_delay = float(data["leaderboard_delay"])
            if "sigma_priority_chance" in data:
                cfg.sigma_priority_chance = float(data["sigma_priority_chance"])
            if "mercy_goal_diff" in data:
                cfg.mercy_goal_diff = int(data["mercy_goal_diff"])
            if "mode_rotation" in data:
                cfg.mode_rotation = [MatchMode(m) for m in data["mode_rotation"]]
            else:
                cfg.mode_rotation = list(MatchMode)

            for src in data.get("bot_sources", []):
                cfg.bot_sources.append(BotSource(
                    path=Path(src["path"]),
                    bots=src.get("bots", []),
                    exclude=src.get("exclude", []),
                ))
        else:
            # Fallback: use a local bots/ directory
            cfg.mode_rotation = list(MatchMode)
            cfg.bot_sources = [BotSource(path=Path("bots"))]

        # Environment variable overrides
        if p := os.environ.get("RLGYMSTREAM_DB_PATH"):
            cfg.db_path = Path(p)
        if p := os.environ.get("RLGYMSTREAM_OVERLAY_PORT"):
            cfg.overlay_port = int(p)

        return cfg

