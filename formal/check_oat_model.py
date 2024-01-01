#!/usr/bin/env python3
"""Exhaustive (BFS) model checker for the PROSE Object Admission Transaction.

This is a Java-free mirror of ``formal/prose_oat.tla``. It explores the SAME
finite instance declared in ``prose_oat.cfg`` (2 objects, 2 generations, 2
slots, MaxPins=2) exhaustively and asserts the SAME invariants:

    - TypeOK             (implicit: states are constructed in-type)
    - InvTransferBinding : every in-flight descriptor has MAP[id]=<slot,g> and
                           PIN[id,g] > 0                              (Invariant 1)
    - InvZeroRPE         : StalePayload stays empty                   (Theorem 1 / C1)
    - InvPinBounded      : pins never exceed MaxPins

The data mover (issue_payload) does NOT re-validate the binding, exactly as the
non-preemptive DMA path behaves in hardware. Safety therefore rests on the pin
discipline: Admit sets PIN>0, and Reclaim is legal only at PIN=0. To prove the
check has teeth, ``--break`` removes the PIN=0 guard on Reclaim and expects a
reachable stale-payload state (a counterexample), mirroring the note in the
TLA+ header.

Usage:
    python formal/check_oat_model.py            # verify (must find no violation)
    python formal/check_oat_model.py --break     # falsify (must find a violation)
"""
from __future__ import annotations

import sys
from itertools import product
from collections import deque

OBJECT_IDS = ("o1", "o2")
GENERATIONS = ("g0", "g1")
SLOTS = ("s0", "s1")
MAX_PINS = 2


# A state is fully described by hashable, frozen structures:
#   map_:   tuple over OBJECT_IDS of (slot, g)
#   pin:    tuple over (id, g) pairs of int count
#   inflight: frozenset of (id, g, slot) descriptors
#   stale:  frozenset of (id, g, slot) issues that were invalid at issue time
PIN_KEYS = tuple((i, g) for i in OBJECT_IDS for g in GENERATIONS)


def _cur_slot(map_, id_):
    return map_[OBJECT_IDS.index(id_)][0]


def _cur_gen(map_, id_):
    return map_[OBJECT_IDS.index(id_)][1]


def _pin(pin, id_, g):
    return pin[PIN_KEYS.index((id_, g))]


def _with_pin(pin, id_, g, delta):
    lst = list(pin)
    lst[PIN_KEYS.index((id_, g))] += delta
    return tuple(lst)


def _with_map(map_, id_, slot, g):
    lst = list(map_)
    lst[OBJECT_IDS.index(id_)] = (slot, g)
    return tuple(lst)


def successors(state, allow_unsafe_reclaim=False):
    map_, pin, inflight, stale = state
    out = []

    # Admit / Reject over every candidate descriptor
    for id_, g, slot in product(OBJECT_IDS, GENERATIONS, SLOTS):
        can_admit = (
            _cur_slot(map_, id_) == slot
            and _cur_gen(map_, id_) == g
            and _pin(pin, id_, g) < MAX_PINS
        )
        d = (id_, g, slot)
        if can_admit:
            out.append((map_, _with_pin(pin, id_, g, +1), inflight | {d}, stale))
        # Reject is a stutter on the persistent variables; skip (no new state).

    # IssuePayload: fires for ANY in-flight descriptor, no re-validation.
    for d in inflight:
        id_, g, slot = d
        valid = (
            _cur_slot(map_, id_) == slot
            and _cur_gen(map_, id_) == g
            and _pin(pin, id_, g) > 0
        )
        new_stale = stale if valid else (stale | {d})
        out.append((map_, pin, inflight, new_stale))

    # Complete / RELEASE
    for d in list(inflight):
        id_, g, slot = d
        if _pin(pin, id_, g) > 0:
            out.append((map_, _with_pin(pin, id_, g, -1), inflight - {d}, stale))

    # Reclaim: legal only at PIN==0 for the current binding (unless --break).
    for id_, new_slot, new_g in product(OBJECT_IDS, SLOTS, GENERATIONS):
        old_g = _cur_gen(map_, id_)
        if allow_unsafe_reclaim or _pin(pin, id_, old_g) == 0:
            out.append((_with_map(map_, id_, new_slot, new_g), pin, inflight, stale))

    return out


def initial_states():
    # Init: MAP arbitrary in [ObjectIds -> Slots x Generations], PIN all 0.
    for combo in product(list(product(SLOTS, GENERATIONS)), repeat=len(OBJECT_IDS)):
        map_ = tuple(combo)
        pin = tuple(0 for _ in PIN_KEYS)
        yield (map_, pin, frozenset(), frozenset())


def inv_transfer_binding(state):
    map_, pin, inflight, _ = state
    for (id_, g, slot) in inflight:
        if _cur_slot(map_, id_) != slot:
            return False
        if _cur_gen(map_, id_) != g:
            return False
        if _pin(pin, id_, g) <= 0:
            return False
    return True


def inv_zero_rpe(state):
    return len(state[3]) == 0


def inv_pin_bounded(state):
    return all(c <= MAX_PINS for c in state[1])


def check(allow_unsafe_reclaim=False):
    """BFS the whole reachable state space; return (n_states, violation-or-None)."""
    seen = set()
    frontier = deque()
    for s in initial_states():
        if s not in seen:
            seen.add(s)
            frontier.append(s)

    while frontier:
        s = frontier.popleft()
        # InvTransferBinding is expected to hold ONLY in the safe model; under
        # --break we care specifically about InvZeroRPE.
        if not inv_pin_bounded(s):
            return len(seen), ("InvPinBounded", s)
        if not inv_zero_rpe(s):
            return len(seen), ("InvZeroRPE", s)
        if not allow_unsafe_reclaim and not inv_transfer_binding(s):
            return len(seen), ("InvTransferBinding", s)
        for ns in successors(s, allow_unsafe_reclaim=allow_unsafe_reclaim):
            if ns not in seen:
                seen.add(ns)
                frontier.append(ns)

    return len(seen), None


def main(argv):
    break_mode = "--break" in argv
    n, violation = check(allow_unsafe_reclaim=break_mode)

    if break_mode:
        # Removing the PIN=0 guard on Reclaim MUST make the safety invariant fail.
        if violation is None:
            print(f"[FAIL] --break explored {n} states and found NO violation; "
                  "the pin guard is not what enforces safety — check the model.")
            return 1
        inv, state = violation
        print(f"[OK]  --break reached a violation of {inv} after exploring "
              f"{n} states: pin-less reclaim exposes stale payload.")
        print(f"      counterexample state: MAP={state[0]} PIN={state[1]} "
              f"stale={set(state[3])}")
        return 0

    if violation is not None:
        inv, state = violation
        print(f"[FAIL] invariant {inv} violated in reachable state: {state}")
        return 1
    print(f"[OK]  OAT model: explored {n} reachable states; "
          "InvTransferBinding, InvZeroRPE, InvPinBounded all hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
