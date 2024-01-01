"""Shared plotting / JSON-IO helpers for the SimCXL-extension experiments.

Output is written under ``artifact/experiments/out/{data,figures}`` so a
reproduction run leaves all generated tables and figures in one place.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# experiments/ lives one level above simcxl_ext/ once installed in-tree; we
# resolve relative to this file so the helper works regardless of CWD.
_PKG_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = _PKG_ROOT / "experiments" / "out"
DATA_DIR = OUT_DIR / "data"
FIG_DIR = OUT_DIR / "figures"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FIG_DIR.mkdir(parents=True, exist_ok=True)

plt.rcParams.update({
    "figure.dpi":         150,
    "savefig.dpi":        300,
    "font.family":        "serif",
    "font.serif":         ["DejaVu Serif", "Times New Roman", "serif"],
    "font.size":          16,
    "axes.titlesize":     18,
    "axes.labelsize":     16,
    "legend.fontsize":    13,
    "legend.frameon":     False,
    "xtick.labelsize":    13,
    "ytick.labelsize":    13,
    "axes.spines.top":    False,
    "axes.spines.right":  False,
    "axes.linewidth":     1.5,
    "lines.linewidth":    2.5,
    "lines.markersize":   8,
})

# Colour-blind-friendly palette, keyed by enforcement boundary.
C = {
    "fts":     "#d62728",
    "sw_host": "#ff7f0e",
    "sw_gpu":  "#bcbd22",
    "iommu":   "#17becf",
    "cefe":    "#2ca02c",
    "oracle":  "#7f7f7f",
    "accent1": "#1f77b4",
    "accent2": "#9467bd",
}


def save_json(name: str, obj: Dict[str, Any]) -> Path:
    """Write ``obj`` as pretty JSON to ``data/<name>.json`` and return its path."""
    path = DATA_DIR / f"{name}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    return path


def save_fig(fig, name: str) -> Path:
    """Save ``fig`` as PNG (and PDF) under ``figures/`` and return the PNG path."""
    png = FIG_DIR / f"{name}.png"
    fig.savefig(png, bbox_inches="tight")
    fig.savefig(FIG_DIR / f"{name}.pdf", bbox_inches="tight")
    plt.close(fig)
    return png
