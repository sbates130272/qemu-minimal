# LMCache built from source for gfx1250, on ROCm 10.1.
#
#   docker build -t qemu-tool/lmcache-rocm:0.5.6-gfx1250 qemu/docker/lmcache
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
# 10.1 / 26.04 tag is py3.14 and LMCache 0.5.6 pins
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
ARG LMCACHE_VERSION=v0.5.6

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

# providers/ionic -- the userspace half of the emulated ernic -- first appears
# in rdma-core 61. Checked against the upstream tree: v59.0 and v60.0 have no
# ionic directory, v61.0 does. 61 is therefore the floor, not the target.
# Parity with the guest's own rdma-core is not required: this provider talks
# to the guest KERNEL's ionic_rdma, not to the guest's userspace.
#
# PINNED TO A SHA, NOT A TAG, AND PATCHED. SRQ support for providers/ionic is
# not in any rdma-core release -- not v65, not master -- it is still open as
# linux-rdma/rdma-core#1799. Without it ibv_create_srq() returns EOPNOTSUPP,
# UCX's rc_verbs transport declines the device, and the only verbs transport
# left is ud_verbs, which has no one-sided RDMA. That is precisely why LMCache
# P2P fell back to TCP. A release tag here is therefore a TCP build.
ARG RDMA_CORE_VERSION=bd3282a1cf1025c7869a8664c2d54266a4dded44

# The series applied on top, from the build context rather than fetched.
#
# NOT a curl of the PR. A GitHub PR patch URL is not immutable -- the author
# force-pushes and the same URL yields different code with nothing to notice
# -- and the series is not purely upstream rdma-core anyway: 0005 and 0006 fix
# bugs in #1799 itself. 0005 is the one that decides whether SRQ works at all
# (it rings the CQ qtype instead of the RQ qtype, so the device never learns a
# receive was posted and the first message hangs); 0006 stops a failed prepare
# being counted as a posted receive.
#
# These are byte-identical to rocm-ernic's own patches/rdma-core, which is now
# the series' home -- it vendored them in f608391 and its CI checks the base
# SHA above against its patches/rdma-core/README.md. Take changes from there
# rather than re-deriving them, and keep the two in step: the whole directory
# is applied by glob, so adding a file upstream is all it takes.
#
# Deliberately NOT paired with #1733 (RCQ), which collides with #1799 in
# ionic_verbs.c and is not needed: the guest kernel's rcq_sign_bit +
# comp_mask occupy exactly the three bytes upstream still calls rsvd2[3],
# and the provider never reads phc_offset.
COPY rdma-core-patches/ /tmp/rdma-core-patches/

# Newest UCX release. Note the prebuilt nixl wheel reports UCX 1.23.0 -- that
# is an unreleased master build ("git branch <unknown>, revision 65e342a"),
# not something pinnable; 1.22.0 is the latest tag with a release tarball, and
# the tarball matters because it ships a generated ./configure (no autotools).
ARG UCX_VERSION=1.22.0

# Newest nixl release, and the same version the prebuilt wheel shipped -- so
# this changes HOW nixl is built without also moving WHAT is built.
ARG NIXL_VERSION=v1.5.0

RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
        git ca-certificates curl \
        cmake ninja-build pkg-config make \
        libnl-3-dev libnl-route-3-dev libudev-dev libnuma-dev \
    && rm -rf /var/lib/apt/lists/*

# ── RDMA: rdma-core, UCX and nixl, all from source ───────────────────────────
#
# WHY NOT `pip install nixl`. The prebuilt wheel cannot drive this device, and
# the way it fails is silent: nixl comes up, reports "Backend UCX was
# instantiated", and moves every byte over TCP. Measured on a 4-node fleet --
# cross-node P2P hit 100% of chunks while ernic_qp_bytes_rdma_{read,write}_total
# stayed at exactly zero and ernic_ip_bytes_total climbed by 423 MB.
#
# The chain of causes, each verified rather than assumed:
#
#   1. The wheel vendors its own rdma-core, auditwheel-mangled to
#      libibverbs-<hash>.so.1.14.48.0 -- i.e. rdma-core 48, a RHEL 8 build.
#   2. `ionic` entered rdma-core at 61. So the vendored tree PREDATES the only
#      provider this device has; there was never a version of it to load.
#   3. libibverbs therefore enumerates nothing, UCX finds no IB device
#      ("ib_md.c:887 no devices are found") and correctly reports rc_verbs as
#      unavailable. The verbs transport itself is present and loads fine -- it
#      has no hardware to drive.
#   4. None of this is fixable from outside the wheel. The mangled SONAME
#      defeats LD_PRELOAD; the RHEL-8 search path
#      (/usr/lib64/libibverbs/lib%s-rdmav34.so) defeats bind-mounting Ubuntu's
#      providers; and renaming a 61-era provider to -rdmav34 still yields zero
#      devices, because the loader validates the provider ABI struct.
#
# So the wheel has to go. Building the three in order -- rdma-core, then UCX
# against it, then nixl against that UCX -- is what gives the UCX plugin a
# libibverbs that can actually open rocep1s0.
#
# The acceptance test is not "it starts": it is ernic_qp_bytes_rdma_read_total
# leaving zero during a cross-node fetch. Both counters are already scraped.
RUN --mount=type=cache,target=/root/.cache/pip set -eux; \
    SP=/usr/local/lib/python3.12/dist-packages; \
    \
    # No --depth 1 -b: that only resolves a tag or branch, and the pin above
    # is a SHA. blob:none keeps the clone cheap anyway.
    git clone --filter=blob:none \
        https://github.com/linux-rdma/rdma-core.git /tmp/rdma-core; \
    git -C /tmp/rdma-core checkout -q "${RDMA_CORE_VERSION}"; \
    test -d /tmp/rdma-core/providers/ionic; \
    for p in /tmp/rdma-core-patches/*.patch; do \
        git -C /tmp/rdma-core \
            -c user.email=build@invalid -c user.name=build \
            am --whitespace=fix "$p"; \
    done; \
    # The whole reason for the patch: fail the build here rather than ship an
    # image whose UCX silently has no rc_verbs.
    grep -q "create_srq" /tmp/rdma-core/providers/ionic/ionic_verbs.c; \
    cmake -G Ninja -S /tmp/rdma-core -B /tmp/rdma-core/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/rdma \
        -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_INSTALL_SYSCONFDIR=/etc \
        -DNO_MAN_PAGES=1 -DENABLE_STATIC=0; \
    ninja -C /tmp/rdma-core/build; \
    ninja -C /tmp/rdma-core/build install; \
    # Globbed, not a fixed name: the provider ABI suffix tracks the release
    # (61 -> libionic-rdmav59.so, 65 -> libionic-rdmav65.so), so pinning it
    # would break the build on the next bump for no reason.
    ls /opt/rdma/lib/libibverbs/libionic-rdmav*.so >/dev/null; \
    test -f /etc/libibverbs.d/ionic.driver; \
    rm -rf /tmp/rdma-core; \
    \
    # ROCm here is pip packages, not /opt/rocm, and _rocm_sdk_core ships the
    # runtime .so under its SONAME only. UCX's configure link tests want the
    # bare -l name, so give it the two dev symlinks. They go in _rocm_sdk_core
    # and not _rocm_sdk_devel deliberately: the devel tree is 18 GB and is
    # deleted below, while core survives into the runtime stage, so UCX's rocm
    # module still resolves there.
    ln -sf libhsa-runtime64.so.1 "$SP/_rocm_sdk_core/lib/libhsa-runtime64.so"; \
    ln -sf libamdhip64.so.7      "$SP/_rocm_sdk_core/lib/libamdhip64.so"; \
    \
    curl -sSL -o /tmp/ucx.tar.gz \
        "https://github.com/openucx/ucx/releases/download/v${UCX_VERSION}/ucx-${UCX_VERSION}.tar.gz"; \
    tar xf /tmp/ucx.tar.gz -C /tmp; \
    cd "/tmp/ucx-${UCX_VERSION}"; \
    # --with-rocm is not decoration on an AMD image: it builds the rocm UCT
    # module, which is what lets UCX register and move HIP device memory. The
    # fleet's L1 is host memory today and the emulated rocjitsu exposes no KFD
    # node, so nothing exercises it yet -- but compiling it out would bake a
    # ceiling into the image that is invisible until the day L1 moves to VRAM.
    # --without-cuda stays: there is no CUDA here and probing for it only
    # produces a confusing wheel variant.
    # --with-verbs=DIR/--with-rdmacm=DIR are not enough on their own: the
    # configure checks LINK against librdmacm, and with rdma-core in a
    # non-default prefix the link fails with "RDMACM requested but librdmacm
    # is not found or does not provide rdma_establish() API" -- which reads
    # like a too-old rdma-core and is really just a search path. The rpath
    # matters as much as the -L: without it the built modules would need
    # LD_LIBRARY_PATH set for every later configure test too.
    export CPPFLAGS="-I/opt/rdma/include"; \
    export LDFLAGS="-L/opt/rdma/lib -Wl,-rpath,/opt/rdma/lib"; \
    export LD_LIBRARY_PATH=/opt/rdma/lib; \
    ./configure --prefix=/opt/ucx \
        --with-verbs=/opt/rdma --with-rdmacm=/opt/rdma \
        --with-rocm="$SP/_rocm_sdk_core" \
        --without-cuda --without-java --without-go \
        --enable-mt --disable-doxygen-doc; \
    make -j"${MAX_JOBS}"; \
    make install; \
    # Fail here rather than in a fleet: an rdma-core that configure could not
    # link against silently drops the ib module and everything still builds.
    /opt/ucx/bin/ucx_info -b | grep -q '^#define HAVE_IB  *1$'; \
    /opt/ucx/bin/ucx_info -b | grep -q '^#define HAVE_ROCM  *1$'; \
    test -f /opt/ucx/lib/ucx/libuct_ib.so; \
    cd /; rm -rf "/tmp/ucx-${UCX_VERSION}" /tmp/ucx.tar.gz; \
    \
    pip install meson meson-python ninja pybind11 patchelf; \
    git clone --depth 1 -b "${NIXL_VERSION}" \
        https://github.com/ai-dynamo/nixl.git /tmp/nixl; \
    cd /tmp/nixl; \
    # wheel_variant=rocm names the package nixl_rocm, which is the third name
    # the `nixl` meta package looks for (nixl_cu13, nixl_cu12, nixl_rocm) --
    # so LMCache's `from nixl._api import ...` resolves to it with no shim.
    # The autodetected default would be nixl_cu12, which works but describes a
    # CUDA build that is not what this is.
    pip install . --no-build-isolation \
        -Csetup-args=-Ducx_path=/opt/ucx \
        -Csetup-args=-Drocm_path="$SP/_rocm_sdk_core" \
        -Csetup-args=-Dwheel_variant=rocm \
        -Csetup-args=-Dbuild_docs=false; \
    cd /; rm -rf /tmp/nixl; \
    \
    # The meta package only, with --no-deps: it is pure Python and its
    # dependency is a prebuilt variant wheel -- the very thing being replaced.
    pip install --no-deps "nixl==${NIXL_VERSION#v}"; \
    \
    test -d "$SP/nixl_rocm"; \
    python3 -c "from nixl._api import nixl_agent, nixl_agent_config; import nixl_rocm; print('nixl ok via', nixl_rocm.__name__)"; \
    # The whole point: the plugin must bind OUR ucx, not a vendored copy.
    P="$(find "$SP" -name libplugin_UCX.so -print -quit)"; \
    ldd "$P" | grep -q '/opt/ucx/lib/libucp.so'; \
    echo "UCX plugin bound to /opt/ucx"

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
# No -std=c++17 patch here any more. Through v0.5.5 LMCache gave its .hip
# sources c++20 and its host .cpp sources c++17, which fails against a C++20
# libtorch and could not be overridden from outside, because setuptools
# appends extra_compile_args last and the last -std= wins. v0.5.6 ships c++20
# in both setup_extensions/common_cpp.py and build_profiles/rocm.py, so the
# patch script found nothing and said so -- it is deleted along with this
# step. The only -std=c++17 left is in storage_backend_profiles/hf3fs.py,
# which this image does not build.
RUN --mount=type=cache,target=/root/.cache/pip \
    git clone --depth 1 -b ${LMCACHE_VERSION} \
        https://github.com/LMCache/LMCache.git /tmp/lmcache \
    && cd /tmp/lmcache \
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
# nixl, the P2P transfer engine, is installed above rather than here --
# from source, because the published wheel cannot reach this device. It is
# still the only engine option: the alternative, mooncake_te, is a CUDA build
# and dies on "libcudart.so.12: cannot open shared object file". Without nixl
# a server started with --p2p-advertise-url refuses to start at all.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install openai \
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
# the ROCm runtime and UCX link against; libgomp is torch's OpenMP; the two
# libnl are what libibverbs needs to talk to the kernel's RDMA netlink.
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
        python3 python3-pkg-resources \
        libnuma1 libdrm2 libelf1 libgomp1 libatomic1 \
        libnl-3-200 libnl-route-3-200 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/dist-packages \
                    /usr/local/lib/python3.12/dist-packages
# The console scripts (`lmcache`, `lmcache_server`, `lmcache_controller`) and
# anything else the wheels installed onto PATH.
COPY --from=builder /usr/local/bin /usr/local/bin

# The RDMA stack. ~78 MB for both, against an 11 GB image, and without them
# nixl's UCX plugin has no libibverbs to open the ernic with.
#
# /etc/libibverbs.d comes too and is not optional: libibverbs reads those
# .driver files to learn which provider to dlopen, so without ionic.driver it
# enumerates nothing and UCX falls back to TCP -- which is the silent failure
# this whole arrangement exists to avoid.
COPY --from=builder /opt/rdma /opt/rdma
COPY --from=builder /opt/ucx /opt/ucx
COPY --from=builder /etc/libibverbs.d /etc/libibverbs.d

# libibverbs and the UCX libs are outside the dynamic linker's default search
# path, and UCX's rocm module needs the HIP runtime that lives inside
# site-packages (_rocm_sdk_core survived the prune; _rocm_sdk_devel did not).
ENV LD_LIBRARY_PATH=/opt/rdma/lib:/opt/ucx/lib:/usr/local/lib/python3.12/dist-packages/_rocm_sdk_core/lib
ENV PATH=/opt/rdma/bin:/opt/ucx/bin:$PATH

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

# The RDMA half, checked in the shipping stage for the same reason as above:
# the COPY steps must have brought across a usable stack, not just some files.
#
# These are the checks that would have caught the original defect. A plain
# "nixl imports" says nothing -- it imported fine on the wheel too, and then
# ran everything over TCP.
RUN set -eux; \
    ibv_devinfo -l > /dev/null 2>&1 || true; \
    command -v ibv_devinfo > /dev/null; \
    ucx_info -b | grep -q '^#define HAVE_IB  *1$'; \
    ucx_info -b | grep -q '^#define HAVE_ROCM  *1$'; \
    ls /etc/libibverbs.d/ionic.driver > /dev/null; \
    ls /opt/rdma/lib/libibverbs/libionic-rdmav*.so > /dev/null; \
    python3 -c "from nixl._api import nixl_agent, nixl_agent_config; import nixl_rocm; print('nixl', nixl_rocm.__name__)"; \
    P="$(find /usr/local/lib/python3.12/dist-packages -name libplugin_UCX.so -print -quit)"; \
    ldd "$P" | grep -q '/opt/ucx/lib/libucp.so'; \
    echo "rdma stack ok: verbs+rocm UCX, ionic provider, nixl bound to /opt/ucx"

WORKDIR /workspace

# No default command. The image has two jobs and they are peers: the compose
# stack runs `lmcache coordinator`, the in-VM systemd unit runs
# `lmcache server`. Naming one here would make the other look like the
# exception.
ENTRYPOINT []
CMD ["lmcache", "--help"]
