"""Plot MMR of all bots over time by replaying match history.

Replays every match in chronological order, recording each bot's MMR
after every match.  Produces one chart per mode showing how each bot's
MMR evolved over time.

Usage:
    python scripts/plot_mmr_history.py                   # all modes
    python scripts/plot_mmr_history.py --mode 1v1        # single mode
    python scripts/plot_mmr_history.py --mode 1v1 2v2    # multiple modes
    python scripts/plot_mmr_history.py --output mmr.png  # save to file instead of showing
"""

import argparse
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt

from rlgymstream.config import AppConfig
from rlgymstream.db.database import Database
from rlgymstream.matchmaking.ratings import update_ratings, configure_defaults

ALL_MODES = ["1v1", "2v2", "3v3", "solo_2v2", "solo_3v3"]

MODE_LABELS = {
    "1v1": "1v1",
    "2v2": "2v2",
    "3v3": "3v3",
    "solo_2v2": "Solo Queue 2v2",
    "solo_3v3": "Solo Queue 3v3",
}


def mu_to_mmr(mu: float) -> int:
    return round(20 * mu + 100)


def replay_and_collect(db_path: Path, config: AppConfig, modes: list[str], shared_x: bool = False):
    """Replay all matches on a temporary copy and collect MMR snapshots.

    Args:
        shared_x: If True, the x-axis value is the global match number for
            that mode (so all bots share the same timeline).  If False,
            x counts only the matches each individual bot participated in.

    Returns:
        bot_names: {bot_id: name}
        history: {mode: {bot_id: [(x_value, mmr)]}}
    """
    # Work on a temporary copy so the real database is untouched
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    shutil.copy2(db_path, tmp_path)

    try:
        db = Database(tmp_path)

        bot_names = {b.id: b.name for b in db.get_all_bots(enabled_only=False)}

        # Fetch all matches in chronological order
        with db._conn() as conn:
            rows = conn.execute("SELECT * FROM matches ORDER BY id ASC").fetchall()

        if not rows:
            print("No matches in database.")
            return bot_names, {}

        # Reset all ratings
        with db._conn() as conn:
            conn.execute("DELETE FROM ratings")

        # Seed anchored bots
        anchored_per_mode: dict[str, set[int]] = {m: set() for m in ALL_MODES}
        for anchor in config.anchored_ratings:
            bot = db.get_bot_by_name(anchor.bot_name)
            if bot and bot.id is not None:
                target_modes = anchor.modes if anchor.modes else ALL_MODES
                for mode in target_modes:
                    if mode in anchored_per_mode:
                        anchored_per_mode[mode].add(bot.id)
                        r = db.get_rating(bot.id, mode)
                        r.mu = anchor.mu
                        r.sigma = anchor.sigma
                        db.save_rating(r)

        # history[mode][bot_id] = [(match_index, mmr)]
        history: dict[str, dict[int, list[tuple[int, int]]]] = {
            m: defaultdict(list) for m in modes
        }

        # Track per-mode match counters
        mode_match_count: dict[str, int] = defaultdict(int)
        # Track per-bot match counters (used when shared_x is False)
        bot_match_count: dict[str, dict[int, int]] = {m: defaultdict(int) for m in modes}
        # Track all bots ever seen per mode and their last known MMR (for shared_x)
        seen_bots: dict[str, dict[int, int]] = {m: {} for m in modes}

        for row in rows:
            mode = row[1]
            if mode not in modes:
                continue

            team_blue_ids = [int(x) for x in row[4].split(",")]
            team_orange_ids = [int(x) for x in row[5].split(",")]
            winner = row[8]
            is_solo = mode.startswith("solo_")

            update_ratings(
                db, mode, team_blue_ids, team_orange_ids, winner,
                is_solo_queue=is_solo,
                anchored_bot_ids=anchored_per_mode.get(mode, set()),
            )

            mode_match_count[mode] += 1
            match_idx = mode_match_count[mode]

            # Update last known MMR for bots involved in this match
            involved = set(team_blue_ids) | set(team_orange_ids)
            for bid in involved:
                r = db.get_rating(bid, mode)
                seen_bots[mode][bid] = mu_to_mmr(r.mu)

            if shared_x:
                # Record a point for every bot seen so far (carry forward last MMR)
                for bid, mmr in seen_bots[mode].items():
                    history[mode][bid].append((match_idx, mmr))
            else:
                for bid in involved:
                    bot_match_count[mode][bid] += 1
                    history[mode][bid].append((bot_match_count[mode][bid], seen_bots[mode][bid]))

        total = sum(mode_match_count.values())
        print(f"Replayed {total} matches across {len(mode_match_count)} modes.")
        return bot_names, history

    finally:
        tmp_path.unlink(missing_ok=True)


