from __future__ import annotations

import math

import pytest

from rlgymstream.matchmaking.ratings import (
    MIN_SIGMA,
    _consolidate_posteriors,
    configure_defaults,
    get_leaderboard,
    predict_win_probability,
    update_ratings,
)


def test_winner_gains_and_loser_loses(db, make_bots):
    a, b = make_bots("A", "B")
    update_ratings(db, "1v1", [a.id], [b.id], "blue")

    ra, rb = db.get_rating(a.id, "1v1"), db.get_rating(b.id, "1v1")
    assert ra.mu > 25.0 > rb.mu
    assert ra.sigma < 25 / 3 and rb.sigma < 25 / 3
    assert ra.matches_played == rb.matches_played == 1


def test_standard_team_mode_rates_like_one_on_one(db, make_bots):
    """[A, A] vs [B, B] is deduplicated — same update as A vs B."""
    a, b = make_bots("A", "B")
    update_ratings(db, "1v1", [a.id], [b.id], "orange")
    update_ratings(db, "2v2", [a.id, a.id], [b.id, b.id], "orange")

    for bot in (a, b):
        r1, r2 = db.get_rating(bot.id, "1v1"), db.get_rating(bot.id, "2v2")
        assert r1.mu == pytest.approx(r2.mu)
        assert r1.sigma == pytest.approx(r2.sigma)
        assert r2.matches_played == 1


def test_draw_changes_nothing(db, make_bots):
    a, b = make_bots("A", "B")
    update_ratings(db, "1v1", [a.id], [b.id], "draw")
    r = db.get_rating(a.id, "1v1")
    assert (r.mu, r.matches_played) == (25.0, 0)


def test_anchored_bot_is_frozen_but_counted(db, make_bots):
    a, b = make_bots("A", "B")
    update_ratings(db, "1v1", [a.id], [b.id], "orange", anchored_bot_ids={a.id})

    ra, rb = db.get_rating(a.id, "1v1"), db.get_rating(b.id, "1v1")
    assert ra.mu == 25.0 and ra.sigma == pytest.approx(25 / 3)
    assert ra.matches_played == 1
    assert rb.mu > 25.0


def test_sigma_never_drops_below_floor(db, make_bots):
    a, b = make_bots("A", "B")
    for bot in (a, b):
        r = db.get_rating(bot.id, "1v1")
        r.sigma = MIN_SIGMA
        db.save_rating(r)

    update_ratings(db, "1v1", [a.id], [b.id], "blue")
    assert db.get_rating(a.id, "1v1").sigma == MIN_SIGMA
    assert db.get_rating(b.id, "1v1").sigma == MIN_SIGMA


def test_solo_queue_bot_on_both_teams_counts_once(db, make_bots):
    a, b, c = make_bots("A", "B", "C")
    update_ratings(db, "solo_2v2", [a.id, b.id], [a.id, c.id], "blue", is_solo_queue=True)

    assert db.get_rating(a.id, "solo_2v2").matches_played == 1
    assert db.get_rating(b.id, "solo_2v2").mu > 25.0
    assert db.get_rating(c.id, "solo_2v2").mu < 25.0


def test_consolidate_single_posterior_is_unchanged():
    assert _consolidate_posteriors(25.0, 8.0, [(27.0, 7.0)]) == (27.0, 7.0)


def test_consolidate_sums_mu_and_precision_deltas():
    mu, sigma = _consolidate_posteriors(25.0, 8.0, [(27.0, 7.0), (23.0, 7.0)])
    assert mu == pytest.approx(25.0)  # opposite deltas cancel
    expected_precision = 1 / 8.0**2 + 2 * (1 / 7.0**2 - 1 / 8.0**2)
    assert sigma == pytest.approx(math.sqrt(1 / expected_precision))


def test_win_probability_sums_to_one_and_favours_stronger(db, make_bots):
    a, b = make_bots("A", "B")
    r = db.get_rating(a.id, "1v1")
    r.mu = 35.0
    db.save_rating(r)

    p_a, p_b = predict_win_probability(db, "1v1", [a.id], [b.id])
    assert p_a + p_b == pytest.approx(1.0)
    assert p_a > 0.5 > p_b


def test_unrated_bot_uses_configured_default(db, make_bots):
    (a,) = make_bots("A")
    configure_defaults({"2v2": 40.0}, {"2v2": 5.0})
    r = db.get_rating(a.id, "2v2")
    assert (r.mu, r.sigma) == (40.0, 5.0)
    assert db.get_rating(a.id, "1v1").mu == 25.0


def test_leaderboard_respects_supported_modes(db, make_bots):
    make_bots("Ones", supported_modes="1v1")
    make_bots("All")
    names_1v1 = [e["bot"].name for e in get_leaderboard(db, "1v1")]
    names_2v2 = [e["bot"].name for e in get_leaderboard(db, "2v2")]
    assert sorted(names_1v1) == ["All", "Ones"]
    assert names_2v2 == ["All"]
