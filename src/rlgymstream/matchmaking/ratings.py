"""OpenSkill (Plackett-Luce) rating wrapper – per-mode ratings for bots."""

from __future__ import annotations

import math
from collections import defaultdict

from openskill.models.weng_lin.plackett_luce import PlackettLuce, PlackettLuceRating

from rlgymstream.db.database import Database

# Single model instance shared across all modes.
# PlackettLuce defaults: mu=25, sigma=25/3 ≈ 8.333
_model = PlackettLuce()

# Minimum sigma floor (matches Rocket League's minimum of 2.5).
MIN_SIGMA = 2.5

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
    is_solo_queue: bool = False,
) -> None:
    """Fetch current ratings, run OpenSkill update, persist new values.

    Draws are skipped (no mu/sigma change) — they shouldn't occur in
    normal Rocket League play and likely indicate an error.

    In standard modes, teams are deduplicated before passing to rate()
    (consistent with predict_win).  In solo queue, full teams are used
    since different bots contribute independently.

    When a bot occupies multiple slots (e.g. on both teams in solo queue),
    its posteriors are consolidated by summing the mu deltas and precision
    (1/σ²) deltas from each slot relative to the prior.
    """
    if winner == "draw":
        return

    if winner == "blue":
        ranks = [0, 1]
    else:
        ranks = [1, 0]

    if is_solo_queue:
        # Full teams — different bots contribute independently
        # Cache the prior ratings before calling rate()
        priors: dict[int, tuple[float, float]] = {}
        for bid in set(team_blue_ids) | set(team_orange_ids):
            r = db.get_rating(bid, mode)
            priors[bid] = (r.mu, r.sigma)

        blue_os = [make_rating(*priors[bid]) for bid in team_blue_ids]
        orange_os = [make_rating(*priors[bid]) for bid in team_orange_ids]

        new_blue, new_orange = _model.rate(
            teams=[blue_os, orange_os],
            ranks=ranks,
        )

        # Collect all posterior ratings per unique bot
        bot_posteriors: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for bid, rating in zip(team_blue_ids, new_blue):
            bot_posteriors[bid].append((rating.mu, rating.sigma))
        for bid, rating in zip(team_orange_ids, new_orange):
            bot_posteriors[bid].append((rating.mu, rating.sigma))

        for bid, posteriors in bot_posteriors.items():
            r = db.get_rating(bid, mode)
            mu_0, sigma_0 = priors[bid]
            r.mu, r.sigma = _consolidate_posteriors(mu_0, sigma_0, posteriors)
            r.matches_played += 1
            r.sigma = max(r.sigma, MIN_SIGMA)
            db.save_rating(r)
    else:
        # Deduplicated — same bot duplicated to fill team
        blue_unique = list(dict.fromkeys(team_blue_ids))
        orange_unique = list(dict.fromkeys(team_orange_ids))

        assert len(blue_unique) == len(orange_unique) == 1, \
            "Non-solo modes should have exactly 1 unique bot per team after deduplication."

        blue_os = [make_rating(db.get_rating(bid, mode).mu, db.get_rating(bid, mode).sigma)
                   for bid in blue_unique]
        orange_os = [make_rating(db.get_rating(bid, mode).mu, db.get_rating(bid, mode).sigma)
                     for bid in orange_unique]

        new_blue, new_orange = _model.rate(
            teams=[blue_os, orange_os],
            ranks=ranks,
        )

        for bid, rating in zip(blue_unique, new_blue):
            r = db.get_rating(bid, mode)
            r.mu = rating.mu
            r.sigma = max(rating.sigma, MIN_SIGMA)
            r.matches_played += 1
            db.save_rating(r)

        for bid, rating in zip(orange_unique, new_orange):
            r = db.get_rating(bid, mode)
            r.mu = rating.mu
            r.sigma = max(rating.sigma, MIN_SIGMA)
            r.matches_played += 1
            db.save_rating(r)


def predict_win_probability(
    db: Database,
    mode: str,
    team_blue_ids: list[int],
    team_orange_ids: list[int],
    is_solo_queue: bool = False,
) -> list[float]:
    """Return [p_blue, p_orange] win probabilities for the current ratings.

    In standard modes, teams are deduplicated (e.g. [A,A,A] → [A]) since
    duplicates are the same bot and would inflate the prediction.

    In solo queue, the full teams are used since different bots on the
    same team contribute independently to team strength.
    """
    if is_solo_queue:
        blue_os = [make_rating(*_mu_sigma(db, bid, mode)) for bid in team_blue_ids]
        orange_os = [make_rating(*_mu_sigma(db, bid, mode)) for bid in team_orange_ids]
    else:
        blue_unique = list(dict.fromkeys(team_blue_ids))
        orange_unique = list(dict.fromkeys(team_orange_ids))
        blue_os = [make_rating(*_mu_sigma(db, bid, mode)) for bid in blue_unique]
        orange_os = [make_rating(*_mu_sigma(db, bid, mode)) for bid in orange_unique]
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


def _consolidate_posteriors(
    mu_0: float,
    sigma_0: float,
    posteriors: list[tuple[float, float]],
) -> tuple[float, float]:
    """Consolidate multiple posterior ratings for a bot that occupied
    multiple slots in one match.

    Instead of naively averaging, we sum the *deltas* from the prior:
    - mu:  sum of (mu_i − mu_0)  (each slot's contribution is additive)
    - σ:   sum precision deltas  (precision = 1/σ², the natural additive
            quantity for Gaussian information)

    This avoids the anomaly where a bot on both teams gets a free sigma
    reduction (and thus MMR boost) from averaging.
    """
    if len(posteriors) == 1:
        return posteriors[0]

    # Sum mu deltas
    delta_mu = sum(mu_i - mu_0 for mu_i, _ in posteriors)
    mu_final = mu_0 + delta_mu

    # Sum precision deltas
    precision_0 = 1.0 / (sigma_0 ** 2)
    delta_precision = sum(
        1.0 / (sigma_i ** 2) - precision_0
        for _, sigma_i in posteriors
    )
    precision_final = precision_0 + delta_precision

    if precision_final <= 0:
        # Shouldn't happen in practice, but guard against it
        return mu_final, sigma_0

    sigma_final = math.sqrt(1.0 / precision_final)
    return mu_final, sigma_final


def _mu_sigma(db: Database, bot_id: int, mode: str) -> tuple[float, float]:
    r = db.get_rating(bot_id, mode)
    return r.mu, r.sigma
