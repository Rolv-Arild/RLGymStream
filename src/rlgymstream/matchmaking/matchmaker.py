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
    chosen_map = map_name or random.choice(STANDARD_MAPS)

    if mode.is_solo_queue:
        return _solo_queue_pick(db, bots, mode, team_size, chosen_map)
    else:
        return _standard_pick(db, bots, mode, team_size, chosen_map)


def _standard_pick(
    db: Database,
    bots: list[Bot],
    mode: MatchMode,
    team_size: int,
    map_name: str,
) -> MatchSetup | None:
    """Standard mode: each team is one bot (duplicated to fill team_size).

    For 1v1 this is just bot A vs bot B.  For 2v2 it's AA vs BB,
    for 3v3 it's AAA vs BBB.  The matchup is weighted by
    p_win × (1 − p_win) so evenly-matched bots play more often.
    """
    if len(bots) < 2:
        return None

    # Build (bot, rating_obj) for every eligible bot
    bot_ratings: list[tuple[Bot, PlackettLuceRating]] = []
    for bot in bots:
        assert bot.id is not None
        r = db.get_rating(bot.id, mode.value)
        bot_ratings.append((bot, make_rating(r.mu, r.sigma)))

    # Enumerate all pairs of distinct bots and weight by p*(1-p)
    pairs: list[tuple[Bot, Bot, float]] = []
    for i, (bot_a, os_a) in enumerate(bot_ratings):
        for j, (bot_b, os_b) in enumerate(bot_ratings):
            if j <= i:
                continue
            blue_team_os = [os_a] * team_size
            orange_team_os = [os_b] * team_size
            probs = _os_model.predict_win([blue_team_os, orange_team_os])
            p = probs[0]
            weight = p * (1 - p)  # maximised at p=0.5
            pairs.append((bot_a, bot_b, weight))

    if not pairs:
        a, b = random.sample(bots, 2)
        return MatchSetup(
            mode=mode,
            team_blue=[a] * team_size,
            team_orange=[b] * team_size,
            map_name=map_name,
        )

    # Weighted random choice
    total = sum(w for _, _, w in pairs)
    if total <= 0:
        a, b, _ = random.choice(pairs)
    else:
        r = random.uniform(0, total)
        cumulative = 0.0
        a, b = pairs[0][0], pairs[0][1]
        for bot_a, bot_b, w in pairs:
            cumulative += w
            if cumulative >= r:
                a, b = bot_a, bot_b
                break

    # Randomly assign which bot is blue vs orange
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
) -> MatchSetup:
    """Solo-queue mode: duplicates allowed, weighted by p × (1 − p).

    Generate a batch of random team pairs and pick one with probability
    proportional to how even the matchup is.
    """
    bot_os: dict[int, PlackettLuceRating] = {}
    for bot in bots:
        assert bot.id is not None
        r = db.get_rating(bot.id, mode.value)
        bot_os[bot.id] = make_rating(r.mu, r.sigma)

    # Generate candidate matchups (sample many, weight, pick one)
    candidates: list[tuple[list[Bot], list[Bot], float]] = []
    n_candidates = min(200, max(50, len(bots) ** 2))
    for _ in range(n_candidates):
        blue = random.choices(bots, k=team_size)
        orange = random.choices(bots, k=team_size)
        blue_os = [bot_os[b.id] for b in blue]
        orange_os = [bot_os[b.id] for b in orange]
        probs = _os_model.predict_win([blue_os, orange_os])
        p = probs[0]
        weight = p * (1 - p)
        candidates.append((blue, orange, weight))

    # Weighted random choice
    total = sum(w for _, _, w in candidates)
    if total <= 0:
        blue = random.choices(bots, k=team_size)
        orange = random.choices(bots, k=team_size)
        return MatchSetup(mode=mode, team_blue=blue, team_orange=orange, map_name=map_name)

    r = random.uniform(0, total)
    cumulative = 0.0
    for blue, orange, w in candidates:
        cumulative += w
        if cumulative >= r:
            return MatchSetup(mode=mode, team_blue=blue, team_orange=orange, map_name=map_name)

    # Fallback (shouldn't reach here)
    blue, orange, _ = candidates[-1]
    return MatchSetup(mode=mode, team_blue=blue, team_orange=orange, map_name=map_name)



def pick_mode(
    rotation: list[MatchMode],
    counter: int,
) -> MatchMode:
    """Randomly select a mode from the rotation."""
    return random.choice(rotation)

