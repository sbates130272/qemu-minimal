// Smallest useful proof that the emulated GPU executes: host-to-device copy,
// a kernel dispatch, and a copy back, all verified. Runs before the scratch
// tests so a failure here says "the device is not working" rather than "the
// scratch path is not working".
//
//   vector_add <n>

#include <hip/hip_runtime.h>
#include <cstdio>
#include <cstdlib>

#define BLOCK 64

__global__ void vector_add(float *out, const float *a, const float *b, int n)
{
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n)
        out[i] = a[i] + b[i];
}

int main(int argc, char **argv)
{
    // See scratch.hip: a timeout-killed process never flushes a block-buffered
    // stdout, so a redirected run would lose everything it had printed.
    setvbuf(stdout, NULL, _IONBF, 0);

    int n = (argc > 1) ? atoi(argv[1]) : 4096;
    size_t sz = (size_t)n * sizeof(float);

    hipDeviceProp_t prop;
    if (hipGetDeviceProperties(&prop, 0) != hipSuccess) {
        printf("vector-add: FAIL: no device\n");
        return 1;
    }
    printf("device=%s CUs=%d n=%d\n", prop.gcnArchName,
           prop.multiProcessorCount, n);

    float *ha = (float *)malloc(sz), *hb = (float *)malloc(sz);
    float *ho = (float *)malloc(sz);
    for (int i = 0; i < n; i++) {
        ha[i] = (float)i;
        hb[i] = (float)(2 * i);
    }

    float *da = nullptr, *db = nullptr, *dc = nullptr;
    if (hipMalloc(&da, sz) != hipSuccess || hipMalloc(&db, sz) != hipSuccess ||
        hipMalloc(&dc, sz) != hipSuccess) {
        printf("vector-add: FAIL: hipMalloc\n");
        return 1;
    }
    if (hipMemcpy(da, ha, sz, hipMemcpyHostToDevice) != hipSuccess ||
        hipMemcpy(db, hb, sz, hipMemcpyHostToDevice) != hipSuccess) {
        printf("vector-add: FAIL: memcpy to device\n");
        return 1;
    }

    hipLaunchKernelGGL(vector_add, dim3((n + BLOCK - 1) / BLOCK), dim3(BLOCK),
                       0, 0, dc, da, db, n);
    hipError_t e = hipGetLastError();
    if (e != hipSuccess) {
        printf("vector-add: FAIL: launch: %s\n", hipGetErrorString(e));
        return 1;
    }

    e = hipDeviceSynchronize();
    if (e != hipSuccess) {
        printf("vector-add: FAIL: sync: %s\n", hipGetErrorString(e));
        return 1;
    }

    if (hipMemcpy(ho, dc, sz, hipMemcpyDeviceToHost) != hipSuccess) {
        printf("vector-add: FAIL: memcpy back\n");
        return 1;
    }

    int bad = 0;
    for (int i = 0; i < n; i++)
        if (ho[i] != 3.0f * (float)i)
            bad++;

    printf("mismatches=%d\n", bad);
    printf(bad ? "vector-add: FAIL\n" : "vector-add: PASS\n");
    return bad != 0;
}
