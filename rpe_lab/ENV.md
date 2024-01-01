# RPE-lab Environment Record (ENV)

## Host machine

- OS: Windows 11 (10.0.26200), x86_64
- All experiments ran inside the WSL2 default distribution **Ubuntu 24.04.3 LTS**
  (kernel `6.6.87.2-microsoft-standard-WSL2`, x86_64)
- Resources: 12 vCPU, 13.6 GB RAM (WSL allocation), 946 GB free disk
- No GPU, no RDMA NIC, no docker; git-protocol access to GitHub was disrupted by
  the carrier (GnuTLS/Schannel both RST), while curl/OpenSSL worked — the repo
  was therefore reconstructed from a codeload tarball and verified via the git
  trees API (see manifest.md)

## Compiler and toolchain

- gcc/g++ 13.3.0 (Ubuntu 13.3.0-6ubuntu2~24.04.1)
- cmake 3.28.3, GNU Make 4.3, Python 3.12.3
- Dependencies: `sudo bash dependencies.sh -y` (apt system packages +
  yalantinglibs@6a0e067 built from source + Go; the RDMA dev package
  libibverbs-dev is installed by the script, but only TCP is used at runtime)

## Code

- TESTED_COMMIT: `f20b7061097e4e2fda825f4106f215c71f13274a`
  (main @ 2026-07-17T10:48:09Z, full-tree 1371-blob verification, zero diff)
- Working copy: `$MOONCAKE_REPO` (default `$HOME/mooncake`; `git init` import +
  `rpe-lab` branch; the only source change is the Phase 2 passive
  instrumentation, as its own commit)

## CMake configure line

```
cmake .. -DCMAKE_BUILD_TYPE=Release -DWITH_STORE_RUST=OFF -DBUILD_UNIT_TESTS=OFF -DBUILD_EXAMPLES=OFF
```

(Defaults USE_ETCD/STORE_USE_ETCD=OFF; metadata goes over P2PHANDSHAKE, no etcd
needed. There is no USE_RDMA switch — RDMA code is compiled into the binary but
unused at runtime with protocol=tcp.)

## Three build artifacts

- `build/mooncake-store/src/mooncake_master` (also installed to /usr/local/bin)
- `build/mooncake-store/src/libmooncake_store.a` (C++ client library)
- Python bindings: `/usr/local/lib/python3.12/dist-packages/mooncake/store.cpython-312-x86_64-linux-gnu.so`;
  `from mooncake.store import MooncakeDistributedStore` verified

## master startup flags (transcribed from the startup log, smoke cell)

Command line: `--rpc_port=50051 --eviction_high_watermark_ratio=0.5 --eviction_ratio=0.1 --default_kv_lease_ttl=5000 --allow_evict_soft_pinned_objects=1`

Full parsed config printed by the master at startup (master.cpp:1366):

```
Master service started on port 50051, max_threads=12, enable_metric_reporting=1, metrics_port=9003, default_kv_lease_ttl=5000, default_kv_soft_pin_ttl=1800000, allow_evict_soft_pinned_objects=1, eviction_ratio=0.1, eviction_high_watermark_ratio=0.5, enable_ha=0, enable_offload=0, enable_kv_events=0, kv_events_bind_endpoint=, kv_events_backend_id=, offload_on_evict=0, offload_force_evict=0, offloading_queue_limit=50000, offload_cap_ratio=0.5, ha_backend_type=etcd, ha_backend_connstring=, etcd_endpoints=, client_ttl=10, rpc_thread_num=12, rpc_port=50051, rpc_address=0.0.0.0, rpc_interface=, rpc_conn_timeout_seconds=0, rpc_enable_tcp_no_delay=1, rpc protocol=tcp, cluster_id=mooncake_cluster, root_fs_dir=, global_file_segment_size=9223372036854775807, memory_allocator=offset, enable_http_metadata_server=0, http_metadata_server_port=8080, http_metadata_server_host=0.0.0.0, enable_metadata_cleanup_on_timeout=0, put_start_discard_timeout_sec=30, put_start_release_timeout_sec=600, max_total_finished_tasks=10000, max_total_pending_tasks=10000, max_total_processing_tasks=10000, pending_task_timeout_sec=300, processing_task_timeout_sec=300, enable_snapshot=0, enable_snapshot_restore=0, snapshot_interval_seconds=600, snapshot_backup_dir=, snapshot_object_store_type=, snapshot_catalog_store_type=, snapshot_retention_count=2, max_retry_attempts=10, enable_cxl=0, cxl_path=/dev/dax0.0, cxl_size=8589934592
```

Note: on this branch `--default_kv_lease_ttl` defaults to **10000ms** (not the
5000ms assumed in the plan); every experiment passes the TTL explicitly.

## client configuration

- One independent process per tenant, `metadata_server="P2PHANDSHAKE"`,
  `protocol="tcp"`, `local_hostname="localhost"` (the store layer auto-selects a
  port in 12300-14300; the TE RPC port is randomized on top, so multiple
  processes on one machine do not conflict)
- `global_segment_size=2147483648` (2 GB/tenant, two tenants = 4 GB pool),
  `local_buffer_size=16777216`
- Python API (measured at the pinned commit): `setup(...) -> int rc`,
  `put(key, bytes) -> int rc`, `get(key) -> bytes (empty on failure)`,
  `get_into(key, buf_ptr, size) -> int64 (success = byte count; failure =
  negative error code, -707 lease expired / -704 not found)`,
  `is_exist(key) -> int`, `close()`. Errors are return codes, not exceptions.
