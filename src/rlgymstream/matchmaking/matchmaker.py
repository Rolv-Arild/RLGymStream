"""Matchmaking – select bots for a given mode.

Standard modes use Temperature-Controlled Active Learning: for every
possible pair, a Beta distribution is formed from the OpenSkill prior
(weighted by ``n_prior``) plus the empirical head-to-head record.  The
Beta variance is raised to ``1/temperature`` to give a selection weight.
High-variance (uncertain) matchups are naturally favoured.

Solo-queue modes draw a few thousand random candidate matchups (with a
random threshold for teammate evenness) and pick one weighted by how much
its result would tell us about the ratings, raised to ``1/temperature``.

Every match in both kinds of mode is built around one *featured* bot:
usually drawn with weight 1 + (matches since it last played in the mode),
sometimes (sigma priority) the least-calibrated bot.  This keeps bots whose
matches are hard to make even (top and bottom of the leaderboard) playing.
"""

from __future__ import annotations

import logging
import math
import random
import re
from collections import Counter
from dataclasses import dataclass
from statistics import NormalDist

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
        solo_temperature: float = 0.25,
        solo_candidates: int = 3000,
) -> MatchSetup | None:
    """Select bots for a match in the given mode.

    Every match is built around one bot (see _pick_featured_bot); the rest
    of the match is chosen by the mode's weighting.  *n_prior* and
    *temperature* tune standard modes; *solo_temperature* and
    *solo_candidates* tune solo queue.
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

    featured = _pick_featured_bot(db, bots, mode, sigma_priority_chance, anchored_bot_ids or set())

    if mode.is_solo_queue:
        return _solo_queue_pick(db, bots, mode, team_size, chosen_map, featured,
                                temperature=solo_temperature, n_candidates=solo_candidates)
    else:
        return _standard_pick(db, bots, mode, team_size, chosen_map, featured,
                              n_prior=n_prior, temperature=temperature)


def _pick_featured_bot(
        db: Database,
        bots: list[Bot],
        mode: MatchMode,
        sigma_priority_chance: float,
        anchored_bot_ids: set[int],
) -> Bot:
    """Choose the bot the match must include.

    With *sigma_priority_chance* probability it is the unanchored bot with
    the highest sigma (ties: fewest games in this mode), to calibrate new
    bots and slowly even out game counts.  Otherwise it is drawn with weight
    1 + (matches in this mode since it last played one), so no bot sits out
    for long, even when close matches for it are hard to find (e.g. the top
    and bottom of the leaderboard).
    """
    unanchored = [b for b in bots if b.id not in anchored_bot_ids]
    if unanchored and random.random() < sigma_priority_chance:
        ratings = {b.id: db.get_rating(b.id, mode.value) for b in unanchored}
        bot = max(unanchored, key=lambda b: (ratings[b.id].sigma, -ratings[b.id].matches_played))
        logger.debug("Sigma priority: building the match around %s (sigma=%.2f)",
                     bot.name, ratings[bot.id].sigma)
        return bot

    idle = db.get_matches_since_last_played(mode.value, [b.id for b in bots if b.id is not None])
    bot = random.choices(bots, weights=[idle[b.id] + 1 for b in bots], k=1)[0]
    logger.debug("Featured bot: %s (%d matches since it last played)", bot.name, idle[bot.id])
    return bot


# Maximum p*(1-p) is 0.25 (when p=0.5).  Used to normalise teammate evenness.
_MAX_WEIGHT = 0.25
# Solo queue: the teammate threshold is squared every _RELAX_EVERY rejected
# draws (e.g. 0.99 → ~0.00003 after 10 squarings).  If no candidate is found
# within _MAX_DRAWS draws, a simple valid fallback matchup is used.
_RELAX_EVERY = 1000
_MAX_DRAWS = 1_000_000
# Per-player performance variance of the rating model, and the constant of
# the information approximation (Houlsby et al. 2011): pi * ln(2) / 2.
_PERF_VAR = _os_model.beta ** 2
_INFO_C2 = math.pi * math.log(2) / 2
_NORMAL_CDF = NormalDist().cdf


def _binary_entropy(p: float) -> float:
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


def information_score(
        blue_ids: list[int],
        orange_ids: list[int],
        mu: dict[int, float],
        var: dict[int, float],
) -> float:
    """Expected information (bits) the match result gives about the ratings.

    The teams' performance difference is modelled as Gaussian.  Each bot
    enters with its net count k = (copies on blue) - (copies on orange):
    its skill adds k*mu to the mean and |k|*sigma^2 to the rating
    uncertainty (each slot counted once).  A bot on both teams cancels out,
    so the result says nothing about it.  Game-to-game luck adds beta^2 per
    player.

    The score is the uncertainty of the result minus the uncertainty that
    would remain if the ratings were known exactly, so it is highest for
    close matches between bots with uncertain ratings.
    """
    net = Counter(blue_ids)
    net.subtract(orange_ids)
    noise = (len(blue_ids) + len(orange_ids)) * _PERF_VAR
    m = sum(k * mu[b] for b, k in net.items()) / math.sqrt(noise)
    v = sum(abs(k) * var[b] for b, k in net.items()) / noise
    p_blue = _NORMAL_CDF(m / math.sqrt(1 + v))
    remaining = math.sqrt(_INFO_C2 / (v + _INFO_C2)) * math.exp(-m * m / (2 * (v + _INFO_C2)))
    # The approximation can dip just below zero for very lopsided matches.
    return max(_binary_entropy(p_blue) - remaining, 1e-12)


def _standard_pick(
        db: Database,
        bots: list[Bot],
        mode: MatchMode,
        team_size: int,
        map_name: str,
        featured: Bot,
        n_prior: float = 1.0,
        temperature: float = 1.0,
) -> MatchSetup | None:
    """Standard mode: each team is one bot (duplicated to fill team_size).

    The *featured* bot plays; its opponent is chosen by Temperature-Controlled
    Active Learning:
      1. For every pair (featured, opponent), compute the expected win
         probability from OpenSkill and use it as a Beta prior (weighted by
         *n_prior*).
      2. Fold in empirical head-to-head wins/losses.
      3. Compute the resulting Beta variance — high variance means we're
         uncertain about the true win probability of this matchup.
      4. Raise variance to ``1/temperature`` to control exploration.
      5. Normalise into a probability distribution and sample one pair.
    """
    if len(bots) < 2:
        return None

    # Pre-compute ratings
    bot_ratings: dict[int, PlackettLuceRating] = {}
    for bot in bots:
        assert bot.id is not None
        r = db.get_rating(bot.id, mode.value)
        bot_ratings[bot.id] = make_rating(r.mu, r.sigma)

    # Bulk h2h lookup — one pass through match history
    h2h = db.get_pairwise_h2h(mode.value)

    # Build weighted list of all unique pairs
    pairs: list[tuple[Bot, Bot]] = []
    weights: list[float] = []
    inv_temp = 1.0 / max(temperature, 1e-9)

    for i in range(len(bots)):
        for j in range(i + 1, len(bots)):
            a, b = bots[i], bots[j]

            # Only pairs with the featured bot
            if featured.id not in (a.id, b.id):
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
        # Edge case: the featured bot isn't among *bots*
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
        featured: Bot,
        temperature: float = 0.25,
        n_candidates: int = 3000,
) -> MatchSetup:
    """Solo-queue mode: duplicates allowed, but the two teams cannot be
    identical (same bots in the same quantities).

    1. Draw *n_candidates* random matchups: the *featured* bot goes in a
       random slot and every other slot is an independent random bot.  A
       draw is rejected if the teams are identical or if two different
       teammates are too far apart in skill (see below).
    2. Score each candidate with information_score() and pick one with
       probability proportional to score ** (1 / temperature).

    The *teammate* threshold is rolled once per match: within each team,
    every pair of distinct bots must have pairwise p*(1-p)/0.25 ≥ it.  It is
    squared every _RELAX_EVERY rejected draws so candidates always fill up.

    Bots on both teams cancel out in the score, so mirror-like matchups are
    rare without being ruled out.
    """
    bot_os: dict[int, PlackettLuceRating] = {}
    bot_mus: dict[int, float] = {}
    bot_vars: dict[int, float] = {}
    for bot in bots:
        assert bot.id is not None
        r = db.get_rating(bot.id, mode.value)
        bot_os[bot.id] = make_rating(r.mu, r.sigma)
        bot_mus[bot.id] = r.mu
        bot_vars[bot.id] = r.sigma ** 2

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

    def _acceptable(blue: list[Bot], orange: list[Bot], thresh: float) -> bool:
        # Reject if teams are identical (same bots in same quantities)
        if sorted(b.id for b in blue) == sorted(b.id for b in orange):
            return False
        # No huge skill gaps within a team
        return _teammates_even(blue, thresh) and _teammates_even(orange, thresh)

    teammate_threshold = random.random()
    candidates: list[tuple[list[Bot], list[Bot]]] = []
    rejected = 0
    for _ in range(_MAX_DRAWS):
        if len(candidates) >= n_candidates:
            break
        blue = random.choices(bots, k=team_size)
        orange = random.choices(bots, k=team_size)
        slot = random.randrange(2 * team_size)
        (blue if slot < team_size else orange)[slot % team_size] = featured
        if _acceptable(blue, orange, teammate_threshold):
            candidates.append((blue, orange))
        else:
            rejected += 1
            if rejected % _RELAX_EVERY == 0:
                teammate_threshold *= teammate_threshold

    if candidates:
        scores = [
            information_score([b.id for b in blue], [b.id for b in orange], bot_mus, bot_vars)
            for blue, orange in candidates
        ]
        # Normalise first so a low temperature can't underflow every weight to 0.
        top = max(scores)
        inv_temp = 1.0 / max(temperature, 1e-9)
        weights = [(s / top) ** inv_temp for s in scores]
        blue, orange = random.choices(candidates, weights=weights, k=1)[0]
        return MatchSetup(
            mode=mode,
            team_blue=blue,
            team_orange=orange,
            map_name=map_name,
        )

    # Fallback (should be unreachable): the featured bot against another bot,
    # one per team, so the teams can never be identical.
    logger.warning("Solo queue matchmaking found no candidates in %d draws; using fallback", _MAX_DRAWS)
    a, b = featured, random.choice([x for x in bots if x.id != featured.id])
    return MatchSetup(
        mode=mode,
        team_blue=[a] * team_size,
        team_orange=[b] * team_size,
        map_name=map_name,
    )


def pick_mode(rotation: list[MatchMode]) -> MatchMode:
    """Randomly select a mode from the rotation."""
    return random.choice(rotation)
