#!/usr/bin/env python3
"""RPE-lab workload driver (plan Phase 3).

Subcommands:
  smoke     Phase 1 DoD: put/get 100 objects, force eviction, probe evicted key
  seed      put the tenant-A hot set (identity-header payloads) + write ledger
  victim    tenant A: replay BurstGPT arrivals as Get workload
  pressure  tenant B: replay BurstGPT arrivals as Put workload (pool pressure)
  run       orchestrate one full run: master + seed + victim + pressure,
            then aggregate results/<run_id>.json per plan section 8

Design notes:
  * Two tenant processes (victim / pressure) as required by the plan; `run`
    spawns them as subprocesses.
  * Gets use get_into() with a pre-allocated buffer so the raw int64 return
    code is visible: -707 (LEASE_EXPIRED, guard fire) vs -704
    (OBJECT_NOT_FOUND, evicted) are counted separately.
  * Every successful Get is header-validated (success_mismatch must stay 0).
  * C++ passive patch (Phase 2) writes discard events to $RPE_LAB_EVENTS;
    this driver only reads that file during aggregation, never writes to it.
  * stdlib only. Config files are JSON syntax in .yaml files (JSON is YAML).
"""

import argparse
import ctypes
import csv
import json
import os
import queue
import random
import signal
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rpe_header as rh  # noqa: E402

LEASE_EXPIRED = -707        # guard fire (plan: guard_fires)
OBJECT_NOT_FOUND = -704     # evicted / never existed (counted separately)

HOT_OBJECTS = 430           # ~1.5 GB at 3.5 MB
OBJ_SIZE = 3_670_016        # 3.5 MB: Qwen2.5-7B 128K ctx / 64-token chunk
POOL_BYTES = 4 * 1024**3    # 2 tenants x 2 GB


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)  # configs are JSON-syntax .yaml
    # Configs reference portable env-var placeholders ($MOONCAKE_BUILD,
    # $RPE_LAB); expand them here so every subcommand sees real paths.
    for key in ("master_bin", "trace_path", "tested_commit_file"):
        if isinstance(cfg.get(key), str):
            cfg[key] = os.path.expandvars(cfg[key])
    return cfg


def now_ns():
    return time.time_ns()


# ---------------------------------------------------------------- trace ----
class Trace:
    """BurstGPT replay: IAT sequence + context lengths, de-identified."""

    def __init__(self, path, start_row, n, speedup, seed):
        self.rows = []
        with open(path, newline="") as f:
            rd = csv.reader(f)
            header = next(rd)
            col = {name: i for i, name in enumerate(header)}
            tcol, scol, lcol = col["Timestamp"], col.get("Session ID"), col["Total tokens"]
            for i, row in enumerate(rd):
                if i < start_row:
                    continue
                if len(self.rows) >= n:
                    break
                try:
                    self.rows.append((float(row[tcol]),
                                      row[scol] if scol is not None else str(i),
                                      int(float(row[lcol]))))
                except (ValueError, IndexError):
                    continue
        if not self.rows:
            raise SystemExit(f"trace window empty: {path} start_row={start_row}")
        t0 = self.rows[0][0]
        self.arrivals = [(t - t0) / speedup for t, _, _ in self.rows]  # seconds
        rng = random.Random(seed)
        self.session_hash = [rh.fnv1a64(s.encode()) ^ rng.getrandbits(64)
                             for _, s, _ in self.rows]
        self.ctx_lens = [l for _, _, l in self.rows]


# ---------------------------------------------------------------- client ---
def make_store(tenant_name, cfg):
    from mooncake.store import MooncakeDistributedStore
    store = MooncakeDistributedStore()
    rc = store.setup(local_hostname=cfg.get("local_hostname", "localhost"),
                     metadata_server="P2PHANDSHAKE",
                     global_segment_size=cfg["global_segment_size"],
                     local_buffer_size=cfg.get("local_buffer_size", 16 * 1024**2),
                     protocol="tcp",
                     rdma_devices="",
                     master_server_addr=cfg["master_server_addr"])
    if rc != 0:
        raise SystemExit(f"{tenant_name}: setup failed rc={rc}")
    return store


