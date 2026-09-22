// rocjitsu-scratch-repro.hip
//
// Minimal reproducer: on rocJITsu's emulated gfx1250, any kernel with a
// non-zero private (scratch) segment fails. A kernel that is identical except
// that it uses no scratch succeeds.
//
// BUILD -- the optimisation level matters, see the note below:
//
//     /opt/rocm/core-10.0/bin/hipcc -O2 --offload-arch=gfx1250 \
//         -o rocjitsu-scratch-repro rocjitsu-scratch-repro.hip
//
// RUN -- needs access to /dev/kfd, which is root:render, so either add
// yourself to the render group or use sudo:
//
//     sudo ./rocjitsu-scratch-repro       # 20 s watchdog per kernel
//     sudo ./rocjitsu-scratch-repro 60    # or pass your own timeout
//
// Without it HIP reports "no ROCm-capable device is detected", which looks
// like an emulator problem and is not one.
//
// EXIT CODES
//     0  every kernel passed        -- the bug did NOT reproduce
//     2  a kernel hung              -- watchdog fired in hipDeviceSynchronize
//     3  a kernel returned wrong or untouched results
//     4  the build is unsuitable    -- see below; results are not meaningful
//     1  a HIP API call failed outright
//
// WHY -O2 IS REQUIRED
//     At -O0 the compiler spills the kernel's own arguments before doing
//     anything else, so EVERY kernel gets a non-zero private segment --
//     including the control. That destroys the experiment, because there is
//     then no scratch-free kernel left to compare against. This program
//     therefore checks the control kernel's private segment at runtime and
//     exits 4 rather than reporting a misleading result.
//
// WHAT IT PRINTS
//     For each kernel, the private segment size the compiler actually
//     allocated (hipFuncAttributes::localSizeBytes, which is the
//     .amdhsa_private_segment_fixed_size in the code object) and then the
//     outcome. With the ROCm 10.0 compiler in our guest the three scratch
//     kernels come out at 288, 1056 and 10272 bytes, bracketing the ~10 KiB
//     private segment of the real workload that first hit this. The exact
//     numbers depend on your compiler version; the program prints what it
//     actually measured rather than trusting these.
//
// WHAT WE ACTUALLY OBSERVED
//     Against rocJITsu at 20d4ce1c (branch users/agutierr/gfx1250-vfio-compute-6)
//     with a QEMU guest running real upstream amdgpu over vfio-user:
//
//         device    : AMD Radeon Graphics (gfx1250)
//         watchdog  : 25 s per kernel
//
//         k0 control       private_segment=    0 bytes  PASS
//         k1 scratch-68    private_segment=  288 bytes
//           HANG: k1 scratch-68 did not complete within 25 s.
//         EXIT=2
//
//     288 bytes of scratch is enough. The emulator logs, once per wave and
//     with no accompanying access-fault or GART message:
//
//         cp: terminal VM fault pid=50 qid=2147483701 dispatch=182 outcome=2
//
//     outcome=2 is VmAccessOutcome::Faulted. The "SCRATCH wf... pool=..."
//     line that init_wavefront_regs() logs on the success path never appears,
//     which places the failure before any memory access is attempted.
//
// WHERE IT FAILS IN THE EMULATOR
//     CommandProcessor::init_wavefront_regs() probes the whole per-wave
//     scratch slice and returns Faulted when the probe is not Complete
//     (command_processor.cpp:726-728 at 20d4ce1c). The recovery branch just
//     above is gated on scratch_allocator_, which is only ever installed from
//     kmd/linux/simulated_kfd.cpp -- never on the vfio-user path -- so on this
//     path it is dead code and every wave of every scratch kernel is faulted
//     before a single instruction issues.
//
// STATUS
//     Fixed upstream. The 2026-09-08 rewrite of this branch added
//     CommandProcessor::request_dynamic_scratch() and its reclaim counterpart,
//     which do what the local patch this file was written against did and more
//     -- they also handle the case where the runtime provisions a pool sized
//     for an earlier, smaller dispatch. The forensics above are left as a
//     record of the defect; the program itself is kept because it is still the
//     cheapest way to tell a server that asks the runtime for scratch from one
//     that does not, and nothing in the image build can tell them apart.

#include <hip/hip_runtime.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {

constexpr int kThreads = 64;
constexpr int kBlocks = 1;
constexpr int kN = kThreads * kBlocks;
constexpr int kSentinel = -111111;
constexpr int kRot = 3;

// Exit codes, kept in one place so the header above cannot drift.
constexpr int kExitOk = 0;
constexpr int kExitApi = 1;
constexpr int kExitHang = 2;
constexpr int kExitWrong = 3;
constexpr int kExitUnsuitable = 4;

