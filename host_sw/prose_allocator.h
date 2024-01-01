/**
 * @file prose_allocator.h
 * @brief Memory allocator interface for PROSE-APEX host software stack.
 *
 * Manages physical memory allocations across two tiers:
 *   - GPU HBM: allocated via CUDA Runtime/Driver API (cudaMalloc / cuMemAlloc)
 *   - CXL DRAM: allocated via CXL.mem mapped BAR regions
 *
 * All allocations return physical addresses suitable for DMA descriptor
 * construction. The allocator maintains an internal slab pool to minimize
 * allocation overhead on the critical path.
 *
 * @author Anonymous
 */

#ifndef PROSE_ALLOCATOR_H_
#define PROSE_ALLOCATOR_H_

#include <cstdint>
#include <cstddef>
#include <memory>
#include <vector>
#include <mutex>
#include <unordered_map>

#include "prose_types.h"
#include "runtime_trace.h"

namespace prose {

/// Default chunk size for slab allocations (2 MB — matches GPU huge page).
static constexpr size_t kDefaultChunkSize = 2 * 1024 * 1024;

/// Maximum allocations tracked per tier.
static constexpr size_t kMaxAllocations = 4096;

/**
 * @enum MemoryTier
 * @brief Identifies the target memory tier for allocation.
 */
enum class MemoryTier {
  GPU_HBM,    ///< GPU High-Bandwidth Memory (local to compute).
  CXL_DRAM,   ///< CXL-attached DDR5 (endpoint device memory).
};

/**
 * @struct Allocation
 * @brief Tracks a single memory allocation across tiers.
 */
struct Allocation {
  uint64_t    phys_addr;    ///< Physical address (for DMA descriptors).
  void*       virt_addr;    ///< Virtual address (for host/GPU access).
  size_t      size;         ///< Allocation size in bytes.
  MemoryTier  tier;         ///< Which memory tier this resides in.
  bool        in_use;       ///< Whether currently allocated to a user.
};

/**
 * @class ProseAllocator
 * @brief Unified memory allocator for GPU HBM and CXL DRAM tiers.
 *
 * Design:
 *   - Pre-allocates a slab pool at initialization for each tier.
 *   - alloc() returns a physical address suitable for ProseDescriptor.
 *   - free() returns the allocation to the slab pool.
 *   - Thread-safe via per-tier mutex (not on the hot path — allocations
 *     are batched ahead of submission).
 *
 * GPU HBM allocations use cudaMalloc with pinned physical addressing
 * (cuMemGetAddressRange for phys). CXL DRAM allocations use mmap on
 * the CXL BAR region exposed by the kernel driver.
 *
 * @note In simulation mode (PROSE_SIM=1), allocations use malloc and
 *       synthetic physical addresses for testing without hardware.
 */
class ProseAllocator {
 public:
  /**
   * @brief Construct allocator with specified pool sizes.
   * @param hbm_pool_size  Total GPU HBM to pre-allocate (bytes).
   * @param cxl_pool_size  Total CXL DRAM to pre-allocate (bytes).
   * @param chunk_size     Slab granularity (default 2MB).
   */
  explicit ProseAllocator(size_t hbm_pool_size = 1ULL << 30,   // 1 GB
                          size_t cxl_pool_size = 4ULL << 30,   // 4 GB
                          size_t chunk_size = kDefaultChunkSize,
                          RuntimeTraceSinkPtr trace_sink = nullptr);

  ~ProseAllocator();

  // Non-copyable, non-movable.
  ProseAllocator(const ProseAllocator&) = delete;
  ProseAllocator& operator=(const ProseAllocator&) = delete;

  /**
   * @brief Allocate a chunk from the specified tier.
   * @param tier   Target memory tier (GPU_HBM or CXL_DRAM).
   * @param size   Requested size (rounded up to chunk_size).
   * @return Allocation record with physical and virtual addresses.
   * @throws std::runtime_error if pool is exhausted.
   */
  Allocation alloc(MemoryTier tier, size_t size = kDefaultChunkSize);

  /**
   * @brief Return an allocation to the pool.
   * @param alloc  Allocation record previously returned by alloc().
   */
  void free(const Allocation& alloc);

  /**
   * @brief Get total allocated bytes for a tier.
   */
  size_t used_bytes(MemoryTier tier) const;

  /**
   * @brief Get total available bytes for a tier.
   */
  size_t available_bytes(MemoryTier tier) const;

  /**
   * @brief Translate a virtual address to physical address.
   * @param virt  Virtual address within a managed allocation.
   * @return Physical address, or 0 if not found.
   */
  uint64_t virt_to_phys(void* virt) const;

 private:
  struct TierPool {
    void*                    base_virt;     ///< Base virtual address of pool.
    uint64_t                 base_phys;     ///< Base physical address of pool.
    size_t                   total_size;    ///< Total pool size.
    size_t                   chunk_size;    ///< Slab granularity.
    std::vector<Allocation>  free_list;     ///< Available chunks.
    size_t                   allocated;     ///< Bytes currently in use.
    mutable std::mutex       mu;           ///< Per-tier lock.
  };

  TierPool hbm_pool_;   ///< GPU HBM slab pool.
  TierPool cxl_pool_;   ///< CXL DRAM slab pool.

  RuntimeTraceSinkPtr trace_sink_;  ///< Optional allocator event recorder.

  /// Initialize a tier pool (allocate backing memory).
  void init_pool(TierPool& pool, MemoryTier tier, size_t total, size_t chunk);

  /// Destroy a tier pool (free backing memory).
  void destroy_pool(TierPool& pool, MemoryTier tier);
};

}  // namespace prose

#endif  // PROSE_ALLOCATOR_H_
