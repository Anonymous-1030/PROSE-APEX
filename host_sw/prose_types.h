/**
 * @file prose_types.h
 * @brief Core data structures for PROSE-APEX host-side software stack.
 *
 * Defines the Batch Descriptor Block (BDB) layout, descriptor format,
 * completion status codes, and lock-free ring buffer for MMIO submission
 * and completion queues. All structures align with the PROSE-APEX CXL
 * Type-3 endpoint hardware specification.
 *
 * @note Descriptor layout is 64B-aligned matching CXL.mem flit granularity.
 * @note Memory ordering follows CXL 3.1 §8.2.4 (HDM-D coherence domain).
 *
 * @author Anonymous
 */

#ifndef PROSE_TYPES_H_
#define PROSE_TYPES_H_

#include <cstdint>
#include <atomic>
#include <cassert>
#include <cstring>

namespace prose {

// ===========================================================================
// Constants
// ===========================================================================

/// Maximum descriptors per Batch Descriptor Block (hardware limit).
static constexpr uint32_t kMaxDescriptorsPerBDB = 64;

/// Descriptor size in bytes (must be 64B for CXL flit alignment).
static constexpr uint32_t kDescriptorSizeBytes = 64;

/// Ring buffer depth for submission/completion queues (power of 2).
static constexpr uint32_t kRingDepth = 1024;

/// Doorbell register offset from MMIO base address.
static constexpr uint64_t kDoorbellOffset = 0xFFC;

/// Maximum number of chunks managed by the endpoint.
static constexpr uint32_t kMaxChunks = 512;

/// Bitmap words needed for chunk validity tracking (512 / 32 = 16).
static constexpr uint32_t kBitmapWords = (kMaxChunks + 31) / 32;

// ===========================================================================
// Completion Status Codes
// ===========================================================================

/**
 * @enum ProseCplStatus
 * @brief Completion status returned by the endpoint via Completion Ring.
 *
 * After the PCM (Payload Commitment Mechanism) validates a descriptor:
 *   - ADMITTED: chunk passed epoch/namespace/residency checks; DMA initiated.
 *   - REJECTED_PCM: descriptor failed PCM validation (stale epoch, wrong
 *     namespace, or chunk already resident). No payload moves.
 *   - REJECTED_HEAP: Top-K heap is full and chunk priority is below the
 *     current eviction threshold. No payload moves.
 */
enum class ProseCplStatus : uint8_t {
  ADMITTED = 1,
  REJECTED_PCM = 2,
  REJECTED_HEAP = 3,
};

// ===========================================================================
// BDB Header (16 bytes, padded to 64B boundary in hardware)
// ===========================================================================

/**
 * @struct BDB_Header
 * @brief Batch Descriptor Block header. Precedes the descriptor array in MMIO.
 *
 * The endpoint reads this header first to determine how many descriptors
 * to DMA-pull from the batch. sequence_num provides ordering guarantees
 * across multiple doorbell rings.
 *
 * Layout (16 bytes effective, 64B slot in hardware):
 *   [0:7]   sequence_num  — monotonically increasing per-host sequence
 *   [8:9]   vc_id         — virtual channel ID (tenant isolation)
 *   [10:11] valid_count   — number of valid descriptors in this BDB
 *   [12:15] reserved
 */
struct alignas(64) BDB_Header {
  uint64_t sequence_num;   ///< Monotonic sequence for ordering/replay detection.
  uint16_t vc_id;          ///< Virtual Channel (tenant) identifier.
  uint16_t valid_count;    ///< Number of valid descriptors following this header.
  uint32_t reserved;       ///< Reserved for future use (must be zero).
};

static_assert(sizeof(BDB_Header) == 64,
              "BDB_Header must occupy exactly one 64B flit slot");

// ===========================================================================
// Prose Descriptor (64 bytes, hardware-defined format)
// ===========================================================================

/**
 * @struct ProseDescriptor
 * @brief Single chunk promotion descriptor within a BDB.
 *
 * Hardware format (64 bytes total):
 *   [0:1]   chunk_id       — target chunk index (0..511)
 *   [2:3]   epoch          — expected hardware epoch for PCM validation
 *   [4:5]   priority       — attention-derived priority (higher = more important)
 *   [6:6]   probe_tag      — 1 = probe-only (no DMA), 0 = full promotion
 *   [7:7]   exploit_tag    — 1 = exploit path, 0 = explore path
 *   [8:15]  src_addr       — source physical address in CXL DRAM (64B-aligned)
 *   [16:23] dst_addr       — destination physical address in GPU HBM
 *   [24:27] length         — transfer length in bytes (max 2MB per chunk)
 *   [28:29] namespace_id   — namespace for multi-tenant isolation
 *   [30:31] flags          — control flags (reserved)
 *   [32:35] pasid          — PCIe/CXL Process Address Space ID (tenant bind)
 *   [36:63] reserved       — padding to 64B
 */
struct alignas(64) ProseDescriptor {
  uint16_t chunk_id;       ///< Chunk index in the endpoint's address space.
  uint16_t epoch;          ///< Expected epoch for PCM validation.
  uint16_t priority;       ///< Attention-derived priority score.
  uint8_t  probe_tag;      ///< Probe-only flag (skip DMA if set).
  uint8_t  exploit_tag;    ///< Exploit vs explore classification.
  uint64_t src_addr;       ///< Source physical address (CXL DRAM).
  uint64_t dst_addr;       ///< Destination physical address (GPU HBM).
  uint32_t length;         ///< Transfer length in bytes.
  uint16_t namespace_id;   ///< Namespace ID for tenant isolation.
  uint16_t flags;          ///< Control flags (reserved, must be 0).
  uint32_t pasid;          ///< Process Address Space ID bound to every DMA
                           ///< write TLP so the IOMMU can trap out-of-bounds
                           ///< P2P writes to another tenant's HBM region.
  uint8_t  reserved[28];   ///< Padding to 64B alignment.
};

static_assert(sizeof(ProseDescriptor) == 64,
              "ProseDescriptor must be exactly 64 bytes");

// ===========================================================================
// Completion Entry (16 bytes)
// ===========================================================================

/**
 * @struct ProseCompletion
 * @brief Single entry in the Completion Ring.
 *
 * Written by hardware after PCM decision. Host polls for new entries
 * by checking the phase bit (toggles each wrap-around).
 */
struct ProseCompletion {
  uint64_t       sequence_num;  ///< Matches BDB sequence for correlation.
  uint16_t       chunk_id;      ///< Which chunk this completion refers to.
  ProseCplStatus status;        ///< PCM decision result.
  uint8_t        phase;         ///< Phase bit for wrap-around detection.
  uint32_t       latency_ns;    ///< Hardware-measured processing latency.
};

static_assert(sizeof(ProseCompletion) == 16,
              "ProseCompletion must be 16 bytes");

// ===========================================================================
// Chunk Candidate (host-side request structure)
// ===========================================================================

/**
 * @struct ChunkCandidate
 * @brief Host-side representation of a chunk promotion request.
 *
 * Built by the scheduler/attention engine and submitted to ProseRuntime
 * for packing into BDBs.
 */
struct ChunkCandidate {
  uint16_t chunk_id;       ///< Target chunk index.
  uint16_t epoch;          ///< Current epoch from scheduler.
  uint16_t priority;       ///< Computed priority (attention mass).
  uint64_t src_addr;       ///< CXL DRAM physical address.
  uint64_t dst_addr;       ///< GPU HBM physical address.
  uint32_t length;         ///< Chunk size in bytes.
  uint16_t namespace_id;   ///< Tenant namespace.
  bool     probe_only;     ///< Probe without DMA.
  bool     exploit;        ///< Exploit vs explore.
};

// ===========================================================================
// Lock-Free Ring Buffer (SPSC — Single Producer, Single Consumer)
// ===========================================================================

/**
 * @class LockFreeRing
 * @brief Wait-free SPSC ring buffer for MMIO submission and Completion Ring.
 *
 * @tparam T     Element type (BDB_Header+descriptors or ProseCompletion).
 * @tparam Depth Queue depth (must be power of 2).
 *
 * Memory ordering semantics:
 *   - Producer writes data with std::memory_order_relaxed, then publishes
 *     head with std::memory_order_release (store-release barrier).
 *   - Consumer reads head with std::memory_order_acquire, then reads data.
 *   - This matches CXL 3.1 §8.2.4 ordering requirements for MMIO queues.
 *
 * Backpressure handling:
 *   - try_push() returns false when ring is full (head - tail == Depth).
 *   - Caller must retry or drain completions before re-attempting.
 */
template <typename T, uint32_t Depth = kRingDepth>
class LockFreeRing {
 public:
  static_assert((Depth & (Depth - 1)) == 0, "Depth must be power of 2");

  LockFreeRing() : head_(0), tail_(0) {
    std::memset(buffer_, 0, sizeof(buffer_));
  }

  /**
   * @brief Attempt to enqueue an element (producer side).
   * @param item Element to enqueue.
   * @return true if enqueued, false if ring is full (backpressure).
   *
   * @note Must be called from a single producer thread only.
   * @note Uses release semantics on head update to ensure data visibility.
   */
  bool try_push(const T& item) {
    const uint64_t current_head = head_.load(std::memory_order_relaxed);
    const uint64_t current_tail = tail_.load(std::memory_order_acquire);

    // Check for backpressure: ring full when head - tail == Depth.
    if (current_head - current_tail >= Depth) {
      return false;  // Backpressure — caller must drain or retry.
    }

    buffer_[current_head & (Depth - 1)] = item;

    // Release barrier: ensures buffer write is visible before head advances.
    // This maps to CXL.mem store ordering requirements.
    head_.store(current_head + 1, std::memory_order_release);
    return true;
  }

  /**
   * @brief Attempt to dequeue an element (consumer side).
   * @param[out] item Dequeued element (valid only if returns true).
   * @return true if dequeued, false if ring is empty.
   *
   * @note Must be called from a single consumer thread only.
   * @note Uses acquire semantics on head read to observe producer's writes.
   */
  bool try_pop(T& item) {
    const uint64_t current_tail = tail_.load(std::memory_order_relaxed);
    const uint64_t current_head = head_.load(std::memory_order_acquire);

    if (current_tail >= current_head) {
      return false;  // Empty.
    }

    item = buffer_[current_tail & (Depth - 1)];

    // Release barrier on tail: producer can now reuse this slot.
    tail_.store(current_tail + 1, std::memory_order_release);
    return true;
  }

  /**
   * @brief Returns the number of elements currently in the ring.
   * @note Approximate — may be stale by the time caller acts on it.
   */
  uint64_t size() const {
    return head_.load(std::memory_order_acquire) -
           tail_.load(std::memory_order_acquire);
  }

  /**
   * @brief Check if ring is full (backpressure condition).
   */
  bool full() const { return size() >= Depth; }

  /**
   * @brief Check if ring is empty.
   */
  bool empty() const { return size() == 0; }

 private:
  alignas(64) T buffer_[Depth];         ///< Slot array (cache-line aligned).
  alignas(64) std::atomic<uint64_t> head_;  ///< Producer write index.
  alignas(64) std::atomic<uint64_t> tail_;  ///< Consumer read index.
};

}  // namespace prose

#endif  // PROSE_TYPES_H_
