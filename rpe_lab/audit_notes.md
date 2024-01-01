# RPE Race Audit Notes (Mooncake Store source, read-only audit)

Background: verifying the Reclaimed-Payload Exposure race — after a client
learns replica locations via GetReplicaList it reads asynchronously; if the
master evicts the object before the transfer completes and reallocates the
slot to a new object, the in-flight read can pull back the new object's
bytes. Mooncake's protection is the TTL lease: after lease expiry the client
declares the Get failed and discards the bytes already read. This document
locates the exact code positions of the protection mechanism and provides
anchors for the instrumentation experiment.

> The audit is based on the `Mooncake-main` source snapshot (not a git repo).
> Line numbers come from actually reading the file contents.
> **Two deviations from the original plan**: (1) this branch's default lease
> TTL is **10000ms** (not the 5000ms assumed in the plan background), see Q3;
> (2) `transfer_task.cpp` does exist (`mooncake-store/src/transfer_task.cpp`,
> 1461 lines), but the lease check on the Get completion path is **not** in
> it — it is in `client_service.cpp`, see Q1.

## Summary table

| Question | Conclusion | Evidence (file:line) | Impact on experiment design |
|---|---|---|---|
| Q1 lease-check location | **Client-side local clock comparison, no master round-trip.** After `TransferRead` completes synchronously, `Client::Get` judges via `query_result.IsLeaseExpired()` (a client-side `steady_clock` deadline); on expiry it logs a WARNING and returns `ErrorCode::LEASE_EXPIRED`, and the data already read into `slices` is discarded. Three check points: `client_service.cpp:1160-1164` (whole-object Get), `client_service.cpp:1205-1209` (ranged Get), `client_service.cpp:1481-1487` (BatchGet) | `mooncake-store/src/client_service.cpp:1114-1172`, `1160-1164`; `mooncake-store/include/client_service.h:39-58`; `mooncake-store/src/client_service.cpp:1055-1066` | Instrumentation point = just before/inside the `if` at line 1160 of `Client::Get`; key, buffer pointer (`slices`), length, replica descriptor, and lease deadline are all accessible there |
| Q2 slot-reclaim timing | **Master-side metadata deletion frees immediately, no client confirmation.** BatchEvict pops replicas from the metadata into a local `deferred_replicas`; at `clear()` the destructor chain `Replica::~Replica` -> `unique_ptr<AllocatedBuffer>` -> `~AllocatedBuffer` -> `OffsetBufferAllocator::deallocate` returns the offset to the master-side free list, coalescing. Thereafter PutStart can hand the same offset to a new object, whose writing client RDMA-overwrites it — no client remount/reallocation needed | `mooncake-store/src/master_service.cpp:6389-6405`, `6713`; `mooncake-store/src/allocator.cpp:20-29`, `282-298`; `mooncake-store/src/offset_allocator.cpp:287-310` | Overwrite window = eviction completes (synchronous inside the single master process) -> next PutStart allocating that offset -> writing client's RDMA write; nothing in between notifies or waits for the reader |
| Q3 busy semantics | **Eviction is oblivious to in-flight client transfers.** busy = `Replica::refcnt_ > 0`; refcnt is only incremented by master-internal tasks (replication/promotion/offload); client Get/GetReplicaList never increments it. Evictability requires `refcnt==0` plus an expired lease. The lease is **per-object** (`ObjectMetadata::lease_timeout`); the master uses `system_clock`, the client converts to `steady_clock`. soft_pin is an optional per-object `soft_pin_timeout`, present only on objects Put with `with_soft_pin`, and by default does **not** prevent eviction (`allow_evict_soft_pinned_objects` defaults to true); only hard_pin is absolutely eviction-proof | `mooncake-store/include/replica.h:329-333`; `mooncake-store/src/master_service.cpp:6380-6383`, `4037`, `5308`, `6592-6604`; `mooncake-store/include/master_service.h:924-928`, `1091-1126` | In-flight reads are completely invisible to eviction; the only protection is the lease TTL. Producing RPE in the experiment only requires making transfer time exceed TTL. "Full-time pinning" requires Put with `with_hard_pin=true` or periodic lease renewal |
| Q4 lease-expiry error code | `ErrorCode::LEASE_EXPIRED = -707`; `OBJECT_NOT_FOUND = -704`; `OBJECT_HAS_LEASE = -706`. LEASE_EXPIRED is returned in exactly 3 places in the whole codebase (client_service.cpp:1163/1208/1485), each with a greppable WARNING log `lease_expired_before_data_transfer_completed key=`. Python receives int `-707` via `to_py_ret` | `mooncake-store/include/types.h:374-388`; `mooncake-store/src/types.cpp:28-31`; `mooncake-store/src/client_service.cpp:1161-1163`; `mooncake-store/include/utils.h:26-36` | Two ways to count guard_fires: (1) Python return value == -707; (2) grep client logs for `lease_expired_before_data_transfer_completed`. The -707 semantics are exactly "lease expired, data discarded", unconfusable with OBJECT_NOT_FOUND (-704) |
| Q5 pin/renewal API | **No dedicated RenewLease/Pin RPC.** But the master's `ExistKey`/`BatchExistKey`, like `GetReplicaList`/`BatchGetReplicaList`, grants a lease (GrantLease), so periodic Python `is_exist`/`batch_is_exist` calls (lightweight, no replica list transfer) renew the lease; `get_replica_desc`/`batch_get_replica_desc` go through the Query path and also renew. True full-time pinning exists only via Put with `with_hard_pin=true` (Python `ReplicateConfig.with_hard_pin` is exported); soft pin is evictable under the default config | `mooncake-store/src/master_service.cpp:2071-2097`, `2561-2564`; `mooncake-store/src/real_client.cpp:2096-2104`, `5513-5535`; `mooncake-integration/store/store_py.cpp:2285-2299`, `2927-2941`, `1834-1835` | Phase 5: periodic `batch_is_exist` (interval < TTL, recommended <= TTL/2) approximates full-time pinning; or Put with `with_hard_pin=True` for truly unevictable objects (note: pin state cannot be changed after a successful Put) |

