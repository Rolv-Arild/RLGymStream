"""Matchmaking – select bots for a given mode.

A random threshold is rolled once per match, then matchups are generated
until one has  p_win × (1 − p_win) / 0.25  ≥  threshold.  This favours
balanced matchups while allowing occasional mismatches.  Every 1000 failed
attempts the threshold is squared to guarantee convergence.
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

    if mode.is_solo_queue:
        return _solo_queue_pick(db, bots, mode, team_size, chosen_map, sigma_priority_chance)
    else:
        return _standard_pick(db, bots, mode, team_size, chosen_map, sigma_priority_chance)


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
) -> MatchSetup | None:
    """Standard mode: each team is one bot (duplicated to fill team_size).

    Uses roll-once accept/reject sampling — a random threshold is chosen
    once, then matchups are generated until one has p*(1-p)/0.25 ≥ threshold.
    Every 1000 failed attempts the threshold is squared to ensure convergence.

    With *sigma_priority_chance* probability, an additional criterion is
    applied: the matchup must also include the highest-sigma bot,
    helping under-played bots get calibrated faster.
    """
    if len(bots) < 2:
        return None

    # Pre-compute ratings for all bots
    bot_ratings: dict[int, PlackettLuceRating] = {}
    bot_sigmas: dict[int, float] = {}
    for bot in bots:
        assert bot.id is not None
        r = db.get_rating(bot.id, mode.value)
        bot_ratings[bot.id] = make_rating(r.mu, r.sigma)
        bot_sigmas[bot.id] = r.sigma

    # Identify the highest-sigma bot for priority matches
    priority_bot = max(bots, key=lambda b: bot_sigmas[b.id])
    use_sigma_priority = random.random() < sigma_priority_chance
    if use_sigma_priority:
        logger.debug("Sigma priority active: looking for %s (sigma=%.2f)",
                     priority_bot.name, bot_sigmas[priority_bot.id])

    threshold = random.random()
    for attempt in range(_MAX_RETRIES):
        if attempt > 0 and attempt % 1000 == 0:
            threshold *= threshold

        a, b = random.sample(bots, 2)
        os_a = bot_ratings[a.id]
        os_b = bot_ratings[b.id]

        # Accept/reject based on match evenness
        probs = _os_model.predict_win([[os_a], [os_b]])
        p = probs[0]
        weight = p * (1 - p)
        if weight / _MAX_WEIGHT < threshold:
            continue

        # Additionally require the highest-sigma bot when active
        if use_sigma_priority:
            if a.id != priority_bot.id and b.id != priority_bot.id:
                continue

        # Randomly assign blue vs orange
        if random.random() < 0.5:
            a, b = b, a
        return MatchSetup(
            mode=mode,
            team_blue=[a] * team_size,
            team_orange=[b] * team_size,
            map_name=map_name,
        )

    # Fallback: accept any matchup
    a, b = random.sample(bots, 2)
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
) -> MatchSetup:
    """Solo-queue mode: duplicates allowed, but the two teams cannot be
    identical (same bots in the same quantities).

    Uses roll-once accept/reject sampling — a random threshold is chosen
    once, then matchups are generated until one has p*(1-p)/0.25 ≥ threshold.
    Every 1000 failed attempts the threshold is squared to ensure convergence.

    With *sigma_priority_chance* probability, an additional criterion is
    applied: the matchup must also include the highest-sigma bot.
    """
    bot_os: dict[int, PlackettLuceRating] = {}
    bot_sigmas: dict[int, float] = {}
    for bot in bots:
        assert bot.id is not None
        r = db.get_rating(bot.id, mode.value)
        bot_os[bot.id] = make_rating(r.mu, r.sigma)
        bot_sigmas[bot.id] = r.sigma

    priority_bot = max(bots, key=lambda b: bot_sigmas[b.id])
    use_sigma_priority = random.random() < sigma_priority_chance
    if use_sigma_priority:
        logger.debug("Sigma priority (solo): looking for %s (sigma=%.2f)",
                     priority_bot.name, bot_sigmas[priority_bot.id])

    threshold = random.random()
    for attempt in range(_MAX_RETRIES):
        if attempt > 0 and attempt % 1000 == 0:
            threshold *= threshold

        blue = random.choices(bots, k=team_size)
        orange = random.choices(bots, k=team_size)

        # Reject if teams are identical (same bots in same quantities)
        if sorted(b.id for b in blue) == sorted(b.id for b in orange):
            continue

        # Additionally require the highest-sigma bot when active
        if use_sigma_priority:
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
