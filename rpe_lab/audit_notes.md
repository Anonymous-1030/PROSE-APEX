# RPE 竞态审计笔记（Mooncake Store 源码，只读审计）

背景：验证 Reclaimed-Payload Exposure 竞态——client 经 GetReplicaList 拿到副本位置后异步读，若 master 在传输完成前驱逐对象并把槽位重新分给新对象，在途读可能读到新对象字节；Mooncake 的保护是 TTL 租约，租约过期后 client 判定 Get 失败并丢弃已读数据。本文档定位保护机制的确切代码位置，为插桩实验提供锚点。

> 审计基于源码快照 `Mooncake-main`（非 git 仓库）。行号来自实际读取的文件内容。
> **与用户计划的两处出入**：① 本分支默认租约 TTL 是 **10000ms**（不是背景里说的 5000ms），见 Q3；② `transfer_task.cpp` 存在（`mooncake-store/src/transfer_task.cpp`, 1461 行），但 Get 完成路径上的租约检查**不在**其中，而在 `client_service.cpp`，见 Q1。

## 结论总表

| 问题 | 结论 | 证据（文件:行号） | 对实验设计的影响 |
|---|---|---|---|
| Q1 租约检查位置 | **client 本地时钟比对，不回 master 查询**。`Client::Get` 在 `TransferRead` 同步完成后用 `query_result.IsLeaseExpired()`（client 侧 `steady_clock` 截止时间）判定；过期则打 WARNING 日志并返回 `ErrorCode::LEASE_EXPIRED`，已读进 `slices` 的数据被丢弃。三处检查点：`client_service.cpp:1160-1164`（整对象 Get）、`client_service.cpp:1205-1209`（ranged Get）、`client_service.cpp:1481-1487`（BatchGet） | `mooncake-store/src/client_service.cpp:1114-1172`、`1160-1164`；`mooncake-store/include/client_service.h:39-58`；`mooncake-store/src/client_service.cpp:1055-1066` | 插桩点 = `Client::Get` 第 1160 行 `if` 之前/之内；此处 key、buffer 指针（`slices`）、长度、副本 descriptor、租约截止时间全部可访问 |
| Q2 槽位回收时机 | **master 侧元数据删除即释放，不等 client 确认**。BatchEvict 把 replica 从 metadata 弹出到局部 `deferred_replicas`，`clear()` 时析构链 `Replica::~Replica` → `unique_ptr<AllocatedBuffer>` → `~AllocatedBuffer` → `OffsetBufferAllocator::deallocate` → offset 回到 master 侧空闲链表并合并。此后 PutStart 可把同一 offset 分给新对象，由写入方 client RDMA 覆写，无需任何 client 重新 mount/分配 | `mooncake-store/src/master_service.cpp:6389-6405`、`6713`；`mooncake-store/src/allocator.cpp:20-29`、`282-298`；`mooncake-store/src/offset_allocator.cpp:287-310` | 覆写窗口 = 驱逐完成（master 单进程内同步完成）→ 下一次 PutStart 分配到该 offset → 写入 client RDMA 写；中间无任何对读方的通知或等待 |
| Q3 busy 语义 | **驱逐不感知在途 client 传输**。busy = `Replica::refcnt_ > 0`，refcnt 只被 master 内部任务（replication/promotion/offload）递增，client 的 Get/GetReplicaList 从不递增；可驱逐判定要求 `refcnt==0` 且租约已过期。租约 **per-object**（`ObjectMetadata::lease_timeout`），master 用 `system_clock`，client 用 `steady_clock` 各自换算。soft_pin 是 per-object 可选 `soft_pin_timeout`，仅 Put 时 `with_soft_pin` 的对象有，默认**不**阻止驱逐（`allow_evict_soft_pinned_objects` 默认 true）；hard_pin 才绝对免驱逐 | `mooncake-store/include/replica.h:329-333`；`mooncake-store/src/master_service.cpp:6380-6383`、`4037`、`5308`、`6592-6604`；`mooncake-store/include/master_service.h:924-928`、`1091-1126` | 在途读对驱逐完全不可见，唯一保护是租约 TTL；实验里制造 RPE 只需让传输时间超过 TTL。要"全程 pin"须用 `with_hard_pin=true` Put 或周期性续租 |
| Q4 租约过期错误码 | `ErrorCode::LEASE_EXPIRED = -707`；`OBJECT_NOT_FOUND = -704`；`OBJECT_HAS_LEASE = -706`。LEASE_EXPIRED 全代码库仅 3 处返回（client_service.cpp:1163/1208/1485），各伴随可 grep 的 WARNING 日志 `lease_expired_before_data_transfer_completed key=`。Python 侧经 `to_py_ret` 收到 int `-707` | `mooncake-store/include/types.h:374-388`；`mooncake-store/src/types.cpp:28-31`；`mooncake-store/src/client_service.cpp:1161-1163`；`mooncake-store/include/utils.h:26-36` | guard_fires 两种计数法：① Python 返回值 == -707；② grep client 日志 `lease_expired_before_data_transfer_completed`。-707 语义恰好是"租约过期、数据已丢弃"，不会与 OBJECT_NOT_FOUND(-704) 混淆 |
| Q5 pin/续租 API | **无专用 RenewLease/Pin RPC**。但 master 的 `ExistKey`/`BatchExistKey` 与 `GetReplicaList`/`BatchGetReplicaList` 一样会授租（GrantLease），所以 Python 周期调 `is_exist`/`batch_is_exist`（轻量、不传副本列表）即可续租；`get_replica_desc`/`batch_get_replica_desc` 走 Query 路径同样续租。真正的"全程 pin"只有 Put 时 `with_hard_pin=true`（Python `ReplicateConfig.with_hard_pin` 已导出）；soft pin 可被默认配置驱逐 | `mooncake-store/src/master_service.cpp:2071-2097`、`2561-2564`；`mooncake-store/src/real_client.cpp:2096-2104`、`5513-5535`；`mooncake-integration/store/store_py.cpp:2285-2299`、`2927-2941`、`1834-1835` | Phase 5：周期 `batch_is_exist`（间隔 < TTL，建议 ≤ TTL/2）近似全程 pin；或 Put 时 `with_hard_pin=True` 获得真正免驱逐对象（注意 Put 成功后无法事后改 pin 状态） |

