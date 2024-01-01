# RPE-lab NOTES（决策、偏差、操作化定义）

随时更新。凡与原始计划有出入处都记录在此，并给出理由与证据位置。

## 与计划的偏差（全部已确认不影响结论有效性）

1. **默认 TTL 是 10000ms 不是 5000ms**（types.h:85-86 + master.cpp static_assert）。
   处理：所有 run 显式传 `--default_kv_lease_ttl`；TTL 扫描仍按计划 {1000, 5000, 11000}。
2. **租约检查点共 3 处，全在 client_service.cpp**（1160/1205/1482）；计划的
   `transfer_task.cpp` 不涉及租约。插桩点与之一致（audit_notes.md Q1）。
3. **播种并入 victim 进程**：计划里独立 seeder 进程会在退出时卸载其 segment
   （上面的对象副本随之丢失）。改为 victim（tenant A）自播种自持有；
   pressure 等 ledger 文件出现后再开始 Put。两 tenant 进程 × 2GB = 4GB 池不变。
4. **Python `get()` 吞错误码**（失败只返空 bytes）：driver 一律用
   `get_into(key, ptr, size)`，返回值成功=字节数 / 失败=负错误码，
   从而区分 -707（guard 熔断）与 -704（不存在）。
5. **期望代际（generation）在 driver 侧记账**：C++ 探针无法知道期望 gen，
   事件里只有 found_* 与 expected_key_hash；overwritten 判定在聚合时完成
   （按 key+时间戳 join 请求日志里的逐请求 exp_gen）。
6. **C++ 探针事件与请求日志的 join**：`expected_gen_for(key, ts)` 取
   t_lookup_ns ≤ ts 的最近请求；fallback 到账本最终 gen。
7. **success_mismatch 判定放宽到 {exp_gen, cur_gen}**：reseed 可能在
   lookup 与 transfer 之间合法完成（此时读到新 gen 是"最新"而非错误）。
   放宽只覆盖同 key 的新旧 gen；foreign key_hash / 无 magic / 旧 gen 仍判 mismatch。

## 机制发现（审计 + smoke 实测，均有 文件:行号 或日志证据）

- **驱逐 victim = lease_timeout 最老优先**（nth_element 升序，master_service.cpp:6659-6668）；
  **PutEnd 把租约立即置过期**（GrantLease(0)，master_service.cpp:3305-3308）。
  含义：从未被读的对象永远是驱逐首选；被频繁读的热 key 靠租约续命。
- **租约只由 GetReplicaList/ExistKey 授予**（master_service.cpp:2083-2093 一带），
  Put 不授租。client 读路径无 refcnt、无任务登记——租约过期后读侧无任何兜底（F5）。
- **memcpy 快路径**：TCP-only 环境自动启用；仅当副本 endpoint == 本进程 endpoint
  （同机跨进程走 TCP）。worker 池**单线程、队列无界**（transfer_task.cpp:581-625）。
- **副本放置默认 random**（跨 segment 随机起点顺序扫；master.cpp:288-290），
  非 local_first。读副本取列表第一个 COMPLETE（client_service.cpp:3900-3913）。
- **`BatchGetWhenPreferSameNode` 路径无租约检查**（client_service.cpp:1221-1366 区间无
  IsLeaseExpired；本实验负载不经过该路径，但作为攻击面观察记录在案）。
- smoke 实测：高水位 0.5 过线后每 ~350ms 一轮 BatchEvict，每轮 ~30 keys；
  老 smoke key 因有租约续命全部存活，新 fill key 被逐（与 F1 机制一致）。
- tc netem 在 WSL2 lo 上可用（delay/rate 均验证过）；memcpy 路径不受 tc 影响，
  TCP 路径受 tc 影响——两路径要分别对待（Phase 2 烟囱实测：400ms tc delay 下
  TransferRead 仅 408µs（memcpy），过期由控制面 RPC 往返造成）。

## 操作化定义（论文宏的口径）

- **guard_fires**：victim 请求日志中 rc=-707 的计数（与 C++ 事件数
  guard_fires_events 互相印证；两者应相等，不等时以事件文件为准并调查）。
