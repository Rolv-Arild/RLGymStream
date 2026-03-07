"""Matchmaking – select bots for a given mode.

Matchup probability is proportional to  p_win × (1 − p_win),  i.e. the
probability that a best-of-2 series ends 1-1.  This maximises at 50/50
matchups and gracefully falls off for mismatches.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from openskill.models.weng_lin.plackett_luce import PlackettLuceRating

from rlgymstream.config import MatchMode
from rlgymstream.db.database import Database
from rlgymstream.db.models import Bot
from rlgymstream.matchmaking.ratings import make_rating, _model as _os_model


@dataclass
class MatchSetup:
    mode: MatchMode
    team_blue: list[Bot]
    team_orange: list[Bot]
    map_name: str = "DFHStadium"


# Standard competitive maps (subset of GAME_MAP_TO_UPK)
STANDARD_MAPS = [
    "DFHStadium",
    "Mannfield",
    "ChampionsField",
    "UrbanCentral",
    "BeckwithPark",
    "UtopiaColiseum",
    "Wasteland",
    "NeoTokyo",
    "AquaDome",
    "StarbaseArc",
    "Farmstead",
    "SaltyShores",
    "ForbiddenTemple",
    "DeadeyeCanyon",
    "SovereignHeights",
    "RivalsArena",
    "NeonFields",
]


def pick_match(
    db: Database,
    mode: MatchMode,
    map_name: str | None = None,
    last_map: str | None = None,
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
        return _solo_queue_pick(db, bots, mode, team_size, chosen_map)
    else:
        return _standard_pick(db, bots, mode, team_size, chosen_map)


# Maximum p*(1-p) is 0.25 (when p=0.5).  Used to normalise accept probability.
_MAX_WEIGHT = 0.25
_MAX_RETRIES = 1000


def _standard_pick(
    db: Database,
    bots: list[Bot],
    mode: MatchMode,
    team_size: int,
    map_name: str,
) -> MatchSetup | None:
    """Standard mode: each team is one bot (duplicated to fill team_size).

    Uses accept/reject sampling — generate a random pair, accept with
    probability p*(1-p)/0.25 so evenly-matched bots play more often.
    """
    if len(bots) < 2:
        return None

    # Pre-compute ratings for all bots
    bot_ratings: dict[int, PlackettLuceRating] = {}
    for bot in bots:
        assert bot.id is not None
        r = db.get_rating(bot.id, mode.value)
        bot_ratings[bot.id] = make_rating(r.mu, r.sigma)

    for _ in range(_MAX_RETRIES):
        a, b = random.sample(bots, 2)
        os_a = bot_ratings[a.id]
        os_b = bot_ratings[b.id]
        probs = _os_model.predict_win([[os_a], [os_b]])
        p = probs[0]
        weight = p * (1 - p)
        if random.random() < weight / _MAX_WEIGHT:
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
) -> MatchSetup:
    """Solo-queue mode: duplicates allowed.

    Uses accept/reject sampling — generate random teams, accept with
    probability p*(1-p)/0.25.
    """
    bot_os: dict[int, PlackettLuceRating] = {}
    for bot in bots:
        assert bot.id is not None
        r = db.get_rating(bot.id, mode.value)
        bot_os[bot.id] = make_rating(r.mu, r.sigma)

    for _ in range(_MAX_RETRIES):
        blue = random.choices(bots, k=team_size)
        orange = random.choices(bots, k=team_size)
        blue_ratings = [bot_os[b.id] for b in blue]
        orange_ratings = [bot_os[b.id] for b in orange]
        probs = _os_model.predict_win([blue_ratings, orange_ratings])
        p = probs[0]
        weight = p * (1 - p)
        if random.random() < weight / _MAX_WEIGHT:
            return MatchSetup(
                mode=mode,
                team_blue=blue,
                team_orange=orange,
                map_name=map_name,
            )

    # Fallback: accept any matchup
    blue = random.choices(bots, k=team_size)
    orange = random.choices(bots, k=team_size)
    return MatchSetup(mode=mode, team_blue=blue, team_orange=orange, map_name=map_name)



def pick_mode(
    rotation: list[MatchMode],
    counter: int,
) -> MatchMode:
    """Randomly select a mode from the rotation."""
    return random.choice(rotation)

