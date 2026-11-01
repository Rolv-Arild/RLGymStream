"""Offensive/defensive Elo-style ratings from goal counts (standard modes).

Each roster (one bot duplicated to fill a team, per mode) gets an offensive
rating O and a defensive rating D.  Expected goals for a 5-minute match are
exp((O_attacker - D_defender) / C), scaled by the match's actual gameplay
time; after each match O and D move by K x (actual - expected) goals.

Prints the final ratings per mode, then compares the model's predicted
goals and win probability (Poisson goals, sudden-death overtime) with the
real head-to-head results.  Solo-queue matches are skipped.  Read-only.

Usage:
    python scripts/offensive_defensive_ratings.py              # uses data/rlgymstream.db
    python scripts/offensive_defensive_ratings.py path/to.db
"""

import itertools
import math
import sqlite3
import sys
from pathlib import Path

# --- Configuration ---
DEFAULT_DB_PATH = Path("data/rlgymstream.db")
K = 10.0
C = 400.0
DEFAULT_RATING = 1000.0

# --- Time Constants (Seconds) ---
SPAWN_TO_KICKOFF_START = 3.0
KICKOFF_START_TO_TOUCH = 2.0
GOAL_TO_SPAWN = 3.0  # Adjust this based on your replay/celebration skip settings


def get_gameplay_duration(duration_seconds, total_goals, count_kickoffs=False):
    """
    Adjusts the raw match duration to isolate actual gameplay time by
    subtracting kickoffs and goal pauses.
    """
    duration = duration_seconds

    if count_kickoffs:
        kickoff_time = 0  # Don't subtract kickoff time
    else:
        kickoff_time = KICKOFF_START_TO_TOUCH

    duration -= SPAWN_TO_KICKOFF_START + kickoff_time  # Initial kickoff
    duration -= total_goals * (GOAL_TO_SPAWN + SPAWN_TO_KICKOFF_START + kickoff_time)

    return max(0.0, duration)  # Ensure non-negative duration


def get_roster_key(team_ids, mode):
    """
    Sorts bot IDs to ensure consistent roster identification.
    """
    sorted_ids = ",".join(sorted(team_ids.split(",")))
    return f"{mode}|{sorted_ids}"


def get_roster_names(roster_ids, bot_map):
    """
    Converts a string of IDs like '1,2' into 'BotName1, BotName2'
    """
    roster_ids = roster_ids.split(",")
    if len(set(roster_ids)) == 1:
        return bot_map.get(int(roster_ids[0]), f"Unknown({roster_ids[0]})")
    return ", ".join(bot_map.get(int(b_id), f"Unknown({b_id})") for b_id in roster_ids)