## Q1 lease-check location (Get completion path)

The check happens **locally on the client**, with no master round-trip. After
receiving the master's GetReplicaList response, `Client::Query` converts the
master-provided `lease_ttl_ms` into a local `steady_clock` deadline:

```cpp
// mooncake-store/src/client_service.cpp:1055-1066
1055  tl::expected<QueryResult, ErrorCode> Client::Query(
1056      const std::string& object_key) {
1057      std::chrono::steady_clock::time_point start_time =
1058          std::chrono::steady_clock::now();
1059      auto result = master_client_.GetReplicaList(object_key);
1060      if (!result) {
1061          return tl::unexpected(result.error());
1062      }
1063      return QueryResult(
1064          std::move(result.value().replicas),
1065          start_time + std::chrono::milliseconds(result.value().lease_ttl_ms));
1066  }
```

`QueryResult` is a pure client-side value object; expiry is judged against the
client's own `steady_clock`:

```cpp
// mooncake-store/include/client_service.h:39-58
39   class QueryResult {
40      public:
41       /** @brief List of available replicas for the queried key */
42       const std::vector<Replica::Descriptor> replicas;
43       /** @brief Time point when the lease for this key expires */
44       const std::chrono::steady_clock::time_point lease_timeout;
45
46       QueryResult(std::vector<Replica::Descriptor>&& replicas_param,
47                   std::chrono::steady_clock::time_point lease_timeout_param)
48           : replicas(std::move(replicas_param)),
49             lease_timeout(lease_timeout_param) {}
50
51       bool IsLeaseExpired() const {
52           return std::chrono::steady_clock::now() >= lease_timeout;
53       }
54
55       bool IsLeaseExpired(std::chrono::steady_clock::time_point& now) const {
56           return now >= lease_timeout;
57       }
58   };
```

**The core discard branch (instrumentation point)**: `Client::Get` first
completes `TransferRead` synchronously (data has landed in `slices`), and only
then checks the lease; on expiry it returns an error and the caller discards
the buffer:

