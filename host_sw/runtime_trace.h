/**
 * @file runtime_trace.h
 * @brief Low-overhead JSONL instrumentation for allocator/runtime events.
 *
 * The timestamps use one steady-clock origin per process.  The event schema is
 * intentionally shared with experiments/run_runtime_staleness.py so traces
 * captured on a real CXL host and traces captured by the multi-process harness
 * can be replayed by the same analysis code.
 */

#ifndef PROSE_RUNTIME_TRACE_H_
#define PROSE_RUNTIME_TRACE_H_

#include <chrono>
#include <cstdint>
#include <fstream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>

namespace prose {

class RuntimeTraceSink {
 public:
  explicit RuntimeTraceSink(const std::string& path)
      : out_(path, std::ios::out | std::ios::trunc),
        origin_(std::chrono::steady_clock::now()) {
    if (!out_) {
      throw std::runtime_error("RuntimeTraceSink: cannot open " + path);
    }
  }

  uint64_t now_ns() const {
    const auto delta = std::chrono::steady_clock::now() - origin_;
    return static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(delta).count());
  }

  void residency_transition(uint64_t chunk_key, bool resident,
                            const char* tier, uint64_t timestamp_ns = 0) {
    write_event("residency_transition", chunk_key, 0, timestamp_ns,
                resident ? "resident" : "evicted", tier);
  }

  void descriptor_generated(uint16_t chunk_id, uint64_t sequence,
                            uint64_t start_ns, uint64_t end_ns,
                            uint16_t host_id) {
    std::lock_guard<std::mutex> lock(mu_);
    out_ << "{\"event\":\"descriptor_generated\",\"timestamp_ns\":"
         << end_ns << ",\"generation_start_ns\":" << start_ns
         << ",\"generation_time_ns\":" << (end_ns - start_ns)
         << ",\"chunk_id\":" << chunk_id << ",\"sequence\":"
         << sequence << ",\"host_id\":" << host_id << "}\n";
  }

  void descriptor_enqueued(uint16_t chunk_id, uint64_t sequence,
                           uint16_t host_id, uint64_t timestamp_ns = 0) {
    write_event("descriptor_enqueued", chunk_id, sequence, timestamp_ns,
                "", "", host_id);
  }

  void descriptor_ingress(uint16_t chunk_id, uint64_t sequence,
                          uint16_t host_id, uint64_t timestamp_ns = 0) {
    write_event("endpoint_ingress", chunk_id, sequence, timestamp_ns,
                "", "", host_id);
  }

  void descriptor_dequeued(uint16_t chunk_id, uint64_t sequence,
                           uint16_t host_id, uint64_t timestamp_ns = 0) {
    write_event("descriptor_dequeued", chunk_id, sequence, timestamp_ns,
                "", "", host_id);
  }

  void dma_committed(uint16_t chunk_id, uint64_t sequence,
                     uint16_t host_id, uint64_t timestamp_ns = 0) {
    write_event("dma_committed", chunk_id, sequence, timestamp_ns,
                "", "", host_id);
  }

  void completion_observed(uint16_t chunk_id, uint64_t sequence,
                           uint16_t host_id, uint64_t timestamp_ns = 0) {
    write_event("completion_observed", chunk_id, sequence, timestamp_ns,
                "", "", host_id);
  }

 private:
  void write_event(const char* event, uint64_t chunk_key, uint64_t sequence,
                   uint64_t timestamp_ns, const char* state,
                   const char* tier, uint16_t host_id = 0) {
    if (timestamp_ns == 0) timestamp_ns = now_ns();
    std::lock_guard<std::mutex> lock(mu_);
    out_ << "{\"event\":\"" << event << "\",\"timestamp_ns\":"
         << timestamp_ns << ",\"chunk_id\":" << chunk_key
         << ",\"sequence\":" << sequence << ",\"host_id\":"
         << host_id;
    if (state[0] != '\0') out_ << ",\"state\":\"" << state << "\"";
    if (tier[0] != '\0') out_ << ",\"tier\":\"" << tier << "\"";
    out_ << "}\n";
  }

  std::ofstream out_;
  std::chrono::steady_clock::time_point origin_;
  mutable std::mutex mu_;
};

using RuntimeTraceSinkPtr = std::shared_ptr<RuntimeTraceSink>;

}  // namespace prose

#endif  // PROSE_RUNTIME_TRACE_H_