## Q1 租约检查位置（Get 完成路径）

检查发生在 **client 本地**，不回 master 查询。`Client::Query` 在收到 master 的 GetReplicaList 响应后，把 master 给的 `lease_ttl_ms` 换算成本机 `steady_clock` 截止时间：

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

`QueryResult` 是纯 client 侧值对象，过期判定用 client 本机 `steady_clock`：

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

**核心丢弃分支（插桩点）**：`Client::Get` 先同步完成 `TransferRead`（数据已落进 `slices`），随后才查租约；过期即返回错误，调用方丢弃 buffer：

```cpp
// mooncake-store/src/client_service.cpp:1114-1164（函数 Client::Get 完整体见 1114-1172）
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

ranged 读重载（4 参数版 `Client::Get(..., uint64_t src_offset)`，`client_service.cpp:1174-1211`）在 **1205-1209** 有完全相同的分支；批量路径 `Client::BatchGetWhenPreferSameNode` 在 **1481-1487** 用统一时间点检查后逐个改写结果：

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

**此检查点可访问的上下文**（以 `client_service.cpp:1160` 为准）：
- `object_key`（`const std::string&`）——对象 key；
- `slices`（`std::vector<Slice>&`，`Slice{void* ptr; size_t size;}`，types.h:473-476）——**已填满读回字节的用户 buffer 指针与长度**，LogBeforeDiscard 可直接读取其内容；
- `query_result`（`const QueryResult&`）——完整副本列表 + `lease_timeout` 截止时间；
- `replica`（`Replica::Descriptor`）——实际选中的副本（buffer 地址、size、transport endpoint）；
- `err`、`t0_get`/`us_get`——传输结果与耗时。

调用链确认：Python `get_into` → `RealClient::get_into_range_internal`（real_client.cpp:3442）→ `execute_ranged_read`（real_client.cpp:3215）→ 内存副本全量读在 **real_client.cpp:3320** `client_->Get(key, filtered_qr, slices)`，ranged 读在 **3420/3435** 走 4 参数重载——均汇入上述检查点。`TransferRead`（client_service.cpp:3516-3541）→ `TransferData(..., TransferRequest::READ)` 为同步调用，返回时数据已在 slices 中。

## Q2 槽位回收时机

**master 侧元数据删除即释放，不等任何 client 确认。** 驱逐线程每 10ms 唤醒一次（`kEvictionThreadSleepMs = 10`，master_service.h:1568-1569），触发条件 `used_ratio > eviction_high_watermark_ratio_`（默认 0.90）或 PutStart 分配失败置位 `need_mem_eviction_`（master_service.cpp:2923）。`BatchEvict` 的可驱逐判定与弹出：

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

弹出的 replica 放进局部 `deferred_replicas`，每个候选处理完即 `deferred_replicas.clear()`（**master_service.cpp:6713**）；metadata 不再 valid 时 `EraseMetadata`（6704）。析构链：

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
// mooncake-store/src/allocator.cpp:282-297（OffsetBufferAllocator::deallocate）
282  void OffsetBufferAllocator::deallocate(AllocatedBuffer* handle) {
283      try {
284          // The OffsetAllocator handles deallocation automatically through RAII
285          // when the OffsetAllocationHandle goes out of scope
286          size_t freed_size = handle->size();
287          handle->offset_handle_.reset();
288          cur_size_.fetch_sub(freed_size);
```

