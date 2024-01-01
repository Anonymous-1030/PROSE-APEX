/**
 * @file prose_gpu_bridge.cu
 * @brief GPU-side visibility state machine and PVM validation for PROSE-APEX.
 *
 * After a chunk is ADMITTED by PCM and DMA completes, the host must make
 * the chunk "visible" to GPU compute kernels via a valid_chunk_bitmap.
 * This file implements:
 *   1. mark_visible_kernel: atomically sets a chunk's bit in the bitmap
 *      with proper system-level memory ordering (__threadfence_system).
 *   2. PVM (Post-Visibility Validation) simulation: checks chunk integrity
 *      after DMA and triggers host fallback on failure.
 *   3. Test harness demonstrating the visibility state machine.
 *
 * Memory ordering requirements (CUDA + CXL):
 *   - __threadfence_system() ensures that stores made by the DMA engine
 *     (which is a system-level agent) are visible to the GPU before the
 *     bitmap is updated. This is mandatory for CXL coherence domains.
 *   - atomicOr provides the atomic read-modify-write for the bitmap word.
 *
 * @author Anonymous
 */

#include <cstdint>
#include <cstdio>
#include <cuda_runtime.h>
#include <cooperative_groups.h>

namespace cg = cooperative_groups;

namespace prose {

// ===========================================================================
// Constants
// ===========================================================================

/// Maximum chunks managed by the endpoint (matches hardware).
static constexpr uint32_t kMaxChunksGPU = 512;

/// Bitmap words: 512 / 32 = 16 words.
static constexpr uint32_t kBitmapWordsGPU = (kMaxChunksGPU + 31) / 32;

// ===========================================================================
// Visibility Kernel
// ===========================================================================

/**
 * @brief Mark a chunk as visible in the GPU-side validity bitmap.
 *
 * CRITICAL ORDERING (producer release):
 *   1. __threadfence_system() — orders all prior DMA writes from the CXL
 *      endpoint (a system-level agent) BEFORE the bitmap publish. The fence
 *      MUST precede the atomicOr: the pattern is "write data -> fence ->
 *      publish flag". If the fence were placed AFTER the atomicOr, a consumer
 *      could observe the visibility bit before the chunk data landed — the
 *      exact read-after-write hazard this is meant to prevent. (This is a
 *      deliberate deviation from a request to move the fence after the store;
 *      moving it after would reintroduce the hazard.)
 *   2. atomicOr — publishes the chunk's bit. Paired with an acquire on the
 *      consumer side (see is_chunk_visible), this forms a release/acquire
 *      handshake across SMs.
 *
 * PRECONDITION: the caller must only launch this after the CXL fabric has
 * acknowledged the chunk's DMA write completion (see prose_mark_visible_batch's
 * dma_ack gate) — "DMA initiated" is NOT sufficient to publish visibility.
 *
 * @param valid_bitmap  Device pointer to the visibility bitmap (16 words).
 * @param chunk_id      Chunk index to mark visible (0..511).
 */
__global__ void mark_visible_kernel(uint32_t* valid_bitmap,
                                    uint32_t chunk_id) {
  // Bounds check: prevent out-of-range bitmap access.
  if (chunk_id >= kMaxChunksGPU) return;

  // STEP 1: System-level release fence — all endpoint DMA writes to this
  // chunk are made visible to the whole system BEFORE the bit is published.
  __threadfence_system();

  // STEP 2: Publish the visibility bit (release store via atomic RMW).
  atomicOr(&valid_bitmap[chunk_id / 32], 1u << (chunk_id % 32));
}

// ===========================================================================
// PVM Validation Kernel (Post-Visibility Mechanism)
// ===========================================================================

/**
 * @struct PVMResult
 * @brief Per-chunk validation result written by the PVM kernel.
 */
struct PVMResult {
  uint32_t chunk_id;    ///< Validated chunk index.
  uint32_t checksum;    ///< Computed checksum of chunk data.
  uint32_t expected;    ///< Expected checksum (from descriptor metadata).
  uint32_t valid;       ///< 1 = pass, 0 = fail.
};

/**
 * @brief Post-visibility validation kernel.
 *
 * After DMA completes and before marking visible, the host can optionally
 * run this kernel to verify chunk data integrity (checksum, magic bytes, etc.).
 * If validation fails, the chunk is NOT marked visible and the host is
 * notified to take over (fallback path).
 *
 * @param chunk_data       Pointer to the DMA'd chunk data in GPU HBM.
 * @param chunk_size       Size of each chunk in bytes.
 * @param chunk_ids        Array of chunk IDs to validate.
 * @param expected_checksums  Expected checksums per chunk.
 * @param results          Output validation results.
 * @param num_chunks       Number of chunks to validate.
 */
__global__ void pvm_validate_kernel(const uint8_t* chunk_data,
                                    uint32_t chunk_size,
                                    const uint32_t* chunk_ids,
                                    const uint32_t* expected_checksums,
                                    PVMResult* results,
                                    uint32_t num_chunks) {
  uint32_t idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= num_chunks) return;

