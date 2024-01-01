#!/usr/bin/env python3
"""Compose the 1×2 single-column figure fig_baseline_summary.{pdf,svg,png}.

Panel (a): valid-throughput vs stale-payload scatter (paired-bootstrap 95% CIs;
log stale axis with a plotting floor for exact-zero results).
Panel (b): hand-drawn coordination-cost micro-matrix (+RTT, Pin/xfer, Ctl+hdr,
Queue reclaim).

The two panels are arranged side-by-side so the figure fits a single text
column (~3.4 in) and remains legible without down-scaling or zooming.

ALL numbers come from results/baselines/summary_aggregate.csv — the same
dataframe the CSV is built from. Nothing is hand-filled here. Pre-plot
assertions (spec §XIV) guard the mechanism-level conclusions and refuse to emit
a PDF if a mechanism misbehaves.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.baselines.baseline_common import (
    METHOD_ORDER, METHOD_LABELS, SEGMENT_SIZES,
)

RESULTS = ROOT / "results" / "baselines"
AGG_CSV = RESULTS / "summary_aggregate.csv"
FIG_DIR = ROOT / "figures"

SEG_METHODS = [f"Segmented-{s}" for s in SEGMENT_SIZES]


def load_aggregate(path: Path) -> Dict[str, Dict[str, object]]:
    rows: Dict[str, Dict[str, object]] = {}
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows[r["method"]] = r
    return rows


def _f(d: Dict[str, object], k: str) -> float:
    return float(d[k])


# ── pre-plot assertions (spec §XIV) ─────────────────────────────────────────
def verify(agg: Dict[str, Dict[str, object]]) -> None:
    """Refuse to plot if the mechanism-level conclusions are not upheld."""
    problems: List[str] = []

    # universal sanity
    if not (0.98 <= _f(agg["NoCheck"], "normalized_throughput_gmean") <= 1.02):
        problems.append("NoCheck normalized throughput is not ~1.0")
    for m in [m for m in METHOD_ORDER if m in agg]:
        if _f(agg[m], "stale_mib_per_gib") < 0:
            problems.append(f"{m} negative stale")
        if _f(agg[m], "pin_span_ratio_median") < 0:
            problems.append(f"{m} negative pin span")
        if _f(agg[m], "control_header_overhead_pct") < 0:
            problems.append(f"{m} negative overhead")
    seg_sizes = [int(float(agg[m]["segment_bytes"])) for m in SEG_METHODS]
    if seg_sizes != SEGMENT_SIZES:
        problems.append(f"segment sizes {seg_sizes} != {SEGMENT_SIZES}")

    # mechanism correctness (zero-RPE + coordination expectations)
    for m in ("SharedRef", "TwoPhase", "PROSE"):
        if _f(agg[m], "stale_mib_per_gib") != 0.0:
            problems.append(f"{m} stale must be exactly 0")
        if int(float(agg[m]["rpe_events"])) != 0:
            problems.append(f"{m} rpe_events must be 0")
    if int(float(agg["TwoPhase"]["extra_rtt"])) != 1:
        problems.append("TwoPhase extra_rtt must be 1")
    if int(float(agg["PROSE"]["extra_rtt"])) != 0:
        problems.append("PROSE extra_rtt must be 0")
    if agg["SharedRef"]["queue_reclaim"] != "N":
        problems.append("SharedRef queue_reclaim must be N")
    if agg["TwoPhase"]["queue_reclaim"] != "N":
        problems.append("TwoPhase queue_reclaim must be N")
    if agg["PROSE"]["queue_reclaim"] != "Y":
        problems.append("PROSE queue_reclaim must be Y")

    if problems:
        print("VERIFICATION FAILED — not emitting PDF:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        raise SystemExit(2)
    print("Pre-plot assertions passed.")


# ── marker table (shape + colour) ───────────────────────────────────────────
# Colour distinguishes methods in panel (a) (shape is still the primary cue so
# the figure survives greyscale printing). The panel-(b) matrix stays greyscale.
MARKERS = {
    "NoCheck":   dict(marker="x", ms=5.5,  mfc="none",     mec="#d62728", mew=1.3),
    "SharedRef": dict(marker="s", ms=5.2,  mfc="#1f77b4",  mec="#0d3b66", mew=0.7),
    "TwoPhase":  dict(marker="D", ms=5.2,  mfc="#2ca02c",  mec="#14591f", mew=0.7),
    "GenOnly":   dict(marker="^", ms=6.0,  mfc="#ff7f0e",  mec="#7a3d00", mew=0.8),
    "GenOnlyEpochFence": dict(marker=">", ms=6.0, mfc="#e66101", mec="#5e2700", mew=0.8),
    "RDMAKey":   dict(marker="v", ms=6.0,  mfc="#9467bd",  mec="#4b2d66", mew=0.8),
    "PROSE":     dict(marker="*", ms=11.0, mfc="#ffcc00",  mec="black",   mew=0.9),
}
SEG_MARKER = dict(marker="o", ms=4.6, mfc="#8c564b", mec="#4a2c26", mew=0.6)


def setup_rc():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib as mpl
    # Single-column figure: fonts are sized to be legible at ~3.5in native width
    # WITHOUT any further downscaling, and set bold so the serif face survives
    # small print sizes.
    mpl.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "DejaVu Serif", "serif"],
        "font.weight": "bold",
        "mathtext.fontset": "dejavuserif",
        "mathtext.default": "bf",
        "font.size": 8.6,
        "axes.labelsize": 9.4,
        "axes.labelweight": "bold",
        "axes.titlesize": 10.0,
        "axes.titleweight": "bold",
        "xtick.labelsize": 8.4,
        "ytick.labelsize": 8.4,
        "legend.fontsize": 8.4,
        "axes.linewidth": 0.9,
        "lines.linewidth": 1.1,
        "xtick.major.width": 0.8,
        "ytick.major.width": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "pdf.fonttype": 42,     # TrueType/Type-42, avoid Type-3
        "ps.fonttype": 42,
        "svg.fonttype": "none",
    })


def panel_a(ax, agg: Dict[str, Dict[str, object]]) -> None:
    """(a) Normalized valid throughput (x) vs stale MiB/GiB (y, log).

    This is NOT labeled a Pareto frontier: PROSE is dominated in these two
    displayed dimensions by RefCnt/2Phase (they retain more objects by pinning
    at enqueue). The decisive PROSE advantages live in panel (b).
    """
    # zero-floor for log axis: min nonzero stale / 5.
    nonzero = [ _f(agg[m], "stale_mib_per_gib") for m in METHOD_ORDER
                if m in agg and _f(agg[m], "stale_mib_per_gib") > 0 ]
    min_nonzero = min(nonzero) if nonzero else 1.0
    zero_floor = min_nonzero / 5.0

    def yval(v: float) -> float:
        return v if v > 0 else zero_floor

    # exact-zero reference line, labeled once (no redundant per-point "0" tags)
    ax.axhline(zero_floor, ls=":", lw=0.7, color="0.55", zorder=1)

    # segmented connecting line (S64 -> S256 -> S4K -> S16K)
    sx = [_f(agg[m], "normalized_throughput_gmean") for m in SEG_METHODS]
    sy = [yval(_f(agg[m], "stale_mib_per_gib")) for m in SEG_METHODS]
    ax.plot(sx, sy, "-", color="0.45", lw=0.9, zorder=2)

    # RefCnt and 2Phase sit at effectively identical (throughput, stale)
    # coordinates with overlapping CIs, so they share ONE annotation reading
    # "RefCnt / 2Phase" while both markers and both matrix rows are preserved.
    def _coincident(a: str, b: str) -> bool:
        xa = _f(agg[a], "normalized_throughput_gmean")
        xb = _f(agg[b], "normalized_throughput_gmean")
        sa = _f(agg[a], "stale_mib_per_gib")
        sb = _f(agg[b], "stale_mib_per_gib")
        return abs(xa - xb) < 5e-3 and sa == sb

    merge_refcnt_2phase = _coincident("SharedRef", "TwoPhase")
    # every method is annotated exactly once; if merged, the shared call is
    # anchored on SharedRef and TwoPhase is skipped in the per-method loop.
    skip_annot = {"TwoPhase"} if merge_refcnt_2phase else set()

    for m in [m for m in METHOD_ORDER if m in agg]:
        x = _f(agg[m], "normalized_throughput_gmean")
        s = _f(agg[m], "stale_mib_per_gib")
        y = yval(s)
        xlo = _f(agg[m], "normalized_throughput_ci_low")
        xhi = _f(agg[m], "normalized_throughput_ci_high")
        style = SEG_MARKER if m in SEG_METHODS else MARKERS[m]
        # x error bar (throughput CI); y error bar only when stale is nonzero.
        # Thin error bars sit BELOW the markers (markers at zorder>=4).
        yerr = None
        if s > 0:
            slo = max(_f(agg[m], "stale_ci_low"), zero_floor)
            shi = _f(agg[m], "stale_ci_high")
            yerr = [[y - slo], [shi - y]]
        ax.errorbar([x], [y], xerr=[[x - xlo], [xhi - x]], yerr=yerr,
                    ecolor="0.5", elinewidth=0.75, capsize=2.0, capthick=0.75,
                    zorder=1, ls="none", marker="none")
        ax.plot([x], [y], zorder=4, ls="none", **style)
        # short label with a thin leader line (annotate) so no label overlaps a
        # marker, an error bar, or the panel title.
        if m in skip_annot:
            continue
        # Two-line merged label keeps the annotation compact horizontally in
        # the narrow 1×2 layout.
        label = "RefCnt /\n2Phase" if (m == "SharedRef" and
                                        merge_refcnt_2phase) else METHOD_LABELS[m]
        dx, dy, ha = _label_offset(m)
        ax.annotate(
            label, xy=(x, y), textcoords="offset points", xytext=(dx, dy),
            fontsize=8.4, fontweight="bold", ha=ha, va="center", zorder=6,
            arrowprops=dict(arrowstyle="-", lw=0.55, color="0.45",
                            shrinkA=1.0, shrinkB=2.0),
        )

    ax.set_yscale("log")
    ax.tick_params(axis="y", pad=3)
    ax.set_xlabel("Normalized valid throughput")
    ax.set_ylabel("Stale payload\n(MiB/GiB)", labelpad=1)
    ax.set_title("(a) Throughput vs. stale", fontsize=9.6,
                 fontweight="bold", loc="left", pad=1)
    ax.grid(True, which="major", color="0.85", lw=0.4, zorder=0)
    ax.set_axisbelow(True)

    # "exact zero" annotation on the floor line, once. A blended transform pins
    # it to the left of the axes (x in axes fraction) and to the floor value
    # (y in data coords) so it sits just above the dotted line and never crosses
    # the y-axis tick labels. Extra left headroom keeps S64/S256 labels in-axes.
    import matplotlib.transforms as mtransforms
    x_lo = min(_f(agg[m], "normalized_throughput_ci_low") for m in METHOD_ORDER if m in agg)
    x_hi = max(_f(agg[m], "normalized_throughput_ci_high") for m in METHOD_ORDER if m in agg)
    ax.set_xlim(x_lo - 0.100, x_hi + 0.005)
    blended = mtransforms.blended_transform_factory(ax.transAxes, ax.transData)
    ax.text(0.45, zero_floor * 1.16, "exact zero", transform=blended,
            fontsize=7.2, fontweight="bold", va="bottom", ha="center",
            color="0.30", zorder=6)


def _label_offset(method: str):
    """Per-method annotation offset (dx pt, dy pt, ha) with short leader lines.

    Directions chosen to keep every label inside the axes and clear of markers
    and error bars (see review): Unsafe upper-left, GenOnly/RKey pushed apart,
    2Phase lower-right but inside, segment labels away from their error bars.
    """
    # Narrow single-column axes: the three zero-stale points (RefCnt/2Phase,
    # PROSE) sit on the floor at the right; label them UPWARD into the empty
    # lower-right space so nothing runs off the right edge. The mid-cluster
    # (GenOnly/RKey and the segment chain) fans left/right and up/down.
    table = {
        # Unsafe and the segment chain stay on the LEFT; the x-axis lower limit
        # is padded so they clear the y-axis. GenOnly/RKey/PROSE/RefCnt fan out
        # on the RIGHT with enough vertical separation to avoid overlap.
        "NoCheck":   (10, 4, "left"),      # Unsafe: right & up, clear of y-axis
        # The three zero-stale points (PROSE at x~118, RefCnt/2Phase merged at
        # x~151) crowd the bottom-right. Stack BOTH labels straight up into the
        # empty mid-right band, side by side, so their vertical leaders stay
        # separated (x~118 vs x~151) and neither leader crosses the other label.
        # A right-anchored label here would overflow into panel (b)'s matrix.
        "SharedRef": (8, 44, "center"),    # RefCnt/2Phase: up-right, two-line
        "TwoPhase":  (12, -9, "left"),     # only used if NOT merged (branched)
        "GenOnly":   (10, 1, "left"),      # right & just above point, clear of Unsafe
        "RDMAKey":   (10, -11, "left"),    # right & below point
        "Segmented-16384": (-12, 7, "right"),  # left & up
        "Segmented-4096":  (-11, 1, "right"),  # left side
        "Segmented-256":   (-9, 0, "right"),   # left side
        "Segmented-64":    (-10, -10, "right"), # bottom point: left & down
        "PROSE":     (-4, 30, "center"),   # up, left of RefCnt/2Phase leader
    }
    return table.get(method, (12, 7, "left"))


def panel_b(ax, agg: Dict[str, Dict[str, object]]) -> None:
    """(b) Hand-drawn coordination-cost micro-matrix.

    Rows = PANEL_B_ORDER (the S256/S4K segmented rows are omitted from the
    matrix for space; the panel-(a) sweep line still spans 64 B -> 16 KiB),
    columns = +RTT / Pin/xfer / Ctl+hdr(%) / Queue reclaim.
    Cells carry the exact number; a light grey background encodes magnitude for
    Pin/xfer and Ctl+hdr (0 stays white), and Queue-reclaim uses the Y/N glyph.
    """
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    # Place the panel label manually at the top-left of the axes so it does not
    # collide with the rotated column headers.
    ax.text(0.0, 0.965, "(b)", transform=ax.transAxes,
            fontsize=9.6, fontweight="bold", va="bottom", ha="left")

    col_headers = [("+RTT", None), ("Pin/xfer", None),
                   ("Ctl+hdr (%)", None), ("Queue recl.", None)]
    rows = [m for m in METHOD_ORDER
            if m in agg and m not in ("Segmented-256", "Segmented-4096")]
    n_rows = len(rows)
    # geometry (axes fraction) — compressed horizontally for the narrow 1×2 slot
    x_label_w = 0.10
    x0 = x_label_w
    col_w = (1.0 - x_label_w) / len(col_headers)
    y_top = 0.78
    y_bot = 0.035
    row_h = (y_top - y_bot) / n_rows

    def cx(j):
        return x0 + (j + 0.5) * col_w

    def ry(i):
        return y_top - (i + 0.5) * row_h

    # column headers
    for j, (h, _) in enumerate(col_headers):
        ax.text(cx(j), y_top + 0.070, h, ha="center", va="bottom",
                fontsize=6.4, fontweight="bold", rotation=90)
    ax.text(x0 * 0.5, y_top + 0.052, "", ha="center")

    # magnitude ranges for shading (over the shown rows)
    pin_vals = [_f(agg[m], "pin_span_ratio_median") for m in rows]
    ovh_vals = [_f(agg[m], "control_header_overhead_pct") for m in rows]
    pin_max = max(pin_vals) or 1.0
    ovh_max = max(ovh_vals) or 1.0

    # Colour magnitude scale (was greyscale): 0 stays white, larger values move
    # from pale toward a saturated warm hue so "more coordination cost" reads at
    # a glance. Kept on a single hue ramp so it still degrades gracefully in B&W.
    import matplotlib.colors as mcolors
    _cost_cmap = mcolors.LinearSegmentedColormap.from_list(
        "cost", ["#ffffff", "#fee0b6", "#fdae61", "#e34a33", "#a50f15"])

    def shade(val, vmax):
        if val <= 0:
            return "white"
        return mcolors.to_hex(_cost_cmap(0.18 + 0.82 * (val / vmax)))

    for i, m in enumerate(rows):
        y = ry(i)
        # row label (method), never rotated. ONLY the PROSE label is bold — the
        # numeric cells stay in regular weight so the table does not read as
        # author-guided; readers reach the conclusion from the numbers.
        is_prose = (m == "PROSE")
        ax.text(x0 - 0.008, y, METHOD_LABELS[m], ha="right", va="center",
                fontsize=7.6, fontweight="bold" if is_prose else "normal")
        rtt = int(float(agg[m]["extra_rtt"]))
        pin = _f(agg[m], "pin_span_ratio_median")
        ovh = _f(agg[m], "control_header_overhead_pct")
        qr = agg[m]["queue_reclaim"]

        # Queue-reclaim is now colour-coded (Y = green, N = red) in addition to
        # the literal Y/N glyph, so it still reads if printed greyscale.
        cells = [
            (f"{rtt:d}", "white"),
            (f"{pin:.2f}" if pin > 0 else "0", shade(pin, pin_max)),
            (f"{ovh:.1f}", shade(ovh, ovh_max)),
            (qr, "#b7e4c7" if qr == "Y" else "#f4a8a8"),
        ]
        for j, (txt, bg) in enumerate(cells):
            x = x0 + j * col_w
            rect = _cell_rect(x, y - row_h / 2, col_w, row_h, bg)
            ax.add_patch(rect)
            ax.text(cx(j), y, txt, ha="center", va="center", fontsize=7.6)
        # a thin coloured outline around the PROSE row to draw the eye
        if is_prose:
            from matplotlib.patches import Rectangle
            ax.add_patch(Rectangle((x0, y - row_h / 2), 1.0 - x0, row_h,
                                   facecolor="none", edgecolor="#1a5fb4",
                                   lw=1.3, zorder=4))

    # thin separators
    for j in range(len(col_headers) + 1):
        xx = x0 + j * col_w
        ax.plot([xx, xx], [y_bot, y_top], color="0.8", lw=0.4, zorder=1)
    ax.plot([x0, 1.0], [y_top, y_top], color="0.6", lw=0.6)
    ax.plot([x0, 1.0], [y_bot, y_bot], color="0.6", lw=0.6)

    # Definition of Pin/xfer is in the emitted caption; omit the in-panel
    # footnote in the narrow 1×2 layout so the table is not clipped.


def _cell_rect(x, y, w, h, facecolor):
    from matplotlib.patches import Rectangle
    return Rectangle((x, y), w, h, facecolor=facecolor, edgecolor="none",
                     zorder=0.5)


def compose(agg: Dict[str, Dict[str, object]]) -> Path:
    setup_rc()
    import matplotlib.pyplot as plt

    # Single-column 1×2 layout: both panels sit side-by-side inside a ~3.4in
    # wide figure so it drops into one text column with no downscaling. The
    # scatter (a) gets a little more horizontal room; the matrix (b) is packed
    # into a narrower slot while keeping all text readable.
    fig = plt.figure(figsize=(3.40, 2.52))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.12, 0.88], wspace=0.20,
                          left=0.085, right=0.970, top=0.885, bottom=0.150)
    ax_a = fig.add_subplot(gs[0, 0])
    ax_b = fig.add_subplot(gs[0, 1])
    panel_a(ax_a, agg)
    panel_b(ax_b, agg)

    FIG_DIR.mkdir(parents=True, exist_ok=True)
    pdf = FIG_DIR / "fig_baseline_summary.pdf"
    # Fixed canvas (no bbox_inches="tight") so the figure stays exactly 3.40in wide.
    fig.savefig(pdf)
    fig.savefig(FIG_DIR / "fig_baseline_summary.svg")
    fig.savefig(FIG_DIR / "fig_baseline_summary.png", dpi=300)
    _check_layout(fig, ax_a)
    plt.close(fig)
    _check_pdf_bbox(pdf)
    return pdf


def _check_layout(fig, ax_a) -> None:
    """Renderer-based layout audit of panel (a) annotation TEXT.

    Verifies (via drawn window extents): every method label appears exactly
    once, no label text falls outside the figure canvas, and no two label TEXT
    boxes overlap. The leader-line arrows are excluded from the audit because
    their bounding boxes are much larger than the text itself in this narrow
    1×2 layout. Raises SystemExit(4) on any violation.
    """
    from matplotlib.text import Annotation
    from matplotlib.transforms import Bbox
    renderer = fig.canvas.get_renderer()
    fig_bbox = fig.bbox

    def _text_bbox(ann: Annotation) -> Bbox:
        """Return the annotation's text-only bbox in display coordinates."""
        xy_disp = ax_a.transData.transform(ann.xy)
        offset = ann.xyann
        anchor = xy_disp + offset
        layout_bbox, _, _ = ann._get_layout(renderer)
        pts = layout_bbox.get_points() + anchor
        return Bbox(pts)

    labels = []  # (text, bbox)
    for child in ax_a.get_children():
        if isinstance(child, Annotation):
            bb = _text_bbox(child)
            labels.append((child.get_text(), bb))

    problems = []

    # 1) no duplicated label text (each method annotated exactly once)
    seen = {}
    for txt, _ in labels:
        seen[txt] = seen.get(txt, 0) + 1
    dups = [t for t, c in seen.items() if c > 1]
    if dups:
        problems.append(f"duplicated annotation labels: {dups}")

    # 2) no label outside the figure bounding box
    pad = 0.5  # px tolerance
    for txt, bb in labels:
        if (bb.x0 < fig_bbox.x0 - pad or bb.y0 < fig_bbox.y0 - pad or
                bb.x1 > fig_bbox.x1 + pad or bb.y1 > fig_bbox.y1 + pad):
            problems.append(f"label {txt!r} extends outside the figure bounds")

    # 2b) no label spilling past panel (a)'s right spine — the earlier check only
    # guarded the whole-figure canvas, so a label could overflow into panel (b)'s
    # matrix (this is what caused the RefCnt/2Phase overlap). Keep labels inside
    # the panel-(a) axes horizontally, with a small tolerance.
    ax_bbox = ax_a.get_window_extent()
    edge_pad = 2.0  # px tolerance past the spine
    for txt, bb in labels:
        if bb.x1 > ax_bbox.x1 + edge_pad:
            problems.append(
                f"label {txt!r} spills past panel (a) right edge into panel (b)")

    # 3) no overlapping label bounding boxes
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            (ti, bi), (tj, bj) = labels[i], labels[j]
            if bi.overlaps(bj):
                inter = bi.intersection(bi, bj)
                if inter is not None and inter.width > 1.0 and inter.height > 1.0:
                    problems.append(f"labels {ti!r} and {tj!r} overlap")

    if problems:
        print("LAYOUT CHECK FAILED:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        raise SystemExit(4)
    print(f"Layout check passed ({len(labels)} annotations, no dup/overlap/clip).")


def _check_pdf_bbox(pdf: Path) -> None:
    """Auto-verify the emitted PDF: vector, embedded font, correct dimensions."""
    import re
    data = pdf.read_bytes()
    problems = []
    if b"/Subtype/Image" in data or b"/Subtype /Image" in data:
        problems.append("PDF contains a raster image XObject (must be vector)")
    if b"/Type3" in data:
        problems.append("PDF uses Type-3 fonts (must be Type-42/TrueType)")
    if b"/FontFile2" not in data and b"/FontFile3" not in data:
        problems.append("PDF has no embedded font")
    m = re.search(rb"/MediaBox\s*\[([^\]]+)\]", data)
    if m:
        w, h = [float(v) for v in m.group(1).split()][2:4]
        if abs(w - 3.40 * 72) > 1.0:
            problems.append(f"width {w/72:.3f}in != 3.40in")
        if h > 2.60 * 72 + 1.0:
            problems.append(f"height {h/72:.3f}in exceeds 2.60in")
    if problems:
        print("PDF BBOX/FONT CHECK FAILED:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        raise SystemExit(3)
    print("PDF bbox/font check passed (vector, embedded font, 3.40x2.52in).")


def write_caption() -> Path:
    """Emit a caption draft next to the plotting script (spec §XVI)."""
    cap = (
        "Comparison under identical request-arrival, scheduling, and "
        "eviction-attempt traces; mechanisms may accept, defer, or reject the "
        "same eviction attempts. (a) Valid payload throughput normalized within "
        "each workload and seed to the Unsafe design, versus stale payload "
        "traffic; error bars are 95% paired-bootstrap confidence intervals. "
        "Exact-zero measurements are placed at the plotting floor. Connected "
        "circles sweep cancelable-DMA segment sizes from 64 B to 16 KiB. "
        "(b) Additional serialized round trips, protection duration normalized "
        "to payload duration (Pin/xfer), control/header traffic, and whether the "
        "endpoint may reclaim an object while its request queues.\n"
    )
    path = Path(__file__).resolve().parent / "fig_baseline_summary.caption.txt"
    path.write_text(cap, encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agg-csv", type=Path, default=AGG_CSV)
    args = ap.parse_args()
    agg = load_aggregate(args.agg_csv)
    verify(agg)
    pdf = compose(agg)
    cap = write_caption()
    print(f"Figure : {pdf}")
    print(f"SVG    : {pdf.with_suffix('.svg')}")
    print(f"PNG    : {pdf.with_suffix('.png')}")
    print(f"Caption: {cap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