```cpp
// mooncake-store/src/client_service.cpp:1114-1164 (full Client::Get body: 1114-1172)
1114  tl::expected<void, ErrorCode> Client::Get(const std::string& object_key,
1115                                            const QueryResult& query_result,
1116                                            std::vector<Slice>& slices) {
1117      // Find the first complete replica
1118      Replica::Descriptor replica;
1119      ErrorCode err = FindFirstCompleteReplica(query_result.replicas, replica);
...
1133      auto t0_get = std::chrono::steady_clock::now();
1134      err = TransferRead(replica, slices);
...
1148      if (err != ErrorCode::OK) {
1149          LOG(ERROR) << "transfer_read_failed key=" << object_key;
1150          return tl::unexpected(err);
1151      }
...
1160      if (query_result.IsLeaseExpired()) {
1161          LOG(WARNING) << "lease_expired_before_data_transfer_completed key="
1162                       << object_key;
1163          return tl::unexpected(ErrorCode::LEASE_EXPIRED);
1164      }
```

The ranged-read overload (the 4-argument `Client::Get(..., uint64_t
src_offset)`, `client_service.cpp:1174-1211`) has an identical branch at
**1205-1209**; the batch path `Client::BatchGetWhenPreferSameNode` checks all
results against a single time point at **1481-1487**:

```cpp
// mooncake-store/src/client_service.cpp:1477-1487
1477      // As lease expired is a rare case, we check all the results with the same
1478      // time_point to avoid too many syscalls
1479      std::chrono::steady_clock::time_point now =
1480          std::chrono::steady_clock::now();
1481      for (size_t i = 0; i < object_keys.size(); ++i) {
1482          if (results[i].has_value() && query_results[i].IsLeaseExpired(now)) {
1483              LOG(WARNING) << "lease_expired_before_data_transfer_completed key="
1484                           << object_keys[i];
1485              results[i] = tl::unexpected(ErrorCode::LEASE_EXPIRED);
1486          }
1487      }
```

**Context accessible at this check point** (as of `client_service.cpp:1160`):
- `object_key` (`const std::string&`) — the object key;
- `slices` (`std::vector<Slice>&`, `Slice{void* ptr; size_t size;}`,
  types.h:473-476) — **the user buffer pointer and length, already filled with
  the bytes read back**; LogBeforeDiscard can read their contents directly;
- `query_result` (`const QueryResult&`) — full replica list plus the
  `lease_timeout` deadline;
- `replica` (`Replica::Descriptor`) — the replica actually selected (buffer
  address, size, transport endpoint);
- `err`, `t0_get`/`us_get` — transfer result and elapsed time.

Call-chain confirmation: Python `get_into` ->
`RealClient::get_into_range_internal` (real_client.cpp:3442) ->
`execute_ranged_read` (real_client.cpp:3215) -> for a full in-memory replica
read, **real_client.cpp:3320** `client_->Get(key, filtered_qr, slices)`; a
ranged read goes through the 4-argument overload at **3420/3435** — both merge
into the check points above. `TransferRead`
(client_service.cpp:3516-3541) -> `TransferData(..., TransferRequest::READ)`
is a synchronous call; when it returns, the data is already in slices.

## Q2 slot-reclaim timing

**Master-side metadata deletion frees immediately, without waiting for any
client confirmation.** The eviction thread wakes every 10ms
(`kEvictionThreadSleepMs = 10`, master_service.h:1568-1569), triggered when
`used_ratio > eviction_high_watermark_ratio_` (default 0.90) or when a PutStart
allocation failure sets `need_mem_eviction_` (master_service.cpp:2923).
`BatchEvict`'s evictability predicate and pop:

```cpp
// mooncake-store/src/master_service.cpp:6378-6405
6378      auto now = std::chrono::system_clock::now();
6379
6380      auto is_evictable_memory_replica = [](const Replica& replica) {
6381          return replica.is_memory_replica() && replica.is_completed() &&
6382                 replica.get_refcnt() == 0;
6383      };
...
6389      auto evict_replicas =
6390          [&, this](ObjectMetadata& metadata,
6391                    std::vector<std::vector<Replica>>& deferred_replicas) {
6392              const uint64_t before_charge = CompletedMemoryQuotaCharge(metadata);
6393              auto replicas = PopReplicasWithCacheTotalAccounting(
6394                  metadata, is_evictable_memory_replica);
6395              const size_t replica_count = replicas.size();
6396              if (!replicas.empty()) {
6397                  deferred_replicas.emplace_back(std::move(replicas));
6398              }
```