  uint32_t cid = chunk_ids[idx];
  const uint8_t* data = chunk_data + static_cast<uint64_t>(cid) * chunk_size;

  // Simple Fletcher-32 checksum for validation.
  uint32_t sum1 = 0, sum2 = 0;
  for (uint32_t i = 0; i < chunk_size; i += 4) {
    uint32_t word;
    memcpy(&word, &data[i], sizeof(uint32_t));
    sum1 = (sum1 + word) & 0xFFFF;
    sum2 = (sum2 + sum1) & 0xFFFF;
  }
  uint32_t checksum = (sum2 << 16) | sum1;

  results[idx].chunk_id = cid;
  results[idx].checksum = checksum;
  results[idx].expected = expected_checksums[idx];
  results[idx].valid = (checksum == expected_checksums[idx]) ? 1 : 0;
}

// ===========================================================================
// Visibility State Machine (Host-callable)
// ===========================================================================

/**
 * @brief Batch mark-visible with PVM validation gate.
 *
 * Flow:
 *   1. Run pvm_validate_kernel on all pending chunks.
 *   2. For each chunk that passes PVM *and* whose CXL fabric write-completion
 *      has been acknowledged (dma_ack_host[i] != 0): launch mark_visible_kernel.
 *   3. For each chunk that fails PVM: write to fallback_flags (host polls).
 *   4. A chunk that passed PVM but has NOT yet been acked is left unmarked
 *      (not visible, not fallback) for a later call — publishing it now would
 *      expose data the fabric has not confirmed landed.
 *
 * @param valid_bitmap       Device pointer to visibility bitmap.
 * @param chunk_data         Device pointer to chunk data buffer.
 * @param chunk_size         Per-chunk size in bytes.
 * @param chunk_ids          Host array of chunk IDs to process.
 * @param expected_checksums Host array of expected checksums.
 * @param dma_ack_host       Host array; nonzero => CXL fabric acked this
 *                           chunk's write completion. Gates visibility.
 * @param num_chunks         Number of chunks in this batch.
 * @param fallback_flags     Device array — set to 1 for failed chunks.
 * @param stream             CUDA stream for async execution.
 *
 * @return Number of chunks that passed validation.
 */
