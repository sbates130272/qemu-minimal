# LMCache built from source for gfx1250, on ROCm 10.1.
#
#   docker build -t qemu-tool/lmcache-rocm:0.5.5-gfx1250 qemu/docker/lmcache
#
# Upstream ships docker/Dockerfile.rocm, but it targets gfx942,gfx950 and
# nothing else -- there is no published LMCache wheel for gfx1250, so this
# compiles the HIP extensions against the architecture rather than installing
# a wheel. See ../README.md for why each of the choices below is forced rather
# than preferred.

# Two things about this tag are forced rather than chosen.
#
# WHY A PYTORCH BASE AND NOT rocm/dev-ubuntu-*. Torch is not optional for
# LMCache at either stage: setup.py's HIP path compiles through
# torch.utils.cpp_extension, and lmcache/v1/metadata.py and cache_engine.py
# import torch at module scope, so even the model-less worker needs it at run
# time. It also cannot be installed: ROCm 10.1 exists as a dev image
# (rocm/dev-ubuntu-24.04:10.1.0-full), but there is no matching PyTorch wheel
# index -- the newest download.pytorch.org/whl/rocm* channel is rocm7.2, and
# both rocm10.0 and rocm10.1 return 403. A base that already carries a ROCm
# 10.1 torch is the only route to one.
#
# WHY 24.04 AND NOT 26.04. rocm/pytorch does publish 26.04, but every ROCm
# 10.1 / 26.04 tag is py3.14 and LMCache 0.5.5 pins
# requires-python = ">=3.10,<3.14" (pyproject.toml). So 26.04 is excluded by
# the interpreter, not by preference; revisit when LMCache widens that pin.
# The guest distro does not have to match the container's, and does not here.
ARG BASE_IMAGE=rocm/pytorch:rocm10.1.0_ubuntu24.04_py3.12_pytorch_release_2.14.0

# Ubuntu 24.04 because that is what BASE_IMAGE is built on: the runtime stage
# copies site-packages wholesale, so its interpreter must be the same 3.12
# that compiled those extensions, and the path they live on
# (/usr/local/lib/python3.12/dist-packages) must line up exactly.
ARG RUNTIME_IMAGE=ubuntu:24.04

FROM ${BASE_IMAGE} AS builder

# The newest LMCache release at the time of writing. A tag, not a branch:
# the build clones --depth 1 -b this, and setuptools_scm derives the version
# from it, so a branch name yields a dev version that cannot be pinned.
ARG LMCACHE_VERSION=v0.5.5

# gfx1250 is the whole point of this file. PYTORCH_ROCM_ARCH is what
# torch.utils.cpp_extension reads to pick HIP targets; comma-separate to fatten
# the binary (e.g. "gfx1250,gfx950") if one image has to serve mixed hosts.
ARG PYTORCH_ROCM_ARCH=gfx1250

# hipcc is single-threaded per TU and the kernel set is large. 2 is upstream's
# default and is about right for a shared build host; raise it on a dedicated
# one.
ARG MAX_JOBS=8

# HCC_AMDGPU_TARGET as well as PYTORCH_ROCM_ARCH. The two cover different
# compilations: torch's extension builder reads PYTORCH_ROCM_ARCH and turns it
# into --offload-arch for the .hip sources, but LMCache's host .cpp sources are
# also driven through hipcc with no --offload-arch, where hipcc falls back to
# its own built-in default and emits "warning ... when compiling for gfx906"
# for a target nothing here will ever run on.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTORCH_ROCM_ARCH=${PYTORCH_ROCM_ARCH} \
    HCC_AMDGPU_TARGET=${PYTORCH_ROCM_ARCH} \
    MAX_JOBS=${MAX_JOBS} \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
        git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Build deps come from the cloned source's own requirements/build.txt rather
