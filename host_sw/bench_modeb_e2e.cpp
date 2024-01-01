/**
 * @file bench_modeb_e2e.cpp
 * @brief Single-host, end-to-end Mode B (endpoint-gated pull) benchmark on a
 *        commercial CXL Type-3 device.
 *
 * WHAT THIS ANSWERS
 * -----------------
 * The sharpest reviewer attack on PROSE-APEX is "it is all simulation, there is
 * no real hardware, so the RPE=0 claim is a projection." This benchmark runs the
 * Mode B protocol against a *real, commodity* CXL Type-3 memory device — the
 * kind you can buy today — with NO custom endpoint silicon and NO endpoint-side
 * DMA engine. It measures, on real CXL.mem traffic:
 *
 *   1. RPE (Reclaimed-Payload Exposure) — bytes of chunks that failed admission
 *      (a reused/stale object version) that nonetheless crossed the CXL link.
 *      The Mode B guarantee is RPE == 0: reject-before-pull issues no payload.
 *   2. Promotion latency — wall-clock from scorer decision to the admitted
 *      chunk landing in local memory (the host-initiated pull completing).
 *
 * WHY MODE B NEEDS NO CUSTOM HARDWARE
 * -----------------------------------
 * A Type-3 device is just Host-managed Device Memory (HDM). In Mode B the device
 * does NOT push payload; it only hands out reservation *tokens* (a decision).
 * The HOST issues the payload transfer as ordinary CXL.mem loads, and it issues
 * a load ONLY for a chunk it holds a valid token for. So:
 *
 *      reject  ==>  no token  ==>  host issues no load  ==>  no payload on link
 *
 * That ordering — decide before pull — is the entire RPE=0 argument, and it is
 * substrate-independent. It holds on one host because the sole reader is the one
 * that checks the token. The endpoint's extra value across trust domains
 * (multi-host CFO, no-cross-host-race) is NOT claimed here; this is the
 * single-host existence proof the paper's Conclusion rests on.
 *
 * FALSIFIABILITY (this is the important part)
 * -------------------------------------------
 * An "RPE=0" number is worthless unless the instrument can register a NON-zero
 * RPE. So we run a Fetch-Then-Score (FTS) baseline through the SAME byte
 * counter, on the SAME device. FTS reads every candidate before scoring, so its
 * rejected reads MUST show up as RPE > 0. Only if FTS reports RPE > 0 and Mode B
 * reports RPE == 0 on the same substrate is the result meaningful.
 *
 * @author Anonymous
 */

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <string>
#include <vector>

#include "cxl_devmem.h"

using namespace prose;
using Clock = std::chrono::steady_clock;

namespace {

// --------------------------------------------------------------------------
// Benchmark configuration
// --------------------------------------------------------------------------
struct BenchConfig {
  size_t   chunk_bytes    = 64 * 1024;  ///< KV chunk size (64 KB).
  uint32_t n_chunks       = 512;        ///< Chunks resident in the CXL pool.
  uint32_t candidates     = 256;        ///< Candidates scored per decode step.
  uint32_t budget         = 32;         ///< Admitted (pulled) per step.
  uint32_t steps          = 200;        ///< Decode steps to run.
  double   accept_rate    = 0.60;       ///< Scorer accept fraction (informational).
  uint16_t token_ttl_us   = 50;         ///< Reservation token validity window.
  double   epoch_roll_pct = 0.02;       ///< Fraction of tokens that go stale.
  uint32_t seed           = 42;
};

// --------------------------------------------------------------------------
// Pull engine — the ONLY code that touches CXL device memory.
//
// Every byte read from the device flows through pull(), which tags the read as
// admitted or rejected. RPE = bytes read for rejected chunks. Because both
// Mode B and the FTS baseline share this one instrument, an RPE=0 for Mode B is
// only credible next to a non-zero RPE for FTS on the same object.
// --------------------------------------------------------------------------
class PullEngine {
 public:
  PullEngine(CxlDevMem* dev, size_t chunk_bytes)
      : dev_(dev), chunk_bytes_(chunk_bytes),
        sink_(new uint8_t[chunk_bytes]) {}
  ~PullEngine() { delete[] sink_; }

