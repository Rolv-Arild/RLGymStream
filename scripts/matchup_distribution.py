"""Visualize the distribution of win probabilities for accepted matchups.

Simulates matchmaking strategies and plots their win-probability distributions:
1. Random matchups (baseline)
2. Accept/reject p*(1-p) filtering (used for solo queue)
3. Roll-once accept/reject (legacy)
4. Temperature-Controlled Active Learning (used for standard modes)

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
    n_prior = 1.0
    temperature = 1.0
    for i, a in enumerate(sys.argv[1:]):
        if a == "--samples" and i + 2 < len(sys.argv):
            n_samples = int(sys.argv[i + 2])
        if a == "--n-prior" and i + 2 < len(sys.argv):
            n_prior = float(sys.argv[i + 2])
        if a == "--temperature" and i + 2 < len(sys.argv):
            temperature = float(sys.argv[i + 2])

    db_path = Path(args[0]) if args else Path("data/rlgymstream.db")
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    config = AppConfig.from_toml()
    configure_defaults(config.default_mu, config.default_sigma)
    n_prior = config.matchmaker_n_prior if n_prior == 1.0 else n_prior
    temperature = config.matchmaker_temperature if temperature == 1.0 else temperature
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

        bins = 50
        ax.hist(raw_probs, bins=bins, alpha=0.3, label=f"Random ({len(raw_probs)})",
                density=True, color="gray")
        ax.hist(accepted_probs, bins=bins, alpha=0.5, label=f"Accept/reject ({len(accepted_probs)})",
                density=True, color="steelblue")

        # Temperature-Controlled Active Learning (standard modes only)
        if not mode.is_solo_queue and len(bots) >= 2:
            h2h = db.get_pairwise_h2h(mode.value)
            inv_temp = 1.0 / max(temperature, 1e-9)

            pairs = []
            pair_weights = []
            pair_probs_blue = []  # store the expected blue win prob per pair

            for i in range(len(bots)):
                for j in range(i + 1, len(bots)):
                    a, b = bots[i], bots[j]
                    probs = _os_model.predict_win(
                        [[bot_ratings[a.id]], [bot_ratings[b.id]]],
                    )
                    expected_a = probs[0]

                    alpha_prior = expected_a * n_prior
                    beta_prior = (1.0 - expected_a) * n_prior

                    key = (min(a.id, b.id), max(a.id, b.id))
                    wins_a, wins_b = 0, 0
                    if key in h2h:
                        if a.id == key[0]:
                            wins_a, wins_b = h2h[key]
                        else:
                            wins_b, wins_a = h2h[key]

                    alpha = alpha_prior + wins_a
                    beta = beta_prior + wins_b
                    total = alpha + beta
                    variance = (alpha * beta) / ((total ** 2) * (total + 1))
                    w = variance ** inv_temp

                    pairs.append((a, b))
                    pair_weights.append(w)
                    pair_probs_blue.append(expected_a)

            if pairs:
                # Sample the same number as accepted matchups
                n_tcal = len(accepted_probs)
                selected_indices = random.choices(range(len(pairs)), weights=pair_weights, k=n_tcal)
                # For each selected pair, randomly assign blue/orange → 50% flip
                tcal_probs = []
                for si in selected_indices:
                    p = pair_probs_blue[si]
                    if random.random() < 0.5:
                        p = 1.0 - p
                    tcal_probs.append(p)

                ax.hist(tcal_probs, bins=bins, alpha=0.5,
                        label=f"TCAL T={temperature:.1f} n={n_prior:.1f} ({len(tcal_probs)})",
                        density=True, color="green")

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

