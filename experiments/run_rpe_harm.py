#!/usr/bin/env python3
"""Stale-byte harm surrogate for Reclaimed-Payload Exposure (RPE).

MECHANISTIC SIMULATION — NO REAL LLM. The artifact contains no LLM, tokenizer,
or NIAH/RULER/logits infrastructure, so this script quantifies the *harm of
stale KV bytes* with an honest, fully seeded mechanistic surrogate. Every
number below is produced by the pseudo-random model described here, not by a
real model run.

Model (paper's serving abstraction):
  * One 64K-token context = 1024 chunks x 64 tokens. Serving promotes chunks
    through the CXL pool at 64 KiB chunk granularity, so the stale-*byte*
    fraction equals the stale-*chunk* fraction: with stale fraction s, each
    promoted chunk is independently replaced by a wrong-incarnation chunk.
  * s in {0, 1, 5, 11, 14, 25}%. The 11%/14% points deliberately bracket the
    paper's measured unmitigated RPE band of 11.2-14.4% (see
    docs/RESULT_ALIGNMENT.md).

Metrics (all per s, common random numbers across s so the stale sets are
nested — chunk c is stale at level s iff u_c < s with one shared u per trial):

  A. NIAH retrieval: a needle key-value fact lives in one uniformly random
     chunk per trial. Retrieval SUCCEEDS iff the needle chunk is fresh (a
     stale needle chunk means the fact's bytes are physically absent).
     Mechanism counters are recorded and we assert failures == stale-needle
     events exactly.
  B. RULER-style multi-hop variable tracking: a chain of h=8 assignments in 8
     distinct uniformly random chunks; the final answer is correct iff ALL 8
     tracked chunks are fresh. The dashed reference curve in the figure is the
     independent-freshness bound (1-s)^8.
  C. Output-distribution KL: proxy logits. Each chunk c has a fixed random
     contribution vector v_c in R^512 (seeded); the output distribution is
         p = softmax( (1/Z) * sum_c a_c v_c ),  Z = sum_c a_c,
     with attention weights a_c ∝ exp(sim_c / tau). sim_c are seeded Gaussian
     query-key affinities; the needle chunk is boosted +4.0 and its two
     neighbours +3.5 so attention peaks on the fact and its local context.
     tau is calibrated once (fixed calibration seed) as the LARGEST value in
     TAU_GRID whose mean top-3 attention mass is >= 0.90; the chosen tau and
     the realized top-3 mass are recorded in the JSON config. Stale chunks
     have v_c replaced by independent wrong-incarnation vectors. Attention
     weights are metadata-driven (query x advertised keys) and are therefore
     identical for the clean and corrupt contexts — the surrogate models that
     the serving stack cannot see that the payload bytes are wrong.
     We report KL(p_clean || p_corrupt) in nats (mean + 95% CI). The mean
     curve must be non-decreasing in s; if it is not, we add batches of
     n_trials and re-average until it is (final n_trials documented).

Outputs:
  * results/rpe_harm.json                      (machine-readable tables)
  * experiments/out/rpe_harm/fig_rpe_harm.pdf  (3 panels)
  * experiments/out/rpe_harm/fig_rpe_harm.png  (+ PNG copy in results/)
"""
from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np

# Make the package importable when run directly (no install / PYTHONPATH needed).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simcxl_ext.io_utils import C  # shared colour palette + rcParams

REPO = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO / "results"
FIG_DIR = REPO / "experiments" / "out" / "rpe_harm"

PROVENANCE = ("MECHANISTIC SIMULATION: stale-byte harm surrogate computed from "
              "seeded pseudo-random vectors/indicators. No real LLM, tokenizer, "
              "or NIAH/RULER benchmark infrastructure was used; numbers quantify "
              "the mechanism, not a specific model.")

BASE_SEED = 20260717           # fixed RNG seed base for full reproducibility
N_CHUNKS = 1024                # chunks per 64K-token context
CHUNK_TOKENS = 64              # tokens per chunk
CONTEXT_TOKENS = N_CHUNKS * CHUNK_TOKENS
CHUNK_BYTES = 64 * 1024        # 64 KiB chunk granularity
S_LIST_PCT = [0, 1, 5, 11, 14, 25]
N_TRIALS = 2000                # trials per s (per batch for metric C)
H_CHAIN = 8                    # RULER-style chain length
KL_DIM = 512                   # proxy logit dimension
TAU_GRID = [2.0, 1.5, 1.0, 0.75, 0.5, 0.35, 0.25]   # largest satisfying tau wins
TOP3_TARGET = 0.90             # required mean top-3 attention mass
MAX_KL_BATCHES = 5             # escalation cap for the KL monotonicity rule
PAPER_RPE_BAND_PCT = (11.2, 14.4)   # measured unmitigated RPE band (paper)

