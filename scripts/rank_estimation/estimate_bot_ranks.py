import json
import math
import os
import re
import statistics
import time
from datetime import datetime, timedelta

import ballchasing as bc
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from ballchasing.util import to_rfc3339
from openskill.models import PlackettLuce
from tqdm import tqdm

from util import is_standard_replay, deduplicate, ensure_sorted

# 1. Configuration
BC_API_KEY = os.environ.get("BC_API_KEY")
bc_api = bc.Api(BC_API_KEY)

TARGET_BOTS = ["Nexto", "Necto"]
# TARGET_BOTS += ["Nexto (Toxic!)", "Nexto-EZ", "London", "Ripple v1.1", "Ripple v1.0",
#                "Opti-GP", "Opti-V2.2"]
# TARGET_BOTS += ["King", "Noob Black", "Phoenix CS", "BroccoliBot",
#                 "Kamael", "ReliefBot", "Self-driving car", "Bumblebee", "Botimus Prime", "RedBot",
#                 "Wildfire", "Codename Cryo", "Diablo", "Stick", "St. Peter",
#                 "Lanfear", "PenguinBot", "Beast from the East", "VirxEB", "BribbleBot", "DisasterBot",
#                 "AdversityBot", "Invisibot",
#                 "Psyonix Bot"]

ALLOW_MIXED_TARGET_BOTS = False  # If False, skip replays containing more than one distinct target bot
ALLOW_MIXED_TEAMS = True  # If False, only accept replays where each team is entirely bots or entirely humans

PSYONIX_BOT_LEVELS = ["Beginner", "Rookie", "Pro", "All-Star"]
PSYONIX_BOT_NAMES = ["Armstrong", "Bandit", "Beast", "Boomer", "Buzz", "C-Block", "Casper", "Caveman", "Centice",
                     "Chipper", "Cougar", "Dude", "Foamer", "Fury", "Gerwin", "Goose", "Heater", "Hollywood", "Hound",
                     "Iceman", "Imp", "Jester", "Junker", "Khan", "Marley", "Maverick", "Merlin", "Middy", "Mountain",
                     "Myrtle", "Outlaw", "Poncho", "Rainmaker", "Raja", "Rex", "Roundhouse", "Sabretooth", "Saltie",
                     "Samara", "Scout", "Shepard", "Slider", "Squall", "Sticks", "Stinger", "Storm", "Sultan",
                     "Sundown", "Swabbie", "Tex", "Tusk", "Viper", "Wolfman", "Yuri", ]

# Name can for example be: "Pro Armstrong" or "Rookie Bandit"
_psyonix_bot_pattern = re.compile("^(" + "|".join(PSYONIX_BOT_LEVELS) + r")? ?(" + "|".join(PSYONIX_BOT_NAMES) + r")$",
                                  flags=re.IGNORECASE)

CACHE_FILE = "human_rank_cache.json"

PLAYLIST_MAP = {
    1: 10,
    2: 11,
    3: 13
}

BC_PLAYLIST_MAP = {
    1: "ranked-duels",
    2: "ranked-doubles",
    3: "ranked-standard"
}

TIER_NAMES_SHORT = [
    "Unranked", "B1", "B2", "B3",
    "S1", "S2", "S3", "G1", "G2", "G3",
    "P1", "P2", "P3", "D1", "D2", "D3",
    "C1", "C2", "C3",
    "GC1", "GC2", "GC3", "SSL"
]

TIER_NAMES = [
    "Unranked", "Bronze I", "Bronze II", "Bronze III",
    "Silver I", "Silver II", "Silver III", "Gold I", "Gold II", "Gold III",
    "Platinum I", "Platinum II", "Platinum III", "Diamond I", "Diamond II", "Diamond III",
    "Champion I", "Champion II", "Champion III",
    "Grand Champion I", "Grand Champion II", "Grand Champion III", "Supersonic Legend"
]