def calculate_dual_elo(db_path):
    # Read-only connection
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cursor = conn.cursor()

    # Build the ID to Name mapping
    cursor.execute("SELECT id, name FROM bots")
    bot_map = {row[0]: row[1] for row in cursor.fetchall()}

    # Memory dictionaries to hold live ratings
    O_ratings = {}
    D_ratings = {}
    matches_played = {}

    def get_ratings(roster_key):
        return O_ratings.get(roster_key, DEFAULT_RATING), D_ratings.get(roster_key, DEFAULT_RATING)

    # Fetch all matches chronologically, now including duration_seconds
    cursor.execute("""
        SELECT id, mode, team_blue_ids, team_orange_ids, score_blue, score_orange, duration_seconds 
        FROM matches 
        ORDER BY timestamp ASC, id ASC
    """)
    matches = cursor.fetchall()
    processed = 0

    for match in matches:
        match_id, mode, blue_ids, orange_ids, score_blue, score_orange, duration_seconds = match

        if mode.startswith("solo_"):
            continue
        processed += 1

        blue_roster = get_roster_key(blue_ids, mode)
        orange_roster = get_roster_key(orange_ids, mode)

        matches_played[blue_roster] = matches_played.get(blue_roster, 0) + 1
        matches_played[orange_roster] = matches_played.get(orange_roster, 0) + 1

        O_blue, D_blue = get_ratings(blue_roster)
        O_orange, D_orange = get_ratings(orange_roster)

        # 1. Calculate Expected Goals using the Exponential Model (Base 5-minute expectation)
        try:
            E_blue_base = min(math.exp((O_blue - D_orange) / C), 50.0)
        except OverflowError:
            E_blue_base = 50.0

        try:
            E_orange_base = min(math.exp((O_orange - D_blue) / C), 50.0)
        except OverflowError:
            E_orange_base = 50.0

        # 2. Time Normalization
        total_goals = score_blue + score_orange
        # Fallback to 300 if duration is missing/0 to prevent math errors on old data
        raw_duration = duration_seconds if duration_seconds > 0 else 300.0

        gameplay_duration = get_gameplay_duration(raw_duration, total_goals)

        # Scale expected goals by the actual proportion of a standard 5-minute (300s) match played
        time_scalar = gameplay_duration / 300.0
        E_blue_scaled = E_blue_base * time_scalar
        E_orange_scaled = E_orange_base * time_scalar

        # 3. Calculate new ratings using the time-adjusted expectations
        O_blue_new = O_blue + K * (score_blue - E_blue_scaled)
        D_orange_new = D_orange + K * (E_blue_scaled - score_blue)

        O_orange_new = O_orange + K * (score_orange - E_orange_scaled)
        D_blue_new = D_blue + K * (E_orange_scaled - score_orange)

        # 4. Apply updates to memory
        O_ratings[blue_roster] = O_blue_new
        D_ratings[blue_roster] = D_blue_new
        O_ratings[orange_roster] = O_orange_new
        D_ratings[orange_roster] = D_orange_new

    conn.close()

    # --- Print Results ---
    longest_roster_name_length = 0
    results = []
    for roster_key, O_rating in O_ratings.items():
        mode, roster_ids = roster_key.split("|")
        roster_names = get_roster_names(roster_ids, bot_map)
        D_rating = D_ratings[roster_key]
        played = matches_played[roster_key]
        delta = O_rating - D_rating
        results.append((roster_names, mode, O_rating, D_rating, delta, played))
        if len(roster_names) > longest_roster_name_length:
            longest_roster_name_length = len(roster_names)

    # Sort primarily by mode (alphabetical), then by total rating O + D (descending)
    results.sort(key=lambda x: (x[1], -x[2] - x[3]))

    # Print the table header
    header = f"{'Mode':<5} | {'Roster Names':<{longest_roster_name_length}} | {'O-Rating':<8} | {'D-Rating':<8} | {'Delta':<8} | {'Matches'}"
    total_width = len(header)
    print(f"\nProcessed {processed} standard-mode matches. Final Ratings:")
    print("=" * total_width)
    print(header)
    print("-" * total_width)

    # Print each roster's stats
    for r in results:
        if r[5] > 0:
            truncated_name = r[0][:longest_roster_name_length - 3] + "..." if len(
                r[0]) > longest_roster_name_length else r[0]
            print(
                f"{r[1]:<5} | {truncated_name:<{longest_roster_name_length}} | {r[2]:<8.1f} | {r[3]:<8.1f} | {r[4]:<8.1f} | {r[5]}")
    print("=" * total_width)

    return O_ratings, D_ratings, bot_map


def calculate_h2h_expectation(O_A, D_A, O_B, D_B, C=400.0):
    """
    Calculates expected match outcomes using independent Poisson distributions
    and handles sudden-death overtime mathematically.
    """
    # 1. Expected Goals (Poisson Rate Parameter lambda)
    lambda_A = math.exp((O_A - D_B) / C)
    lambda_B = math.exp((O_B - D_A) / C)

    prob_A_reg = 0.0
    prob_B_reg = 0.0
    prob_draw = 0.0

    # 2. Build the Probability Matrix (capping at 20 goals for computation)
    for x in range(21):
        p_A_x = (math.pow(lambda_A, x) * math.exp(-lambda_A)) / math.factorial(x)
        for y in range(21):
            p_B_y = (math.pow(lambda_B, y) * math.exp(-lambda_B)) / math.factorial(y)

            prob = p_A_x * p_B_y
            if x > y:
                prob_A_reg += prob
            elif x < y:
                prob_B_reg += prob
            else:
                prob_draw += prob

    # 3. Overtime Expected Outcomes (Exponential Distribution of time to next event)
    # The probability of scoring first in OT is proportional to the goal rates
    prob_A_ot_win = lambda_A / (lambda_A + lambda_B) if (lambda_A + lambda_B) > 0 else 0.5
    prob_B_ot_win = 1.0 - prob_A_ot_win

    # 4. Total Match Outcome
    total_prob_A = prob_A_reg + (prob_draw * prob_A_ot_win)
    total_prob_B = prob_B_reg + (prob_draw * prob_B_ot_win)

    return lambda_A, lambda_B, total_prob_A, total_prob_B


