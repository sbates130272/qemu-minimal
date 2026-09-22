#include <hip/hip_runtime.h>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <vector>

constexpr int kM = 128;
constexpr int kN = 128;
constexpr int kK = 128;
constexpr float kTolerance = 1e-3f;

#define HIP_CHECK(expr)                                                       \
    do {                                                                      \
        hipError_t err_ = (expr);                                             \
        if (err_ != hipSuccess) {                                             \
            std::fprintf(stderr, "sgemm-bench: %s failed: %s\n", #expr,       \
                         hipGetErrorString(err_));                             \
            return 1;                                                         \
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

int main(int argc, char** argv) {
    int iters = 8;
    if (argc > 1) {
        iters = std::atoi(argv[1]);
        if (iters < 1) {
            std::fprintf(stderr, "sgemm-bench: iters must be >= 1\n");
            return 1;
        }
    }

    hipDeviceProp_t prop{};
    HIP_CHECK(hipGetDeviceProperties(&prop, 0));
    std::printf("sgemm-bench: device=%s arch=%s\n", prop.name, prop.gcnArchName);

    hipFuncAttributes attr{};
    HIP_CHECK(hipFuncGetAttributes(&attr, reinterpret_cast<const void*>(sgemm)));
    std::printf("sgemm-bench: private_segment_bytes=%zu\n",
                static_cast<size_t>(attr.localSizeBytes));
    if (attr.localSizeBytes != 0) {
        std::fprintf(stderr,
                     "sgemm-bench: kernel needs %zu bytes of scratch; rebuild with -O2.\n",
                     static_cast<size_t>(attr.localSizeBytes));
        return 4;
    }

    std::vector<float> ha, hb;
    fill_operands(ha, hb);
    std::vector<float> hc(static_cast<size_t>(kM) * kN, 0.0f);

    float *da = nullptr, *db = nullptr, *dc = nullptr;
    int exit_code = 0;
    auto hip_ok = [&](hipError_t err, const char* expr) {
        if (err != hipSuccess) {
            std::fprintf(stderr, "sgemm-bench: %s failed: %s\n", expr,
                         hipGetErrorString(err));
            exit_code = 1;
            return false;
        }
        return true;
    };
    if (!hip_ok(hipMalloc(&da, ha.size() * sizeof(float)), "hipMalloc(&da, ha.size() * sizeof(float))")) goto cleanup;
    if (!hip_ok(hipMalloc(&db, hb.size() * sizeof(float)), "hipMalloc(&db, hb.size() * sizeof(float))")) goto cleanup;
    if (!hip_ok(hipMalloc(&dc, hc.size() * sizeof(float)), "hipMalloc(&dc, hc.size() * sizeof(float))")) goto cleanup;
    if (!hip_ok(hipMemcpy(da, ha.data(), ha.size() * sizeof(float), hipMemcpyHostToDevice),
                "hipMemcpy(da, ha.data(), ha.size() * sizeof(float), hipMemcpyHostToDevice)")) goto cleanup;
    if (!hip_ok(hipMemcpy(db, hb.data(), hb.size() * sizeof(float), hipMemcpyHostToDevice),
                "hipMemcpy(db, hb.data(), hb.size() * sizeof(float), hipMemcpyHostToDevice)")) goto cleanup;

    const dim3 block(16, 16);
    const dim3 grid((kN + block.x - 1) / block.x, (kM + block.y - 1) / block.y);
    std::printf("sgemm-bench: m=%d n=%d k=%d\n", kM, kN, kK);

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

    std::printf("sgemm-bench: max_absolute_error=%.6g failures=%ld\n",
                max_abs_error, failures);
    if (failures != 0) {
        std::printf("sgemm-bench: FAIL\n");
        exit_code = 3;
        goto cleanup;
    }

    const auto start = std::chrono::steady_clock::now();
    for (int i = 0; i < iters; ++i) {
        hipLaunchKernelGGL(sgemm, grid, block, 0, 0, da, db, dc, kM, kN, kK);
        if (!hip_ok(hipGetLastError(), "hipGetLastError()")) goto cleanup;
    }
    if (!hip_ok(hipDeviceSynchronize(), "hipDeviceSynchronize()")) goto cleanup;
    const auto stop = std::chrono::steady_clock::now();

    const double seconds =
        std::chrono::duration<double>(stop - start).count() / iters;
    const double flops = 2.0 * kM * kN * kK;
    std::printf("sgemm-bench: seconds_per_iter=%.9g gflops=%.9g\n", seconds,
                flops / seconds / 1e9);
cleanup:
    if (dc != nullptr) {
        (void)hipFree(dc);
    }
    if (db != nullptr) {
        (void)hipFree(db);
    }
    if (da != nullptr) {
        (void)hipFree(da);
    }

    if (exit_code == 0) {
        std::printf("sgemm-bench: PASS\n");
    }
    return exit_code;
}
