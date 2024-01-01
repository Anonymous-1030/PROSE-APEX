# Phase 1 smoke log

日期: 2026-07-17 (UTC+8) · 环境见 ENV.md · commit `f20b706`（未打插桩的原版二进制）

## 命令

```bash
# master（flags 与计划一致；完整启动行抄录于 ENV.md）
~/mooncake/build/mooncake-store/src/mooncake_master \
  --rpc_port=50051 --eviction_high_watermark_ratio=0.5 --eviction_ratio=0.1 \
  --default_kv_lease_ttl=5000 --allow_evict_soft_pinned_objects=1
# client（单进程，P2PHANDSHAKE + tcp，segment 2GB）
python3 rpe_lab/driver.py smoke --config rpe_lab/configs/smoke.yaml
```

编排脚本: `rpe_lab/wsl/smoke_run.sh`；driver 子命令: `smoke`（put/get 100 → 填池过高水位 → 探被逐 key）。

## DoD 结果（全部通过）

| DoD | 结果 | 证据 |
|---|---|---|
| tenant A Put 100 对象、Get 全部成功 | OK (100/100) | `DoD-1 put/get 100: got 100/100 OK` |
| 持续 Put 超高水位 → master 驱逐计数增长 | OK | `:9003/metrics` `master_successful_evictions_total=7, master_attempted_evictions_total=7`；master 日志 `[EVICT-TRIGGER] memory_ratio=0.500732 ...` / `[EVICT-RESULT] evicted_count=30, ...` 每轮 |
| Get 已被驱逐 key → "不存在"类错误，且与租约过期错误区分 | OK | `DoD-3 evicted key fill/00000: get_into rc=-704`（OBJECT_NOT_FOUND=-704；租约过期为 LEASE_EXPIRED=-707，见 audit_notes.md Q4） |

`SMOKE PASS`, `SMOKE_EXIT=0`。master 全量日志: `results/smoke_master.log`。

## 过程中的发现（已修正 driver）

- 首次运行 DoD-3 FAIL：驱逐受害者全是后写入的 `fill/*` key，先写入的 100 个 `smoke/*` key 无一被逐（两轮均驱逐 30 keys/轮 × 7 轮 = 210 keys ≈ 735MB）。说明本分支驱逐选 victim 不偏向"最老"对象；探测被逐对象时改用实际被逐的 fill key 后通过。该行为对 Phase 3 热集设计的影响：热集 key 不会因为是"老住户"而被豁免，与被逐概率与新 key 相当。
- 驱逐触发节奏：usage 刚过 0.5 水位即每 ~350ms 触发一轮（每轮 evict 30 keys），与 `kEvictionThreadSleepMs=10ms` 扫描周期一致（轮间隔由触发频率决定）。
- 高水位下 master 侧观测: `Mem Storage: 948.50 MB / 2.00 GB (46.3%)`，`Eviction: Success/Attempts=7/7, keys=210, size=735.00 MB`。

## 两种错误码实录（后续 guard_fires 计数依据）

| 情形 | Python get_into 返回值 | C++ ErrorCode | 日志串 |
|---|---|---|---|
| key 不存在（被逐/未写） | `-704` | OBJECT_NOT_FOUND | （master 侧查询 miss） |
| 租约过期（guard 熔断） | `-707` | LEASE_EXPIRED | client WARNING `lease_expired_before_data_transfer_completed key=` |

---

# Phase 2 端到端烟囱（插桩后）

patch commit: `e930d82`（client_service.cpp +17 行 + 新增 rpe_lab_probe.h；`git apply --check` 通过；增量重建后 `PYBIND_IMPORT_OK_AFTER_PATCH`）。

方法: master TTL=500ms（烟囱专用值，不进论文）→ put 一个 3.5MB 带头对象 → `tc qdisc dev lo netem delay` 逐级加压（200/400/800ms）→ 观察 get_into 返回值与 `results/events_chimney.jsonl`。

结果（`CHIMNEY_PASS`, `CHIMNEY_EXIT=0`）:

```
tc delay 200ms: get_into rc=3670016 (0.40s) x3   # 未过期，正常成功
tc delay 400ms:
  W client_service.cpp:1162] lease_expired_before_data_transfer_completed key=hot/0000
  E real_client.cpp:3322] Get failed for key: hot/0000 with error: LEASE_EXPIRED
  get_into rc=-707 (0.80s)
```

事件文件记录（字段完整，本例无覆写、符合预期）:

```json
{"ts_ns": 1784297450918523008, "type": "discard", "run_id": "chimney",
 "key": "hot/0000", "expected_key_hash": 17779036219203737137,
 "found_magic": true, "found_tenant": 1, "found_key_hash": 17779036219203737137,
 "found_gen": 1, "found_ts_ns": 1784297448852316646,
 "payload_len": 3670016, "expired_by_us": 300872, "transfer_us": 408}
```

要点: (1) -707 返回值、WARNING 日志串、JSONL 事件三者同时出现，guard_fires 的双通道计数验证闭环；(2) 本例中过期由控制面 RPC 延迟（400ms 单程 × 往返）造成，TransferRead 本身 408µs（同机 memcpy 路径不被 tc 延迟）——Phase 3 窗口公式的带宽项需按实际数据路径（memcpy vs TCP）分别对待；(3) 插桩确认零行为改变：无 `RPE_LAB_EVENTS` 时无任何输出（单测 case 5 覆盖）。