# than being restated here, so they cannot drift from the tag being built.
#
# --no-build-isolation is not an optimisation: build isolation would fetch
# pyproject.toml's pinned torch==2.13.0 from PyPI, which is a CUDA build, and
# the HIP extensions would then compile against the wrong torch headers and
# link against a torch that is not the one at run time.
#
# setup.py selects cupy-rocm-7-0 when BUILD_WITH_HIP=1. That is upstream's
# pin and it predates ROCm 10; it installs and imports here, but if a cupy
# path ever misbehaves on this base, that pin is the first place to look.
#
# patch-cxx-std.py raises LMCache's hardcoded -std=c++17 to c++20 before the
# build. Without it the build FAILS -- libtorch requires C++20, LMCache gives
# its .hip sources c++20 but its host .cpp sources c++17, and the flag cannot
# be overridden from outside because setuptools appends it last. The script
# carries the full diagnosis and exits non-zero if it finds nothing to patch.
COPY patch-cxx-std.py /usr/local/bin/lmcache-patch-cxx-std
RUN --mount=type=cache,target=/root/.cache/pip \
    git clone --depth 1 -b ${LMCACHE_VERSION} \
        https://github.com/LMCache/LMCache.git /tmp/lmcache \
    && cd /tmp/lmcache \
    && python3 -I /usr/local/bin/lmcache-patch-cxx-std /tmp/lmcache \
    && pip install -r requirements/build.txt \
    && python3 -c 'import torch; print("TORCH=", torch.__version__, "HIP=", torch.version.hip)' \
    && BUILD_WITH_HIP=1 CXX=hipcc \
        pip install --no-build-isolation . \
    && rm -rf /tmp/lmcache

# Two runtime extras, both of which the fleet genuinely needs.
#
# openai: not optional despite looking it. The `lmcache` CLI registers every
# subcommand's parser eagerly at startup, and `lmcache bench` imports openai
# at registration time -- so without it `lmcache --help` itself dies on
# ModuleNotFoundError and `lmcache server` and `lmcache coordinator`, the two
# entrypoints this image exists for, are unreachable.
#
# nixl: the P2P transfer engine. LMCache ships it as the `lmcache[nixl]`
# extra, and upstream's own Dockerfile.rocm skips it with "not yet available
# on ROCm" -- that is stale, it installs and its UCX backend instantiates
# fine on this base (verified: "Backend UCX was instantiated"). It is also
# the ONLY option here: the alternative engine, mooncake_te, is a CUDA build
# and fails on this image with "libcudart.so.12: cannot open shared object
# file". Without nixl a server started with --p2p-advertise-url refuses to
# start.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install openai nixl \
    && lmcache --help > /dev/null \
    && python3 -c "import nixl; print('nixl ok')"

# Fail the build rather than the fleet. Two separate things are checked,
# because a build can succeed at one and not the other and neither would
# surface until eight guests were up.
#
# `cuda_ops` is the HIP extension despite the name -- LMCache keeps the CUDA
# module name on ROCm (TORCH_EXTENSION_NAME=cuda_ops in build_profiles/rocm.py)
# -- so importing it is what proves the device half built at all.
#
# Then the architecture: the compiler embeds its offload target as a triple in
# the bundled device code, so grepping the .so for it is how we know
# PYTORCH_ROCM_ARCH was actually honoured rather than silently defaulted. The
# host .cpp sources go through hipcc with no --offload-arch and quietly pick
# hipcc's own default (gfx906 here), so "it compiled" is not evidence.
# Drop what only the BUILD needed, before the runtime stage copies the tree.
#
# _rocm_sdk_devel is the whole reason this image was ~38 GB: it is the ROCm
# DEVELOPMENT sdk -- hipcc, clang, the device-library bitcode and headers --
# shipped as a pip package rather than under /opt/rocm. The runtime half is
# _rocm_sdk_core, which stays. Verified by removing it and re-running
# everything: torch imports and reports rocm10.1.0, both LMCache C extensions
# import, nixl imports, and `lmcache server` starts and serves.
#
# triton, apex and pandas go for the same reason -- nothing in the MP server
# path touches them. The nixl_cu1*/nixl_ep_cu1* trees look like equally dead
# CUDA weight and are NOT removed: they bundle the libgrpc the nixl Python
# package dlopens, and without them the server dies on
# "libgrpc-fd21975d.so.48.0.0: cannot open shared object file".
RUN set -eu; SP=/usr/local/lib/python3.12/dist-packages; \
    rm -rf "$SP/_rocm_sdk_devel" "$SP/triton" "$SP/apex" "$SP/pandas"; \
    \
    # The other half, and the larger one: ROCm's math libraries ship kernels
    # precompiled for EVERY supported GPU, and this image serves exactly one.
    # hipblaslt alone is 2.2 GB, rocblas 809 MB, and there is a ~400 MB
    # libMIOpenCKGroupedConv_<arch>.so per architecture. Keeping only the
    # arches this image was built for drops 11.3 GB across 8101 files.
    #
    # Keyed on the filename's gfx token, which is how every one of them is
    # named -- a file with no gfx token is architecture-independent and stays.
    set -- -type f -name "*gfx*"; \
    for a in $(echo "${PYTORCH_ROCM_ARCH}" | tr ',' ' '); do \
        set -- "$@" ! -name "*${a}*"; \
    done; \
    find "$SP" "$@" -delete; \
    \
    # Prove the target survived its own prune. A typo in PYTORCH_ROCM_ARCH
    # would otherwise match nothing to keep, delete every architecture, and
    # produce an image that imports fine and has no kernels at all.
    for a in $(echo "${PYTORCH_ROCM_ARCH}" | tr ',' ' '); do \
        if [ -z "$(find "$SP" -name "*${a}*" -print -quit)" ]; then \
            echo "ERROR: pruning left no $a files; check PYTORCH_ROCM_ARCH."; \
            exit 1; \
        fi; \
    done; \
    find "$SP" -name "__pycache__" -type d -prune -exec rm -rf {} + ; \
    find "$SP" -name "*.pyc" -delete; \
    du -sh "$SP"

