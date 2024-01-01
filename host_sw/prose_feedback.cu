/**
 * @file prose_feedback.cu
 * @brief Attention mass feedback collection and CXL.mem async writeback.
 *
 * After FlashAttention-2 computes attention scores, this module:
 *   1. Collects per-chunk attention row-sum (attention mass) from the
 *      attention inner loop.
 *   2. Quantizes the floating-point mass to 16-bit fixed-point.
 *   3. Writes back the quantized feedback to the CXL.mem mapped address
 *      using cache-streaming stores (st.global.cs) that bypass L2 and
 *      do not block the GPU compute pipeline.
 *
 * The endpoint hardware uses this feedback to update chunk priorities
 * in the Top-K heap for future scheduling decisions.
 *
 * Design constraints:
 *   - Writeback must NOT stall the attention compute stream.
 *   - Uses separate CUDA stream for feedback to overlap with computation.
 *   - st.global.cs (Cache-Streaming) ensures write goes directly to the
 *     CXL link without polluting GPU L2 cache.
 *   - 16-bit quantization matches hardware register width.
 *
 * @author Anonymous
 */

#include <cstdint>
#include <cstdio>
#include <cuda_runtime.h>
#include <cuda_fp16.h>

namespace prose {

// ===========================================================================
// Constants
// ===========================================================================

/// Maximum attention mass value for 16-bit quantization (maps 1.0 -> 65535).
static constexpr float kMaxAttentionMass = 1.0f;

/// Quantization scale: float [0, 1] -> uint16 [0, 65535].
static constexpr float kQuantScale = 65535.0f;

/// CXL.mem feedback register stride (bytes between consecutive chunk slots).
static constexpr uint32_t kFeedbackRegisterStride = 8;

// ===========================================================================
// Attention Mass Accumulator (injected into FlashAttention-2 inner loop)
// ===========================================================================

/**
 * @brief Accumulate attention row-sum for a given chunk during FlashAttention.
 *
 * This function is designed to be called from within the FlashAttention-2
 * inner loop (the KV-block iteration). For each chunk that contributes
 * to the attention output, it accumulates the row-sum of attention weights.
 *
 * Integration point in FlashAttention-2:
 *   for (int kv_block = 0; kv_block < num_kv_blocks; ++kv_block) {
 *     // ... compute S = Q @ K^T, P = softmax(S) ...
 *     // >>> INJECT HERE <<<
 *     accumulate_attention_mass(mass_buffer, chunk_id, row_sum);
 *     // ... O += P @ V ...
 *   }
 *
 * @param mass_buffer  Device buffer holding per-chunk accumulated mass.
 * @param chunk_id     Chunk index contributing to this attention block.
 * @param row_sum      Sum of attention weights for this chunk's KV block.
 */
__device__ __forceinline__ void accumulate_attention_mass(
    float* mass_buffer, uint32_t chunk_id, float row_sum) {
  // Atomic add: multiple warps may process the same chunk concurrently.
  atomicAdd(&mass_buffer[chunk_id], row_sum);
}

// ===========================================================================
// Feedback Writeback Kernel (CXL.mem store via st.global.cs)
// ===========================================================================

/**
 * @brief Quantize attention mass and write back to CXL.mem endpoint.
 *
 * This kernel takes the accumulated attention mass for a chunk, quantizes
 * it to 16-bit fixed-point, and issues a cache-streaming store (st.global.cs)
 * to the CXL.mem mapped physical address. The store bypasses GPU L2 cache
 * and travels directly over the CXL link to the endpoint's feedback register.
 *
 * Memory ordering:
 *   - st.global.cs is a relaxed, non-blocking store — it does NOT stall
 *     the GPU compute pipeline. The CXL link handles flow control.
 *   - No fence is needed after this store because the endpoint processes
 *     feedback asynchronously (no ordering dependency with subsequent GPU ops).
 *
 * Hardware register layout at cxl_mem_ptr:
 *   [chunk_id * 8 + 0:1]  — quantized attention mass (uint16)
 *   [chunk_id * 8 + 2:3]  — timestamp (set by hardware)
 *   [chunk_id * 8 + 4:7]  — reserved
 *
 * @param cxl_mem_ptr      Device-mapped pointer to CXL.mem feedback region.
 * @param chunk_id         Target chunk for feedback writeback.
 * @param attention_mass   Floating-point accumulated attention mass [0, 1].
 */
__global__ void feedback_writeback_kernel(uint64_t* cxl_mem_ptr,
                                          uint16_t chunk_id,
                                          float attention_mass) {
  // Quantize: clamp to [0, 1] then scale to uint16 range.
  float clamped = fminf(fmaxf(attention_mass, 0.0f), kMaxAttentionMass);
  uint16_t quantized = static_cast<uint16_t>(clamped * kQuantScale);

  // Calculate target address in CXL.mem feedback register space.
  // Each chunk has an 8-byte register slot; feedback is in the first 2 bytes.
  uint64_t* target = reinterpret_cast<uint64_t*>(
      reinterpret_cast<uint8_t*>(cxl_mem_ptr) +
      static_cast<uint64_t>(chunk_id) * kFeedbackRegisterStride);

  // Pack quantized mass into the lower 16 bits of the 64-bit register.
  uint64_t payload = static_cast<uint64_t>(quantized);

  // Use inline PTX for st.global.cs (Cache-Streaming store).
  // This store bypasses L2 cache and writes directly to the CXL link,
  // ensuring minimal interference with active compute workloads.
  // The .cs qualifier maps to the SASS ST.E.CS instruction on sm_80+.
  asm volatile(
      "st.global.cs.u64 [%0], %1;"
      :
      : "l"(target), "l"(payload)
      : "memory");
}

// ===========================================================================
// Batch Feedback Writeback
// ===========================================================================

/**
 * @brief Batch writeback of attention mass for multiple chunks.
 *
 * Processes all chunks that were active in the current decode step,
 * quantizes their attention masses, and issues streaming stores to
 * the CXL.mem endpoint. Designed to run on a separate CUDA stream
 * to overlap with the next attention computation.
 *
 * @param cxl_mem_ptr      Device-mapped CXL.mem feedback region.
 * @param mass_buffer      Per-chunk accumulated attention mass (device).
 * @param chunk_ids        Array of active chunk IDs this step (device).
 * @param num_chunks       Number of active chunks.
 */
__global__ void feedback_writeback_batch_kernel(
    uint64_t* cxl_mem_ptr,
    const float* mass_buffer,
    const uint32_t* chunk_ids,
    uint32_t num_chunks) {
  uint32_t idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= num_chunks) return;

