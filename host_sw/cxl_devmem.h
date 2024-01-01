/**
 * @file cxl_devmem.h
 * @brief Portable CXL Type-3 device-memory backend for the Mode B harness.
 *
 * A commercial CXL Type-3 device exposes Host-managed Device Memory (HDM):
 * the OS surfaces it either as a devdax character device (/dev/dax0.0) or as a
 * CPU-less NUMA memory node. Either way the payload region is *plain memory* —
 * the host reaches it with ordinary CXL.mem loads/stores. That is the whole
 * reason Mode B (endpoint-gated pull) needs no custom endpoint DMA engine: the
 * host, not the device, initiates the payload transfer.
 *
 * This header opens the real device when one is available and falls back to an
 * emulated substrate otherwise. It ALWAYS reports which backend is in use, so a
 * run on the fallback can never be mistaken for a run on silicon. The backend
 * only affects the *latency* of a pull; the RPE=0 ordering property the harness
 * verifies is substrate-independent (see bench_modeb_e2e.cpp).
 *
 * @author Anonymous
 */

#ifndef PROSE_CXL_DEVMEM_H_
#define PROSE_CXL_DEVMEM_H_

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#if defined(__linux__)
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#endif

#if defined(_WIN32)
#include <malloc.h>
#endif

namespace prose {

/// Which physical substrate is backing the CXL payload pool.
enum class CxlBackend {
  kRealDevDax,     ///< Real Type-3 device via /dev/dax* (true CXL.mem path).
  kRealNumaNode,   ///< Real CXL memory exposed as a CPU-less NUMA node.
  kEmulatedFile,   ///< File-backed mmap (tmpfs/hugetlbfs) — NOT real CXL.
  kEmulatedAnon,   ///< Anonymous DRAM mapping — NOT real CXL.
};

inline const char* backend_name(CxlBackend b) {
  switch (b) {
    case CxlBackend::kRealDevDax:   return "real-devdax";
    case CxlBackend::kRealNumaNode: return "real-numa-node";
    case CxlBackend::kEmulatedFile: return "emulated-file";
    case CxlBackend::kEmulatedAnon: return "emulated-anon";
  }
  return "unknown";
}

/// True only for substrates that are actually CXL-attached device memory.
inline bool backend_is_real_cxl(CxlBackend b) {
  return b == CxlBackend::kRealDevDax || b == CxlBackend::kRealNumaNode;
}

/**
 * @class CxlDevMem
 * @brief RAII wrapper over a mapped CXL payload region.
 *
 * The region is used as the "far" chunk-source pool: chunks live here and the
 * host pulls admitted chunks out of it with CXL.mem loads. On the emulated
 * substrate the same code path runs against local DRAM, so the protocol and RPE
 * accounting are identical — only the measured pull latency differs.
 */
class CxlDevMem {
 public:
  CxlDevMem() = default;
  ~CxlDevMem() { close(); }

  CxlDevMem(const CxlDevMem&) = delete;
  CxlDevMem& operator=(const CxlDevMem&) = delete;

  /**
   * @brief Map a payload region of at least @p want_bytes.
   *
   * Backend selection (first that succeeds wins):
   *   1. @p devdax_path (or $PROSE_CXL_DEVDAX) — real Type-3 devdax device.
   *   2. Emulated file mapping under $PROSE_CXL_EMU_DIR (default /dev/shm, then
   *      the system tmp dir) — clearly labelled NOT real CXL.
   *   3. Anonymous mapping — last-resort emulation (also on Windows).
   *
   * @return true on success; on failure the object stays unmapped.
   */
  bool open(size_t want_bytes, const std::string& devdax_path = "") {
    close();
    size_ = want_bytes;

    std::string path = devdax_path;
    if (path.empty()) {
      const char* env = std::getenv("PROSE_CXL_DEVDAX");
      if (env && env[0]) path = env;
    }

#if defined(__linux__)
    if (!path.empty() && open_devdax(path, want_bytes)) return true;
    if (open_emulated_file(want_bytes)) return true;
    if (open_anon(want_bytes)) return true;
#elif defined(_WIN32)
    if (!path.empty()) {
      std::fprintf(stderr,
          "[cxl_devmem] devdax path '%s' ignored: real CXL requires Linux.\n",
          path.c_str());
    }
    if (open_anon(want_bytes)) return true;
#else
    if (open_anon(want_bytes)) return true;
#endif
    size_ = 0;
    return false;
  }