#################### RUNTIME ####################
# A plain Ubuntu, not the ROCm/PyTorch base. Deleting the build sdk in a later
# layer of a single-stage build frees nothing -- the earlier layer still ships
# it -- so the only way to actually shrink the image is to start from
# something small and copy in the pruned tree.
FROM ${RUNTIME_IMAGE} AS runtime

ARG PYTORCH_ROCM_ARCH=gfx1250
ENV DEBIAN_FRONTEND=noninteractive \
    PYTORCH_ROCM_ARCH=${PYTORCH_ROCM_ARCH} \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1

# Runtime shared libraries only, no toolchain. libnuma and libdrm are what
# the ROCm runtime and UCX link against; libgomp is torch's OpenMP.
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
        python3 python3-pkg-resources \
        libnuma1 libdrm2 libelf1 libgomp1 libatomic1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/dist-packages \
                    /usr/local/lib/python3.12/dist-packages
# The console scripts (`lmcache`, `lmcache_server`, `lmcache_controller`) and
# anything else the wheels installed onto PATH.
COPY --from=builder /usr/local/bin /usr/local/bin

# Re-run the checks in the stage that actually ships. Running them only in the
# builder would prove the wrong filesystem: everything below depends on the
# COPY above having brought across a complete, importable tree.
#
# `cuda_ops` is the HIP extension despite the name -- LMCache keeps the CUDA
# module name on ROCm (TORCH_EXTENSION_NAME=cuda_ops in build_profiles/rocm.py)
# -- so importing it is what proves the device half survived.
#
# Then the architecture: the compiler embeds its offload target as a triple in
# the bundled device code, so grepping the .so for it is how we know
# PYTORCH_ROCM_ARCH was actually honoured rather than silently defaulted. The
# host .cpp sources go through hipcc with no --offload-arch and quietly pick
# hipcc's own default (gfx906 here), so "it compiled" is not evidence.
RUN python3 -c "import torch; print('TORCH=', torch.__version__, 'HIP=', torch.version.hip)" \
    && python3 -c "import lmcache, lmcache.cuda_ops, lmcache.lmcache_native; \
        print('lmcache', lmcache.__version__)" \
    && python3 -c "import nixl; print('nixl ok')" \
    && SO=$(python3 -c "import lmcache.cuda_ops as m; print(m.__file__)") \
    && if ! grep -qa "${PYTORCH_ROCM_ARCH%%,*}" "$SO"; then \
           echo "ERROR: $SO contains no ${PYTORCH_ROCM_ARCH%%,*} device code;" \
                "PYTORCH_ROCM_ARCH was not honoured."; exit 1; \
       fi \
    && echo "verified ${PYTORCH_ROCM_ARCH} device code in $SO" \
    && lmcache server --help > /dev/null && echo "cli ok"

WORKDIR /workspace

# No default command. The image has two jobs and they are peers: the compose
# stack runs `lmcache coordinator`, the in-VM systemd unit runs
# `lmcache server`. Naming one here would make the other look like the
# exception.
ENTRYPOINT []
CMD ["lmcache", "--help"]
