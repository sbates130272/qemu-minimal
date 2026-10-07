# `qemu/docker/`

Dockerfiles this repo owns, one directory per image. Build context is the
image's own directory:

```bash
docker build -t qemu-tool/lmcache-rocm:0.5.5-gfx1250 qemu/docker/lmcache
```

| Directory | Image | Used by |
|---|---|---|
| [`lmcache/`](lmcache/) | LMCache v0.5.5 for gfx1250 on ROCm 10.1 | the `lmcache` profile of `vfio-user-ernic-rocjitsu-scale-out`, and the `lmcache_guest` ansible role |

Not to be confused with
[`qemu/compose/vfio-user-ernic-rocjitsu-scale-out/tapsetup/`](../compose/vfio-user-ernic-rocjitsu-scale-out/tapsetup/),
which is three lines built by compose itself as part of one stack. Anything
here is built by hand and referenced by tag.

---

## `lmcache/`

LMCache compiled from source with HIP extensions for `gfx1250`.

### Why it is built and not pulled

Upstream publishes LMCache three ways and none of them covers this:

| | Covers gfx1250? |
|---|---|
| PyPI `lmcache` | No — CUDA wheels |
| the ROCm GitHub Release wheels | No — `gfx942,gfx950` fat binary only |
| `docker/Dockerfile.rocm` upstream | No — `ARG PYTORCH_ROCM_ARCH="gfx942,gfx950"` |

So the HIP kernels have to be compiled against the architecture:
`BUILD_WITH_HIP=1`, `CXX=hipcc`, `PYTORCH_ROCM_ARCH=gfx1250`.

### Why the base image is what it is

Both halves of `rocm/pytorch:rocm10.1.0_ubuntu24.04_py3.12_pytorch_release_2.14.0`
are forced, and both are worth knowing before anyone "simplifies" them.

**A PyTorch base, not `rocm/dev-ubuntu-*`.** Torch is needed at both ends:
`setup.py`'s HIP path compiles through `torch.utils.cpp_extension`, and
`lmcache/v1/metadata.py` and `cache_engine.py` import it at module scope, so
even the model-less worker needs it to start. It also cannot be installed —
ROCm 10.1 exists as a dev image (`rocm/dev-ubuntu-24.04:10.1.0-full`), but
there is no matching PyTorch wheel index:

```console
$ curl -o /dev/null -w '%{http_code}\n' https://download.pytorch.org/whl/rocm10.1/torch/
403
$ curl -o /dev/null -w '%{http_code}\n' https://download.pytorch.org/whl/rocm7.2/torch/
200
```

`rocm7.2` is the newest channel that exists. A base that already carries a
ROCm 10.1 torch is the only route to one.

**24.04, not 26.04.** `rocm/pytorch` does publish 26.04, but every ROCm 10.1 /
26.04 tag is `py3.14`, and LMCache 0.5.5's `pyproject.toml` pins
`requires-python = ">=3.10,<3.14"`. So 26.04 is ruled out by the interpreter,
not by preference — revisit when LMCache widens that pin. The container's
distro does not have to match the guest's, and does not.

`--no-build-isolation` is load-bearing for the same reason: isolation would
fetch `pyproject.toml`'s pinned `torch==2.13.0` from PyPI, which is a CUDA
build, and the HIP extensions would compile against the wrong headers and link
against a torch that is not the one at run time.

The build ends on `import lmcache.c_ops`, so a HIP build that silently fell
back fails here rather than eight guests later.

### What the image runs

No default entrypoint, because it has two jobs and naming one would make the
other look like an exception.

| Command | Where | What |
|---|---|---|
| `lmcache coordinator` | the compose stack's `lmcache-coordinator` | MP coordinator: membership, key directory, L2 quota and eviction, HTTP on `:9300` |
| `lmcache server` | inside each guest, via the `lmcache_guest` role | MP cache server: ZMQ on `:5555`, HTTP/metrics on `:9500`, P2P on `:9400` |

Both are upstream entrypoints. **This is MP mode**, and the distinction from
the legacy in-process mode decides everything else: `lmcache server` is a
standalone process that needs no vLLM, is configured entirely by CLI flags
rather than a YAML engine config, and registers with `lmcache coordinator`.

Do not pair it with `lmcache_controller`. That is the in-process mode's
controller -- it waits on a ZMQ pull/reply pair for `LMCacheWorker`
registrations that an MP server never sends, so the two come up cleanly and
every guest ends up registered with nothing.

The image carries two extras beyond LMCache itself:

- **openai**, which is not optional despite looking it. The `lmcache` CLI
  registers every subcommand's parser eagerly, and `lmcache bench` imports
  openai at registration time -- so without it `lmcache --help` dies and both
  entrypoints above are unreachable.
- **nixl**, the P2P transfer engine, and the only workable one here.
  Upstream's own `Dockerfile.rocm` skips it as "not yet available on ROCm";
  that is stale, it installs and its UCX backend instantiates on this base.
  The alternative, `mooncake_te`, is a CUDA build and fails with
  `libcudart.so.12: cannot open shared object file`.

### vLLM is not in this image

Deliberate. The fleet's GPU is rocjitsu, an emulated device served
single-threaded over vfio-user — loading a model through it is not a thing
that finishes. The MP server's L1 is host memory for the same reason the
stack declines to scrape the in-guest GPU exporters, and its L2 is the
emulated NVMe.

Adding vLLM later is a `uv pip install` against `wheels.vllm.ai/rocm101` in a
second stage, if and when that index exists and there is a real gfx1250 to
point it at.

### Size, and why it matters at fleet scale

**The image is ~38 GB.** Almost all of that is the ROCm 10.1 toolchain in the
base — LMCache itself is a few hundred MB of Python and four `.so`s.

That is fine for the coordinator, which is one container on the host. It is
*not* free for the workers: `lmcache_guest_image_archive` copies the tar into
each guest and `docker load` unpacks it there, so at `VM_COUNT=8` that is
~300 GB written into eight qcow2 overlays that all thin-provision off one
shared backing file. Check free space under `VM_IMAGES_DIR` before running the
fleet play, and prefer a registry pull if the guests can reach one.

The real fix is a second stage that copies the built wheel onto a runtime base
with no compiler — `rocm/dev-ubuntu-24.04:10.1.0-full` plus torch would do it,
if a ROCm 10.1 torch wheel ever publishes. Not done here because today there
is nothing to install torch *from*, which is the same constraint that picked
the base in the first place.

### Build-time knobs

| ARG | Default | Note |
|---|---|---|
| `BASE_IMAGE` | `rocm/pytorch:rocm10.1.0_ubuntu24.04_py3.12_pytorch_release_2.14.0` | see above before changing |
| `LMCACHE_VERSION` | `v0.5.5` | a tag, not a branch — `setuptools_scm` derives the version from it |
| `PYTORCH_ROCM_ARCH` | `gfx1250` | comma-separate for a fat binary |
| `MAX_JOBS` | `2` | `hipcc` is single-threaded per TU |

One known soft spot: `setup.py` selects `cupy-rocm-7-0` when
`BUILD_WITH_HIP=1`. That pin is upstream's and predates ROCm 10; it installs
and imports on this base, but it is the first place to look if a CuPy path
misbehaves.