class JsonlWriter:
    def __init__(self, path):
        self.f = open(path, "a", buffering=1)
        self.lock = threading.Lock()

    def write(self, obj):
        line = json.dumps(obj, separators=(",", ":"))
        with self.lock:
            self.f.write(line + "\n")


# ------------------------------------------------------------------ seed ---
def seed_hot_set(store, cfg):
    """Put the tenant-A hot set from THIS store instance and write the ledger.

    Must run inside the long-lived tenant process: a client that exits
    unmounts its segment and its share of the hot set would be lost.
    With cfg["hard_pin_hot_set"] every object is Put with
    ReplicateConfig.with_hard_pin=True (Phase 5: protection covers the
    whole transfer, objects become unevictable).
    """
    pin_cfg = None
    if cfg.get("hard_pin_hot_set") or cfg.get("soft_pin_hot_set"):
        from mooncake.store import ReplicateConfig
        pin_cfg = ReplicateConfig()
        pin_cfg.with_hard_pin = bool(cfg.get("hard_pin_hot_set"))
        pin_cfg.with_soft_pin = bool(cfg.get("soft_pin_hot_set"))
    size = cfg.get("object_size", OBJ_SIZE)
    n = cfg.get("hot_objects", HOT_OBJECTS)
    ledger = {}
    t0 = time.time()
    for i in range(n):
        key = f"hot/{i:04d}"
        payload = rh.make_payload(rh.TENANT_A, key, 1, now_ns(), size)
        rc = store.put(key, payload, pin_cfg) if pin_cfg else store.put(key, payload)
        if rc != 0:
            log(f"seed put {key} rc={rc} (stopping seed at {i})")
            break
        ledger[key] = {"gen": 1, "size": size, "put_ts_ns": now_ns()}
        if (i + 1) % 50 == 0:
            log(f"seeded {i + 1}/{n}")
    out = cfg["ledger_path"]
    with open(out, "w") as f:
        json.dump({"run_id": cfg["run_id"], "tenant": rh.TENANT_A,
                   "object_size": size, "keys": ledger}, f)
    log(f"seed done: {len(ledger)} keys, {time.time() - t0:.1f}s -> {out}")
    return ledger


def wait_for_ledger(cfg):
    """Pressure tenant: hold puts until the victim finished seeding."""
    deadline = time.monotonic() + cfg.get("seed_wait_s", 600)
    while time.monotonic() < deadline:
        if os.path.exists(cfg["ledger_path"]):
            return
        time.sleep(2)
    raise SystemExit("ledger did not appear in time")


def cmd_seed(args):
    cfg = load_config(args.config)
    store = make_store("seed", cfg)
    seed_hot_set(store, cfg)
    store.close()


