------------------------- MODULE prose_oat_cas -------------------------
(*
  TLA+ model of the consult-then-CAS Object Admission Transaction
  (paper Section IV-B, the answer to the reviewer interleaving).

  The protocol modeled here, per directory entry:
    - An OAT instance performs advisory reads, then one atomic
      compare-and-swap. The CAS re-reads the entry and, in the same atomic
      step, checks MAP[id] = <<g, resident>> and pending_reclaim = 0 and
      pins < MaxPins, and only on a full match increments the pin count.
      That step is the linearization point of the transaction.
    - A placement update shares the entry's single write port with the CAS,
      one write per cycle, so all writes to an entry form a total order.
      An update that finds pins > 0 sets pending_reclaim and waits; while
      pending_reclaim is set no CAS can succeed, so the pin count decreases
      monotonically until the last release, when the waiting update commits.

  Invariants checked by TLC (see prose_oat_cas.cfg):
    - TypeOK
    - InvTransferBinding : every in-flight (admitted, not yet completed)
      descriptor d has MAP[d.id] = <<d.g, TRUE>> and PIN[d.id] > 0. This is
      Invariant 1 of the paper.
    - InvZeroRPE         : IssuedPayload never contains a stale binding.
    - InvPendingBlocksPins : no OAT instance is admitted on an entry whose
      pending_reclaim is set.
    - InvPinBounded      : pin counts stay within MaxPins.

  A same-cycle CAS/update collision is ordered CAS-first, matching the RTL
  arbitration in cefe_directory.sv; because each action is atomic, every
  interleaving is one of the two cases the paper's proof enumerates.

  Run:  tlc2 -config prose_oat_cas.cfg prose_oat_cas.tla
        (or open prose_oat_cas.tla in the TLA+ Toolbox with this model)
  A Java-free exhaustive mirror of the same instance lives in
  formal/check_oat_cas.py for environments without a Java runtime.

  Scope: single-extent KV path. Generation wraparound, descriptor replay,
  and multi-extent objects remain out of scope, as in prose_oat.tla.
*)

EXTENDS Integers, FiniteSets, TLC

CONSTANTS
    ObjectIds,      \* finite set of logical object ids
    Generations,    \* finite set of generation values
    MaxPins         \* per-entry pin bound

VARIABLES
    MAP,            \* MAP[id] = <<gen, resident>>
    PIN,            \* PIN[id] \in 0..MaxPins
    PEND,           \* PEND[id] \in BOOLEAN (pending_reclaim)
    PENDPAY,        \* PENDPAY[id] = <<gen, resident>> waiting update payload
    OAT,            \* OAT[d] = [pc, verdict] per descriptor instance
    IssuedPayload,  \* issues stale AT ISSUE TIME (must stay empty)
    PendingAdmits   \* successful CASes on entries with PEND set (must stay 0)

Descriptor == [id : ObjectIds, g : Generations, k : {0, 1}]
  \* k tags two concurrent OAT instances per binding, so a second CAS can
  \* arrive while an earlier pin and a pended update are both live.

vars == <<MAP, PIN, PEND, PENDPAY, OAT, IssuedPayload, PendingAdmits>>

PCValues   == {"adv", "cas", "flight", "done"}
Verdicts   == {"none", "admit", "reject"}

TypeOK ==
    /\ MAP     \in [ObjectIds -> Generations \X {TRUE}]
    /\ PIN     \in [ObjectIds -> 0..MaxPins]
    /\ PEND    \in [ObjectIds -> BOOLEAN]
    /\ PENDPAY \in [ObjectIds -> Generations \X {TRUE}]
    /\ OAT     \in [Descriptor -> [pc : PCValues, verdict : Verdicts]]
    /\ IssuedPayload \subseteq (ObjectIds \X Generations)
    /\ PendingAdmits \in [ObjectIds -> 0..1]

Init ==
    /\ MAP     = [id \in ObjectIds |-> <<CHOOSE g \in Generations : TRUE, TRUE>>]
    /\ PIN     = [id \in ObjectIds |-> 0]
    /\ PEND    = [id \in ObjectIds |-> FALSE]
    /\ PENDPAY \in [ObjectIds -> Generations \X {TRUE}]
    /\ OAT     = [d \in Descriptor |-> [pc |-> "adv", verdict |-> "none"]]
    /\ IssuedPayload = {}
    /\ PendingAdmits = [id \in ObjectIds |-> 0]

--------------------------------------------------------------------------
(* Advisory read: collects information, declares nothing. *)
Advisory(d) ==
    LET s == OAT[d] IN
    /\ s.pc = "adv"
    /\ OAT' = [OAT EXCEPT ![d] = [@ |-> "cas", @ |-> "none"]]
    /\ UNCHANGED <<MAP, PIN, PEND, PENDPAY, IssuedPayload, PendingAdmits>>

