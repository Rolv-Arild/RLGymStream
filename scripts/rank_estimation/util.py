import heapq
import logging
import shelve
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta

import ballchasing as bc
from ballchasing.util import from_rfc3339
from requests import HTTPError

# Timers
KICKOFF_START_TO_TOUCH = 2  # Approximately
SPAWN_TO_KICKOFF_START = 4
GOAL_TO_SPAWN = 3

# Keys
TEAMS = ("blue", "orange")
ID_KEYS = ("id", "rocket_league_id", "match_guid")

F2P_START = datetime(2020, 9, 22)


def get_scoreline(replay):
    """Returns the scoreline of the replay as a tuple (blue_goals, orange_goals)."""
    goals = [0, 0]
    for i, team in enumerate(TEAMS):
        team_data = replay.get(team, {})
        if "goals" in team_data:
            # Shallow replays
            goals[i] += team_data["goals"]
        elif "stats" in team_data:
            # Deep replays
            goals[i] += team_data["stats"].get("core", {}).get("goals", 0)
    return tuple(goals)


def is_standard_replay(replay, allow_bots=False, check_stats=False):
    playlist_id = replay.get("playlist_id")
    # if playlist_id in bc.Playlist.RANKED:
    #     return True
    if playlist_id in bc.Playlist.EXTRA_MODES + bc.Playlist.OTHER_MODES:
        return False, f"Playlist is not standard: {playlist_id}"

    # Map needs to be standard
    map_id = replay.get("map_code")
    if map_id is None or map_id not in bc.Map.STANDARD_MAPS + (bc.Map.NEOTOKYO_TOON_P,):
        if playlist_id.startswith("ranked"):
            logging.warning("Replay %s has non-standard map %s in ranked playlist %s", replay["id"], map_id,
                            playlist_id)
        return False, f"Map is not standard: {map_id}"
    if map_id in (bc.Map.PARK_SNOWY_P, bc.Map.UTOPIASTADIUM_SNOW_P, bc.Map.EUROSTADIUM_SNOWNIGHT_P):
        if playlist_id not in bc.Playlist.RANKED:
            return False, f"Replay uses snowy map"

    # Draw
    scoreline = get_scoreline(replay)
    if scoreline[0] == scoreline[1]:
        return False, f"Scoreline is a draw: {scoreline[0]}-{scoreline[1]}"

    # Same-size teams
    blue = replay.get("blue", {})
    orange = replay.get("orange", {})
    bp = blue.get("players", [])
    op = orange.get("players", [])
    if len(bp) != len(op):
        return False, f"Teams are not the same size: {len(bp)} vs {len(op)}"
    if len(bp) not in (1, 2, 3):
        return False, f"Replay is not 1v1, 2v2 or 3v3: {len(bp)}v{len(op)}"

    duration = replay.get("duration", 0)
    for i, player in enumerate(bp + op):
        # Check that they're there for the entire game
        name = player.get("name", "")
        if player["start_time"] > SPAWN_TO_KICKOFF_START:  # Kickoff countdown is 4s
            return False, f"Player {name} did not start at the beginning of the game"
        if player["end_time"] < duration - 10:  # A little bit of leeway for leaving right before game ends
            return False, f"Player {name} did not finish the game"

        if player.get("player_number", 0) != 0:
            return False, f"Player {name} has non-zero player number: {player['player_number']}"

        # Bots get an empty ID dictionary
        if not player.get("id") and not allow_bots:
            return False, f"Player {name} has no ID"

        # Check that the stats are reasonable
        if "stats" in player and check_stats:
            all_stats = {k: v for stat in player["stats"].values() for k, v in stat.items()}
            stats_bounds = {
                "bpm": (250, 600),
                "avg_amount": (30, 65),
                "avg_speed": (1300, 1850),
                "percent_ground": (35, 70),
                "percent_low_air": (25, 55),
                "percent_defensive_half": (40, 85),
                "percent_defensive_third": (20, 70),
                "percent_neutral_third": (20, 45),
                "percent_behind_ball": (55, 95)
            }
            for stat, (lo, hi) in stats_bounds.items():
                if stat not in all_stats:
                    return False, f"Player {name} has no stat: {stat}"
                value = all_stats[stat]
                if not (lo <= value <= hi):
                    return False, f"Player {name} has invalid stat {stat}: {value} (expected between {lo} and {hi})"
            percents = [
                ("percent_defensive_third", "percent_neutral_third", "percent_offensive_third"),
                ("percent_defensive_half", "percent_offensive_half"),
                ("percent_behind_ball", "percent_infront_ball"),
                ("percent_slow_speed", "percent_boost_speed", "percent_supersonic_speed"),
                ("percent_ground", "percent_low_air", "percent_high_air"),
                ("percent_boost_0_25", "percent_boost_25_50", "percent_boost_50_75", "percent_boost_75_100"),
            ]
            for keys in percents:
                total = sum(all_stats[key] for key in keys)
                if abs(total - 100) > 1:
                    return False, f"Player {name} has invalid percent stats: {keys} sum to {total:.2f}%"

    if "stats" in blue or "stats" in orange and check_stats:
        for team in (blue, orange):
            core_stats = deepcopy(team.get("stats", {}).get("core", {}))
            for key in ("shooting_percentage", "goals_against", "shots_against"):
                # These stats are not additive, so we remove them from the core stats
                core_stats.pop(key, None)
            # Sum of player stats should equal team stats
            for player in team.get("players", []):
                player_stats = player.get("stats", {}).get("core", {})
                for stat in core_stats.keys():
                    core_stats[stat] -= player_stats.get(stat, 0)
            for stat, value in core_stats.items():
                if value != 0:
                    return False, f"Player stats do not match team stats for {stat}. Difference is {value}"

    # Nonstandard timer check
    gameplay_duration = get_gameplay_duration(replay)
    if gameplay_duration < 90:  # Forfeits can only happen after 3:30 left (1:30 elapsed)
        return False, f"Gameplay duration is too short: {gameplay_duration}s"
    overtime_seconds = replay.get("overtime_seconds", 0)
    if overtime_seconds:
        regulation_time = gameplay_duration - overtime_seconds
        if not (270 < regulation_time <= 360):
            return False, f"Regulation time is not close to 5 minutes in replay with overtime: {regulation_time}+{overtime_seconds}"
    elif gameplay_duration > 360:
        # We only check above because there may be forfeits, and allow up to a minute for 0 second play attempts
        return False, f"Regulation time is above 5 minutes in replay without overtime: {gameplay_duration}s"

    return True, "No issues found"