#define HIP_CHECK(expr)                                                                  \
  do {                                                                                   \
    hipError_t error_ = (expr);                                                          \
    if (error_ != hipSuccess) {                                                          \
      std::fprintf(stderr, "%s:%d: %s -> %s\n", __FILE__, __LINE__, #expr,               \
                   hipGetErrorString(error_));                                           \
      std::exit(kExitApi);                                                               \
    }                                                                                    \
  } while (0)

// Control: no locals, so the compiler allocates no private segment.
__global__ void control_kernel(int *out, const int *in, int n) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < n)
    out[i] = in[i] * 2 + 1;
}

// Scratch: a per-thread array indexed by a value only known at run time, so it
// cannot be promoted to registers and must live in the private segment. The
// "unroll 1" pragmas stop the compiler turning the permutation into a register
// shuffle network, which would defeat the point.
template <int Elems>
__global__ void scratch_kernel(int *out, const int *in, int rot, int n) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= n)
    return;

  int buf[Elems];
#pragma unroll 1
  for (int j = 0; j < Elems; ++j)
    buf[j] = in[i] + j;

  int acc = 0;
#pragma unroll 1
  for (int j = 0; j < Elems; ++j)
    acc += buf[(j * 7 + rot) % Elems];

  out[i] = acc;
}

// (j * 7 + rot) % Elems walks a permutation of [0, Elems) whenever 7 and Elems
// are coprime, which holds for all three sizes below. So the sum is just every
// offset added once, and the expected value has a closed form.
int expected_scratch(int value, int elems) {
  return elems * value + elems * (elems - 1) / 2;
}

int expected_control(int value) { return value * 2 + 1; }

// hipDeviceSynchronize() does not come back when this bug bites, so it cannot
// be waited on with a timeout. Arm a detached watchdog that terminates the
// process instead, and disarm it once the sync returns.
// A generation counter rather than a flag: a plain bool lets the PREVIOUS
// kernel's watchdog thread, which is still sleeping when the next arm() clears
// the flag, wake up and fire under the wrong label.
std::atomic<uint64_t> g_generation{0};

void arm_watchdog(int seconds, const std::string &label) {
  const uint64_t generation = g_generation.fetch_add(1) + 1;
  std::thread([seconds, label, generation]() {
    for (int elapsed = 0; elapsed < seconds * 10; ++elapsed) {
      if (g_generation.load() != generation)
        return;
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    std::fprintf(stderr,
                 "\n  HANG: %s did not complete within %d s.\n"
                 "  The dispatch never retired; hipDeviceSynchronize() is still blocked.\n",
                 label.c_str(), seconds);
    std::fflush(nullptr);
    _exit(kExitHang);
  }).detach();
}

void disarm_watchdog() { g_generation.fetch_add(1); }

struct Outcome {
  bool ran = false;      // results changed at all
  bool correct = false;  // and they were right
  int first_bad = -1;
  int got = 0;
  int want = 0;
};

Outcome check(const std::vector<int> &host, int elems, const std::vector<int> &in) {
  Outcome outcome;
  outcome.ran = false;
  for (int i = 0; i < kN; ++i) {
    if (host[i] != kSentinel) {
      outcome.ran = true;
      break;
    }
  }
  outcome.correct = true;
  for (int i = 0; i < kN; ++i) {
    const int want = elems == 0 ? expected_control(in[i]) : expected_scratch(in[i], elems);
    if (host[i] != want) {
      outcome.correct = false;
      outcome.first_bad = i;
      outcome.got = host[i];
      outcome.want = want;
      break;
    }
  }
  return outcome;
}

int private_bytes(const void *kernel) {
  hipFuncAttributes attributes{};
  HIP_CHECK(hipFuncGetAttributes(&attributes, kernel));
  return static_cast<int>(attributes.localSizeBytes);
}

}  // namespace