# Seed-stream ids per metric (common random numbers across s within a trial).
_STREAM = {"niah": 1, "ruler": 2, "kl": 3, "calib": 999}


def _rng(metric: str, trial: int) -> np.random.Generator:
    return np.random.default_rng([BASE_SEED, _STREAM[metric], trial])


def _log_softmax(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    m = x.max()
    return x - m - math.log(float(np.exp(x - m).sum()))


def _attention_weights(rng: np.random.Generator, tau: float):
    """Seeded query-key affinities peaked on the needle chunk + 2 neighbours."""
    sims = rng.standard_normal(N_CHUNKS)
    needle = int(rng.integers(N_CHUNKS))
    hi = sims.max()
    sims[needle] = hi + 4.0
    sims[(needle - 1) % N_CHUNKS] = hi + 3.5
    sims[(needle + 1) % N_CHUNKS] = hi + 3.5
    w = np.exp((sims - sims.max()) / tau)
    return w / w.sum(), needle


def _calibrate_tau(n_calib: int = 128):
    """Pick the largest tau whose mean top-3 attention mass is >= TOP3_TARGET."""
    means = {}
    for tau in TAU_GRID:
        masses = []
        for t in range(n_calib):
            a, _ = _attention_weights(_rng("calib", t), tau)
            masses.append(float(np.sort(a)[-3:].sum()))
        means[tau] = float(np.mean(masses))
    for tau in TAU_GRID:  # descending: first hit is the largest valid tau
        if means[tau] >= TOP3_TARGET:
            return tau, means[tau], means
    return TAU_GRID[-1], means[TAU_GRID[-1]], means


def _ci95_prop(p: float, n: int) -> float:
    return 1.96 * math.sqrt(max(p * (1.0 - p), 0.0) / max(n, 1))


def _ci95_mean(x: np.ndarray) -> float:
    if x.shape[0] < 2:
        return 0.0
    return 1.96 * float(x.std(ddof=1)) / math.sqrt(x.shape[0])


# ---------------------------------------------------------------- Metric A
def run_niah(s_fracs, n_trials: int):
    pts = [dict(success=0, failures=0, stale_needle=0, realized=0.0)
           for _ in s_fracs]
    succ = np.zeros((n_trials, len(s_fracs)))   # per-trial outcomes (for batches)
    for t in range(n_trials):
        rng = _rng("niah", t)
        u = rng.random(N_CHUNKS)
        needle = int(rng.integers(N_CHUNKS))
        for i, s in enumerate(s_fracs):
            stale = u < s
            p = pts[i]
            p["realized"] += float(stale.mean())
            if stale[needle]:
                p["failures"] += 1
                p["stale_needle"] += 1
            else:
                p["success"] += 1
                succ[t, i] = 1.0
    out = []
    for i, (s_pct, p) in enumerate(zip(S_LIST_PCT, pts)):
        # Mechanism identity: every failure must be a stale-needle event.
        assert p["failures"] == p["stale_needle"], "NIAH mechanism counter mismatch"
        mean = p["success"] / n_trials
        out.append({
            "s_pct": s_pct,
            "mean": mean,
            "ci95": _ci95_prop(mean, n_trials),
            "n_trials": n_trials,
            "batch_means": succ[:, i].reshape(-1, 100).mean(axis=1).tolist(),
            "counters": {
                "trials": n_trials,
                "successes": p["success"],
                "failures": p["failures"],
                "stale_needle_events": p["stale_needle"],
                "realized_stale_chunk_fraction": p["realized"] / n_trials,
            },
        })
    return out


# ---------------------------------------------------------------- Metric B
def run_ruler(s_fracs, n_trials: int):
    pts = [dict(correct=0, incorrect=0, any_stale=0, realized=0.0)
           for _ in s_fracs]
    ok_arr = np.zeros((n_trials, len(s_fracs)))
    for t in range(n_trials):
        rng = _rng("ruler", t)
        u = rng.random(N_CHUNKS)
        tracked = rng.choice(N_CHUNKS, size=H_CHAIN, replace=False)
        for i, s in enumerate(s_fracs):
            stale = u < s
            p = pts[i]
            p["realized"] += float(stale.mean())
            if bool(stale[tracked].any()):
                p["incorrect"] += 1
                p["any_stale"] += 1
            else:
                p["correct"] += 1
                ok_arr[t, i] = 1.0
    out = []
    for i, (s_pct, p) in enumerate(zip(S_LIST_PCT, pts)):
        assert p["incorrect"] == p["any_stale"], "RULER mechanism counter mismatch"
        assert p["correct"] + p["incorrect"] == n_trials
        mean = p["correct"] / n_trials
        out.append({
            "s_pct": s_pct,
            "mean": mean,
            "ci95": _ci95_prop(mean, n_trials),
            "n_trials": n_trials,
            "reference_(1-s)^8": (1.0 - s_pct / 100.0) ** H_CHAIN,
            "batch_means": ok_arr[:, i].reshape(-1, 100).mean(axis=1).tolist(),
            "counters": {
                "trials": n_trials,
                "correct": p["correct"],
                "incorrect": p["incorrect"],
                "chains_with_any_stale_chunk": p["any_stale"],
                "realized_stale_chunk_fraction": p["realized"] / n_trials,
            },
        })
    return out


# ---------------------------------------------------------------- Metric C
def _kl_trial(t: int, s_fracs, tau: float):
    """One trial: KL(p_clean || p_corrupt) at every s, common random numbers."""
    rng = _rng("kl", t)
    a, needle = _attention_weights(rng, tau)
    V = rng.standard_normal((N_CHUNKS, KL_DIM), dtype=np.float32)
    W = rng.standard_normal((N_CHUNKS, KL_DIM), dtype=np.float32)  # wrong incarnation
    u = rng.random(N_CHUNKS)
    logits0 = a @ V
    logp = _log_softmax(logits0)
    p = np.exp(logp)
    DV = np.asarray(W - V, dtype=np.float64)
    top3 = np.zeros(N_CHUNKS, dtype=bool)
    top3[needle] = top3[(needle - 1) % N_CHUNKS] = top3[(needle + 1) % N_CHUNKS] = True
    kls = np.empty(len(s_fracs))
    n_stale = np.empty(len(s_fracs), dtype=int)
    n_stale_top3 = np.empty(len(s_fracs), dtype=int)
    for i, s in enumerate(s_fracs):
        stale = u < s
        n_stale[i] = int(stale.sum())
        n_stale_top3[i] = int((stale & top3).sum())
        logits = logits0 + (a * stale) @ DV if stale.any() else logits0
        logq = _log_softmax(logits)
        kls[i] = max(0.0, float(p @ (logp - logq)))
    return kls, n_stale, n_stale_top3, float(np.sort(a)[-3:].sum())


def run_kl(s_fracs, n_trials: int, tau: float):
    """Mean KL per s; escalate trial batches until the mean is non-decreasing."""
    kls, stale_ct, stale_top3_ct, top3_mass = [], [], [], []
    n_done, escalated = 0, False
    for batch in range(MAX_KL_BATCHES):
        for t in range(n_done, n_done + n_trials):
            k, ns, nt, m3 = _kl_trial(t, s_fracs, tau)
            kls.append(k)
            stale_ct.append(ns)
            stale_top3_ct.append(nt)
            top3_mass.append(m3)
        n_done += n_trials
        arr = np.asarray(kls)
        means = arr.mean(axis=0)
        if np.all(np.diff(means) >= -1e-12):
            break
        escalated = True  # mean not yet non-decreasing: add another batch
    else:
        raise AssertionError(f"KL mean not non-decreasing after {n_done} trials")
    arr = np.asarray(kls)
    stale_arr = np.asarray(stale_ct)
    top3_arr = np.asarray(stale_top3_ct)
    out = []
    for i, s_pct in enumerate(S_LIST_PCT):
        col = arr[:, i]
        nb = int(arr.shape[0] // 100)
        batches = col[: nb * 100].reshape(nb, 100).mean(axis=1) if nb else col
        out.append({
            "s_pct": s_pct,
            "mean": float(col.mean()),
            "ci95": _ci95_mean(col),
            "n_trials": int(arr.shape[0]),
            "batch_means": [float(b) for b in batches],
            "counters": {
                "trials": int(arr.shape[0]),
                "mean_stale_chunks": float(stale_arr[:, i].mean()),
                "mean_stale_top3_attention_chunks": float(top3_arr[:, i].mean()),
                "realized_stale_chunk_fraction":
                    float(stale_arr[:, i].mean()) / N_CHUNKS,
            },
        })
    meta = {
        "final_n_trials": int(arr.shape[0]),
        "escalated_beyond_requested": escalated and arr.shape[0] > n_trials,
        "mean_top3_attention_mass": float(np.mean(top3_mass)),
    }
    return out, meta


# ------------------------------------------------------------------ driver
def run(n_trials: int = N_TRIALS) -> dict:
    s_fracs = [s / 100.0 for s in S_LIST_PCT]
    tau, calib_mass, calib_all = _calibrate_tau()
    niah = run_niah(s_fracs, n_trials)
    ruler = run_ruler(s_fracs, n_trials)
    kl, kl_meta = run_kl(s_fracs, n_trials, tau)

    def monotone(points, direction):
        m = [p["mean"] for p in points]
        d = np.diff(m)
        ok = bool(np.all(d <= 1e-12)) if direction == "nonincreasing" \
            else bool(np.all(d >= -1e-12))
        return {"direction": direction, "ok": ok}

    return {
        "provenance": PROVENANCE,
        "config": {
            "base_seed": BASE_SEED,
            "context_tokens": CONTEXT_TOKENS,
            "n_chunks": N_CHUNKS,
            "chunk_tokens": CHUNK_TOKENS,
            "chunk_bytes": CHUNK_BYTES,
            "s_list_pct": S_LIST_PCT,
            "n_trials_requested": n_trials,
            "h_chain": H_CHAIN,
            "kl_dim": KL_DIM,
            "paper_rpe_band_pct": list(PAPER_RPE_BAND_PCT),
            "attention": {
                "form": "a_c ∝ exp(sim_c/tau); p = softmax((1/Z) Σ_c a_c v_c), Z = Σ a",
                "sim": "seeded Gaussian q·k affinities; needle +4.0, 2 neighbours +3.5",
                "tau": tau,
                "tau_grid": TAU_GRID,
                "top3_target": TOP3_TARGET,
                "calibrated_top3_mass": calib_mass,
                "calibrated_top3_mass_all_tau": {str(k): v for k, v in calib_all.items()},
            },
        },
        "metrics": {
            "niah": {
                "description": "NIAH retrieval success (needle chunk fresh)",
                "unit": "success probability",
                "points": niah,
                "monotonicity": monotone(niah, "nonincreasing"),
            },
            "ruler": {
                "description": "RULER-style 8-hop variable-tracking accuracy "
                               "(all 8 tracked chunks fresh)",
                "unit": "accuracy",
                "points": ruler,
                "monotonicity": monotone(ruler, "nonincreasing"),
            },
            "kl": {
                "description": "KL(p_clean || p_corrupt) of proxy output logits",
                "unit": "nats",
                "points": kl,
                "monotonicity": dict(monotone(kl, "nondecreasing"), **kl_meta),
            },
        },
    }


def report(res: dict) -> None:
    m = res["metrics"]
    print("=" * 78)
    print("RPE stale-byte harm surrogate  (MECHANISTIC SIMULATION — no real LLM)")
    print("=" * 78)
    hdr = f"{'s (%)':>7} | {'mean':>10} {'95% CI':>9} | {'n':>6} | mechanism counters"
    print("\nMetric A — NIAH retrieval success (fails iff needle chunk stale)")
    print(hdr)
    print("-" * 78)
    for p in m["niah"]["points"]:
        c = p["counters"]
        print(f"{p['s_pct']:>7} | {p['mean']:>10.4f} {p['ci95']:>9.4f} | "
              f"{p['n_trials']:>6} | failures == stale-needle == {c['failures']}")
    print("\nMetric B — RULER-style 8-hop tracking accuracy (all 8 chunks fresh)")
    print(hdr)
    print("-" * 78)
    for p in m["ruler"]["points"]:
        c = p["counters"]
        print(f"{p['s_pct']:>7} | {p['mean']:>10.4f} {p['ci95']:>9.4f} | "
              f"{p['n_trials']:>6} | incorrect == any-stale == {c['incorrect']} "
              f"(ref (1-s)^8={p['reference_(1-s)^8']:.4f})")
    print("\nMetric C — output-distribution KL(p_clean || p_corrupt), nats")
    print(hdr)
    print("-" * 78)
    for p in m["kl"]["points"]:
        c = p["counters"]
        print(f"{p['s_pct']:>7} | {p['mean']:>10.4f} {p['ci95']:>9.4f} | "
              f"{p['n_trials']:>6} | mean stale top-3 chunks "
              f"{c['mean_stale_top3_attention_chunks']:.3f}")
    print("-" * 78)
    for name in ("niah", "ruler", "kl"):
        mo = m[name]["monotonicity"]
        extra = (f", final n_trials={mo['final_n_trials']}"
                 if name == "kl" else "")
        print(f"monotonicity[{name}]: {mo['direction']} -> "
              f"{'OK' if mo['ok'] else 'VIOLATED'}{extra}")
    print(f"attention tau={res['config']['attention']['tau']} "
          f"(top-3 mass target {TOP3_TARGET}); "
          f"realized top-3 mass={m['kl']['monotonicity']['mean_top3_attention_mass']:.4f}")


def plot(res: dict):
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.size": 10.5, "font.weight": "bold",
        "axes.labelsize": 11, "axes.labelweight": "bold",
        "axes.titlesize": 11, "axes.titleweight": "bold",
        "xtick.labelsize": 9.5, "ytick.labelsize": 9.5,
        "legend.fontsize": 8.5, "lines.linewidth": 2.4,
        "axes.linewidth": 1.2,
    })
    s = [p["s_pct"] for p in res["metrics"]["niah"]["points"]]
    band_label = "measured unmitigated RPE band (11.2–14.4%)"

    def _panel(ax, points, color, marker, ylabel, title, ylim=None):
        # real per-batch variability behind the mean curve (measured data)
        for i, p in enumerate(points):
            bm = np.asarray(p.get("batch_means", [p["mean"]]))
            jw = 0.55 if i > 0 else 0.0
            xs = p["s_pct"] + np.linspace(-jw, jw, bm.size)
            ax.scatter(xs, bm, s=7, color=color, alpha=0.30, zorder=1,
                       linewidths=0)
        ax.errorbar(s, [p["mean"] for p in points],
                    yerr=[p["ci95"] for p in points], marker=marker,
                    markersize=6.5, color=color, capsize=3, elinewidth=1.6,
                    zorder=3, label="surrogate")
        ax.set_xlabel("stale-byte fraction s (%)")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        if ylim:
            ax.set_ylim(*ylim)
        ax.axvspan(PAPER_RPE_BAND_PCT[0], PAPER_RPE_BAND_PCT[1],
                   color=C["accent1"], alpha=0.12)
        ax.grid(alpha=0.25, linewidth=0.6)

    # main-text figure: two panels (needle retrieval + output-distribution
    # shift); the multi-hop panel moves to the supplementary figure below.
    # No in-figure legend: the LaTeX caption explains the band and the curve.
    fig, axes = plt.subplots(1, 2, figsize=(6.4, 2.5))
    _panel(axes[0], res["metrics"]["niah"]["points"], C["fts"], "o",
           "NIAH retrieval success", "(a) needle-in-a-haystack", (0.0, 1.08))
    _panel(axes[1], res["metrics"]["kl"]["points"], C["accent2"], "^",
           "KL (nats)", "(b) output-distribution shift")
    fig.tight_layout()

    # supplementary figure: RULER-style multi-hop panel with (1-s)^8 reference
    fig_s, ax_s = plt.subplots(1, 1, figsize=(4.0, 2.8))
    d = res["metrics"]["ruler"]["points"]
    _panel(ax_s, d, C["fts"], "s", "8-hop tracking accuracy",
           "RULER-style multi-hop (supplementary)", (0.0, 1.08))
    xs = np.linspace(0, max(s), 200)
    ax_s.plot(xs, (1 - xs / 100.0) ** H_CHAIN, ls="--", lw=2.0,
              color=C["oracle"], label=r"$(1-s)^8$ reference")
    ax_s.legend(loc="upper right", framealpha=0.9)
    fig_s.tight_layout()
    return fig, fig_s


def main() -> None:
    try:  # keep table output printable on non-UTF-8 Windows consoles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    res = run()
    report(res)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    jpath = RESULTS_DIR / "rpe_harm.json"
    with jpath.open("w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    fig, fig_s = plot(res)
    fig.savefig(FIG_DIR / "fig_rpe_harm.pdf", bbox_inches="tight")
    fig.savefig(FIG_DIR / "fig_rpe_harm.png", bbox_inches="tight")
    shutil.copy(FIG_DIR / "fig_rpe_harm.png", RESULTS_DIR / "fig_rpe_harm.png")
    fig_s.savefig(FIG_DIR / "fig_rpe_harm_ruler_supp.pdf", bbox_inches="tight")
    fig_s.savefig(FIG_DIR / "fig_rpe_harm_ruler_supp.png", bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    plt.close(fig_s)
    print(f"\nSaved: {jpath}")
    print(f"Saved: {FIG_DIR / 'fig_rpe_harm.pdf'} (+ .png; PNG copy in results/)")
    print(f"Saved: {FIG_DIR / 'fig_rpe_harm_ruler_supp.pdf'} (+ .png)")


if __name__ == "__main__":
    main()
