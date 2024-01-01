#!/usr/bin/env python3
"""Generate the Phase 4 config matrix (plan section 6).

Two documented variants (see results/NOTES.md):
  * baseline: plan-faithful (er=0.1, paced pressure) -- boundary/negative arm
  * evictaggr: eviction-aggressive regime calibrated by DoD runs 2-6
    (er=0.5, B soft-pin, max-rate backpressure) -- the regime where the
    evict->realloc->overwrite chain synchronizes inside the overstay window

Matrix per variant: TTL {1000,5000,11000} x concurrency {32,64,128} x
seed {42,43,44}. tc rates are deployment conditions from the window
formula (backlog = c*3.5MB must exceed TTL*rate) and are recorded per run.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

BASE = {
    "tier": "A",
    # Env-var placeholders, expanded by driver.load_config via os.path.expandvars:
    #   MOONCAKE_BUILD = Mooncake build dir containing mooncake-store/src/mooncake_master
    #   RPE_LAB        = this rpe_lab/ directory
    "master_bin": "$MOONCAKE_BUILD/mooncake-store/src/mooncake_master",
    "rpc_port": 50051,
    "metrics_port": 9003,
    "master_server_addr": "127.0.0.1:50051",
    "local_hostname": "localhost",
    "global_segment_size": 2147483648,
    "local_buffer_size": 16777216,
    "eviction_high_watermark_ratio": 0.5,
    "allow_evict_soft_pinned": True,
    "object_size": 3670016,
    "hot_objects": 430,
    "duration_s": 7200,
    "trace_path": "$RPE_LAB/trace/BurstGPT_1.csv",
    "trace_start": 1154000,
    "trace_start_b": 90000,
    "trace_requests": 30000,
    "trace_speedup": 10.0,
    "tested_commit_file": "$RPE_LAB/TESTED_COMMIT.txt",
}

# tc rate per TTL so the queue depth crosses the TTL boundary (window
# formula: c*3.5MB / rate > TTL at c=64), calibrated by DoD runs 2-5.
TC_BY_TTL = {1000: "800mbit", 5000: "300mbit", 11000: "150mbit"}

VARIANTS = {
    "baseline": {"eviction_ratio": 0.1},
    "evictaggr": {"eviction_ratio": 0.5, "soft_pin_bulk": True,
                  "pressure_max_rate": True, "pressure_workers": 8},
}

n = 0
for variant, extra in VARIANTS.items():
    for ttl in (1000, 5000, 11000):
        for conc in (32, 64, 128):
            for seed in (42, 43, 44):
                run_id = f"tierA_{variant}_ttl{ttl}_c{conc}_seed{seed}"
                cfg = dict(BASE)
                cfg.update(extra)
                cfg.update(run_id=run_id, ttl_ms=ttl, concurrency=conc,
                           seed=seed, tc=TC_BY_TTL[ttl],
                           notes=f"Tier-A {variant} cell ttl={ttl} c={conc} seed={seed}")
                with open(os.path.join(HERE, f"{run_id}.yaml"), "w") as f:
                    json.dump(cfg, f, indent=2)
                n += 1
print(f"wrote {n} tierA configs ({len(VARIANTS)} variants x 27 cells)")
