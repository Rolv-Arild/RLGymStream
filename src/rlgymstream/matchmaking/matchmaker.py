"""Matchmaking – select bots for a given mode.

Standard modes use Temperature-Controlled Active Learning: for every
possible pair, a Beta distribution is formed from the OpenSkill prior
(weighted by ``n_prior``) plus the empirical head-to-head record.  The
Beta variance is raised to ``1/temperature`` to give a selection weight.
High-variance (uncertain) matchups are naturally favoured.

Solo-queue modes use accept/reject sampling with a random threshold for
match evenness and a separate threshold for teammate evenness.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass

from openskill.models.weng_lin.plackett_luce import PlackettLuceRating
from rlbot.utils.maps import STANDARD_MAPS

from rlgymstream.config import MatchMode
from rlgymstream.db.database import Database
from rlgymstream.db.models import Bot
from rlgymstream.matchmaking.ratings import make_rating, _model as _os_model

logger = logging.getLogger(__name__)


def format_map_name(raw: str) -> str:
    """Convert a map name like 'ForbiddenTemple_FireAndIce' to
    'Forbidden Temple (Fire And Ice)'.

    Inserts spaces before uppercase letters that follow a lowercase letter,
    e.g. 'NeoTokyo' → 'Neo Tokyo', 'DFHStadium' → 'DFH Stadium'.
    Underscore separates the base from a parenthetical variant.
    """

    def _split(s: str) -> str:
        return re.sub(r'(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', ' ', s)

    parts = raw.split("_", 1)
    base = _split(parts[0])
    if len(parts) == 1:
        return base
    variant = _split(parts[1])
    return f"{base} ({variant})"


@dataclass
class MatchSetup:
    mode: MatchMode
    team_blue: list[Bot]
    team_orange: list[Bot]
    map_name: str = "DFHStadium"

    @property
    def display_map_name(self) -> str:
        return format_map_name(self.map_name)


def pick_match(
        db: Database,
        mode: MatchMode,
        map_name: str | None = None,
        last_map: str | None = None,
        sigma_priority_chance: float = 0.0,
        anchored_bot_ids: set[int] | None = None,
        n_prior: float = 1.0,
        temperature: float = 1.0,
) -> MatchSetup | None:
    """Select bots for a match in the given mode.

    Returns None if not enough bots are available.
    """
    all_bots = db.get_all_bots(enabled_only=True)
    # Filter to bots that support this mode (based on [details].tags)
    bots = [b for b in all_bots if b.supports_mode(mode.value)]
    if len(bots) < mode.min_bots_required:
        return None

    team_size = mode.team_size
    if map_name:
        chosen_map = map_name
    else:
        # Avoid repeating the same map consecutively
        candidates = [m for m in STANDARD_MAPS if m != last_map] or STANDARD_MAPS
        chosen_map = random.choice(candidates)

    _anchored = anchored_bot_ids or set()

    if mode.is_solo_queue:
        return _solo_queue_pick(db, bots, mode, team_size, chosen_map, sigma_priority_chance, _anchored)
    else:
        return _standard_pick(db, bots, mode, team_size, chosen_map,
                              sigma_priority_chance, _anchored,
                              n_prior=n_prior, temperature=temperature)


# Maximum p*(1-p) is 0.25 (when p=0.5).  Used to normalise accept probability.
_MAX_WEIGHT = 0.25
_MAX_RETRIES = 1000


def _standard_pick(
        db: Database,
        bots: list[Bot],
        mode: MatchMode,
        team_size: int,
        map_name: str,
        sigma_priority_chance: float,
        anchored_bot_ids: set[int],
        n_prior: float = 1.0,
        temperature: float = 1.0,
) -> MatchSetup | None:
    """Standard mode: each team is one bot (duplicated to fill team_size).

    Uses Temperature-Controlled Active Learning:
      1. For every unique bot pair, compute the expected win probability
         from OpenSkill and use it as a Beta prior (weighted by *n_prior*).
      2. Fold in empirical head-to-head wins/losses.
      3. Compute the resulting Beta variance — high variance means we're
         uncertain about the true win probability of this matchup.
      4. Raise variance to ``1/temperature`` to control exploration.
      5. Normalise into a probability distribution and sample one pair.

    With *sigma_priority_chance* probability, only pairs that include
    the highest-sigma (least-calibrated) bot are considered.
    """
    if len(bots) < 2:
        return None

    # Pre-compute ratings
    bot_ratings: dict[int, PlackettLuceRating] = {}
    bot_sigmas: dict[int, float] = {}
    bot_games: dict[int, int] = {}
    for bot in bots:
        assert bot.id is not None
        r = db.get_rating(bot.id, mode.value)
        bot_ratings[bot.id] = make_rating(r.mu, r.sigma)
        bot_sigmas[bot.id] = r.sigma
        bot_games[bot.id] = r.matches_played

    # Sigma priority: restrict to pairs containing the highest-sigma bot
    unanchored = [b for b in bots if b.id not in anchored_bot_ids]
    use_sigma_priority = bool(unanchored) and random.random() < sigma_priority_chance
    priority_bot = max(
        unanchored,
        key=lambda b: (bot_sigmas[b.id], -bot_games[b.id]),
    ) if unanchored else None
    if use_sigma_priority and priority_bot:
        logger.debug("Sigma priority active: looking for %s (sigma=%.2f)",
                     priority_bot.name, bot_sigmas[priority_bot.id])

    # Bulk h2h lookup — one pass through match history
    h2h = db.get_pairwise_h2h(mode.value)

    # Build weighted list of all unique pairs
    pairs: list[tuple[Bot, Bot]] = []
    weights: list[float] = []
    inv_temp = 1.0 / max(temperature, 1e-9)

    for i in range(len(bots)):
        for j in range(i + 1, len(bots)):
            a, b = bots[i], bots[j]

            # When sigma priority is active, only keep pairs with the priority bot
            if use_sigma_priority and priority_bot:
                if a.id != priority_bot.id and b.id != priority_bot.id:
                    continue

            # 1. Expected win probability from OpenSkill
            probs = _os_model.predict_win(
                [[bot_ratings[a.id]], [bot_ratings[b.id]]],
            )
            expected_a = probs[0]

            # 2. Beta prior from the model prediction
            alpha_prior = expected_a * n_prior
            beta_prior = (1.0 - expected_a) * n_prior

            # 3. Integrate empirical wins/losses
            key = (min(a.id, b.id), max(a.id, b.id))
            wins_a, wins_b = 0, 0
            if key in h2h:
                if a.id == key[0]:
                    wins_a, wins_b = h2h[key]
                else:
                    wins_b, wins_a = h2h[key]

            alpha = alpha_prior + wins_a
            beta = beta_prior + wins_b

            # 4. Beta distribution variance
            total = alpha + beta
            variance = (alpha * beta) / ((total ** 2) * (total + 1))

            # 5. Temperature weighting
            weight = variance ** inv_temp

            pairs.append((a, b))
            weights.append(weight)

    if not pairs:
        # Edge case: sigma priority filtered everything out
        a, b = random.sample(bots, 2)
        return MatchSetup(
            mode=mode,
            team_blue=[a] * team_size,
            team_orange=[b] * team_size,
            map_name=map_name,
        )

    # 6. Probabilistic selection
    (a, b) = random.choices(pairs, weights=weights, k=1)[0]

    # Randomly assign blue vs orange
    if random.random() < 0.5:
        a, b = b, a

    return MatchSetup(
        mode=mode,
        team_blue=[a] * team_size,
        team_orange=[b] * team_size,
        map_name=map_name,
    )


def _solo_queue_pick(
        db: Database,
        bots: list[Bot],
        mode: MatchMode,
        team_size: int,
        map_name: str,
        sigma_priority_chance: float,
        anchored_bot_ids: set[int],
) -> MatchSetup:
    """Solo-queue mode: duplicates allowed, but the two teams cannot be
    identical (same bots in the same quantities).

    Uses roll-once accept/reject sampling — a random threshold is chosen
    once, then matchups are generated until one has p*(1-p)/0.25 ≥ threshold.
    Every 1000 failed attempts the threshold is squared to ensure convergence.

    A separate *teammate* threshold is also rolled: within each team, every
    pair of distinct bots must have pairwise p*(1-p)/0.25 ≥ teammate_threshold.
    This prevents extreme skill gaps within a team.

    With *sigma_priority_chance* probability, an additional criterion is
    applied: the matchup must also include the highest-sigma bot.
    """
    bot_os: dict[int, PlackettLuceRating] = {}
    bot_sigmas: dict[int, float] = {}
    bot_games: dict[int, int] = {}
    for bot in bots:
        assert bot.id is not None
        r = db.get_rating(bot.id, mode.value)
        bot_os[bot.id] = make_rating(r.mu, r.sigma)
        bot_sigmas[bot.id] = r.sigma
        bot_games[bot.id] = r.matches_played

    # Break sigma ties by fewest games played
    unanchored = [b for b in bots if b.id not in anchored_bot_ids]
    priority_bot = max(unanchored, key=lambda b: (bot_sigmas[b.id], -bot_games[b.id])) if unanchored else None
    use_sigma_priority = bool(unanchored) and random.random() < sigma_priority_chance
    if use_sigma_priority and priority_bot:
        logger.debug("Sigma priority (solo): looking for %s (sigma=%.2f)",
                     priority_bot.name, bot_sigmas[priority_bot.id])

    # Precompute pairwise evenness between all bots for teammate checks.
    # pairwise_weight[(a,b)] = p*(1-p)/0.25  where p = predict_win([a],[b]).
    bot_id_list = list(bot_os.keys())
    pairwise_weight: dict[tuple[int, int], float] = {}
    for i, id_a in enumerate(bot_id_list):
        for id_b in bot_id_list[i + 1:]:
            probs = _os_model.predict_win([[bot_os[id_a]], [bot_os[id_b]]])
            p = probs[0]
            w = p * (1 - p) / _MAX_WEIGHT
            pairwise_weight[(id_a, id_b)] = w
            pairwise_weight[(id_b, id_a)] = w

    def _teammates_even(team: list[Bot], thresh: float) -> bool:
        """Check that every pair of distinct bots on the team is reasonably close."""
        for i in range(len(team)):
            for j in range(i + 1, len(team)):
                a_id, b_id = team[i].id, team[j].id
                if a_id == b_id:
                    continue  # same bot duplicated — skip
                if pairwise_weight.get((a_id, b_id), 0) < thresh:
                    return False
        return True

    threshold = random.random()
    teammate_threshold = random.random()
    for attempt in range(_MAX_RETRIES):
        if attempt > 0 and attempt % 1000 == 0:
            threshold *= threshold
            teammate_threshold *= teammate_threshold

        blue = random.choices(bots, k=team_size)
        orange = random.choices(bots, k=team_size)

        # Reject if teams are identical (same bots in same quantities)
        if sorted(b.id for b in blue) == sorted(b.id for b in orange):
            continue

        # Check teammate evenness — no huge skill gaps within a team
        if not _teammates_even(blue, teammate_threshold):
            continue
        if not _teammates_even(orange, teammate_threshold):
            continue

        # Additionally require the highest-sigma bot when active
        if use_sigma_priority and priority_bot:
            all_ids = {b.id for b in blue} | {b.id for b in orange}
            if priority_bot.id not in all_ids:
                continue

        # Accept/reject based on match evenness
        blue_ratings = [bot_os[b.id] for b in blue]
        orange_ratings = [bot_os[b.id] for b in orange]
        probs = _os_model.predict_win([blue_ratings, orange_ratings])
        p = probs[0]
        weight = p * (1 - p)
        if weight / _MAX_WEIGHT >= threshold:
            return MatchSetup(
                mode=mode,
                team_blue=blue,
                team_orange=orange,
                map_name=map_name,
            )

    # Fallback: accept the last generated matchup
    return MatchSetup(
        mode=mode,
        team_blue=blue,
        team_orange=orange,
        map_name=map_name,
    )


def pick_mode(
        rotation: list[MatchMode],
        counter: int,
) -> MatchMode:
    """Randomly select a mode from the rotation."""
    return random.choice(rotation)