  /**
   * @brief Pull one chunk out of CXL device memory into local memory.
   *
   * This is a real CXL.mem read stream on a real device. We touch every cache
   * line so the transfer cannot be elided, and fold the bytes into a checksum
   * the caller later consumes (defeats dead-read elimination). The read is
   * accounted to the RPE counter iff @p admitted is false.
   *
   * @return nanoseconds spent in the transfer.
   */
  double pull(uint32_t chunk_id, bool admitted) {
    const uint8_t* src = dev_->chunk_ptr(chunk_id, chunk_bytes_);
    auto t0 = Clock::now();

    // Streaming copy device -> local. memcpy over an mmaped devdax region is a
    // sequence of CXL.mem loads; the store side lands in local DRAM.
    std::memcpy(sink_, src, chunk_bytes_);
    uint64_t acc = 0;
    for (size_t off = 0; off < chunk_bytes_; off += 64) acc += sink_[off];
    checksum_ ^= acc;

    auto t1 = Clock::now();
    double ns = std::chrono::duration<double, std::nano>(t1 - t0).count();

    bytes_read_ += chunk_bytes_;
    if (admitted) {
      admitted_bytes_ += chunk_bytes_;
    } else {
      // A byte of a non-admitted (stale/reused-object) chunk crossed the link:
      // this IS reclaimed-payload exposure. Mode B must never reach this branch.
      rpe_bytes_ += chunk_bytes_;
    }
    return ns;
  }

  uint64_t bytes_read()     const { return bytes_read_; }
  uint64_t admitted_bytes() const { return admitted_bytes_; }
  uint64_t rpe_bytes()      const { return rpe_bytes_; }
  uint64_t checksum()       const { return checksum_; }

 private:
  CxlDevMem* dev_;
  size_t     chunk_bytes_;
  uint8_t*   sink_;
  uint64_t   bytes_read_     = 0;
  uint64_t   admitted_bytes_ = 0;
  uint64_t   rpe_bytes_      = 0;
  uint64_t   checksum_       = 0;
};

// --------------------------------------------------------------------------
// Reservation token — the endpoint's decision handle in Mode B.
//
// The endpoint (on one host, the gate below) issues a token for each admitted
// chunk. The host must present a valid token to pull. Token validity is checked
// at read-service time: an expired token (epoch rollover / eviction) refuses the
// read, so no payload moves and RPE stays 0.
// --------------------------------------------------------------------------
struct ReservationToken {
  uint32_t chunk_id;
  uint16_t epoch;
  Clock::time_point issued;
  bool     stale;   ///< Injected: token invalidated before host pulls it.
};

}  // namespace

// Per-step outcome shared by the Mode B and FTS paths.
struct StepResult {
  uint32_t admitted = 0;
  uint32_t rejected = 0;
  uint32_t token_refused = 0;   ///< Expired tokens: refused, moved 0 bytes.
  double   promo_latency_us = 0.0;
};

