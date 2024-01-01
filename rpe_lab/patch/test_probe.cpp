// rpe-lab unit test for the passive probe header v2 (head+tail markers).
// Standalone: g++ -std=c++17 test_probe.cpp -o test_probe && ./test_probe
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "rpe_lab_probe.h"

struct FakeSlice {
    void* ptr;
    size_t size;
};

static void put64(std::vector<unsigned char>& b, size_t off, uint64_t v) {
    std::memcpy(b.data() + off, &v, 8);  // little-endian
}

static void write_marker(std::vector<unsigned char>& b, size_t off,
                         uint64_t tenant, uint64_t key_hash, uint64_t gen) {
    const char magic[8] = {'R', 'P', 'E', 'L', 'A', 'B', '_', '1'};
    std::memcpy(b.data() + off, magic, 8);
    put64(b, off + 8, tenant);
    put64(b, off + 16, key_hash);
    put64(b, off + 24, gen);
    put64(b, off + 32, 1755000000000000000ull);
}

static std::vector<unsigned char> make_payload(uint64_t tenant, uint64_t key_hash,
                                               uint64_t gen, size_t size = 4096) {
    std::vector<unsigned char> b(size, 0xAB);
    write_marker(b, 0, tenant, key_hash, gen);
    write_marker(b, size - 64, tenant, key_hash, gen);
    return b;
}

static std::string slurp(const char* path) {
    std::ifstream f(path);
    std::stringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

static int failures = 0;
static void check(bool cond, const char* name) {
    std::cout << (cond ? "PASS  " : "FAIL  ") << name << "\n";
    if (!cond) ++failures;
}

int main() {
    const char* path = "/tmp/rpe_lab_test_events.jsonl";
    setenv("RPE_LAB_EVENTS", path, 1);
    setenv("RPE_LAB_RUN_ID", "unit_test", 1);

    const std::string key = "hot/0017";
    uint64_t kh = rpe_lab::Fnv1a64(key.data(), key.size());
    auto deadline = std::chrono::steady_clock::now() - std::chrono::milliseconds(1500);

    // 1. correct payload: head and tail match
    std::remove(path);
    {
        auto buf = make_payload(1, kh, 3);
        std::vector<FakeSlice> slices{{buf.data(), buf.size()}};
        rpe_lab::LogGetDiscard(key, slices, deadline, 123456);
        std::string log = slurp(path);
        check(log.find("\"found_magic\":true") != std::string::npos, "head: found_magic");
        check(log.find("\"found_gen\":3") != std::string::npos, "head: found_gen=3");
        check(log.find("\"tail_magic\":true") != std::string::npos, "tail: tail_magic");
        check(log.find("\"tail_gen\":3") != std::string::npos, "tail: tail_gen=3");
        check(log.find("\"expired_by_us\":1") != std::string::npos, "expired_by_us ~1.5s");
        check(log.find("\"transfer_us\":123456") != std::string::npos, "transfer_us");
        check(log.find("\"expected_key_hash\":" + std::to_string(kh)) != std::string::npos,
              "expected_key_hash = fnv1a64(key)");
    }

    // 2. foreign object (both markers foreign)
    std::remove(path);
    {
        auto buf = make_payload(2, 999999, 1);
        std::vector<FakeSlice> slices{{buf.data(), buf.size()}};
        rpe_lab::LogGetDiscard(key, slices, deadline, 10);
        std::string log = slurp(path);
        check(log.find("\"found_tenant\":2") != std::string::npos, "foreign: head tenant B");
        check(log.find("\"tail_key_hash\":999999") != std::string::npos, "foreign: tail key_hash");
    }

    // 3. torn read: head matches, tail foreign
    std::remove(path);
    {
        auto buf = make_payload(1, kh, 3);
        write_marker(buf, buf.size() - 64, 2, 999999, 1);  // tail overwritten
        std::vector<FakeSlice> slices{{buf.data(), buf.size()}};
        rpe_lab::LogGetDiscard(key, slices, deadline, 10);
        std::string log = slurp(path);
        check(log.find("\"found_gen\":3") != std::string::npos, "torn: head still gen=3");
        check(log.find("\"tail_gen\":1") != std::string::npos &&
                  log.find("\"tail_tenant\":2") != std::string::npos,
              "torn: tail shows tenant B gen=1");
    }

    // 4. no magic anywhere / zeroed
    std::remove(path);
    {
        std::vector<unsigned char> buf(4096, 0x00);
        std::vector<FakeSlice> slices{{buf.data(), buf.size()}};
        rpe_lab::LogGetDiscard(key, slices, deadline, 10);
        std::string log = slurp(path);
        check(log.find("\"found_magic\":false") != std::string::npos, "no_magic: head false");
        check(log.find("\"tail_magic\":false") != std::string::npos, "no_magic: tail false");
    }

    // 5. short buffer -> no crash, no markers
    std::remove(path);
    {
        std::vector<unsigned char> tiny(32, 0xFF);
        std::vector<FakeSlice> slices{{tiny.data(), tiny.size()}};
        rpe_lab::LogGetDiscard(key, slices, deadline, 10);
        std::vector<FakeSlice> empty;
        rpe_lab::LogGetDiscard(key, empty, deadline, 10);
        check(slurp(path).find("\"found_magic\":false") != std::string::npos,
              "short buffer: no crash");
    }

    // 6. disabled without env var
    {
        unsetenv("RPE_LAB_EVENTS");
        std::string before = slurp(path);
        auto buf = make_payload(1, kh, 3);
        std::vector<FakeSlice> slices{{buf.data(), buf.size()}};
        rpe_lab::LogGetDiscard(key, slices, deadline, 10);
        check(slurp(path) == before, "disabled: no writes without RPE_LAB_EVENTS");
    }

    // 7. FNV vectors
    check(rpe_lab::Fnv1a64("", 0) == 14695981039346656037ull, "fnv1a64 empty");
    check(rpe_lab::Fnv1a64("a", 1) == 0xAF63DC4C8601EC8Cull, "fnv1a64 'a'");

    std::cout << (failures ? "FAILURES\n" : "all probe v2 unit tests passed\n");
    return failures ? 1 : 0;
}
