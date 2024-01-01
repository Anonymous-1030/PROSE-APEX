"""
Edge-case state-machine sketches for PROSE.

The paper (§II-C, §III-B.b) notes that generation wraparound, post-reset
versions, descriptor replay, duplicate/aborted completions, and multi-extent
rollback require supplementary reclaim-serialization analysis. This module
provides reference Python state machines for those cases so they can be
directed-tested and later ported to RTL.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Set, Tuple, Optional, List
from enum import Enum, auto


class TransferState(Enum):
    PENDING = auto()     # queued, not yet admitted
    ADMITTED = auto()    # OAT passed, pin held
    ISSUING = auto()     # payload in flight
    COMPLETED = auto()   # final transaction done, pin released
    ABORTED = auto()     # aborted, pin released


class ReclaimState(Enum):
    FREE = auto()        # slot unused
    RESIDENT = auto()    # holds an object version
    PINNED = auto()      # holds an object version with active transfer(s)
    EVICTING = auto()    # reclaim requested, waiting for pins to drain


@dataclass
class ObjectVersion:
    """A logical object incarnation."""
    obj_id: int
    generation: int            # monotonic except at wraparound
    post_reset_version: int    # incremented on every pool reset
    slot: int


@dataclass
class Descriptor:
    """A transfer descriptor."""
    desc_id: int
    obj_id: int
    generation: int
    post_reset_version: int
    slot: int
    extents: Tuple[int, ...]   # multi-extent support
    state: TransferState = TransferState.PENDING


@dataclass
class PROSEEdgeCaseModel:
    """
    Reference state machine for the missing edge cases.

    Invariants enforced:
      1. A slot may transition out of PINNED only when all pins on its
         (obj_id, generation, post_reset_version) triple are zero.
      2. Generation wraparound is detected by a strictly-monotonic epoch
         counter; a descriptor with a stale generation is rejected.
      3. Post-reset, the post_reset_version is incremented; any descriptor
         carrying the old post_reset_version is rejected regardless of
         generation match.
      4. Descriptor replay is detected by a nonce/sequence tuple; a duplicate
         nonce for the same descriptor is rejected with null completion.
      5. Multi-extent objects are admitted all-or-none: every extent must map
         to the same (obj_id, generation) and all pins are acquired atomically.
    """

    capacity: int
    max_generation: int = 2 ** 16 - 1

    # Authoritative state
    map_slot_to_version: Dict[int, ObjectVersion] = field(default_factory=dict)
    pins: Dict[Tuple[int, int, int], int] = field(default_factory=dict)
    slot_state: Dict[int, ReclaimState] = field(default_factory=dict)

    # Anti-replay / ordering state
    seen_nonces: Set[Tuple[int, int]] = field(default_factory=set)
    generation_epoch: Dict[int, int] = field(default_factory=dict)
    reset_counter: int = 0

    # In-flight descriptors
    transfers: Dict[int, Descriptor] = field(default_factory=dict)
    next_desc_id: int = 0

    def _version_key(self, obj_id: int, generation: int,
                     post_reset: int) -> Tuple[int, int, int]:
        return (obj_id, generation, post_reset)

    def _slot_version(self, slot: int) -> Optional[ObjectVersion]:
        return self.map_slot_to_version.get(slot)

    def _is_wrapped_generation(self, obj_id: int, gen: int) -> bool:
        """True if gen is behind the current epoch (wraparound)."""
        current = self.generation_epoch.get(obj_id, 0)
        # Allow a small tolerance for in-flight descriptors, but reject
        # generations that are clearly stale after a wrap.
        if current >= gen:
            return (current - gen) > (self.max_generation // 2)
        return (gen - current) > (self.max_generation // 2)

    def admit(self, obj_id: int, generation: int, slot: int,
              extents: Tuple[int, ...] = (), nonce: Optional[Tuple[int, int]] = None) -> bool:
        """
        Atomically validate and acquire pins (the OAT). Returns True iff admitted.
        """
        pr = self.reset_counter
        vkey = self._version_key(obj_id, generation, pr)

        # Anti-replay check
        if nonce is not None and nonce in self.seen_nonces:
            return False

        # Post-reset version check
        # (In a real implementation this would be part of the capability.)

        # Generation wraparound check
        if self._is_wrapped_generation(obj_id, generation):
            return False

        # Single- or multi-extent mapping check
        check_extents = (slot,) + extents
        for ext in check_extents:
            sv = self._slot_version(ext)
            if sv is None:
                return False
            if sv.obj_id != obj_id or sv.generation != generation or sv.post_reset_version != pr:
                return False
            if self.slot_state.get(ext, ReclaimState.FREE) == ReclaimState.EVICTING:
                return False

        # Queue/resource check (simplified: always OK in this model)

        # Atomic pin acquisition
        for ext in check_extents:
            self.slot_state[ext] = ReclaimState.PINNED
            self.pins[vkey] = self.pins.get(vkey, 0) + 1

        if nonce is not None:
            self.seen_nonces.add(nonce)

        desc = Descriptor(
            desc_id=self.next_desc_id,
            obj_id=obj_id,
            generation=generation,
            post_reset_version=pr,
            slot=slot,
            extents=check_extents,
            state=TransferState.ADMITTED,
        )
        self.transfers[self.next_desc_id] = desc
        self.next_desc_id += 1
        return True

    def issue_payload(self, desc_id: int) -> bool:
        """Issue one payload transaction; fails if binding became invalid."""
        desc = self.transfers.get(desc_id)
        if desc is None or desc.state != TransferState.ADMITTED:
            return False
        vkey = self._version_key(desc.obj_id, desc.generation, desc.post_reset_version)
        if self.pins.get(vkey, 0) <= 0:
            return False
        desc.state = TransferState.ISSUING
        return True

    def complete(self, desc_id: int):
        """Complete or abort a transfer and release pins."""
        desc = self.transfers.get(desc_id)
        if desc is None:
            return
        vkey = self._version_key(desc.obj_id, desc.generation, desc.post_reset_version)
        if self.pins.get(vkey, 0) > 0:
            self.pins[vkey] -= 1
        desc.state = TransferState.COMPLETED if desc.state == TransferState.ISSUING else TransferState.ABORTED

        # If no pins remain, any slot that was PINNED (or awaiting reclaim)
        # for this version becomes RESIDENT again so the pending reclaim can
        # proceed on the next request_reclaim call.
        if self.pins.get(vkey, 0) == 0:
            for ext in desc.extents:
                if self.slot_state.get(ext) in (ReclaimState.PINNED, ReclaimState.EVICTING):
                    self.slot_state[ext] = ReclaimState.RESIDENT

    def request_reclaim(self, slot: int) -> bool:
        """
        Request to reuse a slot. Returns True immediately if reclaim is legal,
        otherwise defers (slot moves to EVICTING) until pins drain.
        """
        state = self.slot_state.get(slot, ReclaimState.FREE)
        if state in (ReclaimState.FREE, ReclaimState.RESIDENT):
            return True
        if state == ReclaimState.PINNED:
            self.slot_state[slot] = ReclaimState.EVICTING
            return False
        return False

    def finalize_reclaim(self, slot: int, obj_id: int, generation: int):
        """
        Actually reuse the slot for a new object version. Must only be called
        after request_reclaim has returned True for this slot.
        """
        if self.slot_state.get(slot) == ReclaimState.EVICTING:
            raise RuntimeError(f"Slot {slot} still has pending pins")
        self.generation_epoch[obj_id] = max(self.generation_epoch.get(obj_id, 0), generation)
        self.map_slot_to_version[slot] = ObjectVersion(
            obj_id=obj_id,
            generation=generation,
            post_reset_version=self.reset_counter,
            slot=slot,
        )
        self.slot_state[slot] = ReclaimState.RESIDENT

    def reset_pool(self):
        """Pool-wide reset: bump reset counter, invalidating all old descriptors."""
        self.reset_counter += 1
        self.seen_nonces.clear()

    def invariant_binding(self) -> List[str]:
        """Return list of invariant violations, empty if OK."""
        errors = []
        for desc in self.transfers.values():
            if desc.state in (TransferState.ADMITTED, TransferState.ISSUING):
                sv = self._slot_version(desc.slot)
                if sv is None or sv.obj_id != desc.obj_id or sv.generation != desc.generation:
                    errors.append(
                        f"Binding violation: desc {desc.desc_id} expects "
                        f"({desc.obj_id},{desc.generation}) but slot {desc.slot} holds {sv}"
                    )
                vkey = self._version_key(desc.obj_id, desc.generation, desc.post_reset_version)
                if self.pins.get(vkey, 0) <= 0:
                    errors.append(
                        f"Pin violation: desc {desc.desc_id} in state {desc.state} has no pin"
                    )
        return errors


# ---------------------------------------------------------------------------
# Reference tests for the edge-case state machine
# ---------------------------------------------------------------------------

def test_generation_wraparound():
    m = PROSEEdgeCaseModel(capacity=4)
    m.map_slot_to_version[0] = ObjectVersion(0, 5, 0, 0)
    m.slot_state[0] = ReclaimState.RESIDENT
    m.generation_epoch[0] = 5

    # Admit at current generation
    assert m.admit(0, 5, 0) is True

    # Simulate wraparound: epoch jumps far ahead
    m.generation_epoch[0] = 5 + (m.max_generation // 2) + 10
    assert m.admit(0, 5, 0) is False, "stale generation after wraparound must reject"
    print("[OK] generation wraparound rejects stale descriptors")


def test_post_reset_version():
    m = PROSEEdgeCaseModel(capacity=4)
    m.map_slot_to_version[0] = ObjectVersion(0, 1, 0, 0)
    m.slot_state[0] = ReclaimState.RESIDENT

    assert m.admit(0, 1, 0) is True
    m.reset_pool()
    # Same generation but old post-reset version must reject
    assert m.admit(0, 1, 0) is False, "descriptor with old post-reset version must reject"
    print("[OK] post-reset version rejects stale descriptors")


def test_descriptor_replay():
    m = PROSEEdgeCaseModel(capacity=4)
    m.map_slot_to_version[0] = ObjectVersion(0, 1, 0, 0)
    m.slot_state[0] = ReclaimState.RESIDENT

    nonce = (0, 42)
    assert m.admit(0, 1, 0, nonce=nonce) is True
    assert m.admit(0, 1, 0, nonce=nonce) is False, "duplicate nonce must reject"
    print("[OK] descriptor replay rejected via nonce")


def test_multi_extent_all_or_none():
    m = PROSEEdgeCaseModel(capacity=4)
    m.map_slot_to_version[0] = ObjectVersion(0, 1, 0, 0)
    m.map_slot_to_version[1] = ObjectVersion(0, 1, 0, 1)
    m.map_slot_to_version[2] = ObjectVersion(99, 1, 0, 2)  # wrong object
    m.slot_state[0] = ReclaimState.RESIDENT
    m.slot_state[1] = ReclaimState.RESIDENT
    m.slot_state[2] = ReclaimState.RESIDENT

    # Extent 2 maps to wrong object -> reject all
    assert m.admit(0, 1, 0, extents=(1, 2)) is False
    # All extents map correctly -> admit
    assert m.admit(0, 1, 0, extents=(1,)) is True
    print("[OK] multi-extent all-or-none admission")


def test_pin_blocks_reclaim():
    m = PROSEEdgeCaseModel(capacity=4)
    m.map_slot_to_version[0] = ObjectVersion(0, 1, 0, 0)
    m.slot_state[0] = ReclaimState.RESIDENT

    assert m.admit(0, 1, 0) is True
    desc_id = m.next_desc_id - 1

    # Reclaim must defer while pin held
    assert m.request_reclaim(0) is False
    assert m.slot_state[0] == ReclaimState.EVICTING

    # Complete transfer
    m.complete(desc_id)
    assert m.request_reclaim(0) is True
    print("[OK] pin blocks reclaim until transfer completes")


if __name__ == "__main__":
    test_generation_wraparound()
    test_post_reset_version()
    test_descriptor_replay()
    test_multi_extent_all_or_none()
    test_pin_blocks_reclaim()
    print("\nAll edge-case state-machine tests passed.")