# ---------------------------------------------------------------- victim ---
def cmd_victim(args):
    cfg = load_config(args.config)
    store = make_store("victim", cfg)
    if not os.path.exists(cfg["ledger_path"]):
        # tenant A seeds its own hot set from its long-lived process
        seed_hot_set(store, cfg)
    with open(cfg["ledger_path"]) as f:
        ledger = json.load(f)["keys"]
    hot_keys = sorted(ledger.keys())
    size = cfg.get("object_size", OBJ_SIZE)
    tr = Trace(cfg["trace_path"], cfg.get("trace_start", 0),
               cfg.get("trace_requests", 20000), cfg.get("trace_speedup", 100.0),
               cfg["seed"])
    out = JsonlWriter(cfg["victim_log"])
    stop = threading.Event()
    deadline = time.monotonic() + cfg["duration_s"]
    next_idx = 0
    idx_lock = threading.Lock()
    ledger_lock = threading.Lock()
    reseed_q = queue.Queue()
    stats = {"gets_total": 0, "gets_ok": 0, "guard_fires": 0,
             "not_found": 0, "other_err": 0, "success_mismatch": 0,
             "delivered_wrong_events": 0, "delivered_wrong_bytes": 0,
             "bytes_ok": 0, "reseeds": 0, "reseed_failures": 0,
             "mismatch_detail": []}
    stats_lock = threading.Lock()

    def bump(**kw):
        with stats_lock:
            for k, v in kw.items():
                stats[k] += v

    def reseed_worker():
        """Re-put evicted hot keys with the next generation (cache-reload
        pattern: a missing KV chunk is recomputed and re-stored)."""
        pin_cfg = None
        if cfg.get("hard_pin_hot_set") or cfg.get("soft_pin_hot_set"):
            from mooncake.store import ReplicateConfig
            pin_cfg = ReplicateConfig()
            pin_cfg.with_hard_pin = bool(cfg.get("hard_pin_hot_set"))
            pin_cfg.with_soft_pin = bool(cfg.get("soft_pin_hot_set"))
        while not stop.is_set() or not reseed_q.empty():
            try:
                key = reseed_q.get(timeout=0.5)
            except queue.Empty:
                continue
            with ledger_lock:
                ent = ledger.get(key)
                new_gen = (ent["gen"] + 1) if ent else 1
            payload = rh.make_payload(rh.TENANT_A, key, new_gen, now_ns(), size)
            rc = store.put(key, payload, pin_cfg) if pin_cfg else store.put(key, payload)
            with ledger_lock:
                ent = ledger.get(key)
                if ent is not None:
                    ent["reseed_pending"] = False
                    if rc == 0:
                        ent["gen"] = new_gen
            bump(reseeds=1 if rc == 0 else 0,
                 reseed_failures=0 if rc == 0 else 1)
            out.write({"ts_ns": now_ns(), "type": "reseed", "key": key,
                       "gen": new_gen, "rc": rc})

    threading.Thread(target=reseed_worker, daemon=True).start()

    def worker(wid):
        nonlocal next_idx
        buf = ctypes.create_string_buffer(size)
        ptr = ctypes.addressof(buf)
        while not stop.is_set() and time.monotonic() < deadline:
            with idx_lock:
                i = next_idx
                next_idx += 1
            if i >= len(tr.arrivals):
                break
            due = cfg_start + tr.arrivals[i]
            while not stop.is_set():  # sleep until due, never drop a claimed arrival
                remaining = due - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.5))
            if stop.is_set() or time.monotonic() >= deadline:
                break
            key = hot_keys[tr.session_hash[i] % len(hot_keys)]
            with ledger_lock:
                exp_gen = ledger[key]["gen"]
            t_lookup = now_ns()
            rc = store.get_into(key, ptr, size)
            t_done = now_ns()
            rec = {"ts_ns": t_done, "key": key, "rc": rc, "exp_gen": exp_gen,
                   "t_lookup_ns": t_lookup, "t_done_ns": t_done,
                   "ctx_len": tr.ctx_lens[i], "worker": wid}
            if rc == size:
                bump(gets_total=1, gets_ok=1, bytes_ok=rc)
                chk = rh.check_payload(buf, key, exp_gen)
                with ledger_lock:
                    cur_gen = ledger[key]["gen"]
                # red line = buffer is not a coherent generation of this key:
                # head/tail markers must agree with each other and belong to
                # {exp_gen, cur_gen} (a reseed landing before the lookup
                # legitimately serves the newer gen)
                kh = rh.key_hash(key)
                good = (chk["found_magic"] and chk["tail_magic"]
                        and chk["found_key_hash"] == kh
                        and chk["tail_key_hash"] == kh
                        and chk["found_gen"] == chk["tail_gen"]
                        and chk["found_gen"] in (exp_gen, cur_gen))
                if not good:
                    if cfg.get("guard_bypass"):
                        # Tier-U: expected baseline -- wrong bytes "delivered"
                        # to the checker (count-and-quarantine), not a defect
                        bump(delivered_wrong_events=1, delivered_wrong_bytes=rc)
                    else:
                        bump(success_mismatch=1)
                    with stats_lock:
                        stats["mismatch_detail"].append(
                            {"key": key, "cur_gen": cur_gen, **chk})
                    rec["delivered_wrong" if cfg.get("guard_bypass")
                        else "success_mismatch"] = True
                    rec.update({k: chk[k] for k in ("found_magic", "found_tenant",
                                                    "found_key_hash", "found_gen",
                                                    "tail_magic", "tail_tenant",
                                                    "tail_key_hash", "tail_gen")})
            elif rc == LEASE_EXPIRED:
                bump(gets_total=1, guard_fires=1)
            elif rc == OBJECT_NOT_FOUND:
                bump(gets_total=1, not_found=1)
                with ledger_lock:
                    ent = ledger.get(key)
                    if ent is not None and not ent.get("reseed_pending"):
                        ent["reseed_pending"] = True
                        reseed_q.put(key)
            else:
                bump(gets_total=1, other_err=1)
            out.write(rec)

    cfg_start = time.monotonic()
    threads = [threading.Thread(target=worker, args=(w,), daemon=True)
               for w in range(cfg["concurrency"])]
    for t in threads:
        t.start()
    try:
        while time.monotonic() < deadline and any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    stop.set()
    for t in threads:
        t.join(timeout=5)
    stats["duration_s"] = time.monotonic() - cfg_start
    with open(cfg["victim_stats"], "w") as f:
        json.dump(stats, f, indent=2)
    log(f"victim done: {stats}")
    if stats["success_mismatch"] > 0:
        log("RED LINE: success_mismatch > 0 -- race beat the guard, stop and disclose")
    store.close()


