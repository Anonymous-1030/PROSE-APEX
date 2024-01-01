# Phase 1 smoke log

Date: 2026-07-17 (UTC+8) · environment: see ENV.md · commit `f20b706`
(unpatched, stock binary)

## Commands

```bash
# master (flags per plan; full startup line transcribed in ENV.md)
$MOONCAKE_BUILD/mooncake-store/src/mooncake_master \
  --rpc_port=50051 --eviction_high_watermark_ratio=0.5 --eviction_ratio=0.1 \
  --default_kv_lease_ttl=5000 --allow_evict_soft_pinned_objects=1
# client (single process, P2PHANDSHAKE + tcp, 2GB segment)
python3 $RPE_LAB/driver.py smoke --config $RPE_LAB/configs/smoke.yaml
```

Orchestration script: `rpe_lab/wsl/smoke_run.sh`; driver subcommand: `smoke`
(put/get 100 -> fill pool past high watermark -> probe an evicted key).

## DoD results (all passed)

| DoD | Result | Evidence |
|---|---|---|
| tenant A puts 100 objects, all Get-succeed | OK (100/100) | `DoD-1 put/get 100: got 100/100 OK` |
| sustained Put past high watermark -> master eviction counters grow | OK | `:9003/metrics` `master_successful_evictions_total=7, master_attempted_evictions_total=7`; master log `[EVICT-TRIGGER] memory_ratio=0.500732 ...` / `[EVICT-RESULT] evicted_count=30, ...` each round |
| Get on an evicted key -> "not found" class error, distinct from lease-expired | OK | `DoD-3 evicted key fill/00000: get_into rc=-704` (OBJECT_NOT_FOUND=-704; lease expiry is LEASE_EXPIRED=-707, see audit_notes.md Q4) |

`SMOKE PASS`, `SMOKE_EXIT=0`. Full master log: `results/smoke_master.log`.

## Findings during the run (driver fixed accordingly)

- First run failed DoD-3: the eviction victims were all later-written `fill/*`
  keys; none of the 100 earlier `smoke/*` keys was evicted (each round evicted
  30 keys x 7 rounds = 210 keys ≈ 735 MB). Eviction victim selection on this
  branch does not favor the "oldest" objects; probing an actually-evicted fill
  key passed. Consequence for the Phase 3 hot-set design: hot-set keys are not
  exempted for being "old residents"; their eviction probability is comparable
  to new keys.
- Eviction cadence: once usage crossed the 0.5 watermark, a round fired every
  ~350ms (30 keys/round), consistent with the `kEvictionThreadSleepMs=10ms`
  scan period (round spacing is set by trigger frequency).
- Master-side observation at high watermark: `Mem Storage: 948.50 MB / 2.00 GB
  (46.3%)`, `Eviction: Success/Attempts=7/7, keys=210, size=735.00 MB`.

## The two error codes on record (basis for later guard_fires counting)

| Case | Python get_into return | C++ ErrorCode | Log string |
|---|---|---|---|
| key missing (evicted/never written) | `-704` | OBJECT_NOT_FOUND | (master-side query miss) |
| lease expired (guard trips) | `-707` | LEASE_EXPIRED | client WARNING `lease_expired_before_data_transfer_completed key=` |

---

# Phase 2 end-to-end chimney (after instrumentation)

patch commit: `e930d82` (client_service.cpp +17 lines + new rpe_lab_probe.h;
`git apply --check` passed; after incremental rebuild
`PYBIND_IMPORT_OK_AFTER_PATCH`).

Method: master TTL=500ms (chimney-only value, not used in the paper) -> put one
3.5MB headed object -> step up `tc qdisc dev lo netem delay` (200/400/800ms) ->
observe get_into return codes and `results/events_chimney.jsonl`.

Results (`CHIMNEY_PASS`, `CHIMNEY_EXIT=0`):

```
tc delay 200ms: get_into rc=3670016 (0.40s) x3   # not expired, normal success
tc delay 400ms:
  W client_service.cpp:1162] lease_expired_before_data_transfer_completed key=hot/0000
  E real_client.cpp:3322] Get failed for key: hot/0000 with error: LEASE_EXPIRED
  get_into rc=-707 (0.80s)
```

Event-file record (all fields present; no overwrite in this example, as
expected):

```json
{"ts_ns": 1784297450918523008, "type": "discard", "run_id": "chimney",
 "key": "hot/0000", "expected_key_hash": 17779036219203737137,
 "found_magic": true, "found_tenant": 1, "found_key_hash": 17779036219203737137,
 "found_gen": 1, "found_ts_ns": 1784297448852316646,
 "payload_len": 3670016, "expired_by_us": 300872, "transfer_us": 408}
```

Takeaways: (1) the -707 return code, the WARNING log string, and the JSONL
event all appear together — the dual-channel guard_fires counting closes the
loop; (2) in this example the expiry was caused by control-plane RPC latency
(400ms one-way x round trip) while TransferRead itself took 408µs (the
same-host memcpy path is not affected by tc delay) — the bandwidth term of the
Phase 3 window formula must treat the actual data paths (memcpy vs TCP)
separately; (3) the instrumentation is confirmed zero-behavior-change: with no
`RPE_LAB_EVENTS` set it emits nothing (unit-test case 5 covers this).
