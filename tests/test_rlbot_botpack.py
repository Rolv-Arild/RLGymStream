"""The RLBot botpack flag flows from config → DB → overlay state."""

from __future__ import annotations

import json
import sqlite3

from rlgymstream.config import AppConfig
from rlgymstream.db.database import Database
from rlgymstream.db.models import Bot
from rlgymstream.overlay.state import OverlayBotInfo, OverlayLeaderboardEntry, OverlayMatchState, OverlayState


def test_config_parses_flag(tmp_path):
    cfg_file = tmp_path / "rlgymstream.toml"
    path = tmp_path.as_posix()
    cfg_file.write_text(
        f'[[bot_sources]]\npath = "{path}"\n\n'
        f'[[bot_sources]]\npath = "{path}"\nrlbot_botpack = true\n',
        encoding="utf-8",
    )
    cfg = AppConfig.from_toml(cfg_file)
    assert [s.rlbot_botpack for s in cfg.bot_sources] == [False, True]


def test_flag_round_trips_and_updates(db):
    bot = db.upsert_bot(Bot(name="Nexto", config_path="/x/bot.toml", rlbot_botpack=True))
    assert db.get_bot(bot.id).rlbot_botpack is True

    # Moving the bot to a non-botpack source clears the flag, keeping its id.
    moved = db.upsert_bot(Bot(name="Nexto", config_path="/y/bot.toml"))
    assert moved.id == bot.id
    assert db.get_bot(bot.id).rlbot_botpack is False


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
        author TEXT NOT NULL DEFAULT '', config_path TEXT NOT NULL, logo_path TEXT,
        description TEXT NOT NULL DEFAULT '', fun_fact TEXT NOT NULL DEFAULT '',
        language TEXT NOT NULL DEFAULT '', supported_modes TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1)""")
    conn.execute("INSERT INTO bots (name, config_path) VALUES ('Old', '/old/bot.toml')")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.get_bot_by_name("Old").rlbot_botpack is False


def test_overlay_json_includes_flag():
    state = OverlayState()
    state.update_match(OverlayMatchState(team_blue=[OverlayBotInfo(name="Nexto", rlbot_botpack=True)]))
    state.update_leaderboards({"1v1": [OverlayLeaderboardEntry(bot_name="Nexto", rlbot_botpack=True)]})
    data = json.loads(state.to_json())
    assert data["match"]["team_blue"][0]["rlbot_botpack"] is True
    assert data["leaderboards"]["1v1"][0]["rlbot_botpack"] is True