- **rpe_events / rpe_payload_bytes**：discard 事件中 found_magic=true 且
  (found_key_hash ≠ fnv1a64(key) 或 found_gen ≠ 该请求的 exp_gen) 的计数与字节和。
- **misbw_bytes**：全部 discard 事件的 payload_len 之和（被拉取后丢弃的字节）。
- **no_magic_discards**：discard 时 buffer 无有效头（槽位被无头内容覆写或清零）。
- **burst_share_pct**：以 10s 分箱统计 victim 到达率，速率 > 4× 全 run 均值的
  分箱记为突发窗；落在突发窗内的 discard 事件占比。（计划原文"并发 > 4× 池容量"
  在本实验规模不可达，按"4× 均值到达率"操作化，论文中说明。）
- **throughput_mbps**：victim 成功 Get 字节 / run 时长（MB/s，十进制 MB）。

## Trace 使用

- 文件：BurstGPT_1.csv（manifest.md 有 SHA256）。仅取 Timestamp（IAT）与
  Total tokens（记账用上下文长度）。
- 窗口经全文件密度扫描选定：victim `trace_start=1154000`（10k 请求/577s，
  均值 17.3/s，秒级峰值 91/s）；pressure `trace_start_b=90000`。
- RPS 缩放（trace_speedup）遵循 BurstGPT 官方 README 建议（"scale the average
  RPS according to your evaluation setups"），每个 run 的 config JSON 记录取值。

## DoD 阶梯记录（计划第 10 节排查顺序）

- **DoD run 1**（dod_ttl1000_c64_seed42：TTL=1000, c=64, speedup=10, 无限速, 600s）:
  gets=15798 (ok=10255, -704=5540, -703=3), **guard_fires=0**,
  读延迟 p50=5.1ms / p90=26ms / p99=62ms / max=243ms << TTL=1000ms。
  窗口公式负向验证：有效环路带宽（put 实测 ~336MB/s + read ~63MB/s）下
  c=64 × 3.5MB = 224MB 的排队积压无法形成 —— 需要限速或更高并发。
  驱逐搅动确认（F1 机制活体复现）: 10min 内 5540 次 not_found + 5285 次 reseed
  （热集 key 租约在两次读之间过期 → 被 BatchEvict 清走 → 重读 → re-put）。
  -703 (REPLICA_IS_NOT_READY) ×3: Get 与被逐/重写中的副本竞争的另一表现。
- **DoD run 2**（dod2_tc800m_ttl1000_c64_seed42）: 按计划阶梯加
  `tc netem rate 800mbit`（=100MB/s，对应计划速查表 "100 MB/s" 行；
  窗口 224MB/100MB/s ≈ 2.2s > TTL=1s）。结果见对应 tierA_*.json。
- **DoD run 3**（dod3，er 0.1→0.5，tc 800mbit，TTL=1000）: guard_fires=6709
  （双通道一致），not_found=1583（热集被逐显著增加），**rpe_events=0**。
  结构分析：er=0.5 单轮清空全部候选 → 用量跌破水位 → 触发间隔拉长到 ~13s
  （45 轮/600s）；在途对象租约过期后要等下一个触发周期才可能被逐，而
  overstay 窗口仅 ~1-2s —— **触发率**成为瓶颈；且 oldest-first 排序下
  刚过期对象排在 B 的存量 never-read 饲料之后。
- **DoD run 4**（dod4，B 全部 with_soft_pin + er=0.5 + tc 800mbit）: B 的对象
  退出 pass-1/2 候选集（master_service.cpp:6597 IsSoftPinned 跳过），驱逐被
  引导向 A 的已过期对象；B 的对象仅在 pass-3 兜底（allow_evict_soft_pinned=1
  保持部署默认）。软钉 TTL 30min 默认。**纯负载侧改动（Put 时 ReplicateConfig），
  master 保护参数未动。**
