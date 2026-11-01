from __future__ import annotations

from typing import Callable

import pytest

from rlgymstream.db.database import Database
from rlgymstream.db.models import Bot, Match, MatchPlayerStats
from rlgymstream.matchmaking.ratings import configure_defaults


@pytest.fixture(autouse=True)
def _reset_rating_defaults():
    """ratings.py keeps per-mode defaults in module globals — isolate tests."""
    configure_defaults({}, {})
    yield
    configure_defaults({}, {})


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


@pytest.fixture
def make_bots(db) -> Callable[..., list[Bot]]:
    def _make(*names: str, supported_modes: str = "") -> list[Bot]:
        return [
            db.upsert_bot(Bot(name=n, config_path=f"/bots/{n}/bot.toml",
                              supported_modes=supported_modes))
            for n in names
        ]
    return _make


@pytest.fixture
def add_match(db) -> Callable[..., Match]:
    def _add(mode: str, blue: list[Bot], orange: list[Bot], winner: str,
             score: tuple[int, int] = (1, 0), duration: float = 300.0) -> Match:
        return db.save_match(Match(
            mode=mode,
            map_name="DFHStadium",
            team_blue_ids=",".join(str(b.id) for b in blue),
            team_orange_ids=",".join(str(b.id) for b in orange),
            score_blue=score[0],
            score_orange=score[1],
            winner=winner,
            duration_seconds=duration,
        ))
    return _add


@pytest.fixture
def add_player_stats(db) -> Callable[..., None]:
    def _add(match: Match, bot: Bot, team_num: int = 0, slots: int = 1, **stats) -> None:
        assert match.id is not None
        db.save_match_player_stats([
            MatchPlayerStats(match_id=match.id, bot_id=bot.id, player_name=bot.name,
                             team_num=team_num, **stats)
            for _ in range(slots)
        ])
    return _add