Popped replicas go into the local `deferred_replicas`, which is `clear()`ed
after each candidate is processed (**master_service.cpp:6713**); when the
metadata is no longer valid it is erased via `EraseMetadata` (6704). The
destructor chain:

```cpp
// mooncake-store/src/allocator.cpp:20-29
20   AllocatedBuffer::~AllocatedBuffer() {
21       // Note: This is an edge case. If the 'weak_ptr' is released, the segment
22       // has already been deallocated at this point, and its memory usage details
23       // (capacity/allocated) no longer need to be maintained.
24       auto alloc = allocator_.lock();
25       if (alloc) {
26           alloc->deallocate(this);
27           VLOG(1) << "buf_handle_deallocated size=" << size_;
28       }
29   }
```

```cpp
// mooncake-store/src/allocator.cpp:282-297 (OffsetBufferAllocator::deallocate)
282  void OffsetBufferAllocator::deallocate(AllocatedBuffer* handle) {
283      try {
284          // The OffsetAllocator handles deallocation automatically through RAII
285          // when the OffsetAllocationHandle goes out of scope
286          size_t freed_size = handle->size();
287          handle->offset_handle_.reset();
288          cur_size_.fetch_sub(freed_size);
```

`offset_handle_.reset()` triggers the RAII return; `__Allocator::free`
(offset_allocator.cpp:287-310) coalesces the range with adjacent free blocks
back into the free list. The whole process completes synchronously inside the
single master process — **the real memory lives in the mounted segments of
store clients, the master only keeps the books; freeing neither needs nor
sends any notification to the client holding that segment.**

Overwrite path: any later PutStart, via
`allocation_strategy_->Allocate(allocator_manager, ...)`
(master_service.cpp:2909-2912), allocates that offset from the same segment's
free list, and the new object's writing client RDMA-writes to that address
directly — **no remount, no segment reallocation**.

Note the two distinct release paths:
- an evicted COMPLETE replica: **freed immediately** (the destructor chain
  above, completed inside the BatchEvict call);
- a preempted PROCESSING replica (PutStart/UpsertStart preempting an older
  writer): goes into the `discarded_replicas_` TTL queue for delayed release
  (master_service.cpp:3614-3617), returned by
  `ReleaseExpiredDiscardedReplicas` (5877-5890) when due — that path guards
  against "the old writer is still RDMA-writing" and does not apply to this
  experiment's read-side race.

## Q3 busy / soft-pin semantics

**busy is oblivious to in-flight client transfers.** busy is simply the
replica's reference count:

```cpp
// mooncake-store/include/replica.h:329-333
329       [[nodiscard]] bool is_busy() const { return refcnt_.load() > 0; }
330
331       [[nodiscard]] static bool fn_is_busy(const Replica& replica) {
332           return replica.is_busy();
333       }
```