- 结论性机制（论文用）: RPE 落地需要三要素同时成立 —— (i) guard 熔断
  （窗口公式：积压/带宽 > TTL）；(ii) 驱逐触发率（用量持续过水位）；
  (iii) 候选队列浅到刚过期对象可在 overstay 内被选中（oldest-first 排序的
  队列前沿推进速度）。run2/run3 分别缺 (iii)/(ii)，故 guard 熔断数千次而
  RPE 为零 —— 这本身是"租约保险丝有效但非充分"的直接证据。
- **DoD run 5**（dod5，tc 400mbit=50MB/s，B 软钉，er=0.5，v2 探针）: guard_fires=6590
  （双通道一致），expired_by p50=**3242ms**（绑定在传输途中断裂 3 秒以上），
  transfer p50=3753ms —— 窗口严重超期；但 rpe_events=0、torn=0，全部 6590 份
  被丢弃 buffer 的头尾标记都正确（found (tenant=1,gen=1) 6526 例 + gen=2 64 例）。
  含义：**绑定断裂是常态（6590 次），但断裂后的窗口里槽位没有被覆写**。
  原因分析：B 的 put 被限速压到 ~3.9/s → 复用率 << 过期率（11+/s）→
  oldest-first 的候选队列积压（not_found=3017 佐证）→ 刚过期的在途对象
  永远排不到队首。复用必须"逐出即写"才可见：触发式驱逐（用量过线才跑）
  + 低速背压下，槽位释放与再分配不同步。
- **DoD run 6**（dod6）：pressure 改为 max-rate 背压（8 并发 putter、无节奏），
  让每个 B put 都强制一次候选清空 —— 检验"驱逐-复用-覆写"同步链。
- **DoD run 6（突破）**（dod6，B 全速背压 8 putters + 软钉 + er=0.5 + tc 400mbit，
  TTL=1000，600s）: guard_fires=3589，eviction 60 轮，**rpe_events=2 +
  torn_events=1（rpe_payload_bytes=7.34MB + torn 3.67MB）**，success_mismatch=0。
  三个事件（证据链：事件文件 + 逐请求 exp_gen join）：
  1. hot/0230：exp_gen=1，读到 gen=2（同 key 新化身；超期 2.40s）
  2. hot/0203：头=自身 gen=1，**尾=tenant 2 外来对象**（跨租户覆写混入；超期 2.30s）
  3. hot/0380：exp_gen=1，读到 gen=2（超期 0.54s）
  机制闭环：B 全速背压使"逐出→复用→覆写"在 overstay 内同步完成；
  全部事件被租约保险丝拦下（detect-and-discard 有效），但错误字节已在
  丢弃前经由数据路径读回——RPE 论点（"丢弃兜底但线上字节已发出"）直接成立。
  注：pressure 在 max-rate 下 55333 次 put 报错（池满于软钉对象时的
  背压表现，待分类错误码）；B 成功 8454 puts / 31GB。
- 指标口径补充：同 key 不同 gen（新化身）与 foreign key_hash（外来对象）
  分开呈现：rpe_events=同 key 错 gen 计 2，torn（foreign）计 1；
  论文中 \McRaces 可报两者之和=3（保守口径）或分列。
- **run 6 复核与口径修正（重要）**：对 3 个候选事件做了逐请求时间线核查
  （req 日志 t_lookup/exp_gen/rc + reseed 完成时间戳）：
  * hot/0230、hot/0380 的 found_gen=2 实为**账本过期假象**——对象此前已
    被逐（连续 -704），Get 的 GetReplicaList 实际返回的是 reseed 后的
    gen=2 新副本（合法读取），只是驱动在 claim 时刻读到的 ledger 还是
    gen=1；**不计入 RPE**，改列 gen_skew_events=2。
  * hot/0203 为**唯一无歧义 RPE**：头=自身 gen=1、尾=tenant 2 外来对象
    （跨租户覆写混入，不存在账本解释）。修正后 run 6：
    guard_fires=3589，**rpe_events=1（rpe_payload_bytes=3.67MB，torn）**，
    gen_skew=2，misbw=13.17GB，success_mismatch=0。
* **口径修正（全文适用）**：rpe_events/rpe_payload_bytes 只计
  **foreign key_hash**（头或尾）的 discard 事件——同 key 不同 gen
  无法区分"槽位复用"与"reseed 后合法读到新 gen"（RPC 时序不可得），
  一律列 gen_skew 单列。success_mismatch（红线）规则同：foreign 或
  头尾不连贯才算；同 key 连贯新 gen 视为合法（reseed 竞态）。
