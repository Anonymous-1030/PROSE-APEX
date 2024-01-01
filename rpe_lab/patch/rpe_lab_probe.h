// rpe-lab: passive instrumentation, no behavior change.
//
// Header-only probe called immediately BEFORE the lease-expiry discard
// points in client_service.cpp (Client::Get x2, Client::BatchGetWhenPreferSameNode).
// At those points the caller's slices are already filled with the bytes that
// are about to be discarded; we parse the rpe-lab identity markers (head:
// first 64 bytes, tail: last 64 bytes -- see rpe_lab/rpe_header.py) and
// append one JSONL event to $RPE_LAB_EVENTS.
//
// v2: also parses the tail marker. A mid-transfer slot overwrite can leave
// the head intact (already pulled) while the tail carries the new object's
// marker -> torn read, which v1 would have missed.
//
// The probe never throws, never touches the data path unless the env var
// RPE_LAB_EVENTS is set, and never alters return values.
//
// Marker layout (little-endian): 0..7 magic "RPELAB_1", 8..15 tenant_id,
// 16..23 key_hash (FNV-1a-64 of key), 24..31 generation,
// 32..39 put_timestamp_ns, 40..63 reserved.
#pragma once

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>

namespace rpe_lab {

inline const char* EventsPath() {
    const char* p = std::getenv("RPE_LAB_EVENTS");  // no caching: honors set/unset
    return (p && *p) ? p : nullptr;
}

inline const char* RunId() {
    const char* p = std::getenv("RPE_LAB_RUN_ID");
    return p ? p : "";
}

inline bool Enabled() { return EventsPath() != nullptr; }

inline uint64_t Fnv1a64(const char* data, size_t n) {
    uint64_t h = 14695981039346656037ull;
    for (size_t i = 0; i < n; ++i) {
        h ^= static_cast<unsigned char>(data[i]);
        h *= 1099511628211ull;
    }
    return h;
}

struct ParsedMarker {
    bool magic = false;
    uint64_t tenant = 0;
    uint64_t key_hash = 0;
    uint64_t gen = 0;
    uint64_t ts_ns = 0;
};

inline ParsedMarker ParseMarkerAt(const void* buf, size_t len) {
    ParsedMarker out;
    static const char kMagic[8] = {'R', 'P', 'E', 'L', 'A', 'B', '_', '1'};
    if (!buf || len < 64) return out;
    const unsigned char* p = static_cast<const unsigned char*>(buf);
    if (std::memcmp(p, kMagic, 8) != 0) return out;
    auto rd64 = [&](int off) -> uint64_t {
        uint64_t v;
        std::memcpy(&v, p + off, 8);  // little-endian host (x86_64)
        return v;
    };
    out.magic = true;
    out.tenant = rd64(8);
    out.key_hash = rd64(16);
    out.gen = rd64(24);
    out.ts_ns = rd64(32);
    return out;
}

inline void WriteEvent(const std::string& key,
                       const void* head, size_t head_len,
                       const void* tail, size_t tail_len,
                       size_t payload_len, long long expired_by_us,
                       long long transfer_us) {
    static std::mutex mu;
    ParsedMarker h = ParseMarkerAt(head, head_len);
    ParsedMarker t = ParseMarkerAt(tail, tail_len);
    uint64_t expected_key_hash = Fnv1a64(key.data(), key.size());
    uint64_t ts_ns = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::system_clock::now().time_since_epoch())
            .count());
    std::lock_guard<std::mutex> lk(mu);
    FILE* f = std::fopen(EventsPath(), "a");
    if (!f) return;
    std::fprintf(
        f,
        "{\"ts_ns\":%llu,\"type\":\"discard\",\"run_id\":\"%s\","
        "\"key\":\"%s\",\"expected_key_hash\":%llu,"
        "\"found_magic\":%s,\"found_tenant\":%llu,\"found_key_hash\":%llu,"
        "\"found_gen\":%llu,"
        "\"tail_magic\":%s,\"tail_tenant\":%llu,\"tail_key_hash\":%llu,"
        "\"tail_gen\":%llu,"
        "\"payload_len\":%zu,\"expired_by_us\":%lld,\"transfer_us\":%lld}\n",
        (unsigned long long)ts_ns, RunId(), key.c_str(),
        (unsigned long long)expected_key_hash,
        h.magic ? "true" : "false", (unsigned long long)h.tenant,
        (unsigned long long)h.key_hash, (unsigned long long)h.gen,
        t.magic ? "true" : "false", (unsigned long long)t.tenant,
        (unsigned long long)t.key_hash, (unsigned long long)t.gen,
        payload_len, expired_by_us, transfer_us);
    std::fclose(f);
}

// Duck-typed over any range of {void* ptr; size_t size;} slices.
template <typename SliceRange>
inline void LogGetDiscard(
    const std::string& key, const SliceRange& slices,
    std::chrono::steady_clock::time_point lease_timeout, long long transfer_us) {
    if (!Enabled()) return;
    const void* head = nullptr;
    const unsigned char* tail = nullptr;
    size_t head_len = 0, tail_len = 0, payload_len = 0;
    for (const auto& s : slices) {
        if (!head && s.size >= 64) {
            head = s.ptr;
            head_len = s.size;
        }
        if (s.ptr && s.size >= 64) {
            tail = static_cast<const unsigned char*>(s.ptr) + s.size - 64;
            tail_len = 64;
        }
        payload_len += s.size;
    }
    long long expired_by_us =
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - lease_timeout)
            .count();
    WriteEvent(key, head, head_len, tail, tail_len, payload_len,
               expired_by_us, transfer_us);
}

}  // namespace rpe_lab
