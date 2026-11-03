from __future__ import annotations

import random

import pytest

from rlgymstream.config import MatchMode
from rlgymstream.matchmaking import matchmaker
from collections import Counter

from rlgymstream.matchmaking.matchmaker import format_map_name, information_score, pick_match


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
            setup = pick_match(db, mode, solo_candidates=100)
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
                           anchored_bot_ids={bots[0].id}, solo_candidates=100)
        ids = {b.id for b in setup.team_blue + setup.team_orange}
        assert bots[1].id in ids


def test_solo_teams_are_never_identical(db, make_bots):
    make_bots("A", "B", "C")
    for _ in range(200):
        setup = pick_match(db, MatchMode.SOLO_TWOS, solo_candidates=100)
        assert len(setup.team_blue) == len(setup.team_orange) == 2
        assert sorted(b.id for b in setup.team_blue) != sorted(b.id for b in setup.team_orange)


def test_solo_relaxes_teammate_threshold(db, make_bots, monkeypatch):
    """A teammate threshold of 0.99 can't be met by these ratings until it is
    squared, so candidates still fill up."""
    a, b, c = make_bots("A", "B", "C")
    for bot, mu in ((a, 40.0), (b, 25.0), (c, 10.0)):
        _set_rating(db, bot, "solo_2v2", mu=mu, sigma=2.5)
    monkeypatch.setattr(matchmaker.random, "random", lambda: 0.99)

    setup = pick_match(db, MatchMode.SOLO_TWOS, solo_candidates=50)
    assert sorted(x.id for x in setup.team_blue) != sorted(x.id for x in setup.team_orange)


def test_solo_fallback_is_valid(db, make_bots, monkeypatch):
    """Without any candidates the fallback still yields distinct teams."""
    a, b = make_bots("A", "B")
    monkeypatch.setattr(matchmaker, "_MAX_DRAWS", 0)

    setup = pick_match(db, MatchMode.SOLO_TWOS)
    assert {setup.team_blue[0].id, setup.team_orange[0].id} == {a.id, b.id}
    assert len(setup.team_blue) == len(setup.team_orange) == 2


# information_score: bots 1-6 share a rating unless stated otherwise.
_MU = {i: 25.0 for i in range(1, 7)}
_VAR = {i: 2.5 ** 2 for i in range(1, 7)}


def test_score_prefers_close_matches():
    mu = {**_MU, 1: 35.0}
    assert information_score([2, 3], [4, 5], _MU, _VAR) > information_score([1, 3], [4, 5], mu, _VAR)


def test_score_prefers_uncertain_ratings():
    var = {**_VAR, 1: 6.0 ** 2}
    assert information_score([1, 3], [4, 5], _MU, var) > information_score([2, 3], [4, 5], _MU, _VAR)


def test_bot_on_both_teams_carries_no_information():
    # With 1 on both teams, its rating (even a very uncertain one) adds nothing.
    var = {**_VAR, 1: 8.0 ** 2}
    assert information_score([1, 2], [1, 3], _MU, var) == pytest.approx(information_score([1, 2], [1, 3], _MU, _VAR))
    # ...so a mirror-like match scores below one with a different bot in that slot.
    assert information_score([1, 2], [1, 3], _MU, _VAR) < information_score([1, 2], [4, 3], _MU, _VAR)


def test_duplicate_teammates_count_like_different_bots():
    assert information_score([1, 1], [2, 3], _MU, _VAR) == pytest.approx(information_score([1, 4], [2, 3], _MU, _VAR))


def test_score_is_positive_for_lopsided_matches():
    mu = {**_MU, 1: 200.0}
    assert information_score([1, 1, 1], [2, 3, 4], mu, _VAR) > 0


def test_tiny_solo_temperature_does_not_crash(db, make_bots):
    make_bots("A", "B", "C", "D")
    assert pick_match(db, MatchMode.SOLO_THREES, solo_temperature=1e-6, solo_candidates=200) is not None


def test_solo_mirrors_are_rare(db, make_bots):
    """With 6 equally rated bots, random 3v3 teams share two bots in about
    27% of matches; weighting by information should make that much rarer."""
    make_bots("A", "B", "C", "D", "E", "F")
    shared_two = 0
    for _ in range(100):
        setup = pick_match(db, MatchMode.SOLO_THREES, solo_candidates=300)
        blue = Counter(b.id for b in setup.team_blue)
        orange = Counter(b.id for b in setup.team_orange)
        shared_two += sum((blue & orange).values()) >= 2
    assert shared_two <= 15