`offset_handle_.reset()` 触发 RAII 归还，`__Allocator::free`（offset_allocator.cpp:287-310）把区间与相邻空闲块合并回空闲链表。整个过程在 master 单进程内同步完成——**真实内存在 store client 的挂载 segment 里，master 只是记账；释放不需要、也不会通知持有该 segment 的 client**。

覆写路径：之后任意 PutStart 经 `allocation_strategy_->Allocate(allocator_manager, ...)`（master_service.cpp:2909-2912）从同一 segment 的空闲链表分到这段 offset，新对象的写入方 client 直接 RDMA 写到该地址——**无需重新 mount、无需重新分配 segment**。

注意区分两条释放路径：
- 被驱逐的 COMPLETE replica：**立即释放**（上述析构链，BatchEvict 调用内完成）；
- 被抢占的 PROCESSING replica（PutStart/UpsertStart 抢占旧写入）：进 `discarded_replicas_` TTL 队列延迟释放（master_service.cpp:3614-3617），由 `ReleaseExpiredDiscardedReplicas`（5877-5890）到期归还——这条路径防的是"旧写方还在 RDMA 写"，对本实验的读侧竞态不适用。

## Q3 busy / soft-pin 语义

**busy 不感知在途 client 传输。** busy 就是 replica 的引用计数：

```cpp
// mooncake-store/include/replica.h:329-333
329       [[nodiscard]] bool is_busy() const { return refcnt_.load() > 0; }
330
331       [[nodiscard]] static bool fn_is_busy(const Replica& replica) {
332           return replica.is_busy();
333       }
```

全代码库 `inc_refcnt()` 仅 5 处，全部是 master 内部任务：replication copy/move 源（master_service.cpp:4037、4317）、promotion 源（5308）、offload 入队（3286、6214、6459）。**client 的 Get/GetReplicaList 从不递增 refcnt**（GetReplicaList 全文见 master_service.cpp:2515-2587，只 GrantLease）。所以"有 client 正在 RDMA 读"对驱逐逻辑不可见，可驱逐判定（Q2 的 `is_evictable_memory_replica`）里的 `refcnt==0` 拦不住它——唯一屏障是租约未过期。

租约标记 **per-object**（不是 per-replica），master 用 `system_clock`：

```cpp
// mooncake-store/include/master_service.h:924-928
924          mutable std::chrono::system_clock::time_point lease_timeout
925              GUARDED_BY(lock);  // hard lease
926          mutable std::optional<std::chrono::system_clock::time_point>
927              soft_pin_timeout GUARDED_BY(lock);  // optional soft pin, only
928                                                  // set for vip objects
```