def get_h2h_ground_truth(db_path):
    """
    Parses the database to find the actual historical outcomes
    between any two specific rosters.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cursor = conn.cursor()
    cursor.execute("""
            SELECT mode, team_blue_ids, team_orange_ids, score_blue, score_orange 
            FROM matches
        """)

    h2h_stats = {}
    for row in cursor.fetchall():
        mode, blue_ids, orange_ids, score_blue, score_orange = row
        if mode.startswith("solo_"):
            continue

        blue_key = get_roster_key(blue_ids, mode)
        orange_key = get_roster_key(orange_ids, mode)

        # Sort to ensure consistent ordering (Team A vs Team B)
        if blue_key < orange_key:
            team_a_key, team_b_key = blue_key, orange_key
            a_goals, b_goals = score_blue, score_orange
        else:
            team_a_key, team_b_key = orange_key, blue_key
            a_goals, b_goals = score_orange, score_blue

        pair_key = (team_a_key, team_b_key)

        if pair_key not in h2h_stats:
            h2h_stats[pair_key] = {'matches': 0, 'a_goals': 0, 'b_goals': 0, 'a_wins': 0, 'b_wins': 0}

        stats = h2h_stats[pair_key]
        stats['matches'] += 1
        stats['a_goals'] += a_goals
        stats['b_goals'] += b_goals

        if a_goals > b_goals:
            stats['a_wins'] += 1
        elif b_goals > a_goals:
            stats['b_wins'] += 1

    conn.close()
    return h2h_stats


if __name__ == "__main__":
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    o_ratings, d_ratings, bot_map = calculate_dual_elo(db_path)
    h2h_ground_truth = get_h2h_ground_truth(db_path)

    if o_ratings:
        modes_available = set(k.split("|")[0] for k in o_ratings.keys())

        for mode in sorted(modes_available):
            mode_rosters = [k for k in o_ratings.keys() if k.startswith(mode + "|")]

            if len(mode_rosters) < 2:
                continue

            # Sort by total rating (Offense + Defense) so the table reads strongest-first
            mode_rosters.sort(key=lambda k: o_ratings[k] + d_ratings[k], reverse=True)

            print(f"\n--- Model Predictions vs Ground Truth Reality: {mode} ---")

            # Find longest name for dynamic column formatting, capped at 20 characters
            max_name_len = max([len(get_roster_names(k.split("|")[1], bot_map)) for k in mode_rosters])
            max_name_len = min(max_name_len, 20)

            header = f"{'Team A':<{max_name_len}} | {'Team B':<{max_name_len}} | {'Pred E[G]':<11} | {'Real Avg G':<11} | {'Pred Win%':<10} | {'Real Win%':<10} | {'Matches'}"
            print("=" * len(header))
            print(header)
            print("-" * len(header))

            # Iterate through every unique pairing in this mode
            for team_a_key, team_b_key in itertools.combinations(mode_rosters, 2):
                # Ensure the same key ordering used in the ground truth dictionary
                if team_a_key > team_b_key:
                    team_a_key, team_b_key = team_b_key, team_a_key

                pair_key = (team_a_key, team_b_key)
                gt = h2h_ground_truth.get(pair_key,
                                          {'matches': 0, 'a_goals': 0, 'b_goals': 0, 'a_wins': 0, 'b_wins': 0})

                # Only show matchups that have actually been played so we can compare
                if gt['matches'] == 0:
                    continue

                team_a_name = get_roster_names(team_a_key.split("|")[1], bot_map)
                team_b_name = get_roster_names(team_b_key.split("|")[1], bot_map)

                # Truncate for display if necessary
                t_a_disp = team_a_name[:max_name_len - 3] + "..." if len(team_a_name) > max_name_len else team_a_name
                t_b_disp = team_b_name[:max_name_len - 3] + "..." if len(team_b_name) > max_name_len else team_b_name

                l_A, l_B, p_A, p_B = calculate_h2h_expectation(
                    o_ratings[team_a_key], d_ratings[team_a_key],
                    o_ratings[team_b_key], d_ratings[team_b_key],
                    C
                )

                real_avg_A = gt['a_goals'] / gt['matches']
                real_avg_B = gt['b_goals'] / gt['matches']
                real_win_A = (gt['a_wins'] / gt['matches']) * 100

                pred_eg_str = f"{l_A:.1f} - {l_B:.1f}"
                real_avg_str = f"{real_avg_A:.1f} - {real_avg_B:.1f}"

                print(
                    f"{t_a_disp:<{max_name_len}} | {t_b_disp:<{max_name_len}} | {pred_eg_str:<11} | {real_avg_str:<11} | {p_A * 100:>9.1f}% | {real_win_A:>9.1f}% | {gt['matches']:>7}")

            print("=" * len(header))