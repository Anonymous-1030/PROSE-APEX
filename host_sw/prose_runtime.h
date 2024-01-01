/**
 * @file prose_runtime.h
 * @brief ProseRuntime class — BDB construction and submission engine.
 *
 * @author Anonymous
 */

#ifndef PROSE_RUNTIME_H_
#define PROSE_RUNTIME_H_

#include <cstdint>
#include <vector>
#include <memory>
#include <atomic>
#include <functional>

#include "prose_types.h"
#include "prose_allocator.h"

namespace prose {

/**
 * @struct SubmitStats
 * @brief Per-batch submission statistics.
 */
struct SubmitStats {
  uint32_t total_submitted;    ///< Total descriptors submitted.
  uint32_t admitted;           ///< Descriptors admitted by PCM.
  uint32_t rejected_pcm;      ///< Descriptors rejected by PCM.
  uint32_t rejected_heap;     ///< Descriptors rejected by Top-K heap.
  double   throughput_gbps;   ///< Effective DMA throughput.
  double   rpe;               ///< Reclaimed-Payload Exposure (must be 0).
};

/**
 * @typedef FallbackHandler
 * @brief Callback invoked when a chunk fails GPU-side PVM validation.
 */
using FallbackHandler = std::function<void(uint16_t chunk_id)>;

/**
 * @class ProseRuntime
 * @brief Central submission engine for PROSE-APEX host software stack.
 *
 * Responsibilities:
 *   1. Pack ChunkCandidates into BDBs (respecting max batch size).
 *   2. Write BDBs to MMIO submission queue via memcpy_mmio.
 *   3. Ring the Doorbell (MMIO write to base + 0xFFC).
 *   4. Poll Completion Ring for PCM decisions.
 *   5. Track statistics (throughput, RPE).
 *
 * Thread safety:
 *   - submit_batch() must be called from a single thread (submission path).
 *   - poll_completions() may be called from a separate polling thread.
 *   - Statistics are atomically updated.
 */
class ProseRuntime {
 public:
  /**
   * @brief Construct the runtime with MMIO base and allocator.
   * @param mmio_base     Virtual address of the MMIO BAR mapping.
   * @param allocator     Shared allocator for resource management.
   * @param vc_id         Virtual channel ID for this host/tenant.
   * @param pasid         Process Address Space ID for this tenant. Bound into
   *                      every DMA write descriptor so the IOMMU can isolate
   *                      cross-tenant P2P writes. Must be non-zero; a zero
   *                      PASID is rejected (0 is reserved / "no ASID").
   */
  ProseRuntime(void* mmio_base, std::shared_ptr<ProseAllocator> allocator,
               uint16_t vc_id = 0, uint32_t pasid = 0,
               RuntimeTraceSinkPtr trace_sink = nullptr);

  ~ProseRuntime() = default;

  // Non-copyable.
  ProseRuntime(const ProseRuntime&) = delete;
  ProseRuntime& operator=(const ProseRuntime&) = delete;

  /**
   * @brief Submit a batch of chunk promotion candidates.
   *
   * Packs candidates into one or more BDBs (splitting at kMaxDescriptorsPerBDB),
   * writes each BDB to the MMIO submission queue, and rings the Doorbell.
   *
   * Timing constraints:
   *   - BDB must be fully written before Doorbell (write-combining fence).
   *   - Doorbell write is a single 32-bit store to mmio_base + 0xFFC.
   *   - If submission queue is full, spins until space is available or timeout.
   *
   * @param candidates  Vector of chunk promotion requests.
   * @return Number of descriptors successfully enqueued.
   * @throws std::runtime_error on submission queue timeout.
   */
  uint32_t submit_batch(const std::vector<ChunkCandidate>& candidates);

  /**
   * @brief Poll the Completion Ring for hardware responses.
   *
   * Reads completion entries, updates internal statistics, and recycles
   * resources for rejected descriptors.
   *
   * @param max_entries  Maximum completions to process in one call.
   * @return Number of completions processed.
   */
  uint32_t poll_completions(uint32_t max_entries = 256);

  /**
   * @brief Get cumulative submission statistics.
   */
  SubmitStats get_stats() const;

  /**
   * @brief Reset statistics counters.
   */
  void reset_stats();

  /**
   * @brief Register a fallback handler for PVM validation failures.
   * @param handler  Callback invoked with the failed chunk_id.
   */
  void set_fallback_handler(FallbackHandler handler);

  /**
   * @brief Get the current hardware epoch (read from MMIO config space).
   */
  uint16_t current_epoch() const;

  /**
   * @brief Get the set of chunk_ids currently awaiting completion.
   */
  std::vector<uint16_t> pending_chunks() const;

 private:
  /// MMIO base virtual address (mapped BAR).
  volatile uint8_t* mmio_base_;

  /// Shared memory allocator.
  std::shared_ptr<ProseAllocator> allocator_;

  /// Virtual channel ID for this tenant.
  uint16_t vc_id_;

  /// Process Address Space ID bound to every DMA write from this tenant.
  uint32_t pasid_;

  RuntimeTraceSinkPtr trace_sink_;  ///< Optional host-runtime event recorder.

  /// Monotonically increasing submission sequence number.
  std::atomic<uint64_t> sequence_num_;

  /// Submission ring buffer (host -> hardware).
  LockFreeRing<BDB_Header, 256> submit_ring_;

  /// Completion ring buffer (hardware -> host).
  LockFreeRing<ProseCompletion, 1024> completion_ring_;

  /// Cumulative statistics (atomic for cross-thread visibility).
  std::atomic<uint32_t> stat_submitted_;
  std::atomic<uint32_t> stat_admitted_;
  std::atomic<uint32_t> stat_rejected_pcm_;
  std::atomic<uint32_t> stat_rejected_heap_;
  std::atomic<uint64_t> stat_bytes_transferred_;

  /// Pending chunk tracking (for resource lifecycle).
  std::vector<uint16_t> pending_chunks_;
  mutable std::mutex pending_mu_;

  /// Fallback handler for PVM failures.
  FallbackHandler fallback_handler_;

  /// Write a single BDB to the MMIO submission queue.
  void write_bdb_to_mmio(const BDB_Header& header,
                         const ProseDescriptor* descriptors,
                         uint16_t count);

  /// Ring the hardware doorbell (MMIO store to base + 0xFFC).
  void ring_doorbell(uint64_t sequence);

  /// MMIO memory copy with write-combining semantics.
  void memcpy_mmio(volatile void* dst, const void* src, size_t len);
};

}  // namespace prose

#endif  // PROSE_RUNTIME_H_