`inc_refcnt()` appears exactly 5 times in the whole codebase, all for
master-internal tasks: replication copy/move source (master_service.cpp:4037,
4317), promotion source (5308), offload enqueue (3286, 6214, 6459). **Client
Get/GetReplicaList never increments refcnt** (the full GetReplicaList body is
master_service.cpp:2515-2587 — it only GrantLeases). So "a client is
RDMA-reading right now" is invisible to the eviction logic, and `refcnt==0` in
the evictability predicate (Q2's `is_evictable_memory_replica`) cannot stop it
— the only barrier is an unexpired lease.

The lease marker is **per-object** (not per-replica); the master uses
`system_clock`:

```cpp
// mooncake-store/include/master_service.h:924-928
924          mutable std::chrono::system_clock::time_point lease_timeout
925              GUARDED_BY(lock);  // hard lease
926          mutable std::optional<std::chrono::system_clock::time_point>
927              soft_pin_timeout GUARDED_BY(lock);  // optional soft pin, only
928                                                  // set for vip objects
```

```cpp
// mooncake-store/include/master_service.h:1089-1102 (GrantLease)
1089          // Grant a lease with timeout as now() + ttl, only update if the new
1090          // timeout is larger
1091          void GrantLease(const uint64_t ttl, const uint64_t soft_ttl) const {
1092              SpinLocker locker(&lock);
1093              std::chrono::system_clock::time_point now =
1094                  std::chrono::system_clock::now();
1095              lease_timeout =
1096                  std::max(lease_timeout, now + std::chrono::milliseconds(ttl));
1097              if (soft_pin_timeout) {
1098                  soft_pin_timeout =
1099                      std::max(*soft_pin_timeout,
1100                               now + std::chrono::milliseconds(soft_ttl));
1101              }
1102          }
```

Expiry is likewise judged with the master's `system_clock` (`IsLeaseExpired`,
master_service.h:1117-1126). **The two sides use different clocks**: the master
uses `system_clock` for eviction exemption; the client uses `steady_clock` for
read validity (Q1); the RPC carries only the TTL in milliseconds
(`GetReplicaListResponse.lease_ttl_ms`). Lease granting happens at
GetReplicaList:

```cpp
// mooncake-store/src/master_service.cpp:2557-2565
2557          // Grant a lease to the object so it will not be removed
2558          // when the client is reading it.
2559          auto* ts = accessor.GetTenantState();
2560          if (ts) {
2561              GrantLeaseForGroup(*ts, key, metadata);
2562          } else {
2563              metadata.GrantLease(default_kv_lease_ttl_,
2564                                  default_kv_soft_pin_ttl_);
2565          }
```

Lease/soft-pin/hard-pin decisions in the eviction candidate scan:

```cpp
// mooncake-store/src/master_service.cpp:6592-6604
6592                          if (it->second.IsHardPinned()) continue;
6593                          bool has_evictable = can_evict_replicas(it->second);
6594                          if (has_evictable) shard_evictable_count++;
6595                          if (!it->second.IsLeaseExpired(now) || !has_evictable)
6596                              continue;
6597                          if (!it->second.IsSoftPinned(now)) {
6598                              local_candidates[t].push_back(
6599                                  {s, tenant_id, it->first,
6600                                   it->second.lease_timeout});
6601                          } else if (allow_evict_soft_pinned_objects_) {
6602                              local_soft_pin[t].push_back(
6603                                  it->second.lease_timeout);
6604                          }
```

Semantics summary:
- **hard_pin** (`ReplicateConfig::with_hard_pin`, replica.h:85):
  `IsHardPinned()` is skipped outright — absolutely eviction-proof;
- **soft_pin** (`with_soft_pin`, replica.h:84): per-object `soft_pin_timeout`
  (master_service.h:926-928), set at Put time, extended by
  `default_kv_soft_pin_ttl` on every lease grant (GrantLease, 1097-1101). For
  the `IsSoftPinned` decision see master_service.h:1129-1139. **By default it
  does not prevent eviction** — with `allow_evict_soft_pinned_objects_`
  default true, soft-pinned objects enter the second-priority eviction list
  (6601-6604, and the second scan at 6791-6826);
- re-validation at master_service.cpp:6688-6690 (first pass) and 6753-6757,
  6815-6820 (second pass).

rpc_service.cpp is only a thin forwarding layer
(`WrappedMasterService::GetReplicaList` -> `master_service_.GetReplicaList`,
rpc_service.cpp:177-187; ExistKey likewise, 37-44) and contains no lease logic.

## Q4 error codes

```cpp
// mooncake-store/include/types.h:373-388
373      // Object errors (Range: -703 to -712)
374      REPLICA_IS_NOT_READY = -703,   ///< Replica is not ready.
375      OBJECT_NOT_FOUND = -704,       ///< Object not found.
376      OBJECT_ALREADY_EXISTS = -705,  ///< Object already exists.
377      OBJECT_HAS_LEASE = -706,       ///< Object has lease.
378      LEASE_EXPIRED = -707,  ///< Lease expired before data transfer completed.
379      OBJECT_HAS_REPLICATION_TASK =
380          -708,  ///< Object has ongoing replication task.
...
388      OBJECT_REPLICA_BUSY = -714,  ///< Object replicas have non-zero refcnt.
```

