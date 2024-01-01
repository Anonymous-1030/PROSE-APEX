// rpe-lab Tier-B probe: two-phase Get with constructed delay between
// Query (GetReplicaList) and Get (transfer), all inside this process.
// Passive measurement only -- no Mooncake code changes.
//
// Flow per iteration: Query(key) -> sleep delay_ms -> Get(key, query_result)
// -> parse head/tail identity markers of the (possibly discarded) buffer
// -> append one JSONL record.
//
// rc semantics: 0 = success; -707 = LEASE_EXPIRED (guard fire, buffer held
// the to-be-discarded bytes); -704 = OBJECT_NOT_FOUND.
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <regex>
#include <string>
#include <thread>
#include <vector>

#include "client_service.h"
#include "types.h"

#include "rpe_lab_probe.h"  // ParseMarkerAt, Fnv1a64

using mooncake::Client;
using mooncake::Slice;

static const char* arg_val(int argc, char** argv, const char* name,
                           const char* dflt) {
    std::string want = std::string("--") + name + "=";
    for (int i = 1; i < argc; ++i)
        if (strncmp(argv[i], want.c_str(), want.size()) == 0)
            return argv[i] + want.size();
    return dflt;
}

int main(int argc, char** argv) {
    std::string master = arg_val(argc, argv, "master", "127.0.0.1:50051");
    std::string ledger = arg_val(argc, argv, "ledger", "");
    std::string out_path = arg_val(argc, argv, "out", "/tmp/rpe_probe.jsonl");
    long delay_ms = atol(arg_val(argc, argv, "delay_ms", "0"));
    long duration_s = atol(arg_val(argc, argv, "duration_s", "60"));
    double rate = atof(arg_val(argc, argv, "rate", "1"));
    long mount_mb = atol(arg_val(argc, argv, "mount_mb", "256"));
    size_t obj_size = (size_t)atol(arg_val(argc, argv, "object_size", "3670016"));
    if (argc == 1) {
        fprintf(stderr,
                "usage: rpe_probe --master=H:P --ledger=FILE --delay_ms=X "
                "--duration_s=N --rate=R --out=FILE [--mount_mb=256]\n");
        return 2;
    }

    // hot keys from the ledger (naive regex extraction)
    std::vector<std::string> keys;
    {
        std::ifstream f(ledger);
        std::stringstream ss;
        ss << f.rdbuf();
        std::string text = ss.str();
        std::regex re("hot/[0-9]{4}");
        for (std::sregex_iterator it(text.begin(), text.end(), re), end;
             it != end; ++it)
            keys.push_back(it->str());
    }
    if (keys.empty()) {
        fprintf(stderr, "no hot/* keys in %s\n", ledger.c_str());
        return 1;
    }
    fprintf(stderr, "rpe_probe: %zu keys, delay=%ldms, rate=%.1f/s\n",
            keys.size(), delay_ms, rate);

    auto client_opt = Client::Create("localhost", "P2PHANDSHAKE", "tcp",
                                     std::nullopt, master);
    if (!client_opt) {
        fprintf(stderr, "Client::Create failed\n");
        return 1;
    }
    auto client = *client_opt;
    void* seg_buf = nullptr;
    if (mount_mb > 0) {
        if (posix_memalign(&seg_buf, 4096, (size_t)mount_mb << 20) == 0) {
            auto mrc = client->MountSegment(seg_buf, (size_t)mount_mb << 20);
            if (!mrc)
                fprintf(stderr,
                        "MountSegment failed rc=%d (continuing read-only)\n",
                        static_cast<int>(mrc.error()));
        }
    }

    void* buf = nullptr;
    if (posix_memalign(&buf, 4096, obj_size)) {
        fprintf(stderr, "alloc failed\n");
        return 1;
    }
    FILE* out = fopen(out_path.c_str(), "a");
    if (!out) {
        fprintf(stderr, "cannot open %s\n", out_path.c_str());
        return 1;
    }

    auto t0 = std::chrono::steady_clock::now();
    size_t i = 0, iter = 0;
    while (std::chrono::steady_clock::now() - t0 <
           std::chrono::seconds(duration_s)) {
        const std::string& key = keys[i++ % keys.size()];
        iter++;
        uint64_t t_query = 0, t_done = 0;
        int rc = 0;
        std::vector<Slice> slices{{buf, obj_size}};
        {
            auto q = client->Query(key);
            t_query = (uint64_t)std::chrono::duration_cast<std::chrono::nanoseconds>(
                          std::chrono::system_clock::now().time_since_epoch())
                          .count();
            if (!q) {
                rc = static_cast<int>(q.error());
                t_done = t_query;
            } else {
                auto qr = *q;
                if (delay_ms > 0)
                    std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
                auto g = client->Get(key, qr, slices);
                t_done = (uint64_t)std::chrono::duration_cast<std::chrono::nanoseconds>(
                             std::chrono::system_clock::now().time_since_epoch())
                             .count();
                rc = g ? 0 : static_cast<int>(g.error());
            }
        }
        // Only parse markers when the transfer actually ran to completion:
        // 0 (success) and -707 (guard discard) both leave a fully-read buffer.
        // Any other rc leaves stale bytes from earlier iterations -> null out.
        const bool buffer_valid = (rc == 0 || rc == -707);
        rpe_lab::ParsedMarker h = buffer_valid ? rpe_lab::ParseMarkerAt(buf, obj_size)
                                               : rpe_lab::ParsedMarker{};
        rpe_lab::ParsedMarker t = buffer_valid
                                      ? rpe_lab::ParseMarkerAt((const char*)buf + obj_size - 64, 64)
                                      : rpe_lab::ParsedMarker{};
        fprintf(out,
                "{\"ts_ns\":%llu,\"key\":\"%s\",\"rc\":%d,"
                "\"t_query_ns\":%llu,\"t_get_done_ns\":%llu,\"delay_ms\":%ld,"
                "\"found_magic\":%s,\"found_tenant\":%llu,"
                "\"found_key_hash\":%llu,\"found_gen\":%llu,"
                "\"tail_magic\":%s,\"tail_tenant\":%llu,"
                "\"tail_key_hash\":%llu,\"tail_gen\":%llu,"
                "\"payload_len\":%zu}\n",
                (unsigned long long)t_done, key.c_str(), rc,
                (unsigned long long)t_query, (unsigned long long)t_done,
                delay_ms, h.magic ? "true" : "false",
                (unsigned long long)h.tenant, (unsigned long long)h.key_hash,
                (unsigned long long)h.gen, t.magic ? "true" : "false",
                (unsigned long long)t.tenant, (unsigned long long)t.key_hash,
                (unsigned long long)t.gen, obj_size);
        fflush(out);
        // pace to ~rate iterations/s
        double want = iter / rate;
        double have = std::chrono::duration<double>(
                          std::chrono::steady_clock::now() - t0)
                          .count();
        if (want > have)
            std::this_thread::sleep_for(
                std::chrono::milliseconds((long)((want - have) * 1000)));
    }
    fclose(out);
    free(buf);
    fprintf(stderr, "rpe_probe done: %zu iterations\n", iter);
    return 0;
}