@pytest.mark.parametrize("raw, expected", [
    ("DFHStadium", "DFH Stadium"),
    ("NeoTokyo", "Neo Tokyo"),
    ("ForbiddenTemple_FireAndIce", "Forbidden Temple (Fire And Ice)"),
])
def test_format_map_name(raw, expected):
    assert format_map_name(raw) == expected


def test_matches_since_last_played(db, make_bots, add_match):
    a, b, c, d = make_bots("A", "B", "C", "D")
    add_match("1v1", [a], [c], "blue")          # oldest
    add_match("1v1", [a], [b], "blue")
    add_match("2v2", [d, d], [b, b], "blue")    # other mode: ignored
    add_match("1v1", [b], [a], "orange")        # newest
    idle = db.get_matches_since_last_played("1v1", [a.id, b.id, c.id, d.id])
    assert idle == {a.id: 0, b.id: 0, c.id: 2, d.id: 3}


def test_featured_bot_is_drawn_by_time_out(db, make_bots, add_match):
    a, b, c = make_bots("A", "B", "C")
    for _ in range(9):
        add_match("1v1", [a], [b], "blue")      # C has sat out 9 matches
    picks = [matchmaker._pick_featured_bot(db, [a, b, c], MatchMode.ONES, 0.0, set()).id
             for _ in range(1000)]
    # Weights 1 + idle: A 1, B 1, C 10, so C is featured ~83% of the time.
    assert 0.75 < picks.count(c.id) / len(picks) < 0.9


def test_settled_botpack_bots_are_featured_less(db, make_bots):
    a, b, c = make_bots("A", "B", "C")
    a.rlbot_botpack = b.rlbot_botpack = True
    _set_rating(db, a, "1v1", sigma=2.5)    # botpack, settled → half weight
    _set_rating(db, b, "1v1", sigma=5.0)    # botpack, still calibrating
    _set_rating(db, c, "1v1", sigma=2.5)    # settled, not botpack
    picks = Counter(matchmaker._pick_featured_bot(db, [a, b, c], MatchMode.ONES, 0.0, set(), 0.5).id
                    for _ in range(2000))
    # Weights A 0.5, B 1, C 1, so A is featured ~20% of the time.
    assert 0.16 < picks[a.id] / 2000 < 0.24
    assert 0.35 < picks[b.id] / 2000 < 0.45
    assert 0.35 < picks[c.id] / 2000 < 0.45


def test_zero_weight_settled_botpack_bots_are_never_featured(db, make_bots, add_match):
    a, b, c = make_bots("A", "B", "C")
    a.rlbot_botpack = True
    for bot in (a, b, c):
        _set_rating(db, bot, "1v1", sigma=2.5)  # all at the floor
    for _ in range(50):
        add_match("1v1", [b], [c], "blue")      # A has sat out 50 matches
    # Sigma priority always on: A would win the fewest-games tie-break.
    for chance in (0.0, 1.0):
        picks = {matchmaker._pick_featured_bot(db, [a, b, c], MatchMode.ONES, chance, set(), 0.0).id
                 for _ in range(200)}
        assert a.id not in picks


def test_zero_weight_falls_back_when_only_settled_botpack_bots(db, make_bots):
    a, b = make_bots("A", "B")
    for bot in (a, b):
        bot.rlbot_botpack = True
        _set_rating(db, bot, "1v1", sigma=2.5)
    picks = {matchmaker._pick_featured_bot(db, [a, b], MatchMode.ONES, 0.0, set(), 0.0).id
             for _ in range(200)}
    assert picks == {a.id, b.id}


@pytest.mark.parametrize("mode", [MatchMode.ONES, MatchMode.THREES, MatchMode.SOLO_THREES])
def test_match_always_includes_featured_bot(db, make_bots, mode, monkeypatch):
    bots = make_bots("A", "B", "C", "D", "E")
    monkeypatch.setattr(matchmaker, "_pick_featured_bot", lambda *args: bots[3])
    for _ in range(20):
        setup = pick_match(db, mode, solo_candidates=100)
        assert bots[3].id in {b.id for b in setup.team_blue + setup.team_orange}