def read_rfc3339_date(date_str):
    """Parses Ballchasing RFC3339 dates into naive datetime objects."""
    date_str = date_str.replace('Z', '+00:00')
    date = datetime.fromisoformat(date_str)
    return date.replace(tzinfo=None)


def load_cache(filepath):
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                pass
    return {}


def save_cache(cache, filepath):
    with open(filepath, "w") as f:
        json.dump(cache, f, indent=2)


def build_tier_ranges(playlist_id):
    """
    Constructs the absolute Min and Max MMR boundaries for every tier
    based on the official Rocket League MMR algorithm rules.
    """
    tiers = {}
    overlap = 15

    # Corrected Base Max MMR for Bronze I (Tier 1)
    current_max = 155 if playlist_id == 10 else 195

    for t in range(1, 23):
        # 1v1 ranges
        if playlist_id == 10:
            rank_range = 155 if t == 1 else 60
        # 2v2 and 3v3 ranges
        else:
            if t == 1:
                rank_range = 175
            elif 2 <= t <= 12:
                rank_range = 60
            elif 13 <= t <= 15:
                rank_range = 80
            elif 16 <= t <= 18:
                rank_range = 120
            elif 19 <= t <= 21:
                rank_range = 140
            else:
                rank_range = 160

        if t == 1:
            min_mmr = current_max - rank_range
            max_mmr = current_max
        else:
            min_mmr = current_max - overlap
            max_mmr = min_mmr + rank_range + overlap
            current_max = max_mmr

        tiers[t] = (min_mmr, max_mmr)

    return tiers


def get_mmr_from_rank(playlist_id, tier, division):
    """Maps a tier and division to an estimated MMR using mathematical division slicing."""
    if tier < 1:
        return None

    tiers = build_tier_ranges(playlist_id)
    if tier not in tiers:
        return None

    min_mmr, max_mmr = tiers[tier]

    # Slice the tier range into 4 equal divisions
    div_size = (max_mmr - min_mmr) / 4.0

    # Calculate the exact midpoint of the requested division (1-indexed)
    estimated_mmr = min_mmr + (division - 0.5) * div_size
    return estimated_mmr


def get_rank_name_from_mmr(playlist_id, mmr, short=False):
    """Finds the human-readable rank name for a given MMR."""
    if mmr is None:
        return "Unknown Rank"

    tiers = build_tier_ranges(playlist_id)

    # Iterate top-down to find the highest rank the MMR qualifies for
    for t in range(22, 0, -1):
        min_mmr, _ = tiers[t]
        if mmr >= min_mmr:
            return TIER_NAMES_SHORT[t] if short else TIER_NAMES[t]

    # Fallback to Bronze 1 if below minimum
    return TIER_NAMES_SHORT[1] if short else TIER_NAMES[1]


def get_human_rank_history(platform, identifier, team_size, start_dt, end_dt):
    """Fetches ranked matches within a specific time window."""
    bc_playlist = BC_PLAYLIST_MAP.get(team_size)
    if not bc_playlist:
        return []

    ranks = []
    try:
        replays = list(bc_api.get_replays(
            player_id=f"{platform}:{identifier}",
            playlist=bc_playlist,
            replay_after=to_rfc3339(start_dt),
            replay_before=to_rfc3339(end_dt),
            count=10_000
        ))

        for replay in replays:
            r_date = replay.get("date")
            for team in ("blue", "orange"):
                for p in replay[team].get("players", []):
                    p_id = p.get("id", {})
                    if p_id.get("platform") == platform and str(p_id.get("id")) == str(identifier):
                        rank_info = p.get("rank")
                        if rank_info and "tier" in rank_info and "division" in rank_info:
                            ranks.append({
                                "date": r_date,
                                "tier": rank_info["tier"],
                                "division": rank_info["division"]
                            })
    except Exception:
        pass

    return ranks


