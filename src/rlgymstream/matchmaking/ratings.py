"""OpenSkill (Plackett-Luce) rating wrapper – per-mode ratings for bots."""

from __future__ import annotations

from openskill.models.weng_lin.plackett_luce import PlackettLuce, PlackettLuceRating

from rlgymstream.db.database import Database

# Single model instance shared across all modes.
# PlackettLuce defaults: mu=25, sigma=25/3 ≈ 8.333
_model = PlackettLuce()

# ── Public helpers ────────────────────────────────────────────────────


def ordinal(mu: float, sigma: float) -> float:
    """Conservative display rating  (mu − 3·sigma)."""
    return mu - 3 * sigma


def make_rating(mu: float = 25.0, sigma: float = 8.333333333333334) -> PlackettLuceRating:
    """Create an openskill rating object from stored values."""
    return _model.rating(mu=mu, sigma=sigma)


def update_ratings(
    db: Database,
    mode: str,
    team_blue_ids: list[int],
    team_orange_ids: list[int],
    winner: str,  # "blue", "orange", "draw"
) -> None:
    """Fetch current ratings, run OpenSkill update, persist new values.

    Handles duplicate bot IDs:
    - Within-team duplicates (e.g. standard 2v2: [5,5] vs [7,7]) are
      deduplicated so each bot is rated once.
    - Cross-team duplicates (solo queue: bot on both sides) get the
      *average* of their blue-side and orange-side post-match ratings.
      This is fair: the win and loss partially cancel out, with the
      residual reflecting the strength of the other players.
    """
    blue_unique = list(dict.fromkeys(team_blue_ids))
    orange_unique = list(dict.fromkeys(team_orange_ids))
    shared = set(blue_unique) & set(orange_unique)

    # Build the full deduplicated teams for the OpenSkill update.
    # Shared bots appear in both teams — that's fine for rate().
    blue_db = [db.get_rating(bid, mode) for bid in blue_unique]
    orange_db = [db.get_rating(bid, mode) for bid in orange_unique]

    blue_os = [make_rating(r.mu, r.sigma) for r in blue_db]
    orange_os = [make_rating(r.mu, r.sigma) for r in orange_db]

    # ranks: lower = better.  Same rank → draw.
    if winner == "blue":
        ranks = [0, 1]
    elif winner == "orange":
        ranks = [1, 0]
    else:
        ranks = [0, 0]  # draw

    new_blue, new_orange = _model.rate(
        teams=[blue_os, orange_os],
        ranks=ranks,
    )

    # Map bot_id → new rating from each side
    blue_results = dict(zip(blue_unique, new_blue))
    orange_results = dict(zip(orange_unique, new_orange))

    # Collect all unique bots and save once each
    all_ids = list(dict.fromkeys(blue_unique + orange_unique))
    for bid in all_ids:
        r = db.get_rating(bid, mode)
        in_blue = bid in blue_results
        in_orange = bid in orange_results

        if in_blue and in_orange:
            # Shared bot: average the two post-match ratings
            r.mu = (blue_results[bid].mu + orange_results[bid].mu) / 2
            r.sigma = (blue_results[bid].sigma + orange_results[bid].sigma) / 2
        elif in_blue:
            r.mu = blue_results[bid].mu
            r.sigma = blue_results[bid].sigma
        else:
            r.mu = orange_results[bid].mu
            r.sigma = orange_results[bid].sigma

        r.matches_played += 1
        db.save_rating(r)


def predict_win_probability(
    db: Database,
    mode: str,
    team_blue_ids: list[int],
    team_orange_ids: list[int],
) -> list[float]:
    """Return [p_blue, p_orange] win probabilities for the current ratings."""
    blue_os = [make_rating(*_mu_sigma(db, bid, mode)) for bid in team_blue_ids]
    orange_os = [make_rating(*_mu_sigma(db, bid, mode)) for bid in team_orange_ids]
    return _model.predict_win(teams=[blue_os, orange_os])


def get_leaderboard(db: Database, mode: str) -> list[dict]:
    """Return sorted leaderboard for a mode.

    Only includes bots that support the mode.  Bots that haven't played
    in this mode yet appear with default ratings.
    """
    all_bots = db.get_all_bots(enabled_only=True)
    result = []
    for bot in all_bots:
        if not bot.supports_mode(mode):
            continue
        assert bot.id is not None
        r = db.get_rating(bot.id, mode)
        display = ordinal(r.mu, r.sigma)
        result.append({
            "bot": bot,
            "rating": r,
            "display_rating": round(display, 1),
            "mu": round(r.mu, 1),
            "sigma": round(r.sigma, 1),
        })
    result.sort(key=lambda x: x["display_rating"], reverse=True)
    return result


# ── Private ───────────────────────────────────────────────────────────

def _mu_sigma(db: Database, bot_id: int, mode: str) -> tuple[float, float]:
    r = db.get_rating(bot_id, mode)
    return r.mu, r.sigma
