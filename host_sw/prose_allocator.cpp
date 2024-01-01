/**
 * @file prose_allocator.cpp
 * @brief Implementation of ProseAllocator for GPU HBM and CXL DRAM tiers.
 *
 * In production mode, uses CUDA Driver API (cuMemAlloc) for GPU HBM and
 * mmap on the CXL BAR for CXL DRAM. In simulation mode (PROSE_SIM defined),
 * uses aligned_alloc with synthetic physical address generation.
 *
 * @author Anonymous
 */

#include "prose_allocator.h"

#include <stdexcept>
#include <cstdlib>
#include <cstring>
#include <algorithm>

#ifndef PROSE_SIM
#include <cuda_runtime.h>
#include <cuda.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>
#else
#include <cstdlib>
#endif

namespace prose {

// ===========================================================================
// Construction / Destruction
// ===========================================================================

ProseAllocator::ProseAllocator(size_t hbm_pool_size,
                               size_t cxl_pool_size,
                               size_t chunk_size,
                               RuntimeTraceSinkPtr trace_sink)
    : trace_sink_(std::move(trace_sink)) {
  init_pool(hbm_pool_, MemoryTier::GPU_HBM, hbm_pool_size, chunk_size);
  init_pool(cxl_pool_, MemoryTier::CXL_DRAM, cxl_pool_size, chunk_size);
}

ProseAllocator::~ProseAllocator() {
  destroy_pool(hbm_pool_, MemoryTier::GPU_HBM);
  destroy_pool(cxl_pool_, MemoryTier::CXL_DRAM);
}

// ===========================================================================
// Pool Initialization
// ===========================================================================

void ProseAllocator::init_pool(TierPool& pool, MemoryTier tier,
                               size_t total, size_t chunk) {
  pool.total_size = total;
  pool.chunk_size = chunk;
  pool.allocated = 0;

  const size_t num_chunks = total / chunk;

#ifdef PROSE_SIM
  // Simulation mode: use aligned_alloc with synthetic physical addresses.
  // Base physical address is tier-dependent to avoid overlap.
  // Portable aligned allocation (works on Windows/MinGW and Linux).
#ifdef _WIN32
  pool.base_virt = _aligned_malloc(total, 64);
#else
  pool.base_virt = ::aligned_alloc(64, total);
#endif
  if (!pool.base_virt) {
    throw std::runtime_error("ProseAllocator: aligned_alloc failed");
  }
  std::memset(pool.base_virt, 0, total);

  // Synthetic physical base: 0x1_0000_0000 for HBM, 0x8_0000_0000 for CXL.
  pool.base_phys = (tier == MemoryTier::GPU_HBM)
                       ? 0x100000000ULL
                       : 0x800000000ULL;

#else
  if (tier == MemoryTier::GPU_HBM) {
    // CUDA allocation: use cudaMalloc for device-visible pinned memory.
    // cuMemGetAddressRange provides the physical base for DMA descriptors.
    cudaError_t err = cudaMalloc(&pool.base_virt, total);
    if (err != cudaSuccess) {
      throw std::runtime_error(
          std::string("ProseAllocator: cudaMalloc failed: ") +
          cudaGetErrorString(err));
    }
    // In production, physical address obtained via cuMemGetAddressRange
    // or nvidia-smi / NVML. For DMA, we use the device pointer directly.
    pool.base_phys = reinterpret_cast<uint64_t>(pool.base_virt);
  } else {
    // CXL DRAM: mmap the BAR region exposed by the CXL endpoint driver.
    // Typical path: /dev/dax0.0 or /sys/bus/cxl/devices/mem0/memX.
    int fd = open("/dev/dax0.0", O_RDWR);
    if (fd < 0) {
      throw std::runtime_error(
          "ProseAllocator: failed to open CXL DAX device /dev/dax0.0");
    }
    pool.base_virt = mmap(nullptr, total, PROT_READ | PROT_WRITE,
                          MAP_SHARED | MAP_POPULATE, fd, 0);
    if (pool.base_virt == MAP_FAILED) {
      close(fd);
      throw std::runtime_error("ProseAllocator: mmap CXL BAR failed");
    }
    close(fd);
    // Physical address from DAX is identity-mapped in CXL HDM decoder space.
    pool.base_phys = reinterpret_cast<uint64_t>(pool.base_virt);
  }
#endif

  // Build free list: each chunk is a pre-sliced allocation record.
  pool.free_list.reserve(num_chunks);
  for (size_t i = 0; i < num_chunks; ++i) {
    Allocation a{};
    a.phys_addr = pool.base_phys + i * chunk;
    a.virt_addr = static_cast<uint8_t*>(pool.base_virt) + i * chunk;
    a.size = chunk;
    a.tier = tier;
    a.in_use = false;
    pool.free_list.push_back(a);
  }
}

// ===========================================================================
// Pool Destruction
// ===========================================================================

void ProseAllocator::destroy_pool(TierPool& pool, MemoryTier tier) {
  if (!pool.base_virt) return;

#ifdef PROSE_SIM
  (void)tier;  // Unused in simulation mode.
#ifdef _WIN32
  _aligned_free(pool.base_virt);
#else
  std::free(pool.base_virt);
#endif
#else
  if (tier == MemoryTier::GPU_HBM) {
    cudaFree(pool.base_virt);
  } else {
    munmap(pool.base_virt, pool.total_size);
  }
#endif
  pool.base_virt = nullptr;
}

// ===========================================================================
// Allocation / Free
// ===========================================================================

Allocation ProseAllocator::alloc(MemoryTier tier, size_t /*size*/) {
  TierPool& pool = (tier == MemoryTier::GPU_HBM) ? hbm_pool_ : cxl_pool_;
  std::lock_guard<std::mutex> lock(pool.mu);

  if (pool.free_list.empty()) {
    throw std::runtime_error(
        "ProseAllocator: pool exhausted for tier " +
        std::to_string(static_cast<int>(tier)));
  }

  // Pop from free list (LIFO for cache locality).
  Allocation alloc = pool.free_list.back();
  pool.free_list.pop_back();
  alloc.in_use = true;
  pool.allocated += alloc.size;
  if (trace_sink_) {
    trace_sink_->residency_transition(
        alloc.phys_addr, true,
        tier == MemoryTier::GPU_HBM ? "hbm" : "cxl");
  }
  return alloc;
}

void ProseAllocator::free(const Allocation& alloc) {
  TierPool& pool = (alloc.tier == MemoryTier::GPU_HBM)
                       ? hbm_pool_
                       : cxl_pool_;
  std::lock_guard<std::mutex> lock(pool.mu);

  Allocation freed = alloc;
  freed.in_use = false;
  pool.free_list.push_back(freed);
  pool.allocated -= alloc.size;
  if (trace_sink_) {
    trace_sink_->residency_transition(
        alloc.phys_addr, false,
        alloc.tier == MemoryTier::GPU_HBM ? "hbm" : "cxl");
  }
}

// ===========================================================================
// Query Methods
// ===========================================================================

size_t ProseAllocator::used_bytes(MemoryTier tier) const {
  const TierPool& pool = (tier == MemoryTier::GPU_HBM)
                             ? hbm_pool_
                             : cxl_pool_;
  std::lock_guard<std::mutex> lock(pool.mu);
  return pool.allocated;
}

size_t ProseAllocator::available_bytes(MemoryTier tier) const {
  const TierPool& pool = (tier == MemoryTier::GPU_HBM)
                             ? hbm_pool_
                             : cxl_pool_;
  std::lock_guard<std::mutex> lock(pool.mu);
  return pool.total_size - pool.allocated;
}

uint64_t ProseAllocator::virt_to_phys(void* virt) const {
  auto check = [&](const TierPool& pool) -> uint64_t {
    auto base = reinterpret_cast<uint64_t>(pool.base_virt);
    auto addr = reinterpret_cast<uint64_t>(virt);
    if (addr >= base && addr < base + pool.total_size) {
      return pool.base_phys + (addr - base);
    }
    return 0;
  };

  uint64_t phys = check(hbm_pool_);
  if (phys) return phys;
  return check(cxl_pool_);
}

}  // namespace prose