toString mapping: `mooncake-store/src/types.cpp:28-31`
(`OBJECT_NOT_FOUND`/`OBJECT_HAS_LEASE`/`LEASE_EXPIRED`).

Where each error code sits on the Get path:
- `LEASE_EXPIRED (-707)`: returned in exactly 3 places in the whole codebase —
  client_service.cpp:1163, 1208, 1485 (see the Q1 excerpts), each accompanied
  by the WARNING log `lease_expired_before_data_transfer_completed key=<key>`.
  **This is the precise counting point of guard_fires**;
- `OBJECT_NOT_FOUND (-704)`: returned by the master's `GetReplicaList` when the
  object does not exist (master_service.cpp:2528-2531, VLOG
  `info=object_not_found`), propagated through `Client::Query`; a query
  failure before the client Get takes this path and **never** reaches the lease
  check at 1160;
- `OBJECT_HAS_LEASE (-706)`: returned when a non-force `Remove` hits an
  unexpired lease (master_service.cpp:4497-4500, VLOG
  `error=object_has_lease`) — same lease-protection family, but not on the Get
  path;
- `OBJECT_REPLICA_BUSY (-714)`: returned when UpsertStart hits refcnt>0
  (master_service.cpp:3658-3660).

Python-side presentation: `to_py_ret` (utils.h:26-36) converts a
`tl::expected` error to an int, so Python `get`/`get_into` return `-707` on
lease expiry. `get_buffer`-style interfaces return a null pointer / empty
optional on failure.

## Q5 pin / renewal / ExistKey / GetReplicaList APIs

**There is no dedicated RenewLease/Pin RPC** (neither master_client.h nor
rpc_service.cpp has one). But the following APIs all grant a lease on the
master (`GrantLease(default_kv_lease_ttl_, default_kv_soft_pin_ttl_)`) and can
be used for periodic renewal:

C++ side:
- `Client::Query(object_key)` -> `tl::expected<QueryResult, ErrorCode>`
  (client_service.h:128; implementation client_service.cpp:1055-1066) ->
  grants via the master's `GetReplicaList`;
- `Client::BatchQuery(object_keys)` (client_service.h:147-151) -> grants via
  the master's `BatchGetReplicaList` (master_service.cpp:2713);
- `Client::IsExist(key)` (client_service.cpp:2989-2992) -> master `ExistKey`,
  **grants whenever a complete replica exists** (master_service.cpp:2083-2093);
  `Client::BatchIsExist` (client_service.cpp:2994 onward) -> `BatchExistKey`
  grants (master_service.cpp:2156-2158);
- `RealClient::isExist(key) -> int` (real_client.cpp:2096-2104, returns
  1/0/negative error code), `batchIsExist` (2106 onward), `get_replica_desc`
  (5513-5535), `batch_get_replica_desc` (5536 onward), `batch_query`
  (3645-3653);
- master_client direct: `MasterClient::ExistKey` (master_client.h:112),
  `BatchExistKey` (120), `GetReplicaList` (164-167), `BatchGetReplicaList`
  (187-190).

Evidence that master-side `ExistKey` grants a lease (the basis for
renewal-approximates-pinning):

```cpp
// mooncake-store/src/master_service.cpp:2082-2094
2082      const auto& metadata = accessor.Get();
2083      if (metadata.HasReplica(&Replica::fn_is_completed)) {
2084          // Grant a lease to the object as it may be further used by the
2085          // client.
2086          auto* ts = accessor.GetTenantState();
2087          if (ts) {
2088              GrantLeaseForGroup(*ts, key, metadata);
2089          } else {
2090              metadata.GrantLease(default_kv_lease_ttl_,
2091                                  default_kv_soft_pin_ttl_);
2092          }
2093          return true;
2094      }
```

Python bindings (`mooncake-integration/store/store_py.cpp`, module `store`,
class `MooncakeDistributedStore`):
- `is_exist(key)` (store_py.cpp:2285-2289), `batch_is_exist(keys)`
  (2290-2299) — **lightest-weight renewal**, no replica list transfer;
