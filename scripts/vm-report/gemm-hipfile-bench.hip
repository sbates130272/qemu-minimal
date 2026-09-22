#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <hip/hip_runtime.h>

#if __has_include(<hipfile/hipfile.h>)
#include <hipfile/hipfile.h>
#else
#include <hipfile.h>
#endif

#include <fcntl.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

constexpr int kM = 128;
constexpr int kN = 128;
constexpr int kK = 128;
constexpr size_t kBlock = 1u << 20;
constexpr size_t kBlocks = 32;
constexpr size_t kPayload = kBlock * kBlocks;
constexpr size_t kABytes = static_cast<size_t>(kM) * kK * sizeof(float);
constexpr size_t kBBytes = static_cast<size_t>(kK) * kN * sizeof(float);
constexpr size_t kBOffset = kABytes;
constexpr float kTolerance = 1e-3f;

static std::string g_cleanup_path;

static void cleanup_bench_file() {
    if (!g_cleanup_path.empty()) {
        ::unlink(g_cleanup_path.c_str());
    }
}

#define HIP_CHECK(expr)                                                       \
    do {                                                                      \
        hipError_t err_ = (expr);                                             \
        if (err_ != hipSuccess) {                                             \
            std::fprintf(stderr,                                              \
                         "gemm-hipfile-bench: %s failed: %s\n", #expr,        \
                         hipGetErrorString(err_));                             \
            return 1;                                                         \
        }                                                                     \
    } while (0)

#define HIPFILE_CHECK(expr)                                                   \
    do {                                                                      \
        hipFileError_t s_ = (expr);                                           \
        if (s_.err != hipFileSuccess) {                                       \
            std::fprintf(stderr,                                              \
                         "gemm-hipfile-bench: %s failed: err=%d drv=%d\n",    \
                         #expr, static_cast<int>(s_.err),                     \
                         static_cast<int>(s_.hip_drv_err));                   \
            return 2;                                                         \
        }                                                                     \
    } while (0)

__global__ void sgemm(const float* __restrict__ a, const float* __restrict__ b,
                      float* __restrict__ c, int m, int n, int k) {
    const int row = blockIdx.y * blockDim.y + threadIdx.y;
    const int col = blockIdx.x * blockDim.x + threadIdx.x;
    if (row >= m || col >= n) {
        return;
    }
    float acc = 0.0f;
    for (int i = 0; i < k; ++i) {
        acc += a[row * k + i] * b[i * n + col];
    }
    c[row * n + col] = acc;
}

static void fill_operands(std::vector<float>& a, std::vector<float>& b) {
    a.resize(static_cast<size_t>(kM) * kK);
    b.resize(static_cast<size_t>(kK) * kN);
    for (int row = 0; row < kM; ++row) {
        for (int i = 0; i < kK; ++i) {
            a[static_cast<size_t>(row) * kK + i] =
                static_cast<float>((row + i) % 7) * 0.25f;
        }
    }
    for (int i = 0; i < kK; ++i) {
        for (int col = 0; col < kN; ++col) {
            b[static_cast<size_t>(i) * kN + col] =
                static_cast<float>((i * 2 + col) % 5) * 0.5f;
        }
    }
}

static bool write_payload(const std::string& path, const std::vector<float>& a,
                          const std::vector<float>& b) {
    const int fd = ::open(path.c_str(), O_CREAT | O_WRONLY | O_TRUNC, 0644);
    if (fd < 0) {
        std::fprintf(stderr, "gemm-hipfile-bench: open %s: %s\n", path.c_str(),
                     std::strerror(errno));
        return false;
    }

    std::vector<unsigned char> block(kBlock);
    bool ok = true;
    for (size_t blk = 0; ok && blk < kBlocks; ++blk) {
        for (size_t i = 0; i < kBlock; ++i) {
            block[i] = static_cast<unsigned char>((blk * 31 + i * 7) & 0xff);
        }
        if (blk == 0) {
            std::memcpy(block.data(), a.data(), kABytes);
            std::memcpy(block.data() + kBOffset, b.data(), kBBytes);
        }
        size_t done = 0;
        while (done < kBlock) {
            const ssize_t n = ::write(fd, block.data() + done, kBlock - done);
            if (n <= 0) {
                std::fprintf(stderr, "gemm-hipfile-bench: write: %s\n",
                             std::strerror(errno));
                ok = false;
                break;
            }
            done += static_cast<size_t>(n);
        }
    }

    if (ok && ::fsync(fd) != 0) {
        std::fprintf(stderr, "gemm-hipfile-bench: fsync: %s\n",
                     std::strerror(errno));
        ok = false;
    }
    ::close(fd);
    return ok;
}