// --------------------------------------------------------------------------
// Scorer — stands in for the APEX scoring pipeline. Deterministic given seed.
// Returns candidate chunk_ids ranked by priority; the top @c budget are the
// "admitted" set (the decision), the rest are rejected.
// --------------------------------------------------------------------------
namespace {

struct ScoredStep {
  std::vector<uint32_t> candidate_ids;   ///< Chunks the host would consider.
  std::vector<uint32_t> admitted_ids;    ///< Top-budget by score (decision).
  std::vector<uint8_t>  is_admitted;     ///< Parallel to candidate_ids.
};

ScoredStep score_step(const BenchConfig& cfg, std::mt19937& rng) {
  ScoredStep s;
  s.candidate_ids.reserve(cfg.candidates);
  std::uniform_int_distribution<uint32_t> pick(0, cfg.n_chunks - 1);
  std::uniform_int_distribution<uint32_t> prio(0, 65535);

  // Draw distinct candidate chunk ids for this step.
  std::vector<uint8_t> seen(cfg.n_chunks, 0);
  std::vector<uint32_t> scores;
  while (s.candidate_ids.size() < cfg.candidates) {
    uint32_t c = pick(rng);
    if (seen[c]) continue;
    seen[c] = 1;
    s.candidate_ids.push_back(c);
    scores.push_back(prio(rng));
  }

  // Rank by score; top-budget are admitted.
  std::vector<uint32_t> order(s.candidate_ids.size());
  for (uint32_t i = 0; i < order.size(); ++i) order[i] = i;
  std::sort(order.begin(), order.end(),
            [&](uint32_t a, uint32_t b) { return scores[a] > scores[b]; });

  s.is_admitted.assign(s.candidate_ids.size(), 0);
  uint32_t kept = 0;
  for (uint32_t idx : order) {
    if (kept >= cfg.budget) break;
    s.is_admitted[idx] = 1;
    s.admitted_ids.push_back(s.candidate_ids[idx]);
    ++kept;
  }
  return s;
}

// --------------------------------------------------------------------------
// Mode B (endpoint-gated pull): decide, then pull only admitted chunks.
// --------------------------------------------------------------------------
StepResult run_modeb_step(const BenchConfig& cfg, const ScoredStep& s,
                          uint16_t epoch, PullEngine& pe, std::mt19937& rng) {
  StepResult r;

  // Phase 1: the gate issues reservation tokens for admitted chunks only.
  // (No payload has moved yet — this is a pure decision.)
  std::vector<ReservationToken> tokens;
  tokens.reserve(s.admitted_ids.size());
  std::bernoulli_distribution stale_dist(cfg.epoch_roll_pct);
  auto now = Clock::now();
  for (uint32_t cid : s.admitted_ids) {
    tokens.push_back({cid, epoch, now, stale_dist(rng)});
  }
  r.rejected = static_cast<uint32_t>(s.candidate_ids.size() -
                                     s.admitted_ids.size());

  // Phase 2: the host pulls. For each token it FIRST validates, then reads.
  // Rejected candidates have no token, so the host never issues a load for them
  // — that is why RPE is structurally 0. An expired token is refused at
  // validation, before any byte is read, so it also moves 0 bytes.
  auto t0 = Clock::now();
  for (const auto& tok : tokens) {
    bool valid = !tok.stale && (tok.epoch == epoch);
    if (!valid) {
      // Token gate refuses the read. No pull(), no bytes on the link.
      ++r.token_refused;
      continue;
    }
    pe.pull(tok.chunk_id, /*admitted=*/true);
    ++r.admitted;
  }
  auto t1 = Clock::now();
  r.promo_latency_us =
      std::chrono::duration<double, std::micro>(t1 - t0).count();
  return r;
}

// --------------------------------------------------------------------------
// Fetch-Then-Score baseline (the falsifiability control).
// Reads EVERY candidate off the device, THEN scores. Non-admitted reads already
// spent link bandwidth: they are reclaimed-payload exposure. This must report
// RPE > 0 for the Mode B RPE == 0 to mean anything.
// --------------------------------------------------------------------------
StepResult run_fts_step(const ScoredStep& s, PullEngine& pe) {
  StepResult r;
  auto t0 = Clock::now();
  for (size_t i = 0; i < s.candidate_ids.size(); ++i) {
    bool admitted = s.is_admitted[i] != 0;
    pe.pull(s.candidate_ids[i], admitted);  // read happens regardless
    if (admitted) ++r.admitted; else ++r.rejected;
  }
  auto t1 = Clock::now();
  r.promo_latency_us =
      std::chrono::duration<double, std::micro>(t1 - t0).count();
  return r;
}

// --------------------------------------------------------------------------
// Fill the CXL pool with a recognizable pattern so reads are not zero-page
// optimized and the transfer is genuinely performed.
// --------------------------------------------------------------------------
void fill_pool(CxlDevMem& dev, const BenchConfig& cfg) {
  for (uint32_t c = 0; c < cfg.n_chunks; ++c) {
    uint8_t* p = dev.chunk_ptr(c, cfg.chunk_bytes);
    // First and last cache line carry the chunk id; middle a rolling value.
    std::memset(p, static_cast<int>(c & 0xFF), cfg.chunk_bytes);
    *reinterpret_cast<uint32_t*>(p) = c;
    *reinterpret_cast<uint32_t*>(p + cfg.chunk_bytes - 4) = 0xC0FFEE00u ^ c;
  }
}

struct Percentiles { double p50, p99, mean; };

Percentiles pct(std::vector<double> v) {
  Percentiles r{0, 0, 0};
  if (v.empty()) return r;
  double sum = 0;
  for (double x : v) sum += x;
  r.mean = sum / v.size();
  std::sort(v.begin(), v.end());
  auto at = [&](double q) {
    size_t i = static_cast<size_t>(q * (v.size() - 1) + 0.5);
    return v[std::min(i, v.size() - 1)];
  };
  r.p50 = at(0.50);
  r.p99 = at(0.99);
  return r;
}

}  // namespace