def _assign_bot_colors(bot_names: dict[int, str], history: dict[str, dict]) -> dict[int, tuple]:
    """Assign a consistent color to each bot across all subplots.

    Uses multiple colormaps to support large rosters while keeping colours
    perceptually distinct.  Sorted by bot name so the mapping is
    deterministic regardless of plot order.
    """
    # Collect every bot id that appears in any mode
    all_bids = set()
    for mode_hist in history.values():
        all_bids.update(mode_hist.keys())

    # Sort by name for deterministic ordering
    sorted_bids = sorted(all_bids, key=lambda b: bot_names.get(b, f"#{b}").lower())

    # Build a palette from multiple tab20 variants (up to 60 distinct colours)
    palette = []
    for cmap_name in ("tab20", "tab20b", "tab20c"):
        cmap = plt.colormaps[cmap_name]
        palette.extend(cmap(i / 20) for i in range(20))

    return {bid: palette[i % len(palette)] for i, bid in enumerate(sorted_bids)}


def plot_history(bot_names, history, modes, output_path=None, shared_x=False):
    """Plot MMR over time for each mode."""
    active_modes = [m for m in modes if m in history and history[m]]
    if not active_modes:
        print("No data to plot.")
        return

    bot_colors = _assign_bot_colors(bot_names, history)

    n_modes = len(active_modes)
    fig, axes = plt.subplots(n_modes, 1, figsize=(14, 5 * n_modes), squeeze=False)
    fig.suptitle("Bot MMR Over Time", fontsize=16, fontweight="bold", y=0.98)

    for idx, mode in enumerate(active_modes):
        ax = axes[idx, 0]
        mode_history = history[mode]

        # Sort bots by their final MMR (descending) for legend ordering
        final_mmr = {}
        for bid, points in mode_history.items():
            if points:
                final_mmr[bid] = points[-1][1]

        sorted_bots = sorted(final_mmr.keys(), key=lambda b: final_mmr[b], reverse=True)

        for bid in sorted_bots:
            points = mode_history[bid]
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            name = bot_names.get(bid, f"#{bid}")
            label = f"{name} ({ys[-1]})"
            ax.plot(xs, ys, label=label, linewidth=1.5, alpha=0.85,
                    color=bot_colors.get(bid))

        ax.set_title(MODE_LABELS.get(mode, mode), fontsize=14, fontweight="bold")
        ax.set_xlabel("Match # (all matches)" if shared_x else "Match # (per bot)")
        ax.set_ylabel("MMR")
        ax.grid(True, alpha=0.3)
        ax.legend(
            loc="upper left",
            bbox_to_anchor=(1.01, 1),
            fontsize=8,
            borderaxespad=0,
            framealpha=0.9,
        )

    plt.tight_layout(rect=[0, 0, 0.82, 0.96])

    if output_path:
        plt.savefig(output_path, dpi=150, bbox_inches="tight")
        print(f"Saved plot to {output_path}")
    else:
        plt.show()


def main():
    parser = argparse.ArgumentParser(description="Plot MMR of all bots over time.")
    parser.add_argument(
        "--db", type=str, default="data/rlgymstream.db",
        help="Path to the database file (default: data/rlgymstream.db)",
    )
    parser.add_argument(
        "--mode", nargs="*", default=None,
        help="Mode(s) to plot (e.g. 1v1 2v2). Default: all modes with data.",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Save the plot to a file instead of showing interactively.",
    )
    parser.add_argument(
        "--shared-x", action="store_true", default=False,
        help="Use a shared x-axis (global match number) instead of per-bot match count.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    modes = args.mode if args.mode else ALL_MODES

    config = AppConfig.from_toml()
    configure_defaults(config.default_mu, config.default_sigma)

    bot_names, history = replay_and_collect(db_path, config, modes, shared_x=args.shared_x)
    plot_history(bot_names, history, modes, args.output, shared_x=args.shared_x)


if __name__ == "__main__":
    main()