static bool env_is_true(const char* value) {
    if (value == nullptr) {
        return false;
    }
    return std::strcmp(value, "1") == 0 || std::strcmp(value, "true") == 0 ||
           std::strcmp(value, "TRUE") == 0 || std::strcmp(value, "yes") == 0 ||
           std::strcmp(value, "YES") == 0 || std::strcmp(value, "on") == 0 ||
           std::strcmp(value, "ON") == 0;
}

int main(int argc, char** argv) {
    std::string dir = "/mnt/nvme";
    int iters = 4;
    if (argc > 1) {
        dir = argv[1];
    }
    if (argc > 2) {
        iters = std::atoi(argv[2]);
        if (iters < 1) {
            std::fprintf(stderr, "gemm-hipfile-bench: iters must be >= 1\n");
            return 1;
        }
    }
    const std::string path = dir + "/gemm-hipfile-bench.bin";
    g_cleanup_path = path;
    std::atexit(cleanup_bench_file);

    const char* compat_env = ::getenv("HIPFILE_ALLOW_COMPAT_MODE");
    const bool compat_from_caller = compat_env != nullptr;
    const bool compat_enabled = env_is_true(compat_env);
    if (!compat_from_caller) {
        ::setenv("HIPFILE_ALLOW_COMPAT_MODE", "true", 0);
    } else if (!compat_enabled) {
        std::fprintf(stderr,
                     "gemm-hipfile-bench: HIPFILE_ALLOW_COMPAT_MODE is set but "
                     "disabled; unset it or set it to true for this benchmark.\n");
        return 2;
    }

    hipDeviceProp_t prop{};
    HIP_CHECK(hipGetDeviceProperties(&prop, 0));
    std::printf("gemm-hipfile-bench: device=%s arch=%s\n", prop.name,
                prop.gcnArchName);
    std::printf("gemm-hipfile-bench: path=%s block_bytes=%zu blocks=%zu iters=%d\n",
                path.c_str(), kBlock, kBlocks, iters);

    std::vector<float> ha, hb;
    fill_operands(ha, hb);
    if (!write_payload(path, ha, hb)) {
        return 2;
    }

    hipFuncAttributes attr{};
    HIP_CHECK(hipFuncGetAttributes(&attr, reinterpret_cast<const void*>(sgemm)));
    std::printf("gemm-hipfile-bench: private_segment_bytes=%zu\n",
                static_cast<size_t>(attr.localSizeBytes));
    if (attr.localSizeBytes != 0) {
        std::fprintf(stderr,
                     "gemm-hipfile-bench: kernel needs %zu bytes of scratch; rebuild with -O2.\n",
                     static_cast<size_t>(attr.localSizeBytes));
        return 4;
    }

    bool driver_open = false;
    hipFileError_t hipfile_status = hipFileDriverOpen();
    if (hipfile_status.err != hipFileSuccess) {
        std::fprintf(stderr,
                     "gemm-hipfile-bench: hipFileDriverOpen() failed: err=%d drv=%d\n",
                     static_cast<int>(hipfile_status.err),
                     static_cast<int>(hipfile_status.hip_drv_err));
        return 2;
    }
    driver_open = true;
    int fd = ::open(path.c_str(), O_RDONLY | O_DIRECT);
    if (fd < 0) {
        std::fprintf(stderr, "gemm-hipfile-bench: open %s O_DIRECT: %s\n",
                     path.c_str(), std::strerror(errno));
        hipFileDriverClose();
        return 2;
    }

    hipFileHandle_t fh{};
    hipFileDescr_t descr{};
    descr.handle.fd = fd;
    descr.type = hipFileHandleTypeOpaqueFD;
    bool handle_registered = false;
    int exit_code = 0;
    std::vector<float> hc(static_cast<size_t>(kM) * kN, 0.0f);
    auto hip_ok = [&](hipError_t err, const char* expr) {
        if (err != hipSuccess) {
            std::fprintf(stderr, "gemm-hipfile-bench: %s failed: %s\n", expr,
                         hipGetErrorString(err));
            exit_code = 1;
            return false;
        }
        return true;
    };
    auto hipfile_ok = [&](hipFileError_t err, const char* expr) {
        if (err.err != hipFileSuccess) {
            std::fprintf(stderr,
                         "gemm-hipfile-bench: %s failed: err=%d drv=%d\n", expr,
                         static_cast<int>(err.err), static_cast<int>(err.hip_drv_err));
            exit_code = 2;
            return false;
        }
        return true;
    };
    if (!hipfile_ok(hipFileHandleRegister(&fh, &descr), "hipFileHandleRegister(&fh, &descr)")) goto cleanup;
    handle_registered = true;

    void* dev = nullptr;
    if (!hip_ok(hipMalloc(&dev, kPayload), "hipMalloc(&dev, kPayload)")) goto cleanup;

    const hipFileError_t reg = hipFileBufRegister(dev, kPayload, 0);
    const bool buf_registered = reg.err == hipFileSuccess;
    float* dc = nullptr;
    if (!buf_registered) {
        std::fprintf(stderr,
                     "gemm-hipfile-bench: hipFileBufRegister refused the buffer (err=%d drv=%d)\n",
                     static_cast<int>(reg.err), static_cast<int>(reg.hip_drv_err));
    }
    std::printf("gemm-hipfile-bench: compat_requested=%s buf_registered=%s\n",
                compat_from_caller ? "caller" : "self",
                buf_registered ? "yes" : "no");

    for (size_t blk = 0; blk < kBlocks; ++blk) {
        const ssize_t n = hipFileRead(
            fh, static_cast<char*>(dev) + blk * kBlock, kBlock,
            static_cast<off_t>(blk * kBlock), static_cast<off_t>(blk * kBlock));
        if (n != static_cast<ssize_t>(kBlock)) {
            std::fprintf(stderr,
                         "gemm-hipfile-bench: warm hipFileRead block %zu returned %zd of %zu\n",
                         blk, n, kBlock);
            exit_code = 2;
            goto cleanup;
        }
    }
    if (!hip_ok(hipDeviceSynchronize(), "hipDeviceSynchronize()")) goto cleanup;

    const auto read_start = std::chrono::steady_clock::now();
    for (int iter = 0; iter < iters; ++iter) {
        for (size_t blk = 0; blk < kBlocks; ++blk) {
            const ssize_t n = hipFileRead(
                fh, static_cast<char*>(dev) + blk * kBlock, kBlock,
                static_cast<off_t>(blk * kBlock), static_cast<off_t>(blk * kBlock));
            if (n != static_cast<ssize_t>(kBlock)) {
                std::fprintf(stderr,
                             "gemm-hipfile-bench: hipFileRead block %zu returned %zd of %zu\n",
                             blk, n, kBlock);
                exit_code = 2;
                goto cleanup;
            }
        }
    }
    if (!hip_ok(hipDeviceSynchronize(), "hipDeviceSynchronize()")) goto cleanup;
    const auto read_stop = std::chrono::steady_clock::now();
    const double read_seconds =
        std::chrono::duration<double>(read_stop - read_start).count() / iters;
    std::printf("gemm-hipfile-bench: read_bytes=%zu seconds_per_pass=%.9g read_gbs=%.9g\n",
                kPayload, read_seconds, static_cast<double>(kPayload) / read_seconds / 1e9);

    float* const da = static_cast<float*>(dev);
    float* const db = reinterpret_cast<float*>(static_cast<char*>(dev) + kBOffset);
    if (!hip_ok(hipMalloc(&dc, static_cast<size_t>(kM) * kN * sizeof(float)),
                "hipMalloc(&dc, static_cast<size_t>(kM) * kN * sizeof(float))")) goto cleanup;

    const dim3 block(16, 16);
    const dim3 grid((kN + block.x - 1) / block.x, (kM + block.y - 1) / block.y);
    std::printf("gemm-hipfile-bench: m=%d n=%d k=%d\n", kM, kN, kK);

    hipLaunchKernelGGL(sgemm, grid, block, 0, 0, da, db, dc, kM, kN, kK);
    if (!hip_ok(hipGetLastError(), "hipGetLastError()")) goto cleanup;
    if (!hip_ok(hipDeviceSynchronize(), "hipDeviceSynchronize()")) goto cleanup;

    if (!hip_ok(hipMemcpy(hc.data(), dc, hc.size() * sizeof(float), hipMemcpyDeviceToHost),
                "hipMemcpy(hc.data(), dc, hc.size() * sizeof(float), hipMemcpyDeviceToHost)")) goto cleanup;

    double max_abs_error = 0.0;
    long failures = 0;
    for (int row = 0; row < kM; ++row) {
        for (int col = 0; col < kN; ++col) {
            double ref = 0.0;
            for (int i = 0; i < kK; ++i) {
                ref += static_cast<double>(ha[static_cast<size_t>(row) * kK + i]) *
                       static_cast<double>(hb[static_cast<size_t>(i) * kN + col]);
            }
            const double got =
                static_cast<double>(hc[static_cast<size_t>(row) * kN + col]);
            const double err = std::fabs(got - ref);
            if (err > max_abs_error) {
                max_abs_error = err;
            }
            if (err > kTolerance) {
                ++failures;
            }
        }
    }
    std::printf("gemm-hipfile-bench: max_absolute_error=%.6g failures=%ld\n",
                max_abs_error, failures);
    if (failures != 0) {
        std::printf("gemm-hipfile-bench: FAIL\n");
        exit_code = 3;
        goto cleanup;
    }

    const auto gemm_start = std::chrono::steady_clock::now();
    for (int iter = 0; iter < iters; ++iter) {
        hipLaunchKernelGGL(sgemm, grid, block, 0, 0, da, db, dc, kM, kN, kK);
        if (!hip_ok(hipGetLastError(), "hipGetLastError()")) goto cleanup;
    }
    if (!hip_ok(hipDeviceSynchronize(), "hipDeviceSynchronize()")) goto cleanup;
    const auto gemm_stop = std::chrono::steady_clock::now();
    const double gemm_seconds =
        std::chrono::duration<double>(gemm_stop - gemm_start).count() / iters;
    const double flops = 2.0 * kM * kN * kK;
    std::printf("gemm-hipfile-bench: gemm_seconds_per_iter=%.9g gflops=%.9g\n",
                gemm_seconds, flops / gemm_seconds / 1e9);

cleanup:
    if (dc != nullptr) {
        (void)hipFree(dc);
    }
    if (buf_registered) {
        (void)hipFileBufDeregister(dev);
    }
    if (dev != nullptr) {
        (void)hipFree(dev);
    }
    if (handle_registered) {
        (void)hipFileHandleDeregister(fh);
    }
    if (fd >= 0) {
        ::close(fd);
    }
    if (driver_open) {
        hipFileDriverClose();
    }

    if (exit_code == 0) {
        std::printf("gemm-hipfile-bench: PASS\n");
    }
    return exit_code;
}