  uint32_t cid = chunk_ids[idx];
  float mass = mass_buffer[cid];

  // Quantize: clamp and scale.
  float clamped = fminf(fmaxf(mass, 0.0f), kMaxAttentionMass);
  uint16_t quantized = static_cast<uint16_t>(clamped * kQuantScale);

  // Target address in CXL.mem feedback register space.
  uint64_t* target = reinterpret_cast<uint64_t*>(
      reinterpret_cast<uint8_t*>(cxl_mem_ptr) +
      static_cast<uint64_t>(cid) * kFeedbackRegisterStride);

  uint64_t payload = static_cast<uint64_t>(quantized);

  // Cache-streaming store: bypass L2, write directly to CXL link.
  // Non-blocking — does not stall the SM's compute pipeline.
  asm volatile(
      "st.global.cs.u64 [%0], %1;"
      :
      : "l"(target), "l"(payload)
      : "memory");
}

// ===========================================================================
// Host-Callable API
// ===========================================================================

/**
 * @brief Launch batch feedback writeback on a dedicated stream.
 *
 * This function is called by the host after each decode step completes.
 * It launches the writeback kernel on a separate stream so it overlaps
 * with the next step's attention computation.
 *
 * @param cxl_mem_ptr    Device-mapped CXL.mem feedback region.
 * @param mass_buffer    Device buffer with per-chunk attention mass.
 * @param chunk_ids      Device array of active chunk IDs.
 * @param num_chunks     Number of active chunks.
 * @param stream         Dedicated feedback stream (separate from compute).
 */
extern "C" void prose_launch_feedback_writeback(
    uint64_t* cxl_mem_ptr,
    const float* mass_buffer,
    const uint32_t* chunk_ids,
    uint32_t num_chunks,
    cudaStream_t stream) {
  if (num_chunks == 0) return;

  uint32_t block_size = 128;
  uint32_t grid_size = (num_chunks + block_size - 1) / block_size;

  feedback_writeback_batch_kernel<<<grid_size, block_size, 0, stream>>>(
      cxl_mem_ptr, mass_buffer, chunk_ids, num_chunks);

  // No synchronization here — the stream is fire-and-forget.
  // CXL endpoint processes feedback asynchronously.
  // Error checking deferred to next synchronization point.
}

