from __future__ import annotations


def test_bot_record_matches_ids_exactly(db, make_bots, add_match):
    """Bot 1 must not be credited with matches of bot 11 (substring trap)."""
    bots = make_bots(*[f"B{i}" for i in range(1, 13)])
    b1, b11, b12 = bots[0], bots[10], bots[11]
    assert (b1.id, b11.id) == (1, 11)

    add_match("1v1", [b11], [b12], "blue")
    add_match("1v1", [b1], [b12], "orange")
    add_match("1v1", [b1], [b11], "blue")

    assert db.get_bot_record(b1.id, "1v1") == (1, 1, 0)
    assert db.get_bot_record(b11.id, "1v1") == (1, 1, 0)
    assert db.get_bot_record(b12.id, "1v1") == (1, 1, 0)
    assert db.get_bot_record(b1.id, "2v2") == (0, 0, 0)


def test_head_to_head_only_counts_opposing_teams(db, make_bots, add_match):
    a, b, c = make_bots("A", "B", "C")
    add_match("solo_2v2", [a, c], [b, c], "blue", score=(3, 1))
    add_match("solo_2v2", [b, c], [a, a], "blue", score=(2, 0))
    add_match("solo_2v2", [a, b], [c, c], "blue")  # same team — ignored

    h2h = db.get_head_to_head(a.id, b.id)
    assert (h2h["wins_a"], h2h["wins_b"], h2h["total"]) == (1, 1, 2)
    assert (h2h["goals_a"], h2h["goals_b"]) == (3, 3)


def test_pairwise_h2h_keys_and_counts(db, make_bots, add_match):
    a, b = make_bots("A", "B")
    add_match("1v1", [a], [b], "blue")
    add_match("1v1", [b], [a], "blue")
    add_match("1v1", [b], [a], "blue")

    assert db.get_pairwise_h2h("1v1") == {(a.id, b.id): (1, 2)}


def test_avg_stats_count_matches_not_slots(db, make_bots, add_match, add_player_stats):
    a, b = make_bots("A", "B")
    m = add_match("3v3", [a, a, a], [b, b, b], "blue")
    add_player_stats(m, a, slots=3, goals=1)

    stats = db.get_bot_avg_stats(a.id)
    assert stats["matches"] == 1
    assert stats["avg_goals"] == 1.0


def test_avg_stats_limit_and_mode(db, make_bots, add_match, add_player_stats):
    a, b = make_bots("A", "B")
    # Older 1v1 matches with 0 goals, newer with 2; a 2v2 in between.
    for goals in (0, 0, 2, 2):
        m = add_match("1v1", [a], [b], "blue")
        add_player_stats(m, a, goals=goals)
        if goals == 0:
            m2 = add_match("2v2", [a, a], [b, b], "blue")
            add_player_stats(m2, a, slots=2, goals=5)

    assert db.get_bot_avg_stats(a.id, mode="1v1", limit=2)["avg_goals"] == 2.0
    assert db.get_bot_avg_stats(a.id, mode="1v1")["matches"] == 4
    assert db.get_bot_avg_stats(a.id, mode="1v1")["avg_goals"] == 1.0
    assert db.get_bot_avg_stats(a.id, limit=1)["avg_goals"] == 2.0
    assert db.get_bot_avg_stats(b.id) is None


def test_stat_leaders_min_matches_counts_distinct_matches(db, make_bots, add_match, add_player_stats):
    a, b = make_bots("A", "B")
    m = add_match("3v3", [a, a, a], [b, b, b], "blue")
    add_player_stats(m, a, slots=3, goals=2)

    assert db.get_stat_leaders("goals", min_matches=2) == []
    leaders = db.get_stat_leaders("goals", min_matches=1)
    assert leaders == [{"bot_name": "A", "value": 2.0, "matches": 1}]


def test_stat_leaders_rejects_unknown_column(db):
    assert db.get_stat_leaders("goals; DROP TABLE bots") is None