* 教训：热集 reseed 自身就会产生"同 key 换 gen"的槽位复用，是 RPE 的
  自然放大器，但测量上必须与 foreign 覆写分开报告，否则高估。
- **Tier-A 验证格**（evictaggr, TTL=5000, c=64, tc 300mbit, 30min）: guard_fires=1564
  （双通道一致），rpe=0，misbw=5.7GB，success_mismatch=0。TTL=5000 档的
  tc 校准（300mbit）成立 → 矩阵 TC_BY_TTL={1000:800, 5000:300, 11000:150}mbit 有效。
- **Phase 5**（expB，TTL=5000，tc 800mbit，各 30min）: 结果见 expB_pin_cliff.json。
  pinned/unpinned 吞吐比 1.068；驱逐尝试 41273 vs 538（77×，成功率 1.2%）；
  不可回收容量 39.3% vs 0%；热集丢失 221 vs 1376（-84%）。
  注意：该档（800mbit, TTL=5000）传输 ~1s < TTL，两臂 guard_fires 均为 0 ——
  pin 的收益/代价与 guard 无关，恰说明两者是正交机制（pin 全程覆盖时
  根本不存在"过期窗口"）。
- **Tier-B 验证**（tierB_d2000，delay=2000ms=2×TTL，evictaggr 搅动，30min，
  7200 次迭代）: Query 阶段 -704=7117（高搅动下热 key 常已被逐），完成的
  传输全部熔断（guard_fires=75，gets_ok=0），**rpe_events=5
  （rpe_payload_bytes=17.5MB，全部头尾一致的外来对象：4 例 tenant 2 bulk
  对象 + 1 例同 tenant 错 key）**，success_mismatch=0。
  解读：构造窗口（2s）内"逐出→复用→完整覆写"从容完成，读端拉回的是
  写好的新对象——熔断率虽低（75/7200 查询多数先 -704），但一旦进入传输
  且绑定断裂，6.7% 读到的是完全错误的对象。Tier-B 为结构性上限测量，
  论文与 Tier-A 分开报告（"constructed": true）。
- rpe_probe 的 Query 失败迭代 buffer 置空修正（防止旧迭代字节污染标记）；
  Tier-A 探针不受影响（-707 时 buffer 必为当次传输完整读取）。

## Tier-U 无保护基线（guard bypass，measurement-only）

- 开关：client 侧 `RPE_LAB_BYPASS_GUARD` env（commit 92c36bc），三处 lease 分支
  从"丢弃"变"记录+放行"；默认关闭=原生行为；master 零改动。
  计数但隔离：放行字节仅进 checker buffer，foreign/incoherent 记为
  delivered_wrong_*；红线规则（success_mismatch）不适用于 Tier-U。
- **U1**（tierU_dod6regime_bypass，dod6 同 regime 同 seed，10min）:
  bypass 生效（driver guard_fires=0，C++ 事件 3422 例全 delivered:true，
  gets_ok 3493→6620）；本 run foreign=0（dod6 的 1 例撕裂为随机稀有事件）——
  自然档无保护投递错误字节率同样 ~0，与 wire-level 低率一致。
- **U2**（tierU_B_d2000_bypass，Tier-B 探针 delay=2000 旁路，30min）: 进行中。
- **U2 插曲（记录备查）**：首次 U2 运行中探针二进制仍是旁路补丁前静态链接的
  旧库（静态链接冻结），意外形成第二次 guard-on Tier-B 复测：**rpe=7 + torn=1
  / 90 次熔断**（首次为 5/75）——guard-on 下 RPE 率复现一致（~7%）。
  教训：凡动 libmooncake_store.a 必须重建 rpe_probe；已重建并重跑。
