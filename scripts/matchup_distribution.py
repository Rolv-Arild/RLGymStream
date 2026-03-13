"""Visualize the distribution of win probabilities for accepted matchups.

Simulates the accept/reject sampling from the matchmaker and plots:
1. The raw win-probability distribution of all random matchups
2. The accepted distribution after p*(1-p) filtering

Usage:
    python scripts/matchup_distribution.py                 # uses data/rlgymstream.db
    python scripts/matchup_distribution.py path/to.db      # custom db path
    python scripts/matchup_distribution.py --samples 50000 # number of random matchups
"""

import random
import sys
from pathlib import Path

from rlgymstream.config import AppConfig
from rlgymstream.db.database import Database
from rlgymstream.matchmaking.ratings import make_rating, _model as _os_model, configure_defaults

_MAX_WEIGHT = 0.25


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n_samples = 1_00_000
    for i, a in enumerate(sys.argv[1:]):
        if a == "--samples" and i + 2 < len(sys.argv):
            n_samples = int(sys.argv[i + 2])

    db_path = Path(args[0]) if args else Path("data/rlgymstream.db")
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    config = AppConfig.from_toml()
    configure_defaults(config.default_mu, config.default_sigma)
    db = Database(db_path)

    modes = config.mode_rotation
    all_bots = db.get_all_bots(enabled_only=True)

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is required: pip install matplotlib")
        sys.exit(1)

    fig, axes = plt.subplots(len(modes), 1, figsize=(10, 3 * len(modes)), squeeze=False)

    for idx, mode in enumerate(modes):
        ax = axes[idx][0]
        bots = [b for b in all_bots if b.supports_mode(mode.value)]
        if len(bots) < 2:
            ax.set_title(f"{mode.display_name} — not enough bots")
            continue

        # Load ratings
        bot_ratings = {}
        for bot in bots:
            assert bot.id is not None
            r = db.get_rating(bot.id, mode.value)
            bot_ratings[bot.id] = make_rating(r.mu, r.sigma)

        raw_probs = []
        accepted_probs = []
        threshold_probs = []  # "roll once, search" variant
        team_size = mode.team_size

        def gen_matchup():
            if mode.is_solo_queue:
                while True:
                    blue = random.choices(bots, k=team_size)
                    orange = random.choices(bots, k=team_size)
                    if sorted(b.id for b in blue) != sorted(b.id for b in orange):
                        break
                blue_r = [bot_ratings[b.id] for b in blue]
                orange_r = [bot_ratings[b.id] for b in orange]
            else:
                a, b = random.sample(bots, 2)
                blue_r = [bot_ratings[a.id]]
                orange_r = [bot_ratings[b.id]]
            probs = _os_model.predict_win([blue_r, orange_r])
            return probs[0]

        for _ in range(n_samples):
            p = gen_matchup()
            raw_probs.append(p)

            weight = p * (1 - p)
            if random.random() < weight / _MAX_WEIGHT:
                accepted_probs.append(p)

        # "Roll once, search" variant: roll a threshold, then find a matchup that exceeds it
        # Every 1000 failed attempts, square the threshold to make it easier
        n_threshold_matches = len(accepted_probs)  # same number for fair comparison
        for _ in range(n_threshold_matches):
            threshold = random.random()
            for _attempt in range(10000):
                if _attempt > 0 and _attempt % 1000 == 0:
                    threshold *= threshold
                p = gen_matchup()
                if p * (1 - p) / _MAX_WEIGHT >= threshold:
                    threshold_probs.append(p)
                    break

        bins = 50
        ax.hist(raw_probs, bins=bins, alpha=0.3, label=f"Random ({len(raw_probs)})",
                density=True, color="gray")
        ax.hist(accepted_probs, bins=bins, alpha=0.5, label=f"Current ({len(accepted_probs)})",
                density=True, color="steelblue")
        ax.hist(threshold_probs, bins=bins, alpha=0.5, label=f"Roll-once ({len(threshold_probs)})",
                density=True, color="orange")
        ax.set_title(f"{mode.display_name}")
        ax.set_xlabel("Blue win probability")
        ax.set_ylabel("Density")
        ax.axvline(0.5, color="red", linestyle="--", alpha=0.5, label="50/50")
        ax.legend(fontsize=8)
        ax.set_xlim(0, 1)

    fig.suptitle("Matchup Win Probability Distribution", fontsize=14, fontweight="bold")
    fig.tight_layout()
    plt.savefig("matchup_distribution.png", dpi=150)
    print(f"Saved matchup_distribution.png")
    plt.show()


if __name__ == "__main__":
    main()