def main():
    model = PlackettLuce()

    bot_ratings = {bot: {size: model.rating() for size in PLAYLIST_MAP} for bot in TARGET_BOTS}
    bot_matches = {bot: {size: 0 for size in PLAYLIST_MAP} for bot in TARGET_BOTS}
    # Track (datetime, mmr, sigma) for each bot per mode for plotting
    mmr_history = {bot: {size: [] for size in PLAYLIST_MAP} for bot in TARGET_BOTS}

    human_rank_cache = load_cache(CACHE_FILE)

    print("\nFetching replays...")
    raw_replays = bc_api.get_replays(playlist=[bc.Playlist.LOCAL_LOBBY, bc.Playlist.OFFLINE], count=100_000,
                                     sort_by=bc.ReplaySortBy.REPLAY_DATE, sort_dir=bc.SortDir.ASC,
                                     replay_after="2022-07-01T00:00:00Z")
    sorted_replays = ensure_sorted(raw_replays)
    unique_replays = deduplicate(sorted_replays, check_dates=True)

    try:
        for replay in tqdm(unique_replays, desc="Evaluating Replays", unit="replay"):
            is_valid, reason = is_standard_replay(replay, allow_bots=True, check_stats=False)
            if not is_valid:
                continue

            team_size = None
            valid_teams = True

            for team in ("blue", "orange"):
                players = replay[team].get("players", [])
                if team_size is None:
                    team_size = len(players)
                elif team_size != len(players) or team_size not in PLAYLIST_MAP:
                    valid_teams = False
                    break

            if not valid_teams:
                continue

            roster = {"blue": [], "orange": []}
            has_target_bot = False
            has_other_bot = False

            for team_str in ("blue", "orange"):
                for player in replay[team_str].get("players", []):
                    platform_data = player.get("id", {})
                    is_bot = platform_data.get("platform") is None
                    name = player.get("name", "")

                    if is_bot:
                        m = re.match(r"^(.*)( \(\d+\))$", name, flags=re.IGNORECASE)
                        clean_name = m.group(1) if m else name

                        if clean_name in TARGET_BOTS:
                            has_target_bot = True
                            roster[team_str].append({"type": "bot", "id": clean_name})
                        else:
                            psy = _psyonix_bot_pattern.match(clean_name)
                            if psy and "Psyonix Bot" in TARGET_BOTS:
                                has_target_bot = True
                                # Use the skill level
                                roster[team_str].append({"type": "bot", "id": "Psyonix Bot"})
                            else:
                                has_other_bot = True
                    else:
                        platform = platform_data.get("platform")
                        identifier = platform_data.get("id")
                        if platform and identifier:
                            roster[team_str].append({
                                "type": "human",
                                "platform": platform,
                                "identifier": identifier,
                                "name": name
                            })
                        else:
                            has_other_bot = True

            if not has_target_bot or has_other_bot:
                continue

            if not ALLOW_MIXED_TARGET_BOTS:
                distinct_bots = set(
                    p["id"] for team in roster.values() for p in team if p["type"] == "bot"
                )
                if len(distinct_bots) > 1:
                    continue

            if not ALLOW_MIXED_TEAMS:
                mixed = False
                for team in roster.values():
                    types = set(p["type"] for p in team)
                    if len(types) > 1:
                        mixed = True
                        break
                if mixed:
                    continue

            skip_replay = False
            parsed_teams = {"blue": [], "orange": []}
            target_dt = read_rfc3339_date(replay["date"])

            for team_str in ("blue", "orange"):
                for player in roster[team_str]:
                    if player["type"] == "bot":
                        bot_name = player["id"]
                        parsed_teams[team_str].append({
                            "type": "bot",
                            "id": bot_name,
                            "rating": bot_ratings[bot_name][team_size]
                        })
                    else:
                        platform = player["platform"]
                        identifier = player["identifier"]
                        name = player["name"]
                        cache_key = f"{platform}|{identifier}|{team_size}"
                        target_playlist_id = PLAYLIST_MAP.get(team_size)

                        def find_median_mmr(hist):
                            mmrs = []
                            covered = False
                            for entry in hist:
                                entry_dt = read_rfc3339_date(entry["date"])
                                if abs((entry_dt - target_dt).total_seconds()) <= 90 * 86400:
                                    covered = True
                                    if entry.get("is_sentinel"):
                                        continue

                                    # Use the new mathematical MMR calculator
                                    mmr_val = get_mmr_from_rank(target_playlist_id, entry["tier"], entry["division"])

                                    if mmr_val is not None:
                                        mmrs.append(mmr_val)

                            if not mmrs:
                                return covered, None
                            return covered, statistics.median(mmrs)

                        has_coverage, median_mmr = False, None
                        if cache_key in human_rank_cache:
                            has_coverage, median_mmr = find_median_mmr(human_rank_cache[cache_key])

                        if not has_coverage:
                            tqdm.write(f"  Fetching 6-Month Rank Window for {name} ({platform})...")
                            start_dt = target_dt - timedelta(days=90)
                            end_dt = target_dt + timedelta(days=90)
                            new_history = get_human_rank_history(platform, identifier, team_size, start_dt, end_dt)

                            if cache_key not in human_rank_cache:
                                human_rank_cache[cache_key] = []

                            if not new_history:
                                human_rank_cache[cache_key].append({
                                    "date": replay["date"],
                                    "is_sentinel": True
                                })
                            else:
                                existing_dates = {e["date"] for e in human_rank_cache[cache_key]}
                                for entry in new_history:
                                    if entry["date"] not in existing_dates:
                                        human_rank_cache[cache_key].append(entry)

                            has_coverage, median_mmr = find_median_mmr(human_rank_cache[cache_key])
                            time.sleep(0.5)

                        if median_mmr is None:
                            skip_replay = True
                            break

                        mu = (median_mmr - 100) / 20
                        parsed_teams[team_str].append({
                            "type": "human",
                            "id": identifier,
                            "rating": model.rating(mu=mu, sigma=2.5)
                        })
                if skip_replay:
                    break

            if skip_replay:
                continue

            blue_goals = replay["blue"].get("goals", 0)
            orange_goals = replay["orange"].get("goals", 0)

            if blue_goals > orange_goals:
                ranks = [1, 2]
            elif orange_goals > blue_goals:
                ranks = [2, 1]
            else:
                ranks = [1, 1]

            blue_rating_objs = [p["rating"] for p in parsed_teams["blue"]]
            orange_rating_objs = [p["rating"] for p in parsed_teams["orange"]]

            updated_teams = model.rate([blue_rating_objs, orange_rating_objs], ranks=ranks)

            bot_new_ratings = {}
            for team_str, new_team_ratings in zip(("blue", "orange"), updated_teams):
                for player_dict, new_rating in zip(parsed_teams[team_str], new_team_ratings):
                    if player_dict["type"] == "bot":
                        bot_name = player_dict["id"]
                        if bot_name not in bot_new_ratings:
                            bot_new_ratings[bot_name] = []
                        bot_new_ratings[bot_name].append(new_rating)

            for bot_name, ratings in bot_new_ratings.items():
                if len(ratings) == 1:
                    new_mu = ratings[0].mu
                    new_sigma = ratings[0].sigma
                else:
                    precisions = [1.0 / (r.sigma ** 2) for r in ratings]
                    total_precision = sum(precisions)
                    new_mu = sum(r.mu * p for r, p in zip(ratings, precisions)) / total_precision
                    avg_precision = total_precision / len(ratings)
                    new_sigma = math.sqrt(1.0 / avg_precision)

                bot_ratings[bot_name][team_size] = model.rating(mu=new_mu, sigma=new_sigma)
                bot_matches[bot_name][team_size] += 1

                mmr = (20 * new_mu) + 100
                sigma_mmr = 20 * new_sigma
                mmr_history[bot_name][team_size].append((target_dt, mmr, sigma_mmr))

    finally:
        print(f"\nSaving {len(human_rank_cache)} Rank records to {CACHE_FILE}...")
        save_cache(human_rank_cache, CACHE_FILE)

    print("\n" + "=" * 50)
    print("BOT RANK ESTIMATES PER MODE (Weng-Lin / OpenSkill)")
    print("=" * 50)

    for bot in TARGET_BOTS:
        print(f"\n{bot}:")
        for team_size in PLAYLIST_MAP.keys():
            matches_played = bot_matches[bot][team_size]
            mode_name = f"{team_size}v{team_size}"

            if matches_played > 0:
                rating = bot_ratings[bot][team_size]
                mmr = (20 * rating.mu) + 100
                plus_minus = f"±{(20 * rating.sigma):.0f}"

                # Retrieve the human readable rank string
                rank_label = get_rank_name_from_mmr(PLAYLIST_MAP[team_size], mmr, short=True)

                print(f"  {mode_name} - Estimated MMR={mmr:.0f} {plus_minus} [{rank_label}], "
                      f"Matches: {matches_played} (μ={rating.mu:.4g}, σ={rating.sigma:.4g})")
            else:
                print(f"  {mode_name} - Not enough data.")

    plot_mmr_history(mmr_history)


