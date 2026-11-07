# rocm-sdxi: vfio-user SDXI (Smart Data Accelerator Interface) emulator.
#
# Build the rocm-sdxi binary on the host first (requires libvfio-user installed
# at /usr/local), then bundle it here:
#
#   # Host build (one-time):
#   cd /tmp/rocm-sdxi-work/emulation/sdxi && mkdir -p build && cd build
#   cmake .. -DCMAKE_BUILD_TYPE=Release && cmake --build . --parallel $(nproc)
#
#   # Docker image:
#   docker build \
#     --build-context sdxi-bin=/tmp/rocm-sdxi-work/emulation/sdxi/build \
#     -t qemu-tool/sdxi:latest qemu/docker/sdxi
#
# libvfio-user and json-c are statically linked; only libz is a runtime dep.
# Built from SHA bbbceddbadb878fdc0dc80650f63dcf60995ae00 of ROCm/rocm-systems (emulation/sdxi).

FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=sdxi-bin rocm-sdxi /usr/local/bin/rocm-sdxi

ENTRYPOINT ["rocm-sdxi"]
CMD ["-s", "/run/vfu/sdxi-1.sock"]