extern "C" uint32_t prose_mark_visible_batch(
    uint32_t* valid_bitmap,
    const uint8_t* chunk_data,
    uint32_t chunk_size,
    const uint32_t* chunk_ids_host,
    const uint32_t* expected_checksums_host,
    const uint32_t* dma_ack_host,
    uint32_t num_chunks,
    uint32_t* fallback_flags,
    cudaStream_t stream) {
  if (num_chunks == 0) return 0;

  // Allocate device buffers for validation.
  uint32_t* d_chunk_ids = nullptr;
  uint32_t* d_expected = nullptr;
  PVMResult* d_results = nullptr;

  cudaMallocAsync(&d_chunk_ids, num_chunks * sizeof(uint32_t), stream);
  cudaMallocAsync(&d_expected, num_chunks * sizeof(uint32_t), stream);
  cudaMallocAsync(&d_results, num_chunks * sizeof(PVMResult), stream);

  cudaMemcpyAsync(d_chunk_ids, chunk_ids_host,
                  num_chunks * sizeof(uint32_t),
                  cudaMemcpyHostToDevice, stream);
  cudaMemcpyAsync(d_expected, expected_checksums_host,
                  num_chunks * sizeof(uint32_t),
                  cudaMemcpyHostToDevice, stream);

  // Run PVM validation.
  uint32_t block_size = 256;
  uint32_t grid_size = (num_chunks + block_size - 1) / block_size;
  pvm_validate_kernel<<<grid_size, block_size, 0, stream>>>(
      chunk_data, chunk_size, d_chunk_ids, d_expected, d_results, num_chunks);

  // Copy results back to host for decision.
  PVMResult* h_results = new PVMResult[num_chunks];
  cudaMemcpyAsync(h_results, d_results, num_chunks * sizeof(PVMResult),
                  cudaMemcpyDeviceToHost, stream);
  cudaStreamSynchronize(stream);

  // Process results: mark visible or flag for fallback.
  uint32_t passed = 0;
  for (uint32_t i = 0; i < num_chunks; ++i) {
    if (h_results[i].valid) {
      ++passed;  // Passed PVM integrity check.
      // Visibility gate: only publish once the CXL fabric has ACKed the write
      // completion for this chunk. A passed-but-unacked chunk is deliberately
      // left unmarked (neither visible nor fallback) for a subsequent call.
      if (dma_ack_host != nullptr && dma_ack_host[i] != 0) {
        mark_visible_kernel<<<1, 1, 0, stream>>>(
            valid_bitmap, h_results[i].chunk_id);
      }
    } else {
      // Flag for host fallback — chunk data is NOT visible to compute.
      // Host polls fallback_flags and takes over processing.
      uint32_t one = 1;
      cudaMemcpyAsync(&fallback_flags[h_results[i].chunk_id], &one,
                      sizeof(uint32_t), cudaMemcpyHostToDevice, stream);
    }
  }

  // Cleanup.
  cudaFreeAsync(d_chunk_ids, stream);
  cudaFreeAsync(d_expected, stream);
  cudaFreeAsync(d_results, stream);
  delete[] h_results;

  return passed;
}

// ===========================================================================
// Utility: Check if a chunk is visible
// ===========================================================================

/**
 * @brief Device-side inline check for chunk visibility (acquire semantics).
 *
 * Used by attention kernels to verify a chunk is safe to read before
 * accessing its data. Performs an ACQUIRE load of the bitmap word so that,
 * once the visibility bit is observed set, all of the producer's prior DMA
 * writes to the chunk (ordered before the release in mark_visible_kernel) are
 * guaranteed visible to this thread. This closes the read-after-write hazard
 * even when the attention kernel runs on a different stream than the one that
 * marked visibility.
 *
 * Implementation: __ldcg (cache-global load, bypassing stale L1) followed by a
 * __threadfence_system() acquire barrier when the bit is set. On sm_70+ this is
 * equivalent to an acquire load of the flag.
 */
__device__ __forceinline__ bool is_chunk_visible(
    const uint32_t* valid_bitmap, uint32_t chunk_id) {
  if (chunk_id >= kMaxChunksGPU) return false;
  // Cache-global load to avoid observing a stale L1-cached bitmap word.
  uint32_t word = __ldcg(&valid_bitmap[chunk_id / 32]);
  bool set = (word >> (chunk_id % 32)) & 1u;
  if (set) {
    // Acquire barrier: pair with the producer's release fence so the chunk
    // data ordered before the publish is visible before we read it.
    __threadfence_system();
  }
  return set;
}

// ===========================================================================
// Test Harness
// ===========================================================================

/**
 * @brief Test kernel verifying visibility state machine behavior.
 *
 * Simulates:
 *   - Normal case: chunk passes PVM -> becomes visible.
 *   - Failure case: chunk fails PVM -> NOT visible, fallback triggered.
 */
__global__ void test_visibility_kernel(const uint32_t* valid_bitmap,
                                       const uint32_t* fallback_flags,
                                       uint32_t* test_results,
                                       uint32_t num_chunks) {
  uint32_t idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= num_chunks) return;

  // test_results[idx]:
  //   0 = chunk correctly not visible and not fallback (not processed yet)
  //   1 = chunk correctly visible (PVM passed)
  //   2 = chunk correctly in fallback (PVM failed, not visible)
  //   3 = ERROR: chunk visible AND in fallback (should never happen)
  bool visible = is_chunk_visible(valid_bitmap, idx);
  bool fallback = (fallback_flags[idx] != 0);

  if (visible && !fallback) {
    test_results[idx] = 1;  // Normal: admitted and visible.
  } else if (!visible && fallback) {
    test_results[idx] = 2;  // Correct fallback.
  } else if (!visible && !fallback) {
    test_results[idx] = 0;  // Not processed yet.
  } else {
    test_results[idx] = 3;  // ERROR state.
  }
}

}  // namespace prose