- `get_replica_desc(key)` (2927-2933), `batch_get_replica_desc(keys)`
  (2934-2941) — go through the Query path: renewal + replica locations;
- `get_size(key)` (2311-2315);
- `ReplicateConfig` exports `with_soft_pin` / `with_hard_pin`
  (store_py.cpp:1834-1835; defined at replica.h:81-85), passable to the `put`
  family — `with_hard_pin=True` is true full-time eviction immunity (Q3);
- note: pin state can only be set at Put time; there is no after-the-fact
  modification API; soft pin does not prevent eviction by default
  (`allow_evict_soft_pinned_objects` default true).

Phase 5 recommendation: a periodic thread calling `batch_is_exist` (interval
<= TTL/2; GrantLease takes max so it can only extend, never shorten)
approximates full-time pinning; use `with_hard_pin=True` Put when absolute
eviction immunity is required.

## Instrumentation-point recommendation (LogBeforeDiscard)

**Primary instrumentation point: `mooncake-store/src/client_service.cpp`,
function `Client::Get(const std::string& object_key, const QueryResult&
query_result, std::vector<Slice>& slices)`, inside the `if
(query_result.IsLeaseExpired())` at line 1160, before the `return` (i.e.
between 1161 and 1163).**

Rationale:
- At this point `slices` has been filled by `TransferRead` (line 1134,
  synchronous RDMA read) — what was read is exactly the possibly-overwritten
  post-eviction bytes that we want to record passively;
- Reaching this branch means "lease expired, data will be discarded", matching
  guard_fires semantics one-to-one;
- Accessible variables: `object_key` (key), `slices` (`std::vector<Slice>&`,
  elements `{ptr, size}` — dump per slice or sample a hash),
  `query_result.replicas` (expected replica metadata, including the source
  `buffer_address_`, size, endpoint), `query_result.lease_timeout` (overstay
  `now - lease_timeout` computable), `replica` (the replica actually read),
  `us_get` (transfer time, from which the overwrite window can be estimated).

Secondary instrumentation points (cover all read paths; recommended to
instrument as well):
- `client_service.cpp:1205-1209`, the 4-argument ranged overload
  `Client::Get(..., uint64_t src_offset)` — used by `get_into` with an offset;
  same context plus `src_offset`;
- `client_service.cpp:1481-1487`, `Client::BatchGetWhenPreferSameNode` —
  batch path; the corresponding buffer can be retrieved via
  `slices.find(object_keys[i])`;
- if the experiment goes through `RealClient::get_into` (Python `get_into`),
  note that `execute_ranged_read` (real_client.cpp:3215) logs an ERROR at
  3321-3325 after `client_->Get` returns an error and propagates it — this is
  the last stop of the "discard action", but recording buffer contents is more
  precise at the three client_service.cpp points (there the slices are the
  final user buffer or its direct source).

Counting and logs:
- guard_fires = occurrences of `lease_expired_before_data_transfer_completed
  key=` in the client log (WARNING level, on by default), or the count of
  Python return values == `-707`;
- the control group can grep the master log for `[EVICT-RESULT]
  evicted_count=` (master_service.cpp:6909) and `action=evict_objects` (6886,
  VLOG(1)) to confirm evictions really happened.

## master startup flags confirmation

All of the following flags exist in `mooncake-store/src/master.cpp`, spelled
as in the original plan:

| flag | type/definition | default | definition site | constant source |
|---|---|---|---|---|
| `--default_kv_lease_ttl` | `DEFINE_string` (accepts duration strings like "10000", "500ms", "10s"; validator at master.cpp:130) | `"10000"` (**10000ms, not 5000ms**) | master.cpp:121-123; flag-default constant master.cpp:40 | `DEFAULT_DEFAULT_KV_LEASE_TTL = 10000` (types.h:85-86), pinned by static_assert master.cpp:33-35 |
| `--default_kv_soft_pin_ttl` | `DEFINE_string` (duration string) | `"1800000"` (30 minutes) | master.cpp:124-126; constant master.cpp:41 | `DEFAULT_KV_SOFT_PIN_TTL_MS = 30*60*1000` (types.h:87-88) |
| `--allow_evict_soft_pinned_objects` | `DEFINE_bool` | **true** | master.cpp:127-129 | `DEFAULT_ALLOW_EVICT_SOFT_PINNED_OBJECTS = true` (types.h:89) |
| `--eviction_ratio` | `DEFINE_double` (validator limits to [0,1], master.cpp:159-165) | **0.05** | master.cpp:132-133 | `DEFAULT_EVICTION_RATIO = 0.05` (types.h:90) |
| `--eviction_high_watermark_ratio` | `DEFINE_double` | **0.90** | master.cpp:134-136 | `DEFAULT_EVICTION_HIGH_WATERMARK_RATIO = 0.90` (types.h:91) |

Related supplementary flags:
- `--nof_eviction_ratio` = 0.05, `--nof_eviction_high_watermark_ratio` = 0.90
  (master.cpp:137-141, for NoF SSD);
- `--put_start_discard_timeout_sec` (master.cpp:311-313),
  `--put_start_release_timeout_sec` (314-316) — for delayed release of
  PROCESSING replicas;
- `--memory_allocator` default `"offset"` (master.cpp:286-288) — selects the
  `OffsetBufferAllocator`/offset_allocator.cpp free-list path (Q2);
- the eviction thread scan period is not a flag; it is hard-coded
  `kEvictionThreadSleepMs = 10`ms (master_service.h:1568-1569).

Config injection path: master.cpp:447-450 writes flag values into
`MasterServiceConfig` (`default_kv_lease_ttl`/`default_kv_soft_pin_ttl`),
frozen at MasterService construction (master_service.cpp:176-182).

---

## Appendix: F1-F5 supplementary audit (eviction mechanism and data path, rechecked 2026-07-18)

**F1 Eviction victim selection = ascending lease_timeout (oldest lease
first)**. Candidates = objects with an expired lease and an evictable replica;
`nth_element` takes the first evict_num in ascending lease_timeout order
(master_service.cpp:6659-6668); candidate collection at 6592-6598 (skips hard
pin / unexpired / soft-pin). **Key point**: PutEnd expires the lease
immediately (`GrantLease(0)`, master_service.cpp:3305-3308), so never-read
objects always sort ahead of read ones (their lease_timeout ≈ write time <
read time + TTL). Empirical corroboration: in the smoke run all 100 old
objects survived while 210 new objects were evicted (smoke_log.md).

**F2 Put replica placement defaults to random** (master.cpp:288-290
`--allocation_strategy`), a random-start sequential scan; only with
`local_first` and replica_num=1 does the writer's own segment take priority
(master_service.cpp:2857-2899).

**F3 Read replica selection**: `FindFirstCompleteReplica` linearly takes the
first COMPLETE in the list (client_service.cpp:3900-3913), no local
preference.

**F4 memcpy fast path**: with `MC_STORE_MEMCPY` unset it is auto-enabled per
`engine_.isTcpOnly()` (transfer_task.cpp:948-958); `LOCAL_MEMCPY` is used only
when the replica endpoint == **this process's** endpoint (same-host
cross-process goes over TCP; transfer_task.cpp:1395-1425). **The
MemcpyWorkerPool is fixed at 1 thread with an unbounded queue**
(transfer_task.cpp:581-625).

**F5 The comment at master_service.cpp:1753** concerns the race between
"eviction vs in-flight offload task" (EraseMetadata cleaning up orphaned
offload tasks), not an acknowledgment of in-flight client reads — **the read
side has no fallback at all after lease expiry** (no refcnt, no task
registration; offload goes through `inc_refcnt` (6459) while Get does not).
The official protection for the read side is only the lease TTL (comment at
GetReplicaList, master_service.cpp:2557-2558).

**Additional observation**: `Client::BatchGetWhenPreferSameNode`
(client_service.cpp:1221-1366) has **no IsLeaseExpired check** (only
1160/1205/1482 exist in the whole file) — the batch same-node path is not
covered by the lease guard; this experiment's workload does not use that path,
so it is recorded as an attack-surface observation without further analysis.
