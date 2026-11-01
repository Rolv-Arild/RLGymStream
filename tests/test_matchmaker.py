from __future__ import annotations

import random

import pytest

from rlgymstream.config import MatchMode
from rlgymstream.matchmaking import matchmaker
from rlgymstream.matchmaking.matchmaker import format_map_name, pick_match


def _set_rating(db, bot, mode, mu=25.0, sigma=25 / 3):
    r = db.get_rating(bot.id, mode)
    r.mu, r.sigma = mu, sigma
    db.save_rating(r)


@pytest.fixture(autouse=True)
def _seed():
    random.seed(1234)


@pytest.mark.parametrize("mode", [MatchMode.ONES, MatchMode.TWOS, MatchMode.THREES])
def test_standard_pick_is_two_distinct_bots(db, make_bots, mode):
    make_bots("A", "B", "C", "D")
    for _ in range(50):
        setup = pick_match(db, mode)
        assert len(setup.team_blue) == len(setup.team_orange) == mode.team_size
        assert len({b.id for b in setup.team_blue}) == 1
        assert len({b.id for b in setup.team_orange}) == 1
        assert setup.team_blue[0].id != setup.team_orange[0].id


def test_pick_respects_supported_modes(db, make_bots):
    make_bots("Ones1", "Ones2", supported_modes="1v1")
    make_bots("Team1", "Team2", supported_modes="2v2,3v3")
    for mode in (MatchMode.TWOS, MatchMode.SOLO_THREES):
        for _ in range(30):
            setup = pick_match(db, mode)
            names = {b.name for b in setup.team_blue + setup.team_orange}
            assert names <= {"Team1", "Team2"}


def test_not_enough_bots_returns_none(db, make_bots):
    make_bots("Only")
    assert pick_match(db, MatchMode.ONES) is None
    assert pick_match(db, MatchMode.SOLO_TWOS) is None


def test_map_not_repeated(db, make_bots):
    make_bots("A", "B")
    for _ in range(30):
        assert pick_match(db, MatchMode.ONES, last_map="DFHStadium").map_name != "DFHStadium"


@pytest.mark.parametrize("mode", [MatchMode.ONES, MatchMode.SOLO_THREES])
def test_sigma_priority_includes_most_uncertain_unanchored_bot(db, make_bots, mode):
    bots = make_bots("A", "B", "C", "D", "E")
    for b in bots:
        _set_rating(db, b, mode.value, sigma=3.0)
    _set_rating(db, bots[0], mode.value, sigma=8.0)  # anchored — must be skipped
    _set_rating(db, bots[1], mode.value, sigma=6.0)  # the real priority bot

    for _ in range(30):
        setup = pick_match(db, mode, sigma_priority_chance=1.0,
                           anchored_bot_ids={bots[0].id})
        ids = {b.id for b in setup.team_blue + setup.team_orange}
        assert bots[1].id in ids


def test_solo_teams_are_never_identical(db, make_bots):
    make_bots("A", "B", "C")
    for _ in range(200):
        setup = pick_match(db, MatchMode.SOLO_TWOS)
        assert len(setup.team_blue) == len(setup.team_orange) == 2
        assert sorted(b.id for b in setup.team_blue) != sorted(b.id for b in setup.team_orange)


def test_solo_relaxes_thresholds(db, make_bots, monkeypatch):
    """Thresholds of 0.99 can't be met by these ratings until they are
    squared (they used to never be squared)."""
    a, b, c = make_bots("A", "B", "C")
    for bot, mu in ((a, 40.0), (b, 25.0), (c, 10.0)):
        _set_rating(db, bot, "solo_2v2", mu=mu, sigma=2.5)
    monkeypatch.setattr(matchmaker.random, "random", lambda: 0.99)

    attempts = 0
    real_choices = random.choices

    def counting_choices(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return real_choices(*args, **kwargs)

    monkeypatch.setattr(matchmaker.random, "choices", counting_choices)
    setup = pick_match(db, MatchMode.SOLO_TWOS)
    assert sorted(x.id for x in setup.team_blue) != sorted(x.id for x in setup.team_orange)
    # Two choices() calls per attempt; must have needed more than one round.
    assert matchmaker._RELAX_EVERY * 2 < attempts < matchmaker._MAX_RETRIES * 2


def test_solo_fallback_is_valid(db, make_bots, monkeypatch):
    """With an impossible threshold the fallback still yields distinct teams."""
    a, b = make_bots("A", "B")
    _set_rating(db, a, "solo_2v2", mu=40.0, sigma=2.5)
    _set_rating(db, b, "solo_2v2", mu=10.0, sigma=2.5)
    monkeypatch.setattr(matchmaker.random, "random", lambda: 1.0)
    monkeypatch.setattr(matchmaker, "_MAX_RETRIES", 500)

    setup = pick_match(db, MatchMode.SOLO_TWOS)
    assert {setup.team_blue[0].id, setup.team_orange[0].id} == {a.id, b.id}
    assert len(setup.team_blue) == len(setup.team_orange) == 2


@pytest.mark.parametrize("raw, expected", [
    ("DFHStadium", "DFH Stadium"),
    ("NeoTokyo", "Neo Tokyo"),
    ("ForbiddenTemple_FireAndIce", "Forbidden Temple (Fire And Ice)"),
])
def test_format_map_name(raw, expected):
    assert format_map_name(raw) == expected
