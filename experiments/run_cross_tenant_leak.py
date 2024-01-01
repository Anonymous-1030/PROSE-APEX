#!/usr/bin/env python3
"""Cross-tenant byte-leak proof-of-concept for Reclaimed-Payload Exposure (RPE).

MECHANISTIC SIMULATION PoC — no real hardware, CXL link, or LLM involved. The
binding/reuse semantics come from the artifact's genuine binding model
(``trace_adapter/rpe_binding_model.py``); the payload bytes are an illustrative
per-tenant tag pattern so the leak is visible at the byte level.

Scenario (pool with autonomous reclaim, NO commit-time gate), per race window:
  1. Tenant A's chunk chunk_KA is admitted to the pool -> frame S, generation g.
     A queues promotion descriptor d = (chunk_KA, g, S) built from this snapshot.
  2. Before d issues, tenant B admits chunks; the pool evicts A's chunk and
     tenant B's chunk chunk_KB is written INTO frame S (generation bumps).
  3. d issues and pulls frame S's CURRENT bytes into A's GPU buffer:
     with no gate, A receives tenant B's payload, byte for byte;
     with the commit-time gate (generation re-validated at issue), the binding
     is stale, the descriptor is null-completed METADATA-ONLY, and zero payload
     bytes move.

Byte-level demo: a chunk payload is a 64 KiB buffer whose first 40 bytes are a
header (magic ``PRSEPOC1`` | generation u32be | NUL-padded chunk id) and whose
remaining bytes are the tenant tag (0xA0 for tenant A, 0xB0 for tenant B), so a
hex dump of the first 64 bytes immediately shows which tenant's bytes moved.

Outputs:
  * results/cross_tenant_leak_poc.json  (config, race events, totals, hex)
  * results/cross_tenant_leak_poc.txt   (human-readable hex-dump narrative)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# trace_adapter is a flat script directory (no package); import it directly.
sys.path.insert(0, str(REPO / "trace_adapter"))

import rpe_binding_model as rbm

RESULTS_DIR = REPO / "results"

PROVENANCE = ("MECHANISTIC SIMULATION PoC: frame binding/reuse from the "
              "artifact's RPE binding model; payloads are synthetic per-tenant "
              "tag patterns. No real hardware, CXL link, or tenant data involved.")

POOL_CAPACITY = 2        # frames; B's second admission evicts A and reuses frame S
POLICY = "LRU"
N_WINDOWS = 1000         # race windows
PAYLOAD_BYTES = 64 * 1024   # 64 KiB chunk payload (paper's chunk granularity)
MAGIC = b"PRSEPOC1"
TAG_A = 0xA0
TAG_B = 0xB0
KEY_FIELD = 28           # NUL-padded chunk-id field width in the header
HEX_DUMP_BYTES = 64


def _ascii_side(bs: bytes) -> str:
    """Classic hexdump sidebar: printable ASCII kept, everything else as '.'."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in bs)


def make_payload(tag: int, chunk_key: str, gen: int) -> bytes:
    """Synthetic chunk payload: header + tenant-tag fill (see module docstring)."""
    key = chunk_key.encode("utf-8")[:KEY_FIELD].ljust(KEY_FIELD, b"\x00")
    header = MAGIC + gen.to_bytes(4, "big") + key
    return header + bytes([tag]) * (PAYLOAD_BYTES - len(header))