  void close() {
#if defined(__linux__)
    if (base_ && backend_ != CxlBackend::kEmulatedAnon) {
      munmap(base_, size_);
    } else if (base_ && backend_ == CxlBackend::kEmulatedAnon) {
      munmap(base_, size_);
    }
    if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
#elif defined(_WIN32)
    if (base_) { _aligned_free(base_); }
#else
    if (base_) { std::free(base_); }
#endif
    base_ = nullptr;
    size_ = 0;
  }

  bool valid() const { return base_ != nullptr; }
  void* base() const { return base_; }
  size_t size() const { return size_; }
  CxlBackend backend() const { return backend_; }
  bool is_real_cxl() const { return backend_is_real_cxl(backend_); }

  /// Byte offset of chunk @p i for a fixed @p chunk_bytes stride.
  uint8_t* chunk_ptr(size_t i, size_t chunk_bytes) const {
    return static_cast<uint8_t*>(base_) + i * chunk_bytes;
  }

 private:
#if defined(__linux__)
  bool open_devdax(const std::string& path, size_t want_bytes) {
    int fd = ::open(path.c_str(), O_RDWR);
    if (fd < 0) {
      std::fprintf(stderr, "[cxl_devmem] open('%s') failed: %s\n",
                   path.c_str(), std::strerror(errno));
      return false;
    }
    // devdax mappings must be page-aligned; the device is large enough for the
    // whole KV pool in a real deployment.
    void* p = mmap(nullptr, want_bytes, PROT_READ | PROT_WRITE,
                   MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) {
      std::fprintf(stderr, "[cxl_devmem] mmap('%s', %zu) failed: %s\n",
                   path.c_str(), want_bytes, std::strerror(errno));
      ::close(fd);
      return false;
    }
    fd_ = fd;
    base_ = p;
    backend_ = CxlBackend::kRealDevDax;
    std::fprintf(stderr,
        "[cxl_devmem] mapped REAL CXL Type-3 devdax '%s' (%zu bytes)\n",
        path.c_str(), want_bytes);
    return true;
  }

  bool open_emulated_file(size_t want_bytes) {
    std::string dir;
    if (const char* e = std::getenv("PROSE_CXL_EMU_DIR")) dir = e;
    if (dir.empty()) {
      struct stat st;
      if (stat("/dev/shm", &st) == 0 && S_ISDIR(st.st_mode)) dir = "/dev/shm";
    }
    if (dir.empty()) {
      const char* t = std::getenv("TMPDIR");
      dir = (t && t[0]) ? t : "/tmp";
    }
    std::string fpath = dir + "/prose_cxl_emu.bin";
    int fd = ::open(fpath.c_str(), O_RDWR | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return false;
    if (ftruncate(fd, static_cast<off_t>(want_bytes)) != 0) {
      ::close(fd);
      return false;
    }
    void* p = mmap(nullptr, want_bytes, PROT_READ | PROT_WRITE,
                   MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { ::close(fd); return false; }
    unlink(fpath.c_str());  // Anonymous once mapped; frees space on close.
    fd_ = fd;
    base_ = p;
    backend_ = CxlBackend::kEmulatedFile;
    std::fprintf(stderr,
        "[cxl_devmem] NO real CXL device: using EMULATED file mapping '%s'\n"
        "             (protocol + RPE accounting are real; latency is NOT CXL)\n",
        fpath.c_str());
    return true;
  }
#endif  // __linux__

  bool open_anon(size_t want_bytes) {
#if defined(__linux__)
    void* p = mmap(nullptr, want_bytes, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) return false;
    base_ = p;
#elif defined(_WIN32)
    void* p = _aligned_malloc(want_bytes, 4096);
    if (!p) return false;
    std::memset(p, 0, want_bytes);
    base_ = p;
#else
    void* p = std::malloc(want_bytes);
    if (!p) return false;
    std::memset(p, 0, want_bytes);
    base_ = p;
#endif
    backend_ = CxlBackend::kEmulatedAnon;
    std::fprintf(stderr,
        "[cxl_devmem] NO real CXL device: using EMULATED anonymous DRAM\n"
        "             (protocol + RPE accounting are real; latency is NOT CXL)\n");
    return true;
  }

  void* base_ = nullptr;
  size_t size_ = 0;
  int fd_ = -1;
  CxlBackend backend_ = CxlBackend::kEmulatedAnon;
};

}  // namespace prose

#endif  // PROSE_CXL_DEVMEM_H_