- **U1 四象限**（unprotected_tierU_dod6regime_bypass_*.json）:
  guard-on wire 级错误率 0.0141%（3.67MB/26GB wire 字节）；
  guard-off 本 run 投递错误 0（自然档 RPE 随机稀有，~1 例/10min 量级）——
  两侧一致指向"自然档 wire 级暴露 ~0.01%，投递侧被 guard 压到 0"。
  精确基线率待 24h 档与 U2（构造档）补齐。
- **U2 无保护基线（最终，焊缝数据）**（unprotected_tierU_B_d2000_bypass.json）:
  构造档 delay=2000ms 下，guard-off 基线 **65 次成功投递中 4 次全错对象
  （14.68MB）= 6.15% wrong/delivered**；guard-on 孪生 75 次完成传输 5 次线上
  错误（6.67%）—— 两侧率值互洽，保险丝把同样的线上错误 100% 转为失败
  （投递 0）。**0.0073%（保护档投递归一）vs 6.15%（无保护基线）≈ 3 个数量级，
  断裂即保险丝的过滤效应，已在真实系统实测。**
  另：rerun 的探针日志与首次意外 guard-on 复测 append 在同一文件（前 7200 行
  guard-on：rpe=7+torn=1/90；后 7201 行 bypass：4/65），已按行切分统计；
  run_tierb.sh 已改为跑前清空探针输出。自然档基线见
  unprotected_tierU_dod6regime_bypass_*.json（两侧 ~0.01% 一致）。
- 聚合器修复：tierb_aggregate 输出改名 tier*_probe_<run>.json（不再与
  driver 聚合互相覆盖）；空 rdir 参数不再踩默认路径。

## 回应"常见性"质疑的实验矩阵（2026-07-24）

- **实验 1（竞态窗口普查）**：master 侧被动逐 key 驱逐日志
  （commit c1e8ecd，`RPE_LAB_EVENTS_EVICT` 门控，BatchEvict 成功路径
  log-only，无控制流影响；与 client 插桩同纪律）。窗口 = 对象 K 被驱逐
  时刻落在 K 的某个在途 Get 的 [t_lookup, t_done] 内——RPE 必要条件
  被满足的直接证据，不需 bypass、可在自然负载长跑。
  分析：analysis/window_count.py → results/windows_<run>.json
  （windows/hour、windows/1k transfers、按读 rc 分解、重叠时间分布）。
- **实验 2（wire 级绝对速率）**：aggregate.py 现输出每 run 的 wire 级
  错误字节 MB/hour（替代小数百芬比的陈述方式）。
- **实验 3（去对抗化 + 分层）**：trace_stats.json —— BurstGPT 全程 61 天：
  >4× 均值速率占 34.97% 时间 / 64.64% 请求；>16× 占 5.69% 时间 /
  26.28% 请求；driver 回放窗口 17.3/s = 63.9× 均值（尾部厚，非对抗）。
- **实验 4（窗口宽度-转化率曲线）**：configs/sweep/ 下 11 个扫描配置
  （Tier-A tc 1200→100mbit 6 点；Tier-B probe delay 250→4000ms 5 点，
  每点 15min），产物供给"真实工作点落在曲线陡段"论证。
- 纪律补充：master 侧唯一改动为上述 log-only 普查（不改变任何行为），
  与 §9 "不修改 master 行为逻辑" 一致；逐 key 事件仅用于事后 join。
- **实验 1 结果（census1，dod6 regime，30min）**：master 普查 41,722 次逐 key
  驱逐；在途读与驱逐同 key 重叠 = **36 个竞态窗口（77.5 窗口/小时，
  6.08 窗口/千次完成传输）**；窗内读的结局：-707×27、-704×9；
  驱逐落点在 lookup 后 p50=1.68s / p90=3.2s。本 run 转化（foreign/torn）=0 ——
  **窗口常见而转化靠 drain 时序运气**，恰证明只测交付率会把现象低估
  1-2 个数量级；命题 A（竞态窗口常见）成立。结果文件
  windows_census1_*.json；guard_fires=3179 为窗口的下界子集。
- 一句话写法（论文）："the race fires ~78 times per hour per 4-GB pool pair
  under BurstGPT replay (6.1 windows per 1k completions); conversion to
  wrong bytes depends on drain timing, i.e. implementation luck, not safety."
