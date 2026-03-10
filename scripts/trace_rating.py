"""Trace through a solo queue rating update to understand the deltas."""

import math
from collections import defaultdict
from openskill.models.weng_lin.plackett_luce import PlackettLuce

model = PlackettLuce()


def consolidate_precision(mu_0, sigma_0, posteriors):
    """Sum mu deltas, sum precision deltas."""
    if len(posteriors) == 1:
        return posteriors[0]
    delta_mu = sum(mu_i - mu_0 for mu_i, _ in posteriors)
    mu_final = mu_0 + delta_mu
    precision_0 = 1.0 / (sigma_0 ** 2)
    delta_precision = sum(1.0 / (s ** 2) - precision_0 for _, s in posteriors)
    precision_final = precision_0 + delta_precision
    sigma_final = math.sqrt(1.0 / precision_final) if precision_final > 0 else sigma_0
    return mu_final, sigma_final


def consolidate_rms_avg(mu_0, sigma_0, posteriors):
    """Old method: average mu, RMS average sigma."""
    n = len(posteriors)
    mu_final = sum(m for m, _ in posteriors) / n
    sigma_final = math.sqrt(sum(s ** 2 for _, s in posteriors)) / n
    return mu_final, sigma_final


print("=" * 90)
print("SCENARIO: [A, B, C] beat [B, C, D]")
print("Comparing consolidation methods for shared bots (B, C)")
print("=" * 90)

test_cases = [
    ("All equal (25/5)", {"A": (25, 5), "B": (25, 5), "C": (25, 5), "D": (25, 5)}),
    ("A strong settled, others new", {"A": (30, 2.5), "B": (25, 6), "C": (25, 7), "D": (25, 2.5)}),
    ("C most uncertain", {"A": (28, 2.5), "B": (26, 4), "C": (25, 6), "D": (24, 2.5)}),
]

for label, ratings in test_cases:
    print(f"\n--- {label} ---")

    r = {name: model.rating(mu=mu, sigma=sig) for name, (mu, sig) in ratings.items()}

    blue = [r["A"], r["B"], r["C"]]
    orange = [r["B"], r["C"], r["D"]]
    new_blue, new_orange = model.rate(teams=[blue, orange], ranks=[0, 1])

    results = {
        "A": [new_blue[0]],
        "B": [new_blue[1], new_orange[0]],
        "C": [new_blue[2], new_orange[1]],
        "D": [new_orange[2]],
    }

    print(f"  {'Bot':<5} {'method':<12} {'new_mu':>8} {'new_sig':>8} {'d_MMR':>6}")

    for name in ["A", "B", "C", "D"]:
        mu_0, sig_0 = ratings[name]
        old_display = mu_0 - 3 * sig_0
        posteriors = [(rt.mu, rt.sigma) for rt in results[name]]

        if len(posteriors) == 1:
            mu_f, sig_f = posteriors[0]
            d_mmr = round(20 * (mu_f - mu_0))
            print(f"  {name:<5} {'(single)':<12} {mu_f:8.2f} {sig_f:8.4f} {d_mmr:+6d}")
        else:
            for method_name, method in [("rms_avg", consolidate_rms_avg), ("precision", consolidate_precision)]:
                mu_f, sig_f = method(mu_0, sig_0, posteriors)
                d_mmr = round(20 * (mu_f - mu_0))
                print(f"  {name:<5} {method_name:<12} {mu_f:8.2f} {sig_f:8.4f} {d_mmr:+6d}")

print()
print("=" * 90)
print("Self-play sanity check: if a bot is on both sides equally,")
print("precision method should give ~0 MMR change (just sigma reduction).")
print("=" * 90)
print()

for sig in [3.0, 5.0, 8.33]:
    r_test = model.rating(mu=25, sigma=sig)
    new = model.rate(teams=[[r_test], [r_test]], ranks=[0, 1])
    posteriors = [(new[0][0].mu, new[0][0].sigma), (new[1][0].mu, new[1][0].sigma)]

    mu_rms, sig_rms = consolidate_rms_avg(25, sig, posteriors)
    mu_prec, sig_prec = consolidate_precision(25, sig, posteriors)

    d_rms = round(20 * (mu_rms - 25))
    d_prec = round(20 * (mu_prec - 25))

    print(f"  sigma={sig:.2f}: RMS avg → MMR {d_rms:+d}, precision → MMR {d_prec:+d}")