int main(int argc, char **argv) {
  const int timeout = argc > 1 ? std::atoi(argv[1]) : 20;

  hipDeviceProp_t properties{};
  HIP_CHECK(hipGetDeviceProperties(&properties, 0));
  std::printf("device    : %s (%s)\n", properties.name, properties.gcnArchName);
  std::printf("watchdog  : %d s per kernel\n\n", timeout);

  std::vector<int> in(kN);
  for (int i = 0; i < kN; ++i)
    in[i] = i + 1;

  int *d_in = nullptr;
  int *d_out = nullptr;
  HIP_CHECK(hipMalloc(&d_in, kN * sizeof(int)));
  HIP_CHECK(hipMalloc(&d_out, kN * sizeof(int)));
  HIP_CHECK(hipMemcpy(d_in, in.data(), kN * sizeof(int), hipMemcpyHostToDevice));

  std::vector<int> host(kN);
  int failures = 0;

  // ---- control -----------------------------------------------------------
  const int control_private = private_bytes(reinterpret_cast<const void *>(control_kernel));
  std::printf("k0 control       private_segment=%5d bytes  ", control_private);
  std::fflush(stdout);

  if (control_private != 0) {
    std::printf("\n\n");
    std::fprintf(stderr,
                 "UNSUITABLE BUILD: the control kernel has a %d byte private segment.\n"
                 "Every kernel uses scratch, so there is no baseline to compare against.\n"
                 "This is what -O0 does -- rebuild with -O2 and run again.\n",
                 control_private);
    return kExitUnsuitable;
  }

  {
    std::vector<int> fill(kN, kSentinel);
    HIP_CHECK(hipMemcpy(d_out, fill.data(), kN * sizeof(int), hipMemcpyHostToDevice));
    arm_watchdog(timeout, "k0 control");
    control_kernel<<<kBlocks, kThreads>>>(d_out, d_in, kN);
    HIP_CHECK(hipGetLastError());
    HIP_CHECK(hipDeviceSynchronize());
    disarm_watchdog();
    HIP_CHECK(hipMemcpy(host.data(), d_out, kN * sizeof(int), hipMemcpyDeviceToHost));

    const Outcome outcome = check(host, 0, in);
    if (outcome.correct) {
      std::printf("PASS\n");
    } else {
      ++failures;
      std::printf("FAIL (out[%d]=%d want %d)\n", outcome.first_bad, outcome.got, outcome.want);
    }
  }

  // ---- scratch, at the three sizes we measured failing --------------------
  // Each entry is {label, kernel pointer, element count}. 68/260/2564 ints came
  // out at 288/1056/10272 bytes of private segment with the ROCm 10.0 compiler.
  struct Case {
    const char *label;
    const void *kernel;
    int elems;
    void (*launch)(int *, const int *, int, int);
  };

  const Case cases[] = {
      {"k1 scratch-68 ", reinterpret_cast<const void *>(scratch_kernel<68>), 68,
       [](int *out, const int *input, int rot, int n) {
         scratch_kernel<68><<<kBlocks, kThreads>>>(out, input, rot, n);
       }},
      {"k2 scratch-260", reinterpret_cast<const void *>(scratch_kernel<260>), 260,
       [](int *out, const int *input, int rot, int n) {
         scratch_kernel<260><<<kBlocks, kThreads>>>(out, input, rot, n);
       }},
      {"k3 scratch-2564", reinterpret_cast<const void *>(scratch_kernel<2564>), 2564,
       [](int *out, const int *input, int rot, int n) {
         scratch_kernel<2564><<<kBlocks, kThreads>>>(out, input, rot, n);
       }},
  };

  for (const Case &test : cases) {
    const int bytes = private_bytes(test.kernel);
    std::printf("%s  private_segment=%5d bytes  ", test.label, bytes);
    std::fflush(stdout);

    if (bytes == 0) {
      std::printf("SKIP (compiler kept it in registers; nothing to test)\n");
      continue;
    }

    std::vector<int> fill(kN, kSentinel);
    HIP_CHECK(hipMemcpy(d_out, fill.data(), kN * sizeof(int), hipMemcpyHostToDevice));

    arm_watchdog(timeout, test.label);
    test.launch(d_out, d_in, kRot, kN);
    HIP_CHECK(hipGetLastError());
    HIP_CHECK(hipDeviceSynchronize());
    disarm_watchdog();

    HIP_CHECK(hipMemcpy(host.data(), d_out, kN * sizeof(int), hipMemcpyDeviceToHost));
    const Outcome outcome = check(host, test.elems, in);

    if (outcome.correct) {
      std::printf("PASS\n");
    } else if (!outcome.ran) {
      ++failures;
      std::printf("FAIL silent no-op (hipSuccess, output untouched)\n");
    } else {
      ++failures;
      std::printf("FAIL wrong (out[%d]=%d want %d)\n", outcome.first_bad, outcome.got,
                  outcome.want);
    }
  }

  HIP_CHECK(hipFree(d_in));
  HIP_CHECK(hipFree(d_out));

  std::printf("\n");
  if (failures == 0) {
    std::printf("all kernels passed -- the scratch bug did not reproduce here\n");
    return kExitOk;
  }
  std::printf("%d scratch kernel(s) failed while the scratch-free control passed\n", failures);
  return kExitWrong;
}
