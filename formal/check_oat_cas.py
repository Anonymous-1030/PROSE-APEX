#!/usr/bin/env python3
"""Exhaustive (BFS) model checker for the consult-then-CAS OAT.

Java-free mirror of ``formal/prose_oat_cas.tla``: it explores the SAME finite
instance declared in ``prose_oat_cas.cfg`` (2 objects, 2 generations,
MaxPins=2) exhaustively and asserts the SAME invariants:

    - InvTransferBinding   : every in-flight descriptor d has MAP[d.id]=<g,res>
                             and PIN[d.id] > 0                       (Invariant 1)
    - InvZeroRPE           : IssuedPayload never contains a stale binding
    - InvPendingBlocksPins : no successful CAS on an entry with PEND set
    - InvPinBounded        : pins never exceed MaxPins

Each action is atomic, mirroring the single write port per directory bank, so
every interleaving is one of the two cases the paper's proof enumerates: an
update committed before a CAS is seen by the CAS re-read (reject), and an
update after a successful CAS is blocked by the installed pin (pending, then
drain).

Teeth: ``--break-pend-guard`` removes the pending_reclaim check from the CAS
predicate and must reach a state that violates InvPendingBlocksPins;
``--break-pin-guard`` lets an update commit at nonzero pins and must reach a
stale-payload state (counterexamples, matching the note in the TLA+ header).

Usage:
    python formal/check_oat_cas.py                     # verify (no violation)
    python formal/check_oat_cas.py --break-pend-guard  # falsify (find one)
    python formal/check_oat_cas.py --break-pin-guard   # falsify (find one)
"""
from __future__ import annotations

import sys
from collections import deque

OBJECT_IDS = ("o1", "o2")
GENERATIONS = ("g0", "g1")
MAX_PINS = 2
# Two concurrent OAT instances per binding, so a second CAS can arrive while
# an earlier pin and a pended update are both live (the teeth scenario).
DESCRIPTORS = tuple((i, g, k) for i in OBJECT_IDS for g in GENERATIONS for k in (0, 1))

BREAK_PEND_GUARD = "--break-pend-guard" in sys.argv
BREAK_PIN_GUARD = "--break-pin-guard" in sys.argv


def init_state():
    # map_: per id (gen, resident); pin: per id count; pend: per id bool;
    # pendpay: per id (gen, resident); oat: per descriptor (pc, verdict);
    # issued: frozenset of (id, g); pending_admits: per id count
    return (
        tuple((GENERATIONS[0], True) for _ in OBJECT_IDS),
        tuple(0 for _ in OBJECT_IDS),
        tuple(False for _ in OBJECT_IDS),
        tuple((GENERATIONS[0], True) for _ in OBJECT_IDS),
        tuple(("adv", "none") for _ in DESCRIPTORS),
        frozenset(),
        tuple(0 for _ in OBJECT_IDS),
    )


def idx(id_):
    return OBJECT_IDS.index(id_)


def successors(state):
    map_, pin, pend, pendpay, oat, issued, pending_admits = state
    out = []

    for d_pos, (id_, g, _k) in enumerate(DESCRIPTORS):
        pc, verdict = oat[d_pos]
        i = idx(id_)

        # Advisory read: declares nothing.
        if pc == "adv":
            s = list(oat)
            s[d_pos] = ("cas", "none")
            out.append((map_, pin, pend, pendpay, tuple(s), issued, pending_admits))

        # CAS: atomic re-read and conditional pin install.
        if pc == "cas":
            cur_gen, cur_res = map_[i]
            match = (
                cur_gen == g
                and cur_res
                and (BREAK_PEND_GUARD or not pend[i])
                and pin[i] < MAX_PINS
            )
            s = list(oat)
            pa = list(pending_admits)
            p = list(pin)
            if match:
                p[i] += 1
                s[d_pos] = ("flight", "admit")
                if pend[i]:
                    pa[i] += 1
            else:
                s[d_pos] = ("done", "reject")
            out.append((map_, tuple(p), pend, pendpay, tuple(s), issued, tuple(pa)))

        # Issue payload: in flight, no re-validation. An issue counts as
        # stale only when the binding is already invalid AT THE ISSUE STATE.
        if pc == "flight":
            cur_gen, cur_res = map_[i]
            new_issued = issued if (cur_gen == g and cur_res) else (issued | {(id_, g)})
            out.append(
                (map_, pin, pend, pendpay, oat, new_issued, pending_admits)
            )

        # Release: pin drops; last pin commits the pended update.
        if pc == "flight" and pin[i] > 0:
            s = list(oat)
            p = list(pin)
            m = list(map_)
            pe = list(pend)
            p[i] -= 1
            if p[i] == 0 and pe[i]:
                m[i] = pendpay[i]
                pe[i] = False
            s[d_pos] = ("done", "admit")
            out.append((tuple(m), tuple(p), tuple(pe), pendpay, tuple(s), issued, pending_admits))

    # Placement update on any entry, any generation.
    for id_ in OBJECT_IDS:
        i = idx(id_)
        for g in GENERATIONS:
            if pin[i] == 0 or BREAK_PIN_GUARD:
                m = list(map_)
                m[i] = (g, True)
                out.append((tuple(m), pin, pend, pendpay, oat, issued, pending_admits))
            else:
                pe = list(pend)
                pp = list(pendpay)
                pe[i] = True
                pp[i] = (g, True)
                out.append((map_, pin, tuple(pe), tuple(pp), oat, issued, pending_admits))

    return out


def check_invariants(state):
    map_, pin, pend, pendpay, oat, issued, pending_admits = state
    for d_pos, (id_, g, _k) in enumerate(DESCRIPTORS):
        pc, _ = oat[d_pos]
        i = idx(id_)
        if pc == "flight":
            if map_[i] != (g, True) or pin[i] == 0:
                return "InvTransferBinding"
    # `issued` holds only bindings that were stale at their issue state.
    if issued:
        return "InvZeroRPE"
    for id_ in OBJECT_IDS:
        i = idx(id_)
        if pend[i] and pending_admits[i] > 0:
            return "InvPendingBlocksPins"
        if pin[i] > MAX_PINS:
            return "InvPinBounded"
    return None


def main():
    seen = set()
    queue = deque([init_state()])
    seen.add(init_state())
    explored = 0
    while queue:
        state = queue.popleft()
        explored += 1
        bad = check_invariants(state)
        if bad:
            print(f"[VIOLATION] {bad} after {explored} states")
            print("  state:", state)
            return 1
        for nxt in successors(state):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    mode = (
        "break-pend-guard" if BREAK_PEND_GUARD
        else "break-pin-guard" if BREAK_PIN_GUARD
        else "verify"
    )
    print(f"[PASS] mode={mode}: {explored} reachable states, all invariants hold")
    return 0


if __name__ == "__main__":
    if BREAK_PEND_GUARD or BREAK_PIN_GUARD:
        # In falsification mode a violation is the EXPECTED outcome.
        sys.exit(0 if main() == 1 else 1)
    sys.exit(main())