```cpp
// mooncake-store/include/master_service.h:1089-1102（GrantLease）
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

过期判定同样用 master 的 `system_clock`（`IsLeaseExpired`，master_service.h:1117-1126）。**两侧时钟不同**：master 用 `system_clock` 判定驱逐豁免，client 用 `steady_clock` 判定读取有效性（Q1），RPC 只传 TTL 毫秒数（`GetReplicaListResponse.lease_ttl_ms`）。授租发生在 GetReplicaList：

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

驱逐候选扫描中的 lease/soft-pin/hard-pin 判定：

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

语义小结：
- **hard_pin**（`ReplicateConfig::with_hard_pin`，replica.h:85）：`IsHardPinned()` 直接跳过，绝对免驱逐；
- **soft_pin**（`with_soft_pin`，replica.h:84）：per-object `soft_pin_timeout`（master_service.h:926-928），Put 时置位，每次授租时用 `default_kv_soft_pin_ttl` 顺延（GrantLease，1097-1101）。`IsSoftPinned` 判定见 master_service.h:1129-1139。**默认不防驱逐**——`allow_evict_soft_pinned_objects_` 默认 true 时 soft-pin 对象进第二优先级驱逐名单（6601-6604，及 6791-6826 的第二遍扫描）；
- 二次确认在 master_service.cpp:6688-6690（第一遍 re-validate）与 6753-6757、6815-6820（第二遍）。

rpc_service.cpp 只是薄转发层（`WrappedMasterService::GetReplicaList` → `master_service_.GetReplicaList`，rpc_service.cpp:177-187；ExistKey 同，37-44），不含租约逻辑。

## Q4 错误码

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

toString 映射：`mooncake-store/src/types.cpp:28-31`（`OBJECT_NOT_FOUND`/`OBJECT_HAS_LEASE`/`LEASE_EXPIRED`）。

各错误码在 Get 路径的位置：
- `LEASE_EXPIRED (-707)`：全代码库仅 3 处返回——client_service.cpp:1163、1208、1485（见 Q1 摘录），均伴随 WARNING 日志 `lease_expired_before_data_transfer_completed key=<key>`。**这是 guard_fires 的精确计数点**；
- `OBJECT_NOT_FOUND (-704)`：master `GetReplicaList` 对象不存在时返回（master_service.cpp:2528-2531，VLOG `info=object_not_found`），经 `Client::Query` 透传；client Get 前的 query 失败走这条，**不会**到达 1160 的租约检查；
- `OBJECT_HAS_LEASE (-706)`：非 force 的 `Remove` 撞见未过期租约时返回（master_service.cpp:4497-4500，VLOG `error=object_has_lease`）——与租约保护同源，但不在 Get 路径；
- `OBJECT_REPLICA_BUSY (-714)`：UpsertStart 撞见 refcnt>0 时返回（master_service.cpp:3658-3660）。

Python 侧呈现：`to_py_ret`（utils.h:26-36）把 `tl::expected` 的错误转成 int，所以 Python `get`/`get_into` 租约过期时返回 `-707`。`get_buffer` 类接口失败返回空指针/空 optional。

## Q5 pin / 续租 / ExistKey / GetReplicaList API

**没有专用 RenewLease/Pin RPC**（master_client.h、rpc_service.cpp 中均无）。但以下 API 都会在 master 侧授租（`GrantLease(default_kv_lease_ttl_, default_kv_soft_pin_ttl_)`），可用于周期续租：

C++ 侧：
- `Client::Query(object_key)` → `tl::expected<QueryResult, ErrorCode>`（client_service.h:128；实现 client_service.cpp:1055-1066）→ 经 master `GetReplicaList` 授租；
- `Client::BatchQuery(object_keys)`（client_service.h:147-151）→ master `BatchGetReplicaList` 授租（master_service.cpp:2713）；
- `Client::IsExist(key)`（client_service.cpp:2989-2992）→ master `ExistKey`，**有 complete replica 即授租**（master_service.cpp:2083-2093）；`Client::BatchIsExist`（client_service.cpp:2994 起）→ `BatchExistKey` 授租（master_service.cpp:2156-2158）；
- `RealClient::isExist(key) -> int`（real_client.cpp:2096-2104，返回 1/0/负错误码）、`batchIsExist`（2106 起）、`get_replica_desc`（5513-5535）、`batch_get_replica_desc`（5536 起）、`batch_query`（3645-3653）；
- master_client 直连：`MasterClient::ExistKey`（master_client.h:112）、`BatchExistKey`（120）、`GetReplicaList`（164-167）、`BatchGetReplicaList`（187-190）。

master 侧 `ExistKey` 授租证据（续租近似 pin 的依据）：

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

Python 绑定（`mooncake-integration/store/store_py.cpp`，模块 `store`，类 `MooncakeDistributedStore`）：
- `is_exist(key)`（store_py.cpp:2285-2289）、`batch_is_exist(keys)`（2290-2299）——**最轻量续租**，不传副本列表；
- `get_replica_desc(key)`（2927-2933）、`batch_get_replica_desc(keys)`（2934-2941）——走 Query 路径，续租 + 拿副本位置；
- `get_size(key)`（2311-2315）；
- `ReplicateConfig` 导出 `with_soft_pin` / `with_hard_pin`（store_py.cpp:1834-1835；定义 replica.h:81-85），`put` 系列接口可传——`with_hard_pin=True` 是真正的全程免驱逐（Q3）；
- 注意：pin 状态只能在 Put 时设定，无事后修改 API；soft pin 默认不防驱逐（`allow_evict_soft_pinned_objects` 默认 true）。

Phase 5 建议：周期线程调 `batch_is_exist`（间隔 ≤ TTL/2，`GrantLease` 取 max 所以只会延长不会缩短）近似全程 pin；需要绝对免驱逐时改用 `with_hard_pin=True` Put。

## 插桩点建议（LogBeforeDiscard）

**首选插桩点：`mooncake-store/src/client_service.cpp`，函数 `Client::Get(const std::string& object_key, const QueryResult& query_result, std::vector<Slice>& slices)`，第 1160 行 `if (query_result.IsLeaseExpired())` 内部、`return` 之前（即 1161 与 1163 之间）。**

理由：
- 此处 `slices` 已被 `TransferRead`（1134 行，同步 RDMA 读）填满——读到的就是被驱逐后可能已被覆写的字节，正是要被动记录的内容；
- 只有走到这个分支才说明"租约过期、数据将被丢弃"，与 guard_fires 语义一一对应；
- 可访问变量：`object_key`（key）、`slices`（`std::vector<Slice>&`，元素 `{ptr, size}`，逐 slice dump 或采样哈希）、`query_result.replicas`（期望的副本 metadata，含源 buffer 地址 `buffer_address_`、size、endpoint）、`query_result.lease_timeout`（可算过期时长 `now - lease_timeout`）、`replica`（实际读的副本 descriptor）、`us_get`（传输耗时，可推算覆写窗口）。

次级插桩点（覆盖全部读路径，建议一并打）：
- `client_service.cpp:1205-1209`，4 参数 ranged 重载 `Client::Get(..., uint64_t src_offset)`——`get_into` 带 offset 时走这里，上下文同上，另有 `src_offset`；
- `client_service.cpp:1481-1487`，`Client::BatchGetWhenPreferSameNode`——批量路径，可用 `slices.find(object_keys[i])` 取回对应 buffer；
- 若实验走 `RealClient::get_into`（Python `get_into`），注意 `execute_ranged_read`（real_client.cpp:3215）在 `client_->Get` 返回错误后于 3321-3325 打 ERROR 日志并把错误透传——这里是"丢弃动作"的最后一站，但 buffer 内容记录放在 client_service.cpp 的三处更精确（那里的 slices 就是最终用户 buffer 或其直接来源）。

计数与日志：
- guard_fires = client 日志中 `lease_expired_before_data_transfer_completed key=` 出现次数（WARNING 级，默认输出），或 Python 返回值 == `-707` 的次数；
- 对照组可 grep master 日志 `[EVICT-RESULT] evicted_count=`（master_service.cpp:6909）与 `action=evict_objects`（6886，VLOG(1)）确认驱逐真实发生。

## master 启动 flags 确认

以下 flag 全部存在于 `mooncake-store/src/master.cpp`，拼写与用户计划一致：

| flag | 类型/定义 | 默认值 | 定义位置 | 常量出处 |
|---|---|---|---|---|
| `--default_kv_lease_ttl` | `DEFINE_string`（支持 "10000"、"500ms"、"10s" 等时长串，validator 在 master.cpp:130） | `"10000"`（**10000ms，不是 5000ms**） | master.cpp:121-123；旗标默认值常量 master.cpp:40 | `DEFAULT_DEFAULT_KV_LEASE_TTL = 10000`（types.h:85-86），static_assert 锁定 master.cpp:33-35 |
| `--default_kv_soft_pin_ttl` | `DEFINE_string`（时长串） | `"1800000"`（30 分钟） | master.cpp:124-126；常量 master.cpp:41 | `DEFAULT_KV_SOFT_PIN_TTL_MS = 30*60*1000`（types.h:87-88） |
| `--allow_evict_soft_pinned_objects` | `DEFINE_bool` | **true** | master.cpp:127-129 | `DEFAULT_ALLOW_EVICT_SOFT_PINNED_OBJECTS = true`（types.h:89） |
| `--eviction_ratio` | `DEFINE_double`（validator 限 [0,1]，master.cpp:159-165） | **0.05** | master.cpp:132-133 | `DEFAULT_EVICTION_RATIO = 0.05`（types.h:90） |
| `--eviction_high_watermark_ratio` | `DEFINE_double` | **0.90** | master.cpp:134-136 | `DEFAULT_EVICTION_HIGH_WATERMARK_RATIO = 0.90`（types.h:91） |

相关补充 flag：
- `--nof_eviction_ratio` = 0.05、`--nof_eviction_high_watermark_ratio` = 0.90（master.cpp:137-141，NoF SSD 用）；
- `--put_start_discard_timeout_sec`（master.cpp:311-313）、`--put_start_release_timeout_sec`（314-316）——PROCESSING replica 延迟释放用；
- `--memory_allocator` 默认 `"offset"`（master.cpp:286-288）——决定走 `OffsetBufferAllocator`/offset_allocator.cpp 的空闲链表路径（Q2）；
- 驱逐线程扫描周期非 flag，硬编码 `kEvictionThreadSleepMs = 10`ms（master_service.h:1568-1569）。

config 注入路径：master.cpp:447-450 把 flag 值写入 `MasterServiceConfig`（`default_kv_lease_ttl`/`default_kv_soft_pin_ttl`），MasterService 构造时固化（master_service.cpp:176-182）。

---

## 附录：F1-F5 补充审计（驱逐机制与数据路径，2026-07-18 复核）

**F1 驱逐 victim 选择 = lease_timeout 升序（最老租约先逐）**。候选 = 租约已过期且
有可驱逐副本的对象，`nth_element` 按 lease_timeout 升序取前 evict_num 个
（master_service.cpp:6659-6668）；候选收集 6592-6598（跳过 hard pin / 未过期 /
soft-pin）。**关键**：PutEnd 把租约立即置过期（`GrantLease(0)`，
master_service.cpp:3305-3308），故从未被读的对象永远排在被读过的对象前面
（其 lease_timeout≈写入时刻 < 读时刻+TTL）。实测佐证：smoke 中 100 个老
对象全部存活、210 个新对象被逐（smoke_log.md）。

**F2 Put 副本放置默认 random**（master.cpp:288-290 `--allocation_strategy`），
随机起点顺序扫；`local_first` 且 replica_num=1 时才优先写者本机 segment
（master_service.cpp:2857-2899）。

**F3 读副本选择**：`FindFirstCompleteReplica` 线性取列表第一个 COMPLETE
（client_service.cpp:3900-3913），不做本地优先。

**F4 memcpy 快路径**：`MC_STORE_MEMCPY` 未设时按 `engine_.isTcpOnly()` 自动启用
（transfer_task.cpp:948-958）；仅当副本 endpoint == **本进程** endpoint 时走
`LOCAL_MEMCPY`（同机跨进程走 TCP；transfer_task.cpp:1395-1425）。
**MemcpyWorkerPool 固定 1 线程、队列无界**（transfer_task.cpp:581-625）。

**F5 master_service.cpp:1753 注释**讲的是"驱逐 vs 在途 offload 任务"竞争
（EraseMetadata 清理孤儿 offload 任务），不是对在途 client 读的承认——
**读侧在租约过期后无任何兜底**（无 refcnt、无任务登记；offload 走
`inc_refcnt`（6459）而 Get 不走）。官方对读侧的唯一防护即租约 TTL
（GetReplicaList 处注释 master_service.cpp:2557-2558）。

**额外观察**：`Client::BatchGetWhenPreferSameNode`（client_service.cpp:1221-1366）
**无 IsLeaseExpired 检查**（全文件仅 1160/1205/1482 三处）——批量同节点路径
不受租约保险丝覆盖；本实验负载不经此路径，作为攻击面观察记录，未深入。