# --------------------------------------------------------------- pressure ---
def cmd_pressure(args):
    cfg = load_config(args.config)
    store = make_store("pressure", cfg)
    wait_for_ledger(cfg)  # hold puts until tenant A finished seeding
    tr = Trace(cfg["trace_path"], cfg.get("trace_start_b", 500000),
               cfg.get("trace_requests", 20000), cfg.get("trace_speedup", 100.0),
               cfg["seed"] + 1)
    out = JsonlWriter(cfg["pressure_log"])
    size = cfg.get("object_size", OBJ_SIZE)
    deadline = time.monotonic() + cfg["duration_s"]
    stats = {"puts_total": 0, "puts_ok": 0, "put_failures_pool_full": 0,
             "other_err": 0, "bytes_put": 0}
    stats_lock = threading.Lock()
    stop = threading.Event()
    pin_cfg = None
    if cfg.get("soft_pin_bulk"):
        # tenant B marks its bulk objects soft-pinned: they leave the
        # pass-1/2 eviction candidate list, so eviction under pressure is
        # forced into tenant A's expired-lease objects (workload-side knob,
        # stock ReplicateConfig feature; master flags untouched).
        from mooncake.store import ReplicateConfig
        pin_cfg = ReplicateConfig()
        pin_cfg.with_soft_pin = True
    seq = 0
    seq_lock = threading.Lock()
    start = time.monotonic()

    def put_one():
        nonlocal seq
        with seq_lock:
            key = f"bulk/{cfg['seed']}/{seq:08d}"
            seq += 1
        payload = rh.make_payload(rh.TENANT_B, key, 1, now_ns(), size)
        t0 = now_ns()
        return key, t0, store.put(key, payload, pin_cfg) if pin_cfg \
            else store.put(key, payload)

    def record(key, t0, rc):
        with stats_lock:
            stats["puts_total"] += 1
            if rc == 0:
                stats["puts_ok"] += 1
                stats["bytes_put"] += size
            elif rc == OBJECT_NOT_FOUND or rc <= -700:
                stats["put_failures_pool_full"] += 1
            else:
                stats["other_err"] += 1
        out.write({"ts_ns": t0, "key": key, "rc": rc})

    def pressure_worker():
        # sustained backpressure: put as fast as the store accepts
        while time.monotonic() < deadline and not stop.is_set():
            key, t0, rc = put_one()
            record(key, t0, rc)

    if cfg.get("pressure_max_rate"):
        threads = [threading.Thread(target=pressure_worker, daemon=True)
                   for _ in range(cfg.get("pressure_workers", 8))]
        for t in threads:
            t.start()
        try:
            while time.monotonic() < deadline and any(t.is_alive() for t in threads):
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        stop.set()
        for t in threads:
            t.join(timeout=5)
    else:
        for i in range(len(tr.arrivals)):
            if time.monotonic() > deadline:
                break
            due = start + tr.arrivals[i]
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, 5.0))
                if time.monotonic() < due:
                    pass  # run late rather than dropping pressure
            key, t0, rc = put_one()
            record(key, t0, rc)
    stats["duration_s"] = time.monotonic() - start
    with open(cfg["pressure_stats"], "w") as f:
        json.dump(stats, f, indent=2)
    log(f"pressure done: {stats}")
    store.close()


