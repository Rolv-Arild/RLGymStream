"""Mode tags at discovery: [bot_overrides] in rlgymstream.toml, and validation."""

from __future__ import annotations

import logging

import pytest

from rlgymstream.config import AppConfig, BotOverride, BotSource
from rlgymstream.match import bot_discovery
from rlgymstream.match.bot_discovery import BotTagError, discover_bots


@pytest.fixture(autouse=True)
def skip_rlbot_validation(monkeypatch):
    monkeypatch.setattr(bot_discovery, "load_player_config", lambda *a, **k: None)


def _write_bot(root, name, tags=None):
    folder = root / name
    folder.mkdir()
    details = f"tags = {tags!r}\n".replace("'", '"') if tags is not None else ""
    (folder / "bot.toml").write_text(
        f'[settings]\nname = "{name}"\n\n[details]\n{details}', encoding="utf-8"
    )


def test_config_parses_overrides(tmp_path):
    cfg_file = tmp_path / "rlgymstream.toml"
    cfg_file.write_text(
        '[bot_overrides."Dice"]\ntags = ["1v1", "2v2"]\n\n[bot_overrides.Other]\n',
        encoding="utf-8",
    )
    cfg = AppConfig.from_toml(cfg_file)
    assert cfg.bot_overrides == {"Dice": BotOverride(tags=["1v1", "2v2"]), "Other": BotOverride()}


def test_config_rejects_bad_tags(tmp_path):
    cfg_file = tmp_path / "rlgymstream.toml"
    cfg_file.write_text('[bot_overrides."Dice"]\ntags = "1v1"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Dice"):
        AppConfig.from_toml(cfg_file)


def test_override_replaces_tags(tmp_path, db):
    _write_bot(tmp_path, "Dice")                       # untagged: rejected without the override
    _write_bot(tmp_path, "Necto", ["1v1", "teamplay"])
    overrides = {"Dice": BotOverride(tags=["1v1", "2v2"]), "Necto": BotOverride(tags=["1v1"])}

    discover_bots([BotSource(path=tmp_path)], db, overrides)

    dice = db.get_bot_by_name("Dice")
    assert dice.supported_modes == "1v1,2v2"
    assert not dice.supports_mode("3v3") and not dice.supports_mode("solo_3v3")
    assert db.get_bot_by_name("Necto").supported_modes == "1v1"


def test_no_override_keeps_bot_toml_tags(tmp_path, db):
    _write_bot(tmp_path, "Kira", ["1v1"])
    _write_bot(tmp_path, "Necto", ["1v1", "teamplay", "rumble"])
    _write_bot(tmp_path, "Daizen", ["teamplay"])

    discover_bots([BotSource(path=tmp_path)], db, {"Necto": BotOverride()})

    assert db.get_bot_by_name("Kira").supported_modes == "1v1"
    assert db.get_bot_by_name("Necto").supported_modes == "1v1,2v2,3v3"
    assert db.get_bot_by_name("Daizen").supported_modes == "2v2,3v3"


@pytest.mark.parametrize("tags", [None, [], ["soccar", "rumble"]])
def test_missing_mode_tags_stop_discovery(tmp_path, db, tags):
    _write_bot(tmp_path, "Good", ["1v1", "2v2"])
    _write_bot(tmp_path, "Bad", tags)

    with pytest.raises(BotTagError, match="Bad") as excinfo:
        discover_bots([BotSource(path=tmp_path)], db)
    assert "Good" not in str(excinfo.value)
    assert db.get_bot_by_name("Bad") is None


def test_all_tag_errors_are_reported_together(tmp_path, db):
    _write_bot(tmp_path, "Dice", ["rumble"])
    _write_bot(tmp_path, "Nexto")

    with pytest.raises(BotTagError) as excinfo:
        discover_bots([BotSource(path=tmp_path)], db)
    assert "Dice" in str(excinfo.value) and "Nexto" in str(excinfo.value)
    assert "bot_overrides" in str(excinfo.value)


def test_override_without_mode_tags_is_rejected(tmp_path, db):
    _write_bot(tmp_path, "Dice", ["1v1", "2v2"])

    with pytest.raises(BotTagError, match="Dice.*bot_overrides"):
        discover_bots([BotSource(path=tmp_path)], db, {"Dice": BotOverride(tags=[])})


def test_override_fixes_untagged_bot(tmp_path, db):
    _write_bot(tmp_path, "Dice")

    discover_bots([BotSource(path=tmp_path)], db, {"Dice": BotOverride(tags=[" TeamPlay"])})

    assert db.get_bot_by_name("Dice").supported_modes == "2v2,3v3"


def test_warns_about_missing_bot_and_unknown_tag(tmp_path, db, caplog):
    _write_bot(tmp_path, "Dice")
    overrides = {"Dice": BotOverride(tags=["1v1", "1vs1"]), "Ghost": BotOverride(tags=["1v1"])}

    with caplog.at_level(logging.WARNING):
        discover_bots([BotSource(path=tmp_path)], db, overrides)

    assert "not found: Ghost" in caplog.text
    assert "'1vs1'" in caplog.text
    assert db.get_bot_by_name("Dice").supported_modes == "1v1"