(* The compare-and-swap: one atomic step that re-reads the entry and either
   installs the pin or rejects. This is the linearization point. *)
CAS(d) ==
    LET s  == OAT[d]
        id == d.id
        g  == d.g
        match == (MAP[id] = <<g, TRUE>>) /\ ~PEND[id] /\ (PIN[id] < MaxPins)
    IN
    /\ s.pc = "cas"
    /\ IF match
        THEN /\ PIN'  = [PIN EXCEPT ![id] = @ + 1]
             /\ OAT'  = [OAT EXCEPT ![d] = [@ |-> "flight", @ |-> "admit"]]
             /\ PendingAdmits' =
                    [PendingAdmits EXCEPT ![id] =
                        IF PEND[id] THEN @ + 1 ELSE @]
        ELSE /\ OAT' = [OAT EXCEPT ![d] = [@ |-> "done", @ |-> "reject"]]
             /\ UNCHANGED <<PIN, PendingAdmits>>
    /\ UNCHANGED <<MAP, PEND, PENDPAY, IssuedPayload>>

(* Payload issue: after the linearization point, in flight. The data mover
   does NOT re-validate, so safety rests on the pin discipline. An issue
   counts as stale when the binding is already invalid AT THE ISSUE STATE;
   that set must stay empty. *)
Issue(d) ==
    LET s == OAT[d] IN
    /\ s.pc = "flight"
    /\ IssuedPayload' =
        IF MAP[d.id] = <<d.g, TRUE>>
            THEN IssuedPayload
            ELSE IssuedPayload \cup {<<d.id, d.g>>}
    /\ UNCHANGED <<MAP, PIN, PEND, PENDPAY, OAT, PendingAdmits>>

(* Release: the pin count drops; on the last pin with a pended update, the
   waiting update commits and pending_reclaim clears, one atomic step. *)
Release(d) ==
    LET s  == OAT[d]
        id == d.id IN
    /\ s.pc = "flight"
    /\ PIN[id] > 0
    /\ PIN' = [PIN EXCEPT ![id] = @ - 1]
    /\ IF (PIN[id] = 1) /\ PEND[id]
        THEN /\ MAP'  = [MAP EXCEPT ![id] = PENDPAY[id]]
             /\ PEND' = [PEND EXCEPT ![id] = FALSE]
        ELSE /\ UNCHANGED <<MAP, PEND>>
    /\ OAT' = [OAT EXCEPT ![d] = [@ |-> "done", @ |-> "admit"]]
    /\ UNCHANGED <<PENDPAY, IssuedPayload, PendingAdmits>>

(* Placement update: commits at zero pins, otherwise pends. One atomic step,
   so it is totally ordered with every CAS on the same entry. *)
Update(id, g) ==
    /\ g \in Generations
    /\ IF PIN[id] = 0
        THEN /\ MAP' = [MAP EXCEPT ![id] = <<g, TRUE>>]
             /\ UNCHANGED <<PEND, PENDPAY>>
        ELSE /\ PEND'    = [PEND EXCEPT ![id] = TRUE]
             /\ PENDPAY' = [PENDPAY EXCEPT ![id] = <<g, TRUE>>]
             /\ UNCHANGED <<MAP>>
    /\ UNCHANGED <<PIN, OAT, IssuedPayload, PendingAdmits>>

Next ==
    \/ \E d \in Descriptor : Advisory(d) \/ CAS(d) \/ Issue(d) \/ Release(d)
    \/ \E id \in ObjectIds : \E g \in Generations : Update(id, g)

Spec == Init /\ [][Next]_vars

--------------------------------------------------------------------------
TypeOKInvariant == TypeOK

(* Invariant 1 of the paper, for in-flight admitted descriptors. *)
InvTransferBinding ==
    \A d \in Descriptor :
        (OAT[d].pc = "flight") =>
            (MAP[d.id] = <<d.g, TRUE>> /\ PIN[d.id] > 0)

(* No payload ever issues under a binding that is already invalid at the
   issue state. Entries are added only at issue time, so this is the paper's
   zero-stale-payload property, not a tautology about the current map. *)
InvZeroRPE == IssuedPayload = {}

(* Pending reclaim rejects every new pin. *)
InvPendingBlocksPins ==
    \A id \in ObjectIds : PEND[id] => (PendingAdmits[id] = 0)

InvPinBounded ==
    \A id \in ObjectIds : PIN[id] \in 0..MaxPins

==========================================================================