# ------------------------------------------------------------------ smoke ---
def cmd_smoke(args):
    """Phase 1 DoD: put/get 100 objects, exceed high watermark, observe eviction."""
    cfg = load_config(args.config)
    store = make_store("smoke", cfg)
    size = cfg.get("object_size", OBJ_SIZE)
    ok = True

    # DoD-1: put 100, get all
    for i in range(100):
        key = f"smoke/{i:03d}"
        if store.put(key, rh.make_payload(rh.TENANT_A, key, 1, now_ns(), size)) != 0:
            log(f"FAIL: put {key}"); ok = False; break
    got = 0
    for i in range(100):
        key = f"smoke/{i:03d}"
        data = store.get(key)
        if len(data) == size and rh.check_payload(data, key, 1)["match"]:
            got += 1
    log(f"DoD-1 put/get 100: got {got}/100 {'OK' if got == 100 else 'FAIL'}")
    ok = ok and got == 100

    # DoD-2: push pool past high watermark, check eviction counters on :metrics_port
    # smoke uses a single client: pool = this client's own segment
    pool = cfg["global_segment_size"]
    hi = cfg.get("eviction_high_watermark_ratio", 0.5) * pool
    target = int(hi * 1.3)
    written = 0
    i = 0
    while written < target:
        key = f"fill/{i:05d}"
        rc = store.put(key, rh.make_payload(rh.TENANT_A, key, 1, now_ns(), size))
        if rc == 0:
            written += size
        else:
            time.sleep(0.5)  # let eviction catch up
        i += 1
        if i > 4 * target // size:
            log("FAIL: could not fill pool"); ok = False; break
    time.sleep(3)  # eviction thread scans every 10 ms
    fill_attempts = i
    ev = {}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg.get('metrics_port', 9003)}/metrics",
                                    timeout=5) as r:
            for line in r.read().decode().splitlines():
                if "eviction" in line and not line.startswith("#"):
                    ev[line.split("{")[0].split(" ")[0]] = line.rsplit(" ", 1)[-1]
    except Exception as e:
        log(f"metrics scrape failed: {e}")
    log(f"DoD-2 eviction metrics: {ev}")
    succ = [v for k, v in ev.items() if "successful_evictions" in k]
    dod2 = any(float(v) > 0 for v in succ)
    log(f"DoD-2 eviction counter > 0: {'OK' if dod2 else 'FAIL'}")
    ok = ok and dod2

    # DoD-3: get an evicted key -> OBJECT_NOT_FOUND class error, distinct from -707.
    # Eviction picked fill/ keys as victims (smoke/ keys survived); probe both.
    missing = None
    for i in range(100):
        key = f"smoke/{i:03d}"
        if store.is_exist(key) == 0:
            missing = key
            break
    if missing is None:
        for j in range(fill_attempts):
            key = f"fill/{j:05d}"
            if store.is_exist(key) == 0:
                missing = key
                break
    if missing:
        buf = ctypes.create_string_buffer(size)
        rc = store.get_into(missing, ctypes.addressof(buf), size)
        log(f"DoD-3 evicted key {missing}: get_into rc={rc} "
            f"({'OK, distinct from -707' if rc == OBJECT_NOT_FOUND else 'rc=' + str(rc)})")
        ok = ok and rc == OBJECT_NOT_FOUND
    else:
        log("DoD-3: no smoke key was evicted (check watermark) FAIL"); ok = False
    store.close()
    log(f"SMOKE {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


# --------------------------------------------------------------------- run ---
def scrape_master_metrics(cfg):
    """Pull master eviction/storage counters from the admin HTTP endpoint."""
    out = {}
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cfg.get('metrics_port', 9003)}/metrics",
                timeout=5) as r:
            for line in r.read().decode().splitlines():
                if line.startswith("#"):
                    continue
                for key in ("master_successful_evictions_total",
                            "master_attempted_evictions_total",
                            "master_mem_successful_evictions_total",
                            "master_mem_attempted_evictions_total",
                            "master_put_start_alloc_failures_total"):
                    if line.startswith(key):
                        out[key] = float(line.rsplit(" ", 1)[-1])
    except Exception as e:
        out["scrape_error"] = str(e)
    return out


