"""Tests for small parsing/naming helpers."""

from __future__ import annotations

import pytest

from rlgymstream.chat.chatbot import split_vs
from rlgymstream.config import MatchMode
from rlgymstream.db.models import Bot
from rlgymstream.main import _stats_api_name_to_bot_id, _stats_api_player_names
from rlgymstream.matchmaking.matchmaker import MatchSetup
from rlgymstream.stats_api.client import StatsApiClient


@pytest.mark.parametrize("text, expected", [
    ("Nexto vs Necto", ("Nexto", "Necto")),
    ("Nexto VS Necto", ("Nexto", "Necto")),
    ("Nexto Vs Necto", ("Nexto", "Necto")),
    ("A, B vs. C, D", ("A, B", "C, D")),
    ("Versus vs Bot", ("Versus", "Bot")),
    ("Nexto", None),
    ("Nexto vsNecto", None),
])
def test_split_vs(text, expected):
    assert split_vs(text) == expected


def test_stats_api_names_number_duplicates_across_teams():
    a = Bot(id=1, name="A")
    b = Bot(id=2, name="B")
    setup = MatchSetup(mode=MatchMode.SOLO_THREES,
                       team_blue=[a, a, b], team_orange=[a, b, b])
    assert _stats_api_name_to_bot_id(setup) == {
        "0:A": 1, "0:A (2)": 1, "0:B": 2,
        "1:A (3)": 1, "1:B (2)": 2, "1:B (3)": 2,
    }
    assert _stats_api_player_names(setup) == set(_stats_api_name_to_bot_id(setup))


@pytest.mark.parametrize("buf, expected", [
    (b'{"a": 1}{"b": 2}', 8),
    (b'{"a": {"b": "}"}} trailing', 17),
    (b'{"a": "\\"}"}', 12),
    (b'{"a": {', -1),
])
def test_find_json_end(buf, expected):
    assert StatsApiClient._find_json_end(buf) == expected