def run(n_windows: int = N_WINDOWS) -> dict:
    events = []
    leaked_gate_off = 0
    leaked_gate_on = 0
    gate_on_payload_bytes_moved = 0
    windows_with_leak = 0
    hex_excerpt = None

    for i in range(n_windows):
        ka = f"tenantA/chunk-{i:04d}"
        kb_other = f"tenantB/chunk-{i:04d}-fill"
        kb = f"tenantB/chunk-{i:04d}-loot"

        pool = rbm._Pool(POOL_CAPACITY, POLICY)
        # 1. A's chunk admitted; descriptor snapshot (frame S, gen g) queued.
        slot, gen, _ = pool.access(ka)
        # 2. B's admissions: fills the other frame, then evicts A's chunk and
        #    reuses frame S for B's chunk (generation bumps).
        pool.access(kb_other)
        pool.access(kb)
        occupant = pool.occupant_of(slot)  # additive read-only accessor
        assert occupant is not None and occupant[0] == kb, \
            f"race window {i}: expected B's chunk in slot {slot}, got {occupant}"

        payload_a = make_payload(TAG_A, ka, gen)
        payload_b = make_payload(TAG_B, occupant[0], occupant[1])

        # 3a. NO commit-time gate: d pulls frame S's current bytes -> B's payload.
        received_gate_off = payload_b
        assert received_gate_off == payload_b, "received bytes must be B's payload"
        assert received_gate_off != payload_a, "received bytes must NOT be A's payload"
        leaked = len(received_gate_off)
        leaked_gate_off += leaked
        windows_with_leak += 1

        # 3b. Commit-time gate: re-validate (frame, chunk, gen) at issue.
        assert not pool.binding_valid(slot, ka, gen), \
            "binding must be stale after frame reuse"
        # Stale binding -> null completion, metadata only, zero payload bytes.
        gate_on_payload_bytes_moved += 0
        leaked_gate_on += 0

        events.append({
            "window": i,
            "slot": slot,
            "binding_at_queue": {"chunk": ka, "gen": gen},
            "binding_at_issue": {"chunk": occupant[0], "gen": occupant[1]},
            "gate_off": {"issued": True, "cross_tenant_bytes": leaked},
            "gate_on": {"binding_valid": False, "completion": "metadata-only",
                        "payload_bytes_moved": 0},
        })
        if i == 0:
            hex_excerpt = {
                "bytes": HEX_DUMP_BYTES,
                "a_expected_hex": payload_a[:HEX_DUMP_BYTES].hex(" "),
                "a_received_gate_off_hex": received_gate_off[:HEX_DUMP_BYTES].hex(" "),
                "a_expected_ascii": _ascii_side(payload_a[:HEX_DUMP_BYTES]),
                "a_received_gate_off_ascii": _ascii_side(
                    received_gate_off[:HEX_DUMP_BYTES]),
                "gate_on": "no payload bytes moved (metadata-only null completion)",
            }

    # Whole-run guarantees required by the PoC.
    assert leaked_gate_off == n_windows * PAYLOAD_BYTES
    assert leaked_gate_on == 0, "gate-on must leak exactly zero bytes"
    assert gate_on_payload_bytes_moved == 0, "gate-on must be metadata-only"

    return {
        "provenance": PROVENANCE,
        "config": {
            "pool_capacity_frames": POOL_CAPACITY,
            "eviction_policy": POLICY,
            "n_race_windows": n_windows,
            "payload_bytes_per_chunk": PAYLOAD_BYTES,
            "tenant_tags": {"A": hex(TAG_A), "B": hex(TAG_B)},
            "header": "magic 'PRSEPOC1' (8B) | generation u32be (4B) | "
                      "chunk id NUL-padded (28B); remaining bytes = tenant tag",
        },
        "leak": {
            "gate_off_cross_tenant_bytes": leaked_gate_off,
            "gate_off_windows_with_leak": windows_with_leak,
            "gate_on_cross_tenant_bytes": leaked_gate_on,
            "gate_on_payload_bytes_moved": gate_on_payload_bytes_moved,
            "gate_on_completion": "metadata-only (stale (frame, gen) binding "
                                  "rejected at issue; zero payload)",
        },
        "hex_excerpt_window0": hex_excerpt,
        "race_events": events,
    }


def _txt(res: dict) -> str:
    hx = res["hex_excerpt_window0"]
    ev0 = res["race_events"][0]
    lk = res["leak"]
    n = res["config"]["n_race_windows"]
    pb = res["config"]["payload_bytes_per_chunk"]
    lines = [
        "PROSE cross-tenant byte-leak PoC  (MECHANISTIC SIMULATION — synthetic",
        "per-tenant tag payloads on the artifact's RPE binding model; no real",
        "hardware, CXL link, or tenant data).",
        "",
        f"Pool: {res['config']['pool_capacity_frames']} frames, "
        f"{res['config']['eviction_policy']}, autonomous reclaim, descriptor queue.",
        "",
        f"Race window 0 (of {n}):",
        f"  t0  tenant A admitted '{ev0['binding_at_queue']['chunk']}' -> "
        f"slot {ev0['slot']}, gen {ev0['binding_at_queue']['gen']};",
        f"      descriptor d = (chunk, gen, slot) queued from this snapshot.",
        f"  t1  tenant B admissions evict A's chunk; slot {ev0['slot']} reused for",
        f"      '{ev0['binding_at_issue']['chunk']}' at gen "
        f"{ev0['binding_at_issue']['gen']}.",
        "  t2  d issues and pulls the slot's CURRENT bytes into A's GPU buffer.",
        "",
        f"  What A expected (first {hx['bytes']} bytes):",
        f"    {hx['a_expected_hex']}",
        f"    ASCII: {hx['a_expected_ascii']!r}",
        f"  What A actually received, gate OFF (first {hx['bytes']} bytes):",
        f"    {hx['a_received_gate_off_hex']}",
        f"    ASCII: {hx['a_received_gate_off_ascii']!r}",
        "  -> received == tenant B's payload (0xb0 fill + B's chunk header);",
        "     received != tenant A's payload. The wrong logical object's bytes moved.",
        "",
        f"Totals over {n} race windows:",
        f"  gate OFF: cross-tenant bytes delivered to A = "
        f"{lk['gate_off_cross_tenant_bytes']:,} "
        f"({lk['gate_off_windows_with_leak']}/{n} windows x {pb:,} bytes)",
        f"  gate ON : (frame, gen) re-validated at issue -> stale binding rejected;",
        f"            descriptor null-completed metadata-only; payload bytes moved = "
        f"{lk['gate_on_payload_bytes_moved']}; cross-tenant bytes = "
        f"{lk['gate_on_cross_tenant_bytes']}",
        "",
    ]
    return "\n".join(lines)


def report(res: dict) -> None:
    print(_txt(res))


def main() -> None:
    try:  # keep hex/ASCII output printable on non-UTF-8 Windows consoles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    res = run()
    report(res)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    jpath = RESULTS_DIR / "cross_tenant_leak_poc.json"
    tpath = RESULTS_DIR / "cross_tenant_leak_poc.txt"
    with jpath.open("w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    tpath.write_text(_txt(res), encoding="utf-8")
    print(f"Saved: {jpath}")
    print(f"Saved: {tpath}")


if __name__ == "__main__":
    main()