def cmd_run(args):
    cfg = load_config(args.config)
    run_id = cfg["run_id"]
    rdir = os.path.join(HERE, "results")
    os.makedirs(rdir, exist_ok=True)
    for k, v in {"victim_log": f"req_victim_{run_id}.jsonl",
                 "pressure_log": f"req_pressure_{run_id}.jsonl",
                 "victim_stats": f"stats_victim_{run_id}.json",
                 "pressure_stats": f"stats_pressure_{run_id}.json",
                 "ledger_path": f"ledger_{run_id}.json"}.items():
        cfg.setdefault(k, os.path.join(rdir, v))
    cfg_path = os.path.join(rdir, f"config_{run_id}.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)

    env = dict(os.environ)
    env["RPE_LAB_EVENTS"] = os.path.join(rdir, f"events_{run_id}.jsonl")
    env["RPE_LAB_RUN_ID"] = run_id
    if cfg.get("guard_bypass"):
        # Tier-U: measurement-only guard bypass (count-and-quarantine).
        # Wrong bytes reach only the driver's checker buffer, never a real
        # consumer. Master untouched; stock behavior when env is unset.
        env["RPE_LAB_BYPASS_GUARD"] = "1"

    master_log = os.path.join(rdir, f"master_{run_id}.log")
    master_env = dict(env)
    # master-side passive eviction census (Experiment 1): per-key evict events
    master_env["RPE_LAB_EVENTS_EVICT"] = os.path.join(rdir, f"evict_{run_id}.jsonl")
    master = subprocess.Popen(
        [cfg["master_bin"],
         f"--rpc_port={cfg.get('rpc_port', 50051)}",
         f"--eviction_high_watermark_ratio={cfg.get('eviction_high_watermark_ratio', 0.5)}",
         f"--eviction_ratio={cfg.get('eviction_ratio', 0.1)}",
         f"--default_kv_lease_ttl={cfg['ttl_ms']}",
         f"--allow_evict_soft_pinned_objects={1 if cfg.get('allow_evict_soft_pinned', True) else 0}"],
        stdout=open(master_log, "w"), stderr=subprocess.STDOUT, env=master_env)
    log(f"master pid={master.pid} log={master_log}")
    time.sleep(cfg.get("master_warmup_s", 3))
    cfg["_metrics_start"] = scrape_master_metrics(cfg)
    if cfg.get("tc"):
        subprocess.run(["sudo", "-n", "tc", "qdisc", "add", "dev", "lo",
                        "root", "netem", "rate", cfg["tc"]], check=True)
        log(f"tc netem rate {cfg['tc']} applied on lo")

    # time series of master metrics (Phase 5 reclaimable-capacity analysis)
    metrics_stop = threading.Event()

    def metrics_loop():
        path = os.path.join(rdir, f"metrics_{run_id}.jsonl")
        with open(path, "a") as f:
            while not metrics_stop.is_set():
                row = {"ts": time.time()}
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{cfg.get('metrics_port', 9003)}/metrics",
                            timeout=5) as r:
                        for line in r.read().decode().splitlines():
                            if line.startswith("#") or not line.strip():
                                continue
                            name = line.split("{")[0].split(" ")[0]
                            try:
                                row[name] = float(line.rsplit(" ", 1)[-1])
                            except ValueError:
                                pass
                except Exception:
                    pass
                f.write(json.dumps(row) + "\n")
                f.flush()
                metrics_stop.wait(cfg.get("metrics_interval_s", 10))

    mt = threading.Thread(target=metrics_loop, daemon=True)
    mt.start()

    procs = []
    try:
        for sub in ("victim", "pressure"):  # victim seeds its own hot set first
            procs.append(subprocess.Popen([sys.executable, __file__, sub, "--config", cfg_path], env=env))
            log(f"started {sub} pid={procs[-1].pid}")
        deadline = time.monotonic() + cfg["duration_s"] + 60
        while time.monotonic() < deadline and any(p.poll() is None for p in procs):
            time.sleep(5)
    finally:
        metrics_stop.set()
        mt.join(timeout=3)
        if cfg.get("tc"):
            subprocess.run(["sudo", "-n", "tc", "qdisc", "del", "dev", "lo", "root"],
                           capture_output=True)
            log("tc rule removed")
        cfg["_metrics_end"] = scrape_master_metrics(cfg)
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        if master.poll() is None:
            master.send_signal(signal.SIGINT)
            try:
                master.wait(timeout=10)
            except subprocess.TimeoutExpired:
                master.kill()

    aggregate(cfg, rdir)