/**
 * @brief Causal (t-1) double-buffered feedback writeback.
 *
 * Enforces the causal boundary on the HOST side to match the RTL Expert Bank:
 * the scorer at decode step t must only ever see attention mass committed at
 * step t-1. This launcher writes back the PREVIOUS step's mass buffer
 * (prev_mass_buffer, filled during step t-1) and only when the endpoint
 * pipeline is idle at the step boundary (pipeline_idle != 0), i.e. no step-t
 * descriptor is still in flight. The current step's accumulator
 * (curr_mass_buffer) is left untouched here; the caller swaps the two buffers
 * at the next step boundary (ping-pong), so step-t attention can never flow
 * into step-t scoring.
 *
 * Typical caller pattern:
 *   // end of step t-1, at the drain/step boundary:
 *   prose_launch_feedback_writeback_causal(cxl, prev, curr, ids, n,
 *                                          pipeline_idle, stream);
 *   std::swap(prev, curr);         // curr(t-1) becomes prev for step t
 *   cudaMemsetAsync(curr, 0, ...); // fresh accumulator for step t
 *
 * @param cxl_mem_ptr       Device-mapped CXL.mem feedback region.
 * @param prev_mass_buffer  Committed t-1 attention mass (written back).
 * @param curr_mass_buffer  In-progress step-t accumulator (NOT written).
 * @param chunk_ids         Device array of active chunk IDs (t-1 set).
 * @param num_chunks        Number of active chunks.
 * @param pipeline_idle     Nonzero => endpoint drained; safe to write feedback.
 * @param stream            Dedicated feedback stream.
 * @return true if the writeback was launched, false if gated (not idle).
 */
extern "C" bool prose_launch_feedback_writeback_causal(
    uint64_t* cxl_mem_ptr,
    const float* prev_mass_buffer,
    const float* /*curr_mass_buffer*/,
    const uint32_t* chunk_ids,
    uint32_t num_chunks,
    uint32_t pipeline_idle,
    cudaStream_t stream) {
  if (num_chunks == 0) return false;
  // Step-granularity causal gate: never publish feedback while a step-t
  // descriptor could still be scored against it.
  if (pipeline_idle == 0) return false;

  uint32_t block_size = 128;
  uint32_t grid_size = (num_chunks + block_size - 1) / block_size;

  // Write back ONLY the committed previous-step buffer.
  feedback_writeback_batch_kernel<<<grid_size, block_size, 0, stream>>>(
      cxl_mem_ptr, prev_mass_buffer, chunk_ids, num_chunks);
  return true;
}

/**
 * @brief Launch single-chunk feedback writeback (for testing).
 *
 * @param cxl_mem_ptr    Device-mapped CXL.mem feedback region.
 * @param chunk_id       Target chunk.
 * @param attention_mass Floating-point attention mass.
 * @param stream         CUDA stream.
 */
extern "C" void prose_launch_single_feedback(
    uint64_t* cxl_mem_ptr,
    uint16_t chunk_id,
    float attention_mass,
    cudaStream_t stream) {
  feedback_writeback_kernel<<<1, 1, 0, stream>>>(
      cxl_mem_ptr, chunk_id, attention_mass);
}

// ===========================================================================
// FlashAttention-2 Integration Example
// ===========================================================================

/**
 * @brief Example FlashAttention-2 kernel with PROSE-APEX feedback injection.
 *
 * This demonstrates where to inject the attention mass accumulation
 * in a simplified FlashAttention-2 inner loop. The actual FA2 kernel
 * is much more complex; this shows the integration pattern.
 *
 * @param Q             Query matrix [seq_len, head_dim].
 * @param K             Key matrix [kv_len, head_dim].
 * @param V             Value matrix [kv_len, head_dim].
 * @param O             Output matrix [seq_len, head_dim].
 * @param chunk_map     Maps KV block index to chunk_id.
 * @param mass_buffer   Per-chunk attention mass accumulator.
 * @param seq_len       Query sequence length.
 * @param kv_len        KV sequence length.
 * @param head_dim      Head dimension.
 * @param block_size    KV block size (typically 128 or 256).
 */
__global__ void flash_attention_with_feedback(
    const float* Q, const float* K, const float* V, float* O,
    const uint32_t* chunk_map,
    float* mass_buffer,
    uint32_t seq_len, uint32_t kv_len, uint32_t head_dim,
    uint32_t block_size) {
  // Simplified: each thread handles one query position.
  uint32_t q_idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (q_idx >= seq_len) return;

  float row_max = -1e30f;
  float row_sum = 0.0f;

  // KV-block iteration (FlashAttention-2 tiling pattern).
  uint32_t num_kv_blocks = (kv_len + block_size - 1) / block_size;
  for (uint32_t kv_block = 0; kv_block < num_kv_blocks; ++kv_block) {
    // ... [Simplified] compute S = Q @ K^T for this block ...
    // ... [Simplified] compute local softmax: P = exp(S - max) ...
    // ... [Simplified] accumulate O += P @ V ...

    // Simulated row-sum for this KV block (in real FA2, this is the
    // sum of exp(S - max) values for the current block).
    float block_row_sum = 1.0f / static_cast<float>(num_kv_blocks);

    // >>> PROSE-APEX FEEDBACK INJECTION <<<
    // Accumulate attention mass for the chunk that owns this KV block.
    uint32_t chunk_id = chunk_map[kv_block];
    accumulate_attention_mass(mass_buffer, chunk_id, block_row_sum);
  }
}

}  // namespace prose
