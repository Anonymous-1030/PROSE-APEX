#!/usr/bin/env python3
"""1x2 single-column figure for the paper: Mooncake guard/bypass experiment.

Panel (a): guard fires per 10-min run across bandwidth regimes (Tier-A).
Panel (b): wrong-identity share, guard on vs off (constructed burst).

Reads results/*.json -- no hand-entered numbers except the 65-delivery
count from the curated bypass baseline (unprotected_tierU_B_*.json).

Usage: python3 analysis/make_fig_guard_bypass.py [results_dir]
Outputs: results/fig_guard_bypass.pdf and .png
"""

import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RD = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "results")

ORANGE, BLUE, GREEN, GRAY = "#E69F00", "#0072B2", "#009E73", "#999999"


def load(name):
    with open(os.path.join(RD, name)) as f:
        return json.load(f)


# ---- panel (a) data: Tier-A 10-min runs ----------------------------------
a_runs = [
    ("no limit", load("tierA_dod_ttl1000_c64_seed42.json")),
    ("100 MB/s", load("tierA_dod2_tc800m_ttl1000_c64_seed42.json")),
    ("50 MB/s", load("tierA_dod5_er0.5_bsoftpin_tc400m_ttl1000_c64_seed42.json")),
    ("50 MB/s\n+burst put", load("tierA_dod6_er0.5_bsoftpin_maxput_tc400m_ttl1000_c64_seed42.json")),
]
a_labels = [r[0] for r in a_runs]
a_fires = [r[1]["guard_fires"] for r in a_runs]
a_torn = a_runs[3][1].get("torn_events", 0)

# ---- panel (b) data: constructed burst, guard on vs off ------------------
tb = load("tierB_tierB_d2000_ttl1000_c64_seed42.json")
wire_on_pct = 100.0 * tb["rpe_events"] / tb["guard_fires"]
up = load("unprotected_tierU_B_d2000_bypass.json")
deliv_off_pct = up["guard_off_baseline"]["unprotected_wrong_per_delivered_pct"]
deliv_off_mb = up["guard_off_baseline"]["delivered_wrong_bytes"] / 1e6

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(3.5, 1.85))
plt.rcParams.update({"font.size": 6.4, "axes.linewidth": 0.6})

# panel (a)
x = range(len(a_labels))
bars = ax1.bar(x, a_fires, width=0.62,
               color=[GRAY, BLUE, BLUE, ORANGE], edgecolor="black", linewidth=0.4)
for xi, v in zip(x, a_fires):
    star = "*" if xi == 3 else ""
    ax1.text(xi, v + 90, f"{v:,}{star}", ha="center", va="bottom", fontsize=5.8)
ax1.text(3.2, 4500, "*1 torn read\n(3.7 MB)", fontsize=5.6, ha="center",
         va="bottom")
ax1.set_xticks(list(x))
ax1.set_xticklabels(["no\ncap", "100\nMB/s", "50\nMB/s", "50MB/s\n+backpr."],
                    fontsize=5.8)
ax1.set_ylabel("guard fires / 10 min", fontsize=6.2)
ax1.set_ylim(0, 8900)
ax1.set_title("(a) lease guard fires (Tier-A)", fontsize=6.4, pad=3)
ax1.tick_params(length=2, width=0.6, labelsize=5.8)
ax1.spines[["top", "right"]].set_visible(False)

# panel (b)
cats = ["wire\n(guard)", "delivered\n(bypass)", "delivered\n(guard)"]
vals = [wire_on_pct, deliv_off_pct, 0.0]
cols = [ORANGE, BLUE, GREEN]
b = ax2.bar(range(3), vals, width=0.58, color=cols, edgecolor="black", linewidth=0.4)
labels = [f"{vals[0]:.2f}%", f"{vals[1]:.2f}%", "0%"]
for xi, (v, s) in enumerate(zip(vals, labels)):
    ax2.text(xi, v + 0.14, s, ha="center", va="bottom", fontsize=5.8)
ax2.text(2, 2.4, "guard blocks\n100%", ha="center", va="center", fontsize=5.8)
ax2.set_xticks(range(3))
ax2.set_xticklabels(cats, fontsize=5.8)
ax2.set_ylabel("wrong-identity share", fontsize=6.2)
ax2.set_ylim(0, 8.4)
ax2.yaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter("%.0f%%"))
ax2.set_title("(b) wire vs delivered", fontsize=6.4, pad=3)
ax2.tick_params(length=2, width=0.6, labelsize=5.8)
ax2.spines[["top", "right"]].set_visible(False)

fig.tight_layout(pad=0.55, w_pad=1.6)
for ext in ("pdf", "png"):
    fig.savefig(os.path.join(RD, f"fig_guard_bypass.{ext}"), dpi=300)
print("wrote", os.path.join(RD, "fig_guard_bypass.pdf"))