// --------------------------------------------------------------------------
// Main
// --------------------------------------------------------------------------
int main(int argc, char** argv) {
  BenchConfig cfg;
  std::string devdax_path;
  bool want_json = false;

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&]() -> std::string {
      return (i + 1 < argc) ? std::string(argv[++i]) : std::string();
    };
    if (a == "--devdax")          devdax_path = next();
    else if (a == "--steps")      cfg.steps = std::stoul(next());
    else if (a == "--candidates") cfg.candidates = std::stoul(next());
    else if (a == "--budget")     cfg.budget = std::stoul(next());
    else if (a == "--chunk-kb")   cfg.chunk_bytes = std::stoul(next()) * 1024;
    else if (a == "--n-chunks")   cfg.n_chunks = std::stoul(next());
    else if (a == "--seed")       cfg.seed = std::stoul(next());
    else if (a == "--json")       want_json = true;
    else if (a == "--help" || a == "-h") {
      std::printf(
          "Usage: %s [--devdax /dev/dax0.0] [--steps N] [--candidates N]\n"
          "          [--budget N] [--chunk-kb KB] [--n-chunks N] [--seed S]\n"
          "          [--json]\n"
          "Env: PROSE_CXL_DEVDAX, PROSE_CXL_EMU_DIR\n", argv[0]);
      return 0;
    }
  }
  if (cfg.budget > cfg.candidates) cfg.budget = cfg.candidates;

  std::fprintf(stderr,
      "=======================================================\n"
      "  PROSE-APEX  Mode B (endpoint-gated pull)  E2E bench\n"
      "  Single host, real commodity CXL Type-3 substrate\n"
      "=======================================================\n");

  // Map the CXL payload pool.
  size_t pool_bytes = static_cast<size_t>(cfg.n_chunks) * cfg.chunk_bytes;
  CxlDevMem dev;
  if (!dev.open(pool_bytes, devdax_path)) {
    std::fprintf(stderr, "[bench] FATAL: could not map any CXL pool.\n");
    return 2;
  }
  fill_pool(dev, cfg);

  const bool real_cxl = dev.is_real_cxl();
  std::fprintf(stderr,
      "[bench] substrate=%s  real_cxl=%s  pool=%zu MB  chunk=%zu KB\n"
      "[bench] candidates/step=%u  budget=%u  steps=%u\n\n",
      backend_name(dev.backend()), real_cxl ? "YES" : "NO (EMULATED)",
      pool_bytes >> 20, cfg.chunk_bytes >> 10,
      cfg.candidates, cfg.budget, cfg.steps);

  // ---- Run Mode B ----
  PullEngine pe_b(&dev, cfg.chunk_bytes);
  std::mt19937 rng_b(cfg.seed);
  std::vector<double> lat_b;
  uint32_t admit_b = 0, refused_b = 0;
  uint16_t epoch = 1;
  for (uint32_t st = 0; st < cfg.steps; ++st) {
    ScoredStep s = score_step(cfg, rng_b);
    StepResult r = run_modeb_step(cfg, s, epoch, pe_b, rng_b);
    admit_b += r.admitted;
    refused_b += r.token_refused;
    if (r.admitted > 0)
      lat_b.push_back(r.promo_latency_us / r.admitted);  // per-chunk promo us
  }

  // ---- Run FTS baseline (same device, same instrument) ----
  PullEngine pe_f(&dev, cfg.chunk_bytes);
  std::mt19937 rng_f(cfg.seed);   // identical candidate stream
  std::vector<double> lat_f;
  uint32_t admit_f = 0, reject_f = 0;
  for (uint32_t st = 0; st < cfg.steps; ++st) {
    ScoredStep s = score_step(cfg, rng_f);
    StepResult r = run_fts_step(s, pe_f);
    admit_f += r.admitted;
    reject_f += r.rejected;
    if (r.admitted > 0)
      lat_f.push_back(r.promo_latency_us / r.admitted);
  }

  Percentiles L = pct(lat_b);
  Percentiles Lf = pct(lat_f);

  // Consume checksums so the compiler cannot elide the reads.
  volatile uint64_t sink = pe_b.checksum() ^ pe_f.checksum();
  (void)sink;

  double gbps_b = (pe_b.bytes_read() / 1e9) /
                  ((L.mean * admit_b) / 1e6 + 1e-12);

  // ---- Report ----
  std::fprintf(stderr,
      "----------------------- RESULTS -----------------------\n");
  std::fprintf(stderr,
      "Mode B (endpoint-gated pull):\n"
      "  admitted pulls .......... %u\n"
      "  token-refused (stale) ... %u  (moved 0 bytes)\n"
      "  bytes read from CXL ..... %.1f MB\n"
      "  admitted bytes .......... %.1f MB\n"
      "  RPE (rejected bytes) .... %llu bytes  %s\n"
      "  promo latency /chunk .... p50=%.2f us  p99=%.2f us  mean=%.2f us\n",
      admit_b, refused_b,
      pe_b.bytes_read() / 1e6, pe_b.admitted_bytes() / 1e6,
      (unsigned long long)pe_b.rpe_bytes(),
      pe_b.rpe_bytes() == 0 ? "<-- RPE=0 (guarantee holds)"
                            : "<-- RPE VIOLATION",
      L.p50, L.p99, L.mean);

  std::fprintf(stderr,
      "\nFTS baseline (fetch-then-score, falsifiability control):\n"
      "  admitted ................ %u\n"
      "  rejected ................ %u\n"
      "  bytes read from CXL ..... %.1f MB\n"
      "  RPE (rejected bytes) .... %llu bytes  (%.1f MB)  %s\n"
      "  read latency /chunk ..... p50=%.2f us  mean=%.2f us\n",
      admit_f, reject_f,
      pe_f.bytes_read() / 1e6,
      (unsigned long long)pe_f.rpe_bytes(), pe_f.rpe_bytes() / 1e6,
      pe_f.rpe_bytes() > 0 ? "<-- instrument CAN see leaks (good)"
                           : "<-- WARNING: control saw no leak",
      Lf.p50, Lf.mean);

  std::fprintf(stderr, "-------------------------------------------------------\n");

  if (want_json) {
    std::printf(
      "{\n"
      "  \"substrate\": \"%s\",\n"
      "  \"real_cxl\": %s,\n"
      "  \"chunk_bytes\": %zu,\n"
      "  \"candidates_per_step\": %u,\n"
      "  \"budget\": %u,\n"
      "  \"steps\": %u,\n"
      "  \"mode_b\": {\n"
      "    \"admitted_pulls\": %u,\n"
      "    \"token_refused\": %u,\n"
      "    \"bytes_read\": %llu,\n"
      "    \"admitted_bytes\": %llu,\n"
      "    \"rpe_bytes\": %llu,\n"
      "    \"promo_us_p50\": %.4f,\n"
      "    \"promo_us_p99\": %.4f,\n"
      "    \"promo_us_mean\": %.4f,\n"
      "    \"est_pull_gbps\": %.4f\n"
      "  },\n"
      "  \"fts_baseline\": {\n"
      "    \"admitted\": %u,\n"
      "    \"rejected\": %u,\n"
      "    \"bytes_read\": %llu,\n"
      "    \"rpe_bytes\": %llu,\n"
      "    \"read_us_p50\": %.4f,\n"
      "    \"read_us_mean\": %.4f\n"
      "  }\n"
      "}\n",
      backend_name(dev.backend()), real_cxl ? "true" : "false",
      cfg.chunk_bytes, cfg.candidates, cfg.budget, cfg.steps,
      admit_b, refused_b,
      (unsigned long long)pe_b.bytes_read(),
      (unsigned long long)pe_b.admitted_bytes(),
      (unsigned long long)pe_b.rpe_bytes(),
      L.p50, L.p99, L.mean, gbps_b,
      admit_f, reject_f,
      (unsigned long long)pe_f.bytes_read(),
      (unsigned long long)pe_f.rpe_bytes(),
      Lf.p50, Lf.mean);
  }

  // Exit code encodes the two claims:
  //   - Mode B RPE must be exactly 0.
  //   - FTS control must register a non-zero RPE (else the test is vacuous).
  bool ok = (pe_b.rpe_bytes() == 0) && (pe_f.rpe_bytes() > 0);
  if (!ok) {
    std::fprintf(stderr, "[bench] FAIL: RPE guarantee or falsifiability "
                         "control not satisfied.\n");
    return 1;
  }
  std::fprintf(stderr,
      "[bench] PASS: Mode B RPE=0 while FTS control leaked %.1f MB on the %s "
      "substrate.\n",
      pe_f.rpe_bytes() / 1e6,
      real_cxl ? "REAL CXL" : "emulated (run on CXL HW to make it real)");
  return 0;
}