def aggregate(cfg, rdir):
    run_id = cfg["run_id"]
    def load(p, d):
        try:
            with open(p) as f:
                return json.load(f)
        except OSError:
            return d
    vs = load(cfg["victim_stats"], {})
    ps = load(cfg["pressure_stats"], {})

    events_path = os.path.join(rdir, f"events_{run_id}.jsonl")
    ledger = load(cfg.get("ledger_path", ""), {}).get("keys", {})
    # per-request expected generation, keyed for joining with discard events
    reqs_by_key = {}
    if os.path.exists(cfg.get("victim_log", "")):
        with open(cfg["victim_log"]) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("type") == "reseed" or "exp_gen" not in r:
                    continue
                reqs_by_key.setdefault(r["key"], []).append(r)

    def expected_gen_for(key, ts_ns):
        """exp_gen of the Get nearest to the discard event (guard fires are
        logged by the probe while the request is still unwinding)."""
        cands = reqs_by_key.get(key, [])
        best = None
        for r in cands:
            if r["t_lookup_ns"] <= ts_ns and (best is None or r["t_lookup_ns"] > best["t_lookup_ns"]):
                best = r
        if best is None and cands:
            best = min(cands, key=lambda r: abs(r["t_lookup_ns"] - ts_ns))
        return best.get("exp_gen") if best else ledger.get(key, {}).get("gen")

    rpe_events = rpe_bytes = torn_events = torn_bytes = 0
    gen_skew_events = gen_skew_bytes = 0
    no_magic = guard_events = misbw = 0
    if os.path.exists(events_path):
        with open(events_path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") != "discard":
                    continue
                guard_events += 1
                misbw += e.get("payload_len", 0)
                if not e.get("found_magic") and not e.get("tail_magic", False):
                    no_magic += 1
                    continue
                # identity rule (payload format v2):
                #   RPE (unambiguous) = head or tail marker carries a FOREIGN
                #     key_hash (another object's bytes; B's objects always
                #     have different keys)
                #   torn (subset) = head matches own identity, tail is foreign
                #     (overwrite landed mid-read)
                #   gen_skew (ambiguous, reported separately) = same key but
                #     found_gen != exp_gen -- can be true slot reuse OR the
                #     Get legitimately fetched a reseeded newer gen while the
                #     driver ledger was stale (see NOTES); not counted as RPE
                key = e.get("key", "")
                exp_gen = expected_gen_for(key, e.get("ts_ns", 0))
                kh = rh.key_hash(key)
                head_foreign = e.get("found_magic") and e.get("found_key_hash") != kh
                tail_foreign = e.get("tail_magic") and e.get("tail_key_hash") != kh
                if head_foreign or tail_foreign:
                    rpe_events += 1
                    rpe_bytes += e.get("payload_len", 0)
                    if not head_foreign and tail_foreign:
                        torn_events += 1
                        torn_bytes += e.get("payload_len", 0)
                    continue
                skew = ((e.get("found_magic") and exp_gen is not None
                         and e.get("found_gen") != exp_gen)
                        or (e.get("tail_magic") and exp_gen is not None
                            and e.get("tail_gen") != exp_gen))
                if skew:
                    gen_skew_events += 1
                    gen_skew_bytes += e.get("payload_len", 0)
    res = {
        "run_id": run_id,
        "tier": cfg.get("tier", "A"),
        "constructed": cfg.get("tier", "A") in ("B", "U"),
        "guard_bypass": bool(cfg.get("guard_bypass")),
        "concurrency": cfg.get("concurrency"),
        "seed": cfg.get("seed"),
        "tc": cfg.get("tc"),
        "trace": {"path": cfg.get("trace_path"), "start": cfg.get("trace_start"),
                  "start_b": cfg.get("trace_start_b"),
                  "speedup": cfg.get("trace_speedup")},
        "commit": open(cfg["tested_commit_file"]).read().strip() if cfg.get("tested_commit_file") else "",
        "master_flags": {"eviction_high_watermark_ratio": cfg.get("eviction_high_watermark_ratio", 0.5),
                         "eviction_ratio": cfg.get("eviction_ratio", 0.1),
                         "default_kv_lease_ttl": cfg["ttl_ms"],
                         "allow_evict_soft_pinned_objects": cfg.get("allow_evict_soft_pinned", True)},
        "duration_s": int(vs.get("duration_s", cfg["duration_s"])),
        "gets_total": vs.get("gets_total", 0),
        "gets_ok": vs.get("gets_ok", 0),
        "guard_fires": vs.get("guard_fires", 0),
        "guard_fires_events": guard_events,
        "rpe_events": rpe_events,
        "rpe_payload_bytes": rpe_bytes,
        "torn_events": torn_events,
        "torn_payload_bytes": torn_bytes,
        "gen_skew_events": gen_skew_events,
        "gen_skew_payload_bytes": gen_skew_bytes,
        "payload_bytes_total": vs.get("bytes_ok", 0),
        "rpe_payload_rate_pct": round(100.0 * rpe_bytes / vs["bytes_ok"], 6) if vs.get("bytes_ok") else 0.0,
        "misbw_bytes": misbw,
        "success_mismatch": vs.get("success_mismatch", 0),
        "delivered_wrong_events": vs.get("delivered_wrong_events", 0),
        "delivered_wrong_bytes": vs.get("delivered_wrong_bytes", 0),
        "no_magic_discards": no_magic,
        "not_found": vs.get("not_found", 0),
        "burst_share_pct": None,
        "throughput_mbps": round(vs.get("bytes_ok", 0) / 1e6 / max(vs.get("duration_s", 1), 1), 2),
        "put_failures_pool_full": ps.get("put_failures_pool_full", 0),
        "reclaimable_pct": None,
        "master_metrics": {"start": cfg.get("_metrics_start", {}),
                           "end": cfg.get("_metrics_end", {}),
                           "evictions_delta": {
                               k: cfg.get("_metrics_end", {}).get(k, 0) - cfg.get("_metrics_start", {}).get(k, 0)
                               for k in cfg.get("_metrics_end", {}) if isinstance(cfg["_metrics_end"].get(k), float)}},
        "notes": cfg.get("notes", ""),
    }
    out = os.path.join(rdir, f"tier{res['tier']}_{run_id}.json")
    with open(out, "w") as f:
        json.dump(res, f, indent=2)
    log(f"results -> {out}")
    if res["success_mismatch"] > 0:
        with open(os.path.join(rdir, f"REDLINE_{run_id}"), "w") as f:
            f.write("success_mismatch > 0; preserve all logs and follow disclosure\n")
        log("RED LINE TRIPPED: see REDLINE file")


def main():
    ap = argparse.ArgumentParser(description="RPE-lab driver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("smoke", "seed", "victim", "pressure", "run", "reaggregate"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
    args = ap.parse_args()
    if args.cmd == "reaggregate":
        cfg = load_config(args.config)
        aggregate(cfg, os.path.join(HERE, "results"))
        return
    {"smoke": cmd_smoke, "seed": cmd_seed, "victim": cmd_victim,
     "pressure": cmd_pressure, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
