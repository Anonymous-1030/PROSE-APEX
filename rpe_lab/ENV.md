# RPE-lab 环境档案（ENV）

## 宿主机

- OS: Windows 11 (10.0.26200), x86_64
- 实验全部运行于 WSL2 默认发行版 **Ubuntu 24.04.3 LTS**（内核 `6.6.87.2-microsoft-standard-WSL2`, x86_64）
- 资源: 12 vCPU, 13.6 GB RAM（WSL 分配）, 946 GB 可用磁盘
- 无 GPU、无 RDMA 网卡、无 docker；git 协议访问 GitHub 被运营商干扰（GnuTLS/Schannel 均被 RST），curl/OpenSSL 正常——仓库经 codeload tarball + git trees API 校验重建（见 manifest.md）

## 编译器与工具链

- gcc/g++ 13.3.0 (Ubuntu 13.3.0-6ubuntu2~24.04.1)
- cmake 3.28.3, GNU Make 4.3, Python 3.12.3
- 依赖: `sudo bash dependencies.sh -y`（apt 系统包 + yalantinglibs@6a0e067 源码安装 + Go；RDMA 开发包 libibverbs-dev 随脚本安装，但运行时只用 TCP）

## 代码

- TESTED_COMMIT: `f20b7061097e4e2fda825f4106f215c71f13274a`（main @ 2026-07-17T10:48:09Z，全树 1371 blob 校验零差异）
- 工作副本: `~/mooncake`（git init 导入 + `rpe-lab` 分支；唯一本体改动为 Phase 2 被动插桩，单独 commit）

## CMake 配置行

```
cmake .. -DCMAKE_BUILD_TYPE=Release -DWITH_STORE_RUST=OFF -DBUILD_UNIT_TESTS=OFF -DBUILD_EXAMPLES=OFF
```

（默认 USE_ETCD/STORE_USE_ETCD=OFF；metadata 走 P2PHANDSHAKE，免 etcd。无 USE_RDMA 开关——RDMA 代码编译进二进制但运行时 protocol=tcp 不使用。）

## 三产物

- `build/mooncake-store/src/mooncake_master`（同时 install 到 /usr/local/bin）
- `build/mooncake-store/src/libmooncake_store.a`（C++ client 库）
- Python 绑定: `/usr/local/lib/python3.12/dist-packages/mooncake/store.cpython-312-x86_64-linux-gnu.so`，`from mooncake.store import MooncakeDistributedStore` 验证通过

## master 启动 flags（原样抄录自启动日志, smoke 档）

命令行: `--rpc_port=50051 --eviction_high_watermark_ratio=0.5 --eviction_ratio=0.1 --default_kv_lease_ttl=5000 --allow_evict_soft_pinned_objects=1`

启动时 master 打印的完整解析配置（master.cpp:1366）:

```
Master service started on port 50051, max_threads=12, enable_metric_reporting=1, metrics_port=9003, default_kv_lease_ttl=5000, default_kv_soft_pin_ttl=1800000, allow_evict_soft_pinned_objects=1, eviction_ratio=0.1, eviction_high_watermark_ratio=0.5, enable_ha=0, enable_offload=0, enable_kv_events=0, kv_events_bind_endpoint=, kv_events_backend_id=, offload_on_evict=0, offload_force_evict=0, offloading_queue_limit=50000, offload_cap_ratio=0.5, ha_backend_type=etcd, ha_backend_connstring=, etcd_endpoints=, client_ttl=10, rpc_thread_num=12, rpc_port=50051, rpc_address=0.0.0.0, rpc_interface=, rpc_conn_timeout_seconds=0, rpc_enable_tcp_no_delay=1, rpc protocol=tcp, cluster_id=mooncake_cluster, root_fs_dir=, global_file_segment_size=9223372036854775807, memory_allocator=offset, enable_http_metadata_server=0, http_metadata_server_port=8080, http_metadata_server_host=0.0.0.0, enable_metadata_cleanup_on_timeout=0, put_start_discard_timeout_sec=30, put_start_release_timeout_sec=600, max_total_finished_tasks=10000, max_total_pending_tasks=10000, max_total_processing_tasks=10000, pending_task_timeout_sec=300, processing_task_timeout_sec=300, enable_snapshot=0, enable_snapshot_restore=0, snapshot_interval_seconds=600, snapshot_backup_dir=, snapshot_object_store_type=, snapshot_catalog_store_type=, snapshot_retention_count=2, max_retry_attempts=10, enable_cxl=0, cxl_path=/dev/dax0.0, cxl_size=8589934592
```

注意: 本分支 `--default_kv_lease_ttl` 默认 **10000ms**（不是计划假设的 5000ms），实验全部显式传 TTL。

## client 配置

- 每 tenant 一个独立进程，`metadata_server="P2PHANDSHAKE"`，`protocol="tcp"`，`local_hostname="localhost"`（store 层自动在 12300-14300 选端口，TE 的 RPC 端口再随机化，同机多进程无冲突）
- `global_segment_size=2147483648`（2GB/tenant，两 tenant 共 4GB 池），`local_buffer_size=16777216`
- Python API（pinned commit 实测）: `setup(...) -> int rc`、`put(key, bytes) -> int rc`、`get(key) -> bytes（失败返空）`、`get_into(key, buf_ptr, size) -> int64（成功=字节数，失败=负错误码，-707 租约过期 / -704 不存在）`、`is_exist(key) -> int`、`close()`。错误以返回码表达，不抛异常。
