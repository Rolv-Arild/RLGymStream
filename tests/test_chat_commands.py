"""Chat command behaviour, driven without a Twitch connection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from rlgymstream.chat.chatbot import ChatCommands
from rlgymstream.config import AppConfig
from rlgymstream.overlay.state import OverlayLeaderboardEntry, OverlayState


class _Ctx:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)


@pytest.fixture
def chat(db):
    overlay = OverlayState()
    overlay.update_leaderboards({
        "1v1": [OverlayLeaderboardEntry(rank=1, bot_name="A", mmr=700)],
    })
    bot = SimpleNamespace(_db=db, _overlay=overlay, _config=AppConfig())
    commands = ChatCommands(bot)

    def run(name: str, *args, **kwargs) -> str:
        ctx = _Ctx()
        callback = getattr(ChatCommands, name).callback
        asyncio.run(callback(commands, ctx, *args, **kwargs))
        assert len(ctx.sent) == 1
        return ctx.sent[0]

    return run


def test_lb_requires_mode(chat):
    assert chat("cmd_lb").startswith("Usage: !lb <mode>")
    assert "Unknown mode" in chat("cmd_lb", "4v4")
    assert "#1 A (700)" in chat("cmd_lb", "1s")


def test_stats_top_requires_mode(chat, make_bots):
    assert "Please add a mode" in chat("cmd_stats", args="top goals")
    assert "Please add a mode" in chat("cmd_stats", args="demos")
    assert "Unknown stat" in chat("cmd_stats", args="top nonsense 1v1")
    assert "No data yet" in chat("cmd_stats", args="top goals 1v1")


def test_stats_top_reports_mode(chat, make_bots, add_match, add_player_stats):
    a, b = make_bots("A", "B")
    for _ in range(5):
        m = add_match("2v2", [a, a], [b, b], "blue")
        add_player_stats(m, a, slots=2, goals=1)
    msg = chat("cmd_stats", args="top goals 2s")
    assert msg == "🏆 Top Goals (2v2): 1. A (1.0)"


def test_stats_bot_requires_mode(chat, make_bots, add_match, add_player_stats):
    a, b = make_bots("A", "B")
    m = add_match("3v3", [a, a, a], [b, b, b], "blue")
    add_player_stats(m, a, slots=3, goals=1)
    assert "Please add a mode" in chat("cmd_stats", args="bot A")
    assert "No stats recorded for A in 1v1" in chat("cmd_stats", args="bot A 1v1")
    assert "A (3v3) avg over 1 matches" in chat("cmd_stats", args="bot A 3v3")


def test_predict_requires_mode_for_bot_names(chat, make_bots):
    make_bots("A", "B")
    assert "Please add a mode" in chat("cmd_predict", args="A vs B")
    assert chat("cmd_predict", args="A vs B 1v1") == "🔮 [1v1] A 50% — 50% B"
    # Raw MMR is mode-independent, so no mode needed.
    assert chat("cmd_predict", args="1000 vs 1000").startswith("🔮 MMR 1000 50%")


def test_h2h_accepts_any_vs_capitalisation(chat, make_bots, add_match):
    a, b = make_bots("A", "B")
    add_match("1v1", [a], [b], "blue")
    assert chat("cmd_h2h", args="A Vs B 1v1") == "⚔️ A vs B in 1v1: 1W-0L (1 games)"