def get_players(replay):
    players = []
    for team in TEAMS:
        team_data = replay.get(team, {})
        if "players" in team_data:
            players.extend(team_data["players"])
    return players


def get_pid(player):
    pid = player.get("id", {})
    return f"{pid.get('platform', '')}:{pid.get('id', '')}" if pid else None


def get_goals(player):
    stats = player.get("stats")
    if stats is None:
        return None
    return stats.get("core", {}).get("goals", 0)


def get_gameplay_duration(replay, count_kickoffs=False):
    duration = replay.get("duration", 0)
    goals = sum(get_scoreline(replay))

    if count_kickoffs:
        kickoff_time = 0  # Don't subtract kickoff time
    else:
        kickoff_time = KICKOFF_START_TO_TOUCH
    duration -= SPAWN_TO_KICKOFF_START + kickoff_time  # Initial kickoff
    duration -= goals * (GOAL_TO_SPAWN + SPAWN_TO_KICKOFF_START + kickoff_time)

    return max(0, duration)  # Ensure non-negative duration


def deduplicate_single(replay, seen_replays) -> str | None:
    # Check if any of the ID keys are already seen, and return the first one that is
    for key in ID_KEYS:
        val = replay.get(key)
        if val is None:
            continue
        if val in seen_replays:
            return key
        seen_replays.add(val)
    return None


def deduplicate(replays, check_dates=False):
    # NOTE: check_dates requires replays to be sorted in chronological order
    seen_replays = set()
    latest_appearances = {}
    for replay in replays:
        # First, check
        already_exists = deduplicate_single(replay, seen_replays)
        if already_exists:
            logging.info("Replay %s shares '%s' with a previous replay, skipping", replay["id"], already_exists)
            continue

        # If we want to check dates, track appearances of each player
        if check_dates:
            rid = replay["id"]
            date = from_rfc3339(replay["date"])
            start_time = date - timedelta(seconds=replay.get("duration", 0))
            overlaps = False
            for player in get_players(replay):
                pid = get_pid(player)
                if pid is None:
                    continue
                latest, latest_rid = latest_appearances.get(pid, (None, None))
                if latest is None or start_time > latest:
                    latest_appearances[pid] = (date, rid)
                else:
                    logging.info("Replay %s overlaps with previous replay %s", rid, latest_rid)
                    overlaps = True
                    break
            if overlaps:
                continue
        logging.debug("Found no duplicates for replay %s", replay["id"])
        yield replay


def ensure_sorted(replays, buffer_size=1_000):
    """
    Ensures that the replays are sorted by date.
    It maintains a buffer of the specified size and yields replays in sorted order.
    """
    # Initial replays
    buffer = []
    for i, replay in enumerate(replays):
        # Date first, index to prevent collisions, then the replay itself
        item = (from_rfc3339(replay["date"]), i, replay)  # To make it sortable
        if len(buffer) < buffer_size:
            buffer.append(item)
            if len(buffer) == buffer_size:
                heapq.heapify(buffer)  # Sort the buffer
        else:
            yield heapq.heappop(buffer)[2]  # Yield the smallest item
            heapq.heappush(buffer, item)  # Add the new item
    # Exhaust the buffer
    buffer = sorted(buffer)  # Sort the remaining items
    for item in buffer:
        yield item[2]


def mix_replay_iterators(*iterables, sort_dir=bc.SortDir.ASC):
    it = heapq.merge(
        *iterables,
        key=lambda r: from_rfc3339(r["date"]),
        reverse=(sort_dir == bc.SortDir.DESC)
    )
    yield from it


def get_deep_replays(bc_api, replays, shelf_path, workers=2, batch_size=200):
    with (ThreadPoolExecutor(max_workers=workers) as ex,
          shelve.open(shelf_path) as cache):
        futures = []
        for replay in replays:
            if isinstance(replay, str):
                rid = replay
            else:
                rid = replay["id"]
            if rid in cache:
                logging.debug(f"Using cached deep replay {rid}")
                yield cache[rid]
                continue
            f = ex.submit(bc_api.get_replay, rid)
            futures.append(f)
            while len(futures) >= batch_size:
                try:
                    res = futures.pop(0).result()
                    cache[res["id"]] = res
                    yield res
                except HTTPError:
                    continue
        while futures:
            try:
                res = futures.pop(0).result()
                cache[res["id"]] = res
                yield res
            except HTTPError:
                continue