def plot_mmr_history(mmr_history):
    """Plot estimated MMR over time for each bot, one subplot per team size."""
    mode_labels = {1: "1v1", 2: "2v2", 3: "3v3"}

    # Determine which modes have data
    active_modes = []
    for team_size in PLAYLIST_MAP:
        for bot in TARGET_BOTS:
            if mmr_history[bot][team_size]:
                active_modes.append(team_size)
                break

    if not active_modes:
        print("No MMR history to plot.")
        return

    fig, axes = plt.subplots(len(active_modes), 1, figsize=(14, 5 * len(active_modes)),
                             squeeze=False, sharex=True)

    for idx, team_size in enumerate(active_modes):
        ax = axes[idx, 0]
        playlist_id = PLAYLIST_MAP[team_size]

        for bot in TARGET_BOTS:
            history = mmr_history[bot][team_size]
            if not history:
                continue

            dates = [h[0] for h in history]
            mmrs = [h[1] for h in history]
            sigmas = [h[2] for h in history]

            line, = ax.plot(dates, mmrs, label=bot, linewidth=1.5)
            ax.fill_between(dates,
                            [m - s for m, s in zip(mmrs, sigmas)],
                            [m + s for m, s in zip(mmrs, sigmas)],
                            alpha=0.15, color=line.get_color())

        # Add rank tier background shading
        tiers = build_tier_ranges(playlist_id)
        tier_colors = [
            "#cd7f32",  # Bronze
            "#c0c0c0",  # Silver
            "#ffd700",  # Gold
            "#00e5ff",  # Platinum
            "#1565c0",  # Diamond
            "#7b1fa2",  # Champion
            "#d50000",  # Grand Champion
            "#f5f5f5",  # SSL
        ]
        for t in range(1, 23):
            min_mmr, max_mmr = tiers[t]
            color_idx = (t - 1) // 3
            if color_idx < len(tier_colors):
                ax.axhspan(min_mmr, max_mmr, alpha=0.06, color=tier_colors[color_idx])
            # Label the tier on the right side
            mid = (min_mmr + max_mmr) / 2
            ax.text(1.01, mid, TIER_NAMES_SHORT[t], transform=ax.get_yaxis_transform(),
                    fontsize=6, va="center", alpha=0.5)

        ax.set_ylabel("Estimated MMR")
        ax.set_title(f"{mode_labels[team_size]} MMR Over Time")
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))

    axes[-1, 0].set_xlabel("Replay Date")
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.savefig("bot_mmr_over_time.png", dpi=150, bbox_inches="tight")
    print("\nSaved MMR-over-time plot to bot_mmr_over_time.png")
    plt.show()


if __name__ == "__main__":
    main()
