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
    """Fetch current ratings, run OpenSkill update, persist new values."""

    blue_db = [db.get_rating(bid, mode) for bid in team_blue_ids]
    orange_db = [db.get_rating(bid, mode) for bid in team_orange_ids]

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

    for db_rating, new_r in zip(blue_db, new_blue, strict=True):
        db_rating.mu = new_r.mu
        db_rating.sigma = new_r.sigma
        db_rating.matches_played += 1
        db.save_rating(db_rating)

    for db_rating, new_r in zip(orange_db, new_orange, strict=True):
        db_rating.mu = new_r.mu
        db_rating.sigma = new_r.sigma
        db_rating.matches_played += 1
        db.save_rating(db_rating)


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
    """Return sorted leaderboard for a mode, including every enabled bot.

    Bots that haven't played in this mode yet appear with default ratings.
    """
    all_bots = db.get_all_bots(enabled_only=True)
    result = []
    for bot in all_bots:
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
