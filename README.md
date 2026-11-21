# QEMU and Libvirt Tooling

[![GitHub Stars](https://img.shields.io/github/stars/sbates130272/qemu-minimal?label=Stars&style=flat-square)](https://github.com/sbates130272/qemu-minimal/stargazers)
[![GitHub Issues](https://img.shields.io/github/issues/sbates130272/qemu-minimal?label=Issues&style=flat-square)](https://github.com/sbates130272/qemu-minimal/issues)
[![Last Commit](https://img.shields.io/github/last-commit/sbates130272/qemu-minimal?label=Last%20Commit&style=flat-square)](https://github.com/sbates130272/qemu-minimal/commits/main)
[![Platform](https://img.shields.io/badge/Platform-x86__64-blue?style=flat-square)](https://github.com/sbates130272/qemu-minimal)
[![Ubuntu](https://img.shields.io/badge/Ubuntu-Noble%20%7C%20Resolute-orange?style=flat-square&logo=ubuntu)](https://releases.ubuntu.com/noble/)
[![GitHub Release](https://img.shields.io/github/v/release/sbates130272/qemu-minimal?label=Release&style=flat-square)](https://github.com/sbates130272/qemu-minimal/releases/latest)

<!-- Reports (vm-* lanes pinned to main; date badge computed by Pages publish) -->
[![Basic Report](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-report-for-vm-basic.yml?branch=main&label=report-for-vm-basic&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-report-for-vm-basic.yml)
[![Rocm-golden Report](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-report-for-vm-rocm-golden.yml?branch=main&label=report-for-vm-rocm-golden&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-report-for-vm-rocm-golden.yml)

<!-- Performance endpoint badges: colour is a verdict from Pages publish JSON -->
[![Rocjitsu GEMM](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-gemm.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)
[![Rocjitsu HipFile](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)
[![HipFile Fio](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile-fio.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)
[![Ernic P4 FP8 BW](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-ernic-p4-bw.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)
[![SDXI copy BW](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-sdxi-bw.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)

<!-- CI lanes: smoke-test lanes pinned to main; PR-only lanes carry no branch pin -->
[![Qemu-tool Smoke Test](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-smoke-test-qemu-tool.yml?branch=main&label=qemu-tool-smoke&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-smoke-test-qemu-tool.yml)
[![Quickstart Boot](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-quickstart-boot.yml?label=quickstart-boot&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-quickstart-boot.yml)
[![Dry-Run Tests](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-dry-run-qemu-tool.yml?label=dry-run&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-dry-run-qemu-tool.yml)
[![Ansible Test](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-ansible-setup-test.yml?label=ansible&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-ansible-setup-test.yml)
[![Shell Check](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-shell-check.yml?label=shell-check&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-shell-check.yml)
[![Spell Check](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-spell-check.yml?label=spell-check&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-spell-check.yml)
[![Vm-rocm-golden Smoke Test](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-smoke-test-vm-rocm-golden.yml?branch=main&label=smoke-test-vm-rocm-golden&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-smoke-test-vm-rocm-golden.yml)

<!-- Ansible collections pinned in ansible/requirements.yml -->
[![Batesste Collection](https://img.shields.io/badge/sbates130272.batesste-3.4.0-9cf?style=flat-square)](https://galaxy.ansible.com/ui/repo/published/sbates130272/batesste/)
[![Rocm-ernic Collection](https://img.shields.io/badge/rocm.ernic-bd90234-9cf?style=flat-square)](https://github.com/ROCm/rocm-ernic/tree/bd902345b0f8f3863f535a0a09da9f21ce744737/ansible)

<!-- CI images pinned in qemu/stacks/rocm-golden.xml, and the guest its <backing-ref> pulls, which this repo publishes under its own stamp; QEMU and the ernic server are the sbates-fork pair in every shipped stack and every workflow, for P2P DMA between vfio-user devices -->
[![CI image build](https://img.shields.io/badge/ci--images-20261112.gdca6903-informational?style=flat-square&logo=docker)](https://hub.docker.com/u/sbates130272)
[![Qemu](https://img.shields.io/badge/qemu-11.1.3%20sbates--fork%20qemu.4cb9698-blue?style=flat-square)](https://hub.docker.com/r/sbates130272/batesste-ci-images-ubuntu-qemu-libvfio-user-sbates-fork/tags)
[![Libvfio-user](https://img.shields.io/badge/libvfio--user-3719c3a-blue?style=flat-square)](https://hub.docker.com/r/sbates130272/batesste-ci-images-ubuntu-qemu-libvfio-user-sbates-fork/tags)
[![Rocm-ernic](https://img.shields.io/badge/rocm--ernic-90434a2-blue?style=flat-square)](https://hub.docker.com/r/sbates130272/batesste-ci-images-ubuntu-rocm-ernic-sbates-fork/tags)
[![Rocjitsu](https://img.shields.io/badge/rocjitsu-50be05a-blue?style=flat-square)](https://hub.docker.com/r/sbates130272/batesste-ci-images-ubuntu-rocm-rocjitsu/tags)
[![Guest Qcow2](https://img.shields.io/badge/guest-20261119.g2aea67d-blue?style=flat-square)](https://hub.docker.com/r/sbates130272/qemu-tool-rocm-golden/tags)

<!-- What the rocm-golden guest is baked with: pins in ansible/playbooks/vm-rocm-golden.yml and <bake-release> in qemu/stacks/rocm-golden.xml -->
[![Golden Guest OS](https://img.shields.io/badge/rocm--golden-Ubuntu%20resolute-blueviolet?style=flat-square&logo=ubuntu)](qemu/stacks/rocm-golden.xml)
[![Golden Kernel](https://img.shields.io/badge/kernel-7.2.6%20mainline-blueviolet?style=flat-square&logo=linux)](https://kernel.ubuntu.com/mainline/v7.2.6/)
[![Golden ROCm](https://img.shields.io/badge/ROCm-10.2-blueviolet?style=flat-square)](ansible/playbooks/vm-rocm-golden.yml)
[![Golden amdgpu](https://img.shields.io/badge/amdgpu--dkms-31.70-blueviolet?style=flat-square)](ansible/playbooks/vm-rocm-golden.yml)
[![Golden ernic driver](https://img.shields.io/badge/ionic%20%2B%20SRQ%20%2B%20P4-90434a2-blueviolet?style=flat-square)](https://github.com/ROCm/rocm-ernic/commit/90434a237232275fc8002cb3a6b5121ba836c594)
[![Golden SDXI driver](https://img.shields.io/badge/sdxi-dfd76f2-blueviolet?style=flat-square)](https://github.com/ROCm/rocm-systems/commit/dfd76f23f6f10561333fc0a9a9f23e9f3b255c04)
[![Golden perftest](https://img.shields.io/badge/perftest%20%2B%20ROCm-e68c74e-blueviolet?style=flat-square)](https://github.com/sbates130272/perftest/commit/e68c74e910451682ba1dc91b96602fbc7afaa372)
[![Golden fio](https://img.shields.io/badge/fio--3.43%20%2B%20hipFile-d2dcd7e-blueviolet?style=flat-square)](https://github.com/axboe/fio/commit/d2dcd7e053d1902d2420f151c83990e6acdd9f8c)

[![Quick-start Build](https://img.shields.io/badge/quick--start-20261112.gdca6903-success?style=flat-square&logo=docker)](https://hub.docker.com/u/sbates130272)

## Summary

This repository provides a modern, cloud-init based
environment for running and testing QEMU-based and
Libvirt-based VMs. It's particularly well-suited for NVMe,
PCIe device passthrough, and libvfio-user testing, but can
be used for general-purpose VM creation for development and
testing.

**Key Features:**
- Fast VM creation using Ubuntu cloud images and cloud-init (Noble and Resolute)
- `qemu-tool` Python CLI with `run-vm`, `gen-vm`, `gen-compose`, `compose`,
  `list`, `inspect`, `down`, `check`, `ssh`, `man`, `qcow`, `publish-qcow`,
  `publish-qcow-prune`, `quick-start` and `ernic-stats` subcommands
- One settings format: fleet XML, layered with repeated `--xml`
- Bidirectional libvirt domain XML support (`--domain` input, `--convert-to-libvirt` output)
- NVMe device emulation with tracing support
- PCIe device passthrough (VFIO)
- CXL (Compute Express Link) device emulation
- Multi-architecture support (x86_64, ARM64, RISC-V)
- Declarative package management via manifests
- KVM acceleration support

## Quick Start: two GPU + RDMA VMs

The fastest way to something real: two guests, each with an emulated gfx1250,
an emulated NVMe, an emulated rocm-ernic RDMA NIC and an emulated SDXI device,
with Prometheus, Grafana and log collection beside them, plus a GPU test and
an RDMA test across them. The guest is pulled, not built — it comes from the
qcow2 this repository bakes and publishes, which already carries the mainline
kernel, the ionic driver with SRQ and P4, rdma-core, perftest, ROCm 10.2 with
the hipFile SDK, and the SDXI driver and its CLI — and the GPU, NIC and SDXI
servers come from tagged CI images. Nothing is built on your machine.

You need KVM, Docker with compose, Ansible, [`oras`](https://oras.land/) ≥ 1.3.4,
8 cores, 16 GB of RAM and about 30 GB of disk. Then:

```bash
uv tool install -e ./qemu                                  # or pipx, or the .deb -- see below
ansible-galaxy collection install -r ansible/requirements.yml  # needs git
qemu-tool check                                            # optional; see below
qemu-tool quick-start
```

`qemu-tool check` asks every one of those questions and a few more —
the Docker daemon and your access to it, the uid the containers will run as,
which settings file is in force and whether it is valid fleet XML, whether
the images directory has room — and
prints a line per answer. It writes nothing and needs no `sudo`, so it is
safe on a host you have not decided to use yet, and it exits non-zero if
anything would stop a stack starting. You do not have to run it: it is the
same code `quick-start` runs in its own preflight, so skipping it only means
finding out a few seconds later.

One command, and it sizes the guests for whatever machine you ran it on
rather than asking you to. It shows you the plan — vCPU, RAM, where the
images are going, how much disk that needs — and asks before it does
anything; anything it cannot work out on its own it asks for too. Pass
`--yes` to take every default, which is also what it does when stdin is not a
terminal, so nothing hangs in a script.

The daily
[report-for-vm-rocm-golden](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-report-for-vm-rocm-golden.yml)
lane runs that same command every morning, against the same fleet file. It:

1. checks this machine: KVM actually usable (the ioctl, not just the device
   node), the tools on `PATH`, a writable images directory, free disk, and
   nothing already sitting on the ports it wants;
2. pulls the guest image (~2 GB, unpacking to ~8.5 GB — the long step) and
   its SSH keypair, which
   ships beside it as an ORAS referrer;
3. creates two overlays of one backing file and generates a two-VM stack from
   [`qemu/stacks/rocm-golden.xml`](qemu/stacks/rocm-golden.xml);
4. brings the stack up — per guest a `rocm-ernic` NIC server, a `rocjitsu` GPU
   server, a `rocm-sdxi` server and a QEMU container, with the two NICs joined
   by a TCP mesh, plus Prometheus, Grafana, Loki and Alloy;
5. stages the per-build `ip_discovery.bin`, loads `amdgpu` and gives each guest
   its address on the ernic L2 segment;
6. runs `vector_add` on each guest's GPU and `ib_send_bw` from guest 1 to
   guest 2, then checks the NIC's own byte counter moved — a passing
   `ib_send_bw` proves the verbs worked, only the counter proves the bytes
   crossed the mesh.

Measured on a 32-core workstation: **2 m 47 s** end to end once the backing
image is cached, and a first run is that plus however long pulling the
~2 GB guest image and the container images takes you (about 3 minutes
here). When it finishes it prints how to get in,
which is:

```bash
qemu-tool ssh rocm-golden-1                 # port, account and key resolved for you
qemu-tool ssh rocm-golden-1 -- ibv_devinfo  # or run one command and come back

# the same thing spelled out, if you would rather
ssh -i /var/lib/qemu-tool/quickstart-key/id_rsa -p 2222 ubuntu@localhost
```

and where to watch it:

| Service | Where |
|---|---|
| Grafana | <http://localhost:3000> — anonymous admin, so no login |
| Prometheus | no host port: Grafana's Explore, or `docker exec quickstart-prometheus-1 wget -q -O- 'http://127.0.0.1:9090/api/v1/query?query=up'` |

The ernic dashboard is provisioned from files and is under **Dashboards** in
the Grafana sidebar; its data is the stats dump each `rocm-ernic` writes,
scraped by the exporter. Grafana's port moves automatically if something on
your machine already has it, and `quick-start` prints the one it chose.
`--no-metrics` leaves the pair out.

The home dashboard's **SDXI** row is each guest's SDXI device counters, and
the same numbers answer without Grafana — copy throughput per guest, in
bytes per second, is
`sum by (vm_name) (rate(sdxi_bytes_moved_total{context="1"}[2m]))` in place
of `up` in the Prometheus query above (URL-encode it, or paste it into
Explore).

Those panels are live counters, so they read zero while nothing is copying.
The optional tests are over in seconds and leave nothing a counter
shows, so each records its own result in the guest and the home dashboard
shows the last one that passed: the **P4** row is what `quick-start test --p4-test`
last measured on the first guest (`quickstart_p4_fp8_bytes_per_second`, the
fp8 result bytes per second, with the time per reduction, the size of the
run and how long ago it was), and the SDXI row's "SDXI Copy Verified" tile
is how long ago `--sdxi-test` last copied data through each guest's device
and found it identical (`quickstart_sdxi_copy_timestamp_seconds`). Both
read "no test run" until the test has passed once, and a later run that
fails leaves the last pass on the tiles: its growing age is the only sign.
The **hipFile** row, under the NVMe panels, is the same for
`quick-start test --hipfile-test`, per guest: the rate of the verified read
from the NVMe into GPU memory (`quickstart_hipfile_read_bytes_per_second`,
over `quickstart_hipfile_read_bytes` a pass), the rate of fio's `libhipfile`
engine (`quickstart_hipfile_fio_bytes_per_second`, "no fio result" on a
guest whose image has no such fio), and how long ago
(`quickstart_hipfile_timestamp_seconds`).

**The guests' SSH ports and Grafana answer on this machine only.** Every port
a stack publishes is bound to `<publish-bind-addr>`, which is `127.0.0.1`
unless a fleet file says otherwise. That holds on Docker Engine 28.0 and
later: earlier engines let a machine on the same network segment reach a
port published on loopback, and `qemu-tool check` warns when it finds one.
To use them from another machine, tunnel
in — `ssh -L 3000:localhost:3000 -L 2222:localhost:2222 <host>` — or put the
stack on the network with a one-element overlay:

```bash
cat > lan.xml <<'XML'
<fleet xmlns="http://qemu-tool/fleet/1.0">
  <publish-bind-addr>0.0.0.0</publish-bind-addr>
</fleet>
XML
qemu-tool quick-start --xml qemu/stacks/rocm-golden.xml --xml lan.xml
```

Know what that publishes first. The guest image is public, and so are both
ways into it: its SSH key ships beside it, and the password of its `ubuntu`
account is in the image's `vm-info.json`; the guest's sshd accepts either,
and the account has sudo. Grafana has anonymous admin. On `0.0.0.0`, anyone
who can reach the host has root in every guest and Grafana's admin. One
address of the host in place of `0.0.0.0` limits it to that network.

On WSL2 a Windows browser still reaches `http://localhost:3000`: Windows
connects to a listener on WSL's loopback, which was checked in mirrored
networking mode with a plain listener and is what `localhostForwarding`
does in NAT mode. A running stack has been checked from inside WSL, where
Grafana answers on `127.0.0.1` and on no other address of the host; it has
not been checked from a Windows browser.

Useful variations — `qemu-tool quick-start <verb>`:

| Verb | What it does |
|---|---|
| `preflight` | check this machine and touch nothing |
| `up` | stop once the guests are booted |
| `test` | configure and test a stack that is already up |
| `down` | power the guests off and stop the stack, keeping the overlays |

**The GPU needs `test` before it is usable: firmware first, then the
probe.** The guest image carries no `ip_discovery.bin` — it belongs to the
rocjitsu build serving the guest — and `amdgpu` cannot probe the GPU without
it. `quick-start` with no verb, and `quick-start test`, stage it from the
running rocjitsu and then load the driver. After `quick-start up` alone, or
on a stack started with `qemu-tool compose`, neither has happened: the
guest's login message says the GPU cannot be probed yet and names the host
command, and `sudo amdgpu-probe` in the guest refuses rather than load a
driver whose probe would fail and leave it resident with nothing bound.
`amdgpu-probe --status` prints which state a guest is in; `~/WELCOME.md` in
the guest lists them. That behaviour is in the image the fleet files pull,
`20261119.g2aea67d`, and in every image since `20261117.g481c892` — the one
published before that, `20261117.g80c17f8`, still tells you to run the
probe first.

Two more tests are behind switches. Both are off by default, run with
`test` or with no verb, and run in the guest as its login user with no
sudo:

| Switch | What it runs | A pass prints |
|---|---|---|
| `--sdxi-test` | on every guest, 4 MiB of random data copied through `/dev/sdxi0` with `sdxi-cli` and compared with its source | `rocm-golden-1: SDXI copy of 4194304 bytes verified`, one line a guest |
| `--p4-test` | on the first guest only, `p4_fp8_client` loads the fp8 P4 program into its NIC and checks one reduction, then `p4_fp8_bench` times 2000 of them (`--p4-iters` for another count) | `rocm-golden-1: P4 FP8 reduction <rate> MB/s (262144 elements x 2000 iterations)` |

```bash
qemu-tool quick-start test --sdxi-test --p4-test
```

Each result is also in its guest's summary under the stack's `artifacts/`
and in the report. A fleet that cannot run one is refused before anything
is started, naming the element to change: `--sdxi-test` needs
`<sdxi-per-vm>` of at least 1, and `--p4-test` needs `<p4-enable>true` and
`<p4-guest-scope>load`, the scope that lets a guest replace the program and
write a table entry. `rocm-golden.xml` has all three. The SDXI test is a
functional check with no figure. The P4 figure is the rate of the reduction
inside one NIC, in fp8 result bytes per second, with no peer guest and
nothing crossing the mesh; the development host measured about 195 MB/s
with 8-vCPU guests.
**The P4 program and its table entry stay in that NIC until its ernic
server restarts.** From then on an RDMA WRITE on it with two equal-length
operands is a reduction; ordinary transfers, which have one, are untouched.
A third switch, `--rocm-rdma-test`, is under
[RDMA with GPU memory](#rdma-with-gpu-memory) below, and a fourth,
`--hipfile-test`, under [NVMe to GPU memory](#nvme-to-gpu-memory). Both
need what the image the fleet files pull has had since `20261119.g2aea67d`:
the ROCm perftest, and fio with the hipFile engine. On a guest of an
older image the first stops at its image check and the second runs
without fio.

`qemu-tool quick-start --help` has the rest, and `qemu-tool man` has all of
it. For more than two guests see [A fleet of your own](#a-fleet-of-your-own),
which is the same stack generator with a larger `<vm-count>`.

### NVMe to GPU memory

hipFile reads a file on an NVMe namespace straight into GPU memory.
`quick-start --hipfile-test` does that on every guest and checks what
arrived. It is off by default:

```bash
qemu-tool quick-start test --hipfile-test
```

Two reads. A bench the test compiles in the guest with the image's `hipcc`
reads 32 MiB through hipFile, multiplies the two matrices in it on the GPU
and compares the product with one computed on the host. Then fio's
`libhipfile` engine reads a 64 MiB file. A pass prints one line a guest:

```
rocm-golden-1: hipFile read <rate> MB/s (33554432 bytes, verified); fio <rate> MB/s
```

- **fio is the one the golden image carries**, `/opt/fio-hipfile/bin/fio`:
  fio 3.43, the first release with the engine, built by the bake from a
  pinned commit (`fio_source_ref` and `fio_source_pin` in
  [`vm-rocm-golden.yml`](ansible/playbooks/vm-rocm-golden.yml), the
  [`fio_hipfile_setup`](ansible/playbooks/roles/fio_hipfile_setup/defaults/main.yml)
  role). It is on no PATH: a bare `fio` in a guest is still Ubuntu's, which
  has no such engine and is what the NVMe test above runs.
  **That is in an image baked from this checkout, not yet in the one the
  fleet files pull.** On a guest without it the line ends
  `fio: not in this image` and the test passes on the bench alone; nothing
  is built in the guest. `cat /etc/fio-hipfile.json` in a guest says
  whether it has it.
- **The NVMe namespace is scratch.** The test makes a new filesystem on
  `/dev/nvme0n1` every run, mounts it on a mount point of its own and
  unmounts it again whatever happens, so the NVMe test of the next run
  finds it free. A namespace that is already mounted is refused, not
  formatted. Root is taken for the filesystem, the mount and fio; the
  bench runs as the login user.
- **How fast depends on the stack.** `rocm-golden.xml` sets
  `<vm-nvme-vram>true`, which on the fork QEMU lets the emulated NVMe map
  the GPU's memory: measured by hand on the development host, 831 MB/s for
  the bench and 959 MB/s for fio. (fio's figure is from the build the
  benchmark script made in the guest, six commits before 3.43; none has
  been taken with 3.43.) Without the element, or on a QEMU that is
  not the fork, the read still works and passes, at 11 to 13 MB/s, and
  `quick-start` says so before it starts anything. The test asserts a
  verified read at a rate above zero, not a floor.
- A fleet with no NVMe (`<vm-nvme>0`) is refused before anything is
  started, and so is a flavor that runs no playbook. A guest with no bound
  GPU, no hipFile SDK or no `hipcc` is told which, by the test.
- Each result is in its guest's summary under the stack's `artifacts/`, in
  the report, and on the home dashboard's **hipFile** row.

**This test has run on guests by hand, and in no lane**: four runs on two
guests of a ROCm 10.2 image on the development host, both reads passing
each time. The bench and the fio command line are the ones the daily
benchmark lane runs.

### RDMA with GPU memory

Ubuntu's `perftest` is built without ROCm, so its `ib_send_bw` has no
`--use_rocm`. The golden bake builds perftest from source against the
guest's ROCm and installs it in `/usr/local/bin`, where it is what a bare
`ib_send_bw` runs; the archive's build stays in `/usr/bin`, and is what the
RDMA test above keeps running. The source is
[sbates130272/perftest](https://github.com/sbates130272/perftest) at a
pinned commit of `feat/rocm-data-validation-legacy`, which also gives
`ib_write_bw` and `ib_read_bw` `--data_validation` for GPU memory. The
repository, the ref and the commit are `perftest_source_repo`,
`perftest_source_ref` and `perftest_source_pin` in
[`vm-rocm-golden.yml`](ansible/playbooks/vm-rocm-golden.yml), and the
[`perftest_setup`](ansible/playbooks/roles/perftest_setup/defaults/main.yml)
role's defaults say what else can be set.

**That is in an image baked from this checkout, not yet in the one the
fleet files pull**, which was published before it: there `ib_send_bw` is the
archive's. `cat /etc/perftest-rocm.json` in a guest says which it has.

**`quick-start --rocm-rdma-test` runs it between the two guests, with every
byte checked.** Off by default, like the two switches above; before it
there was a play for this in `vm-quickstart.yml` that only a hand-typed
`ansible-playbook -e quickstart_rocm_rdma=true` reached. It runs
`ib_write_bw --use_rocm=0 --data_validation` from the first guest to the
last, with the buffers allocated through HIP on both sides, and passes when
the receiving side prints `VALIDATION: PASSED`:

```bash
qemu-tool quick-start test --rocm-rdma-test
```

```
==> rocm-golden-1 -> rocm-golden-2: ib_write_bw with ROCm buffers (host-alloc), <rate> MiB/s, 58720256 bytes in 7 chunks validated
```

By default the buffers are host memory the GPU can address
(`PERFTEST_ROCM_HOST_ALLOC=1`), not VRAM, the form that passes on any QEMU:
it proves HIP, the registration, the transfer and the validator, everything
but the NIC reaching the GPU's own memory. `--rocm-rdma-mode dmabuf` and
`--rocm-rdma-mode vram` put the buffers in VRAM. With both devices
emulated, the NIC reading GPU memory is one vfio-user server reaching into
another's BAR. The fork QEMU every shipped stack pins hands the NIC's
server that memory, so `dmabuf` passes there (measured on the default
fleet: 486 and 460 MiB/s through this switch, 535 MiB/s by hand, every
byte validated). Stock QEMU does not pass
it on: on a stack whose `<qemu-ref>` names it, `dmabuf` registers the
buffer and the first work request ends in `remote access error`, and
`quick-start` says so before it starts. `vram` is refused at registration
inside the guest (`Couldn't allocate MR with error=14`) whichever QEMU is
pinned. Either failure is reported as the known limitation of its mode,
with that line, and the run still exits non-zero. **It needs a guest
image that has the ROCm perftest**, which the image the fleet files pull
has since `20261119.g2aea67d`: on a guest of an older image the test stops at
its first check and says so. The result and both sides' output are in
`artifacts/rocm-rdma.md` under the stack and in the report. A fleet with
one guest is refused before anything is started. A run that is timed out
leaves the guests' RDMA devices needing the stack recreated. `AGENTS.md`
has what each run measured and what each failure means.

### Building against RDMA in the guest

The golden guest has two rdma-cores and one to build against. Ubuntu's
rdma-core 61 is there as a runtime under `/usr`: its udev rules and units,
and the libraries the archive's own tools are linked to.
[rocm-ernic][rocm-ernic]'s rdma-core 66, with the SRQ-capable ionic
provider, is installed from source under `/usr/local`; it is what every
tool loads, and it is the only one with headers and link libraries. So a
build takes no flags:

```bash
gcc prog.c -libverbs                     # headers, link and load: /usr/local
pkg-config --cflags --libs libibverbs    # -I/usr/local/include -L/usr/local/lib -libverbs
```

`libibverbs-dev`, `librdmacm-dev` and `libibumad-dev` are deliberately not
in the image, and the bake purges them from a base that has them. **Do not
install them in a golden guest.** With them the compiler reads the headers
in `/usr/local/include` and the linker takes the archive's library, which
is how perftest failed to link there (`undefined reference to
ibv_alloc_buf`). `libnl-3-dev` and `libnl-route-3-dev` stay. What went with
the three is the verbs API man pages and static linking against rdma-core.
`/etc/rocm-golden-guest.json` records both versions, as
`rdma_core_archive_version` and `rdma_core_local_version`, and
`qemu/checks/rocm-golden.sh` fails an image with a second rdma-core to
build against. A `vm-basic.yml` guest has no rdma-core in `/usr/local`;
there the archive's `-dev` packages are the right ones to install.

**This too is in an image baked from this checkout, not in the one the
fleet files pull**, which still has the three packages. It was measured by
purging them on a guest of that image and rebuilding everything that
compiles against verbs; no bake has run with the change.

## System-wide Install (Debian/Ubuntu)

Download the `.deb` from the [GitHub Releases](../../releases) page and install:

```bash
sudo apt install --no-install-recommends \
  ./python3-qemu-tool_*.deb \
  cloud-image-utils openssh-client wget
```

Use `apt` rather than `dpkg -i`: the package depends on a QEMU system emulator
and `qemu-utils`, and `dpkg` will not resolve those for you.

`--no-install-recommends` is worth the extra typing. Every `qemu-system-*`
package *recommends* `qemu-system-gui`, so a plain `apt install` drags in GTK,
SDL and the rest of a desktop display stack — 293 packages instead of 73 — none
of which a headless VM needs. No package can decline another package's
recommends, so the flag is the only way to say no. The three named packages are
what the flag would otherwise skip and `gen-vm` genuinely needs: `cloud-localds`,
`ssh` and `wget`.

Add `ansible` too if you intend to use `gen-vm --ansible-playbook`.

This installs `qemu-tool` to `/usr/bin/qemu-tool` and creates
`/var/lib/qemu-tool/images`. Where a `kvm` group exists — the normal case on a
host with QEMU installed — that directory is owned `root:kvm` with mode `2775`,
so the setgid bit keeps new images group-writable. Add yourself to the group if
you have not already:

```bash
sudo usermod -aG kvm $USER
# re-login for the group to take effect
```

On a system with no `kvm` group the install still succeeds, but the directory is
left `root`-owned and not group-writable, and the package says so. Once a `kvm`
group exists, apply the intended ownership yourself:

```bash
sudo chown root:kvm /var/lib/qemu-tool/images
sudo chmod 2775 /var/lib/qemu-tool/images
```

## Quick Start (qemu-tool)

Install from PyPI with [pipx](https://pipx.pypa.io/), which puts `qemu-tool` on
your `PATH` in its own isolated virtualenv:

```bash
sudo apt install -y pipx
pipx ensurepath          # adds ~/.local/bin to PATH; open a new shell after
pipx install qemu-tool
```

That gives you a working `gen-vm` and `run-vm`: both package manifests, the
four fleet files, the man page and the compose stacks all ship inside the
wheel, so none of those depend on a checkout.

`qemu-tool compose` is the exception. The stacks are there, but each one
bind-mounts a qemu-tool source tree into its container and builds the tool
inside it, and neither a wheel nor the `.deb` is a source tree. To bring a
stack up, point `QEMU_TOOL_SRC` at a checkout. It is one of the four variables
the stacks leave to `docker compose` to interpolate, so it is read from your
environment (or from `<qemu-tool-src>` in a fleet file):

```bash
QEMU_TOOL_SRC=/path/to/qemu-minimal qemu-tool compose --stack rocm-golden up -d
```

To work on the tool itself, install the checkout in editable mode instead, so
edits take effect without reinstalling:

```bash
pipx install -e ./qemu   # from the repo root
```

What a pipx install still does not get is the parts that are install-tree
artifacts by nature — `/etc/qemu-tool/fleet.xml`, `/var/lib/qemu-tool/images` and a
man page on your `MANPATH`. Those defaults fall back to per-user locations
rather than failing:

| Default | `.deb` | pipx / uv / venv |
|---|---|---|
| Settings file | `/etc/qemu-tool/fleet.xml` | `$XDG_CONFIG_HOME/qemu-tool/fleet.xml` |
| Fleet files | `/usr/share/qemu-tool/stacks/` | bundled in the wheel |
| Images directory | `/var/lib/qemu-tool/images` | `$XDG_DATA_HOME/qemu-tool/images` |
| Package manifest | `/usr/share/qemu-tool/packages-default` | bundled in the wheel |
| Compose stacks | `/usr/share/qemu-tool/compose/` | bundled in the wheel |
| Fleet XML schemas | `/usr/share/qemu-tool/schema/` | bundled; write one with `qemu-tool fleet-schema --output FILE` |
| Manual page | `/usr/share/man/man1/` | bundled; read it with `qemu-tool man` |

The manual page is the one of those a fallback path cannot fix. Neither pipx
nor uv has any mechanism for installing onto `MANPATH` — the page ships as
package data either way — so `man qemu-tool` works only from the `.deb`.
`qemu-tool man` opens the bundled copy regardless of how the tool was
installed, and `qemu-tool man --path` prints where it is.

A per-user settings file **beats** the system one, as XDG expects: if you
wrote `$XDG_CONFIG_HOME/qemu-tool/fleet.xml` under a pipx install and later
install the `.deb`, your file still wins and edits to
`/etc/qemu-tool/fleet.xml` are ignored. Delete the per-user copy, or pass `--xml`, to hand control back
to the system file. The images directory works the other way round but has
its own catch: the system path is preferred only when it is *writable*, so if
you are not in the `kvm` group you quietly get the per-user path instead of a
permission error. Pass `--images` to be explicit.

A plain virtualenv works too, if you prefer to activate it explicitly:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ./qemu
```

If a build fails with `Cannot update time stamp of directory
'qemu_tool.egg-info'`, a previous root-owned build left an artifact behind.
Delete the `qemu/qemu_tool.egg-info` directory as root and retry.

Generate and run a Noble VM:

```bash
qemu-tool gen-vm --vm-name myvm --release noble
qemu-tool run-vm --vm-name myvm
qemu-tool ssh myvm                          # from another terminal
```

On x86, `run-vm` also tells the guest its name: the VM name goes in the
SMBIOS system serial number (`sudo cat /sys/class/dmi/id/product_serial`),
and a guest made from the rocm-golden image sets its hostname from it at
boot, so `rocm-golden-1` on the host is `rocm-golden-1` inside. It takes
both halves. The guest half is in the image, from the next one published
after `20261117.g481c892`; a guest of an image without it names itself as
before. The host half is this `run-vm`, and in a stack the `run-vm` is the
one inside each qemu container: built from the source tree `QEMU_TOOL_SRC`
names when there is one, and otherwise installed from PyPI, where `2.0.0`
passes no name. So a stack run without a checkout needs the first qemu-tool
release after `2.0.0` as well as the new image.

`gen-vm` generates an SSH keypair for each image it bakes, keeps it in
`<images>/keys/<vm-name>_id_rsa` and prints the path; `qemu-tool ssh` finds
it. By hand that is `ssh -i <images>/keys/myvm_id_rsa -p 2222
ubuntu@localhost`. `run-vm` forwards host port 2222 unless told otherwise,
and QEMU does not start if that port is held; `run-vm --ssh-port auto` takes
the first free one and `qemu-tool ssh` finds it. Your own `~/.ssh` keys are not authorised unless you name
one: `gen-vm --ssh-key-file ~/.ssh/id_rsa.pub`, or `<vm-ssh-key-file>` in a
fleet file. An `--ansible-playbook` that runs the `user_setup` role is told
the image's key too, so it does not add `~/.ssh/id_rsa.pub` behind your
back. Baking again under the same name replaces the generated keypair.
An overlay made with `--backing-file` uses its backing image's key, and a
guest on a pulled `--backing-image` the keypair published with that image.

Generate and run a Resolute VM:

```bash
qemu-tool gen-vm --vm-name myvm --release resolute
qemu-tool run-vm --vm-name myvm
```

Convert options to a libvirt domain XML:

```bash
qemu-tool run-vm --vm-name myvm --nvme 2 --convert-to-libvirt myvm.xml
virsh define myvm.xml && virsh start myvm
```

## Quick Start (Libvirt)

```bash
./libvirt/virt-install-ubuntu
```

## Directory Layout

```
qemu-minimal/
  qemu/
    pyproject.toml  Python package manifest for qemu-tool
    qemu-tool/      Python package (qemu-tool CLI, imported as qemu_tool)
    packages.d/     Cloud-init package manifests
    stacks/         Fleet XML settings: rocm-golden.xml, helios-tray.xml, mi355x-server.xml, xio.xml
    compose/        The compose stack generated from each of those, same names
  docs/             The published site's pages (MkDocs); docs/api/ lists the modules of the Python API pages
  .mkdocs/          What the site build needs to read the package's docstrings
  libvirt/
    virt-install-ubuntu  Create VMs via libvirt
    create-nvme          Generate NVMe XML for libvirt
  ansible/
    playbooks/vm-basic.yml  Post cloud-init Ansible playbook
    requirements.yml        Galaxy collection requirements
  images/                VM images (created at runtime)
```

## VFIO PCI Passthrough

Bind the host device to `vfio-pci`, then pass its PCI address
to `run-vm` via `--pci-hostdev`. QEMU must be able to open the
IOMMU group device under `/dev/vfio/N`; by default those
nodes are root-only.

Install the udev rules once (requires sudo). Your user must
be in the `kvm` group:

```bash
./scripts/install-vfio-rules
```

Re-login after group changes. `qemu-tool run-vm` checks VFIO access
and prints this path if permissions are still wrong.

## Docker Compose: guests with emulated devices

For testing with emulated AMD devices over libvfio-user — a
[rocjitsu][rocjitsu] GPU, a [rocm-ernic][rocm-ernic] RDMA NIC and an SDXI
device — the Docker Compose stacks under `qemu/compose/` start the device
server containers and the `qemu-system` guests in one command. This is the
recommended path when you do not have a physical GPU to pass through but need
a guest that sees PCIe GPU devices.

There are four stacks. Each is **generated** by `qemu-tool gen-compose` from
the fleet XML file of the same name in `qemu/stacks/`, and committed so it
can be read in a diff and used with plain `docker compose`:

| Stack | Fleet file | Guests | Per guest |
|---|---|---|---|
| `rocm-golden` (the default) | [`qemu/stacks/rocm-golden.xml`](qemu/stacks/rocm-golden.xml) | 2 | 1 GPU, 1 NVMe, 1 RDMA NIC (P4 in guest scope), 1 SDXI |
| `helios-tray` | [`qemu/stacks/helios-tray.xml`](qemu/stacks/helios-tray.xml) | 1 | 4 GPUs, 8 RDMA NICs, 4 PCIe switches, 1 SDXI |
| `mi355x-server` | [`qemu/stacks/mi355x-server.xml`](qemu/stacks/mi355x-server.xml) | 4 | 8 GPUs, 8 RDMA NICs, 8 PCIe switches |
| `xio` | [`qemu/stacks/xio.xml`](qemu/stacks/xio.xml) | 1 | 2 GPUs, 2 RDMA NICs (loopback backend; NVMe-oF with a block and a KV namespace), 1 NVMe behind `pci-mmio-bridge`, 1 SPDK NVMe-KV controller |

`qemu-tool quick-start` is the easy way to run one (`--flavor default`, which
is the rocm-golden fleet, `helios`, `mi355x` or `xio`): it pulls the guest
image, creates the overlays, sizes the guests and tests them. `--flavor xio`
is the rocm-xio development guest. Its `test` verb stages the GPU firmware
and probes amdgpu; with `--xio-test` it also installs rocm-xio, which is not
in the guest image:

```bash
qemu-tool quick-start --flavor xio                  # boot the guest
qemu-tool quick-start --flavor xio test --xio-test  # install rocm-xio, check it
qemu-tool ssh xio-1 -- xio-tester --list-endpoints  # no sudo needed
```

`--xio-test` builds `xio-tester` and the `rocm-xio` kernel module at a pinned
commit, loads the module, lets the login user open `/dev/rocm-xio` through
the `rocm-xio` group, connects the guest to the NVMe-oF controller in its
second NIC, and checks for `/dev/rocm-xio`, `/dev/xio-kv`, `/dev/xio-blk`
and that controller's block and KV namespaces. By hand, any stack is:

```bash
# the guest overlays first: one pull, then overlays on that backing file
qemu-tool gen-vm --xml qemu/stacks/rocm-golden.xml --vm-name rocm-golden-1
qemu-tool gen-vm --xml qemu/stacks/rocm-golden.xml --vm-name rocm-golden-2 \
    --backing-file /var/lib/qemu-tool/images/rocm-golden-1-backing.qcow2

qemu-tool compose --stack rocm-golden --profile metrics up -d
qemu-tool ssh rocm-golden-1
qemu-tool down rocm-golden
```

`qemu-tool compose` is a wrapper around `docker compose`. It locates the
stack whether running from source, a wheel or the `.deb`, runs the containers
as you rather than as root, and passes everything else on the line to
`docker compose` in the order typed (`up`, `down`, `ps`, `logs ernic-1`, …).

### Plain `docker compose`

A generated stack has its fleet shape baked in at render time. What is left for
`docker compose` to interpolate is four variables, because they differ per
host and so cannot be committed:

| Variable | Meaning | If unset |
|---|---|---|
| `CONTAINER_USER` | `uid:gid` the containers run as | compose refuses, naming it |
| `CONTAINER_KVM_GID` | the gid that can open `/dev/kvm` | compose refuses, naming it |
| `VM_IMAGES_DIR` | directory holding the guest images | `/var/lib/qemu-tool/images` |
| `QEMU_TOOL_SRC` | qemu-tool source tree mounted into the qemu containers | `../../..` from the stack directory, which in a checkout is the checkout |

`qemu-tool compose` supplies all four through the environment of the
`docker compose` process it starts — the first two from your own ids, the
other two from `--images`, `<vm-images-dir>` and `<qemu-tool-src>` — and
never overrides one you have already exported. It writes no env file and
passes no `--env-file`. To drive a stack without the wrapper, export them:

```bash
cd qemu/compose/rocm-golden
CONTAINER_USER=$(id -u):$(id -g) \
CONTAINER_KVM_GID=$(stat -c %g /dev/kvm) docker compose up -d
```

These are the only settings spelled as environment variables. Everything else
is a fleet XML element.

### A fleet of your own

`qemu/stacks/rocm-golden.xml` is the template. For a fleet that differs from
it in a few values, write an overlay — a short fleet XML file naming only the
elements that change — and layer it with a second `--xml`. `--xml` is
repeatable and a later file overrides only the elements it sets:

```xml
<!-- my-fleet.xml -->
<fleet xmlns="http://qemu-tool/fleet/1.0">
  <vm-count>8</vm-count>
  <vm-name-prefix>mine</vm-name-prefix>
  <vm-nvme>2</vm-nvme>
</fleet>
```

```bash
qemu-tool gen-compose --xml qemu/stacks/rocm-golden.xml --xml my-fleet.xml \
    --output-dir /var/lib/qemu-tool/stacks/mine
qemu-tool compose --stack-dir /var/lib/qemu-tool/stacks/mine \
    --profile metrics up -d
```

No overlay is committed to this repository, and none should be: a fleet for
one host or one CI lane is a local file (the CI lanes write theirs inline in
the workflow), and anything every fleet wants belongs in the stack's own
file.

`<vm-nvme-vram>true</vm-nvme-vram>` points every emulated NVMe of a guest at
the BAR of that guest's first GPU, which is what makes hipFile's direct
NVMe-to-VRAM path fast: on the default fleet that path measured 11 to
13 MB/s without it and 831 and 959 MB/s with it, one run each. The likely
cause of the slow rate, inferred and not profiled, is QEMU copying each
read through a 4 KiB bounce buffer. It needs `<vm-nvme>` of at least 1 and
the fork QEMU; `rocm-golden.xml` sets it, and the other two shipped fleets
do not, since it has not been run behind a PCIe switch. An overlay on
`rocm-golden.xml` that removes the NVMe (`<vm-nvme>0`) is refused unless it
also sets `<vm-nvme-vram>false`.

`<vm-nvme-block-size>4096</vm-nvme-block-size>` gives every emulated NVMe
namespace of a guest 4096-byte blocks, where QEMU's default, and this
tool's, is 512. It sets the logical and the physical block size alike, and
is what an "LBA format" request comes to: the guest then sees the 4096-byte
format in use (`nvme id-ns` marks LBA Format 4, `Data Size: 4096 bytes`,
and `/sys/block/nvme0n1/queue/logical_block_size` reads 4096). The values
are `512` and `4096`; anything else is a parse error. It needs an emulated
NVMe (`<vm-nvme>` other than 0), and a fleet that leaves it out renders
exactly what it did before.

- **It is for QEMU's emulated NVMe and nothing else.** The other NVMe
  devices a fleet can have set their own block size, in their own
  `<devices>` entry. An SPDK controller's LBA namespaces are 4096 bytes
  unless the namespace says otherwise (`lba:malloc:512M:512` in
  `<namespaces>`), and its KV namespace has no blocks. An `nvmeof`
  backend's namespaces are 512 bytes unless its options say `bs=4096`
  (`<backend type='nvmeof'>size=1G,bs=4096</backend>`). Both read from the
  help of the pinned images, not run here.
- **Nothing on an existing scratch disk is converted or reformatted.** The
  NVMe qcow2 keeps its bytes when the block size changes, and what was
  written at the old size may not be readable at the new one. Seen on a
  guest, 512 to 4096: a GPT is not found, so the disk shows no partitions;
  XFS does not mount (`device supports 4096 byte sectors (not 512)`); ext4
  made with 1024-byte blocks does not mount (`bad block size 1024`); ext4
  with 4096-byte blocks, which is what `mkfs.ext4` made here, mounts. From
  4096 back to 512, ext4 and XFS both mounted and the GPT was again not
  found. `quick-start test` and `--hipfile-test` treat the namespace as
  scratch and write it afresh on every run, so neither minds.
  `run-vm --nvme-recreate` starts a guest on an empty disk.
- **No difference in the test rates was shown, and none was ruled out.**
  On the default fleet with one guest, in single runs (two a side for the
  hipFile reads, three for the NVMe test), the 4096 and the 512 figures
  overlapped in every row. That many runs cannot show a small difference
  if there is one: see the table in `AGENTS.md`.

Every later command against that fleet needs no flags, unless it starts
the fleet and the fleet's `<vm-images-dir>` or `<qemu-tool-src>` is not the
default (see below):

```bash
cd /var/lib/qemu-tool/stacks/mine && qemu-tool compose ps
# or, from anywhere:
export QEMU_TOOL_STACK=/var/lib/qemu-tool/stacks/mine
qemu-tool compose ps
```

A stack is found three ways, in this order: `--stack-dir`, then
`$QEMU_TOOL_STACK`, then the current directory when it is itself a rendered
stack — recognised by the generator's provenance header, not by the presence
of a `docker-compose.yml`, so an unrelated docker project is never adopted.
There is deliberately no walk-up: `docker compose` can walk up because what it
finds is what it runs, whereas here a walk-up from inside this checkout
reaches `qemu/compose/rocm-golden/` and would drive the committed two-guest stack
under the name of your fleet.

A rendered stack driven with no `--xml` is driven with no settings at all,
which is correct for everything baked into it. The one value it does not
bake in is the images directory: a fleet whose `<vm-images-dir>` is not
`/var/lib/qemu-tool/images` needs `--xml` (or `--images`, or `VM_IMAGES_DIR`
exported) on the commands that start it. `QEMU_TOOL_SRC` is the same kind of
value: unset, it is `../../..` from the stack directory, which is the
checkout only for `qemu/compose/<stack>/`. For a render anywhere else that is
an unrelated directory, and the qemu containers then install qemu-tool from
PyPI rather than from your checkout, so set `<qemu-tool-src>` to an absolute
path and pass `--xml`, or export `QEMU_TOOL_SRC`. A `.env` in the stack
directory is read by `docker compose` only when no settings are in force.

A named stack is the other way round: `qemu-tool compose --stack NAME` with
no `--xml` reads the first `fleet.xml` on the search path below, else the
stack's own `qemu/stacks/NAME.xml`, for those two values, and for the stale
check when it is the stack's own file. Layers that are not the stack's own
file are a different fleet; given explicitly they get a warning that points
at `gen-compose --output-dir` and `compose --stack-dir`.

An overlay render needs both directory flags, and omitting either fails
quietly. `gen-compose` defaults its output to the committed stack named by
the first `--xml` file, so without `--output-dir` the overlay's fleet is
written over `qemu/compose/rocm-golden` and `Gen Compose Check` fails on the next
push. And the guest count and names are baked into the generated YAML rather
than read from the settings at run time, so without `--stack-dir` (or one of
the other two ways above) `compose` brings up the committed two-guest stack
whatever layers it is given.

Three elements take a per-guest comma-list: `<vm-shm-size>`,
`<rocjitsu-config>` and `<vm-filesystem>`. Each distinct `<vm-filesystem>`
share is bind-mounted into the qemu containers once. A bare value broadcasts to every guest, a list of length
`<vm-count>` applies positionally, and any other length is an error naming the
element and both lengths.

`<vm-extra-hostfwd>` is the exception, and getting it backwards fails
silently: its commas separate several rules belonging to **one** guest, so
the whole value goes to **every** guest.
`<vm-extra-hostfwd>a,b</vm-extra-hostfwd>` is two hostfwd rules per guest,
not one rule each for two guests.

`<vm-qemu-args>` passes extra arguments to every guest's QEMU, written as
you would type them in a shell:

```xml
<vm-qemu-args>-device pci-testdev -d guest_errors -D /tmp/qemu.log</vm-qemu-args>
```

Quotes group and nothing is expanded. The words go on the end of the QEMU
command line `run-vm` builds, ahead of the guest's name. Nothing checks
them: a word QEMU refuses stops the guest when it starts, and the reason is
in that guest's log (`qemu-tool compose logs qemu-1`). An argument that
repeats one `run-vm` already writes is QEMU's to resolve, and for most
options the last one wins. `run-vm --qemu-arg=WORD`, repeatable, is the same
thing for one guest outside a stack.

**A guest is an AMD machine on every host.** `run-vm` starts an amd64 guest
with `-cpu EPYC,vendor=AuthenticAMD`. The vendor is stated because under KVM
QEMU gives every CPU model the host's CPUID vendor: on an Intel host a bare
`-cpu EPYC` guest reads `GenuineIntel` with family 23, and the guest kernel
then refuses peer-to-peer DMA between devices that share no upstream bridge
("cannot be used for peer-to-peer DMA as the client and provider (...) do
not share an upstream bridge or whitelisted host bridge", where the
parentheses hold the provider's PCI address). Seen on a guest
given the `GenuineIntel` vendor on an AMD host: hipFile's direct path falls
back and reads at about a twelfth of the rate, still verified; a direct NVMe
read into GPU peer-to-peer pages fails with that message; the dma-buf RDMA
test still passes. `<vm-cpu-vendor>` in a fleet file, or
`run-vm --cpu-vendor`, changes it:

```xml
<vm-cpu-vendor>GenuineIntel</vm-cpu-vendor>   <!-- twelve characters -->
<vm-cpu-vendor/>                              <!-- no vendor stated: the host's -->
```

An empty element is not the default. A render or a `run-vm` with anything
but `AuthenticAMD` prints a note saying the guest is not presented as AMD.
Any other CPU property goes in a whole `-cpu` in `<vm-qemu-args>`: QEMU
takes the last `-cpu` on its command line and nothing of an earlier one
(checked on QEMU 10.2.1 and on the pinned fork), so that `-cpu` has to
restate the vendor and any `phys-bits`, for example
`<vm-qemu-args>-cpu EPYC-Milan,vendor=AuthenticAMD,phys-bits=42</vm-qemu-args>`.
Keep it an AMD model of family 23 or later (`EPYC`, `EPYC-Rome`,
`EPYC-Milan`, `EPYC-Genoa`): the guest kernel asks for the family as well
as the vendor, and `-cpu host` or `-cpu max` take the host's family. Read
from source and not run: on an Intel host `-cpu host,vendor=AuthenticAMD`
is family 6 and is refused peer-to-peer DMA all the same, and it also
turns on KVM paravirtual features whose hypercalls an AMD-vendor guest
cannot make on Intel hardware, so expect such a guest not to boot.
Not yet run on an Intel host: the `quickstart-boot` check prints the host's
vendor and asserts the guest's, and its first run on an Intel runner is the
proof. On an AMD runner the check passes with or without this and says so
in its summary ("what this run shows"). Read from the guest kernel's source
and not run: on Intel hardware an AMD-vendor guest kernel treats itself as
not affected by the Intel-only CPU vulnerabilities (Meltdown, L1TF, MDS,
MMIO stale data, BHI) and does not apply those mitigations inside the
guest, and applies the AMD family 23 ones instead, so benchmark figures
from an Intel host are not comparable with earlier ones from it. Also read
and not run: an AMD-vendor guest gets QEMU's AMD memory map, with a reserved
range just below 1 TiB, so on an Intel host a fleet of your own whose
`<vm-pci-hole64-size>` reaches 1 TiB may now need a larger
`<vm-cpu-phys-bits>` (QEMU says `phys-bits too low` at start); and `modprobe
ptp_kvm` in a guest on an Intel host is expected to fault. A domain from
`run-vm --convert-to-libvirt` has the host's CPU whatever these say, and
says so on stderr.

`<vm-vcpus>` must not drop below 4 on a stack with an ernic. `ionic_lif_size()`
derives its EQ count from `num_online_cpus()` and `ionic_create_rdma_admin()`
rejects fewer than `IONIC_EQ_COUNT_MIN`, so `ionic_rdma` never probes; the only
symptom is "Failed to register ibdev" in the guest log.

The generated `docker-compose.yml` records the hash of the fleet XML layers it
was built from, and `qemu-tool compose --xml ...` warns on stderr when they
disagree. It never blocks: `down` against a stale tree is how you recover from
a bad edit. `gen-compose --check` turns the same comparison into a CI gate.

With `--profile metrics` the stack also brings up Prometheus and
`qemu-tool ernic-stats`, which serves the per-QP counters each `rocm-ernic -S`
writes. Full detail in [the stack README](qemu/compose/rocm-golden/README.md).

### Settings file

Fleet XML is the only settings format: one `<fleet>` element in the namespace
`http://qemu-tool/fleet/1.0`, whose children each hold one setting.
[`qemu/stacks/rocm-golden.xml`](qemu/stacks/rocm-golden.xml) is the annotated
reference; copy it, or layer an overlay on it. Every subcommand that reads
settings takes it as `--xml`. qemu-tool reads no env-format (`KEY=value`)
file anywhere.

The parser is strict. A misspelt element, an element given twice in one file,
an attribute, a nested element or stray text under `<fleet>` is an error
naming the file and the element, with the closest known name for a typo:

```
Error: my-fleet.xml: unknown element <vm-vcpu>; did you mean <vm-vcpus>?
```

Comments are allowed. `qemu-tool check --xml my-fleet.xml` reports the same
problems without stopping at the first.

Ten elements the format used to have are refused too, with an error that
says so and suggests no setting in its place. Seven were parsed and read by
no command, so deleting one from a file changes nothing: `<qemu-digest>`,
`<ernic-digest>`, `<rocjitsu-digest>`, `<backing-digest>`,
`<domain-pattern>`, `<prom-port>` and `<loki-port>`.

```
Error: my-fleet.xml: <prom-port> was removed from the fleet format: no command ever read it; Prometheus has no host port and is asked inside its container. Delete the element.
```

Three were read, and their errors say what to do instead:

- `<vm-backing-shared>` turned image locking off for a `run-vm` given
  `--xml`, or finding a `fleet.xml` on the search path. A generated
  stack always starts its guests with locking off, whatever the element
  said, so a stack is unchanged. A `run-vm` outside a stack that shares a
  backing file now needs `--backing-shared` on its command line.
- `<ernic-subnet>` and `<ernic-mac-prefix>` are fixed at `192.168.100` and
  `72:6f:63:6d`. The guest image addresses its NICs on that network at boot
  and the quick-start playbook finds a NIC by that prefix, so no other
  value gave a working fleet. A file that set either to its default
  deletes the element and renders as before.

```
Error: my-fleet.xml: <vm-backing-shared> was removed from the fleet format: a generated stack always starts its guests with image locking off, whatever this said; for a run-vm outside a stack, pass --backing-shared. Delete the element.
```

A misspelling of a removed element is told which one it looks like, and why
that one went, not the nearest setting:

```
Error: my-fleet.xml: unknown element <prom-prot>; it looks like <prom-port>, which was removed from the fleet format: no command ever read it; Prometheus has no host port and is asked inside its container. Delete the element.
```

A removed element set more than once is reported once; a misspelling is
reported once per copy, like any other unknown element.

A guest image is pinned by digest in `<backing-ref>` itself, as
`name@sha256:...`.

Every element, with its kind, its default and what it is for, is in the
[fleet XML reference](docs/fleet-xml.md), which is also a page of the
[site](https://sbates130272.github.io/qemu-minimal/fleet-xml/). It is
generated from the parser's tables, and so are two schemas for editors and
for validators that do not have `qemu-tool`: RELAX NG,
[`qemu/schema/fleet.rng`](qemu/schema/fleet.rng), and XML Schema (XSD 1.0),
[`qemu/schema/fleet.xsd`](qemu/schema/fleet.xsd). The `.deb` installs both
under `/usr/share/qemu-tool/schema/`.

```bash
qemu-tool fleet-schema --output fleet.rng       # or --format md for the page
xmllint --noout --relaxng fleet.rng my-fleet.xml

qemu-tool fleet-schema --format xsd --output fleet.xsd
xmllint --noout --schema fleet.xsd my-fleet.xml
```

The parser stays the authority. Neither schema rejects a file the parser
accepts, and both accept some the parser refuses: a schema describes one
file, so it knows nothing of layers, and it cannot see two `<server>`
entries with one id, a count that disagrees with a `<devices>` block, text
between elements, or any rule `gen-compose` holds between elements. The
reference lists them.

Use the RELAX NG schema where the tool takes one: it is the precise one. The
XSD is for an editor or a tool that takes only XSD. It accepts everything
the RELAX NG schema accepts, and three things besides, which XSD 1.0 cannot
refuse: a `<server>` holding the children of another server type than its
own (still never of two types at once), option text in
`<backend type='tcp'>`, and three attributes of XML Schema's own:
`xsi:schemaLocation` and `xsi:noNamespaceSchemaLocation` on any element, and
`xsi:type` on a flat setting when it names the type the setting already has.
The parser refuses all three, so a fleet file
cannot name its XSD in an attribute: associate the two in the editor's
settings (`xml.fileAssociations` for the XML extension of VS Code), or with
an `xml-model` processing instruction before the root element. The
[reference](docs/fleet-xml.md#editors) shows both.

#### The `<devices>` block

One element has structure. `<devices>` lists the vfio-user servers every
guest gets, one `<server>` entry each, and is how two servers of one type are
given different settings:

```xml
<devices>
  <server type='ernic' id='kv'>
    <backend type='nvmeof'>kv=1,size=256M</backend>
    <log-level>info</log-level>
  </server>
  <server type='ernic' id='mesh'/>
  <server type='rocjitsu' id='gpu0'>
    <cpu-thread-budget>2</cpu-thread-budget>
  </server>
  <server type='rocjitsu' id='gpu1'>
    <config>gfx1250_mi455x.json</config>
  </server>
  <server type='sdxi' id='sdxi0'/>
</devices>
```

- **`type`** is `ernic`, `rocjitsu`, `sdxi` or `spdk`: an ernic NIC, a
  rocjitsu GPU, an SDXI device or an SPDK NVMe controller. **`id`** names the entry: it
  is what a later layer and an error message refer to. Containers keep the
  names the generator gives them (`ernic-1-2` is guest 1's second NIC).
- **The block replaces the counts.** With it, `<ernic-per-vm>`,
  `<rocjitsu-per-vm>` and `<sdxi-per-vm>` are what the block lists. One of
  those set beside the block, or in a later layer, to a different number is
  an error. Without a block the counts apply as before.
- **Each other child is one command-line option** of that server, named
  after the long option: `<eth-queues>4</eth-queues>` is `--eth-queues 4`.
  The options are a fixed list per type (below); the ones the generator
  derives, such as the socket and the MAC, are not in it.
- **`<backend>`** sets an ernic's RDMA backend: `none`, `loopback`, `tcp`,
  `nvmeof` or `s3`, with the backend's own options as the element's text. A
  NIC with no `<backend>`, or `tcp`, is a node of the TCP mesh, which the
  generator derives. Any other backend takes that NIC out of the mesh.
- **`<config>`** names a rocjitsu config file, in place of
  `<rocjitsu-config>` for that GPU.
- **Layers merge by `id`.** A later `--xml` file's entry with the same id
  changes what it names and keeps the rest; a new id adds a device. A layer
  cannot remove one.

| Type | Option elements |
|---|---|
| `ernic` | `<log-level>`, `<nic-profile>` (repeatable), `<stats-interval-ms>`, `<eth-queues>`, `<inject>` (repeatable) |
| `rocjitsu` | `<node>`, `<cpu-thread-budget>`, `<wait-checking>` |
| `sdxi` | `<verbose/>` (repeatable) |
| `spdk` | `<namespaces>`, `<serial>`, `<cpumask>` |

A `spdk` entry is an SPDK vfio-user NVMe controller, run from `<spdk-image>`.
Its two options set the image's `NVME_NAMESPACES` (for example `kv:mem`, one
in-memory NVMe-KV namespace) and the controller's serial number;
`<cpumask>` sets the cores its poll-mode reactor spins on (`0x1`, host CPU 0,
by default: one full core per server). A guest takes one, and the image's
`vfu.<sha>` has to be the `<qemu-ref>`'s. `<vm-pci-mmio-bridge>true</vm-pci-mmio-bridge>`, a flat setting
beside the block, gives each guest the fork QEMU's `pci-mmio-bridge` device
for its emulated NVMe; it needs `<vm-nvme>` of at least 1.

Every guest of a fleet gets the same devices, in the generator's order (NICs,
then GPUs, or grouped under PCIe switches), not the order of the block. On a
fleet of more than one guest, `quick-start test` needs each guest's first NIC
on the mesh, and refuses to start otherwise.

`gen-vm` and `run-vm` take the per-guest elements as the defaults of their
flags — `<vm-vcpus>` is `--vcpus`, `<vm-images-dir>` is `--images`,
`<vm-nvme-block-size>` is `run-vm --nvme-block-size` (512 or 4096, the block
size of QEMU's emulated NVMe; see "A fleet of your own" above),
`<backing-ref>` is `--backing-image`. Values are read lowest to highest from
the built-in defaults, a `--domain` XML, the fleet XML, then explicit CLI
flags, so a flag always wins over the file. One-shot actions (`--dry-run`,
`--force`, `--restore-image`, `--ansible-only`, `--nvme-recreate`,
`--cache-replace`) are flags only. `qemu-tool man` lists every element.

With no `--xml`, the first of these that exists is used, alone:

| Location | Used for |
|---|---|
| `$QEMU_TOOL_FLEET` | explicit, per shell |
| `./fleet.xml` | a per-project file |
| `qemu/stacks/fleet.xml` | a source checkout (git-ignored) |
| `$XDG_CONFIG_HOME/qemu-tool/fleet.xml` | a pipx or venv install (`~/.config` by default) |
| `/etc/qemu-tool/fleet.xml` | a site-wide file an admin creates |

Nothing creates any of these for you. The `.deb` makes `/etc/qemu-tool/` and
leaves it empty on purpose: a file there is every bare command's settings, so
a copy of `rocm-golden.xml` would have a plain `gen-vm` pull that fleet's
`<backing-ref>` instead of baking, and a plain `run-vm` take its NVMe and its
four fixed port forwards, which a second guest on the host cannot bind. Copy
`/usr/share/qemu-tool/stacks/rocm-golden.xml` there and trim it if the site wants
one.

With none, the built-in defaults apply. Four commands differ: `gen-compose`
and `compose --stack NAME` fall back to the stack's own file in
`qemu/stacks/`, `quick-start` does not search and uses its flavor's file, and
`publish-qcow` reads no settings file unless `--xml` names one.

A `~` in a path element (`<vm-images-dir>`, `<vm-ssh-key-file>`, the data
directories, `<bake-packages>`, `<bake-playbook>`, `<bake-cache-dir>`) is
expanded when the file
is parsed, so every command and `docker compose` see the same path.

### Baking and publishing the guest image

`rocm-golden.xml` also says how the image its guests boot from is made, in its
`<bake-*>` and `<publish-*>` elements. A plain `gen-vm` reads none of them — it
pulls the image named by `<backing-ref>`, which is a dated tag of the
repository `<publish-ref>` names: the guest a fleet boots is one those
elements built and the publish workflow qualified. `<backing-ref>` does not
follow a publish by itself; it is moved by hand to a newer dated tag, in all
four fleet files, and `gen-compose` refuses `latest` there because a tag
that moves would leave each host on whichever image it pulled first. From
the checkout root, because `<bake-packages>` and `<bake-playbook>` are
resolved against the current directory:

```bash
qemu-tool gen-vm --bake --xml qemu/stacks/rocm-golden.xml   # bake it from the cloud image
qemu-tool publish-qcow --xml qemu/stacks/rocm-golden.xml \
    --tag "$(date +%Y%m%d).g$(git rev-parse --short=7 HEAD)" --dry-run
```

The keypair is published with the image, private half included, so neither
command takes a key by default: the bake generates one for the image and
`publish-qcow` publishes that one, saying which. A bake given
`--ssh-key-file` (or a fleet file with `<vm-ssh-key-file>`) authorises that
key instead, and `publish-qcow` then stops until `--key` names its private
half; it never takes one from a settings file or from `~/.ssh`. The record
it goes by is the one beside the image file, whatever `--vm-name` says, and
a generated key is only ever taken from `<images>/keys/`, where it has to
match the fingerprint the bake recorded. It also refuses a `--key` that is
not the one `gen-vm` recorded baking in, a private key that is not its
`.pub`'s other half, and an image `gen-vm` pulled rather than baked. A bake leaves out the fleet's `<vm-extra-hostfwd>`, because it runs
QEMU on the host, where a forward whose port is taken stops it starting.

The baked image carries one kernel, the pinned mainline one. The Ubuntu
release kernel the cloud image came with is purged at the end of the bake —
about 370 MB, and the entry GRUB would otherwise fall back to, a kernel none
of the image's modules were built for — and GRUB is told to boot the pinned
kernel by name (`GRUB_TOP_LEVEL` in
`/etc/default/grub.d/99-rocm-ernic-kernel.cfg`) rather than because it sorts
first. `qemu/checks/rocm-golden.sh` asserts both on the booted guest.

**A compiler cache for local bakes.** About fourteen minutes of a bake is the
plays that build kernel modules, and their sources move only when a pin does.
`--cache-dir` names a directory that carries a ccache between bakes:

```bash
qemu-tool gen-vm --bake --xml qemu/stacks/rocm-golden.xml \
    --cache-dir ~/.cache/qemu-tool/bake
```

`gen-vm` creates the directory and hands it to the playbook, which unpacks
`ccache.tar` from it into the guest before the first build, saves it back
after the last, and leaves nothing of the cache in the image. The same can be
set once in an overlay of your own as `<bake-cache-dir>`; the flag wins.
`--cache-replace` says the cache there was saved for other pins and is to be
saved again however little this bake missed. Nothing in the environment names
a cache. `rocm-golden.xml` sets no `<bake-cache-dir>`: a directory is a fact
about one host, so it is a flag or an overlay of your own.

**The published image is baked with a cache too.** The publish workflow and
the playbook-test lane both pass `--cache-dir` and share one cache through
GitHub's: the publish workflow saves on `main`, and a pull request reads
what `main` saved. A kernel module in a published image may therefore be
linked from objects an earlier run on `main` compiled; ccache reuses an
object only for the same compiler, flags and source. The playbook takes
ccache back out of the guest, `qemu/checks/rocm-golden.sh` fails an image
that still carries it, and the publish workflow runs that check before it
pushes. Measured on GitHub's runners, 2026-11-17: the publish workflow's
bake step took 18m25s with the cache against 25m08s without it that morning,
and a pull request's lane run starting from the entry `main` held baked in
13m26s against 30m42s cold. Every warm bake so far has still missed 235 to
265 of its 2053 cacheable compiler calls, and a bake does not save the cache
again for that few.

**What the publish workflow pushes, and what it later deletes.** Each publish
is one manifest under three tags, in this order:

| Tag | What it is |
|---|---|
| `<YYYYMMDD>.g<sha7>` | The dated tag consumers pin. First, because the keypair referrer attaches to the first tag. |
| `latest` | Rolls to every publish. |
| `qm.<sha7>` | The qemu-minimal commit alone; rolls when the same commit is published again. |

After a push the workflow runs `qemu-tool publish-qcow-prune --yes`, which
keeps the 10 newest dated tags and any dated tag pushed within the last 30
days, and deletes the other dated tags. `latest`, every `qm.*` and any tag
that is not exactly of the dated form are never deleted. A tag name is all
that goes: the image it named, and the `vm-info.json` and keypair attached
to that image, stay in the registry, still reachable by `qm.<sha7>`. Run
without `--yes` it prints the plan and deletes nothing:

```bash
qemu-tool publish-qcow-prune --xml qemu/stacks/rocm-golden.xml
```

It deletes through the Docker Hub API, so it refuses a repository on any
other registry, and it reads `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` from
the environment — credentials, not settings, and never flags. The token
needs delete access as well as read and write. If the prune fails, the
publish run stays green and carries a warning: the image is already out, and
re-running a failed publish would bake and push it again.

The bake boots the guest on the host to run its playbook, with a host port
forwarded to the guest's sshd, and QEMU does not start if that port is held
— by another guest, or on WSL2 by something on the Windows side.
`rocm-golden.xml` therefore sets `<bake-ssh-port>auto</bake-ssh-port>`, the
bake's `--ssh-port`: the first port from 2222 up that can actually be bound
is used and printed, and if it is taken before QEMU binds it the boot is
started again on the next one. The guest's MAC does not move with the port.
`--ssh-port <number>` on the command line pins it. `auto` is for a single
guest run on the host, by `gen-vm` or `run-vm`; a fleet's `<ssh-port-base>`
takes a number only, because a stack's published ports are written into its
generated compose file, and `quick-start` already moves them off a taken
port at run time.

## Images Directory

When installed system-wide via the `.deb` package, `gen-vm` and `run-vm`
default to `/var/lib/qemu-tool/images` for storing VM disk images.
The directory is created by the package installer. Where a `kvm` group
exists it is given `root:kvm` ownership and mode `2775` (setgid), so any
member of that group can read and write images without `sudo`. On a host
with no `kvm` group the installer leaves it `root:root` and mode `755`
and prints the two commands to run once the group appears.

When using a source checkout (`pipx install -e ./qemu`), override the
default with `--images`:

```bash
qemu-tool gen-vm --vm-name myvm --images ./images
```

Cloud images are downloaded into the images directory, and the generated
qcow2 files (backing and final) are stored there.

## Package Manifests

The `packages.d/` directory contains YAML-formatted package
lists consumed by cloud-init during VM creation. Two
manifests are provided:

- **packages-default** -- a broad set of development and
  debugging packages. Installed to `/usr/share/qemu-tool/packages-default`
  by the `.deb` package and used as the default.
- **packages-minimal** -- a smaller set with just `emacs-nox`,
  `fio`, `sysstat`, and `tree`. Installed to
  `/usr/share/qemu-tool/packages-minimal` by the `.deb` package.

Both also ship inside the wheel, so a pipx install resolves the default
manifest without a checkout or a system install. `/usr/share/qemu-tool` wins
when it exists, so editing the installed copy still takes effect.

Select a manifest via the `--packages` flag:

```bash
# system install — use the installed minimal manifest or a custom path
qemu-tool gen-vm --packages /usr/share/qemu-tool/packages-minimal

# source checkout
qemu-tool gen-vm --packages qemu/packages.d/packages-minimal
```

Set `--packages none` to skip package installation entirely.

## Ansible Post-Setup

After cloud-init first boot, `gen-vm` can optionally run an
Ansible playbook from the top-level `ansible/` directory against
the backing image. This installs roles from the
[sbates130272.batesste][batesste-galaxy] Galaxy collection
(user setup, favourite packages, git configuration, and more).

The host must have `ansible`, `ansible-galaxy`, and the Python
`jmespath` module for the same interpreter as `ansible-playbook`
(install with `pip install jmespath` inside the venv when needed). When the collection is not already installed, `gen-vm` runs
`ansible-galaxy collection install --no-deps -r ansible/requirements.yml`.
`requirements.yml` lists every collection the playbooks need, including the
ones that would otherwise arrive as transitive dependencies, so `--no-deps`
skips roughly 10 MiB of collections this repository never uses.

One exception: `rocm.ernic` is installed from the upstream
[rocm-ernic][rocm-ernic] git tree at a pinned SHA, not from Galaxy, where
upstream no longer publishes it. That pin is the roles. The sources the
guest's half of the NIC is built from are a second pin, `ernic_source_pin` in
`vm-rocm-golden.yml`, which moves with the `ernic.<sha>` of the server images
and need not be the same commit; the badges above show both.

### Available playbooks

There are two image playbooks. `vm-basic.yml` is a complete bake on its own
and the base of the other; `vm-rocm-golden.yml` imports it and adds everything
else, so the shared work has exactly one definition.

| Playbook | Description |
|---------|-------------|
| `vm-basic.yml` | User setup, favourite packages, git config |
| `vm-rocm-golden.yml` | `vm-basic.yml`, plus the pinned mainline kernel, ROCm, the rocjitsu GPU firmware and patched amdgpu driver, the [rocm-ernic][rocm-ernic] RDMA NIC with SRQ and P4, SDXI, and perftest built with ROCm memory support. `--tags configure` sets up the NIC on a live guest and `--tags test` runs the GPU workloads; see [the live stages](#the-live-stages) |

Each bake ends with one slim pass, which deletes the scratch the bake left
behind — the apt cache and package lists, DKMS build trees, archived journals,
`/tmp` and `/var/tmp` — and then `fstrim`s it back out of the qcow2.
`vm-rocm-golden.yml` passes `image_slim_enable: false` when it imports
`vm-basic.yml`, so the pass runs once, after everything that builds.

```bash
qemu-tool gen-vm \
  --vm-name base \
  --ansible-playbook ansible/playbooks/vm-basic.yml
```

Ansible changes are written into the backing qcow2, so overlays
created with `--backing-file` inherit them. Ansible is skipped when
`--restore-image` or `--backing-file` is set.

#### The live stages

A bake has no device attached, so three plays of `vm-rocm-golden.yml` are
left for a guest that is running behind a stack. Each is tagged `never` and
runs only when named:

```bash
ansible-playbook -i HOST, ansible/playbooks/vm-rocm-golden.yml \
  --tags configure -e ansible_port=2222 -e ansible_user=ubuntu
```

| Tag | What it does |
|-----|--------------|
| `configure` | Loads the baked ionic modules if they are not loaded, brings the emulated NICs up, and gives `rocm-ernic0` the address `192.168.200.<10 * vm_index>` (`-e vm_index=N` or `-e vm_ip=ADDR`) |
| `firmware` | Installs `ip_discovery.bin` from the rocjitsu image serving the guest, which amdgpu needs to probe |
| `test` | Runs the GPU workloads |

**`--tags configure` builds no driver, and only works on a guest this
playbook baked.** The image carries `ionic-ernic` built with rocm-ernic's
SRQ and P4 patch series. Up to 2.0.0 this stage built the driver again from
the base series alone and reloaded it, which replaced the image's driver in
that guest. Measured on a guest from the published image: behind a stack
that offers it no P4 the stage finished green and left `max_srq` 0, without
a word; behind the default stack, which offers P4, the stage failed at
`modprobe ionic_rdma` and left no RDMA device at all. The stage now starts
and ends by checking that the driver is the baked one, and fails if it is
not: the loaded modules are the build the bake recorded in
`/etc/ionic-srq-p4.json`, and every emulated NIC reports `max_srq` above
zero in `ibv_devinfo -v`. A guest that an earlier version configured still
has the base driver in its overlay; make a new overlay on the baked image,
which was never changed. A fleet guest needs none of this: the image loads
the modules and addresses its NICs at boot.

## qemu-tool CLI Reference

`qemu-tool` is the primary interface. Both subcommands accept `--domain
<file.xml>` to load a libvirt domain XML as base configuration (CLI
flags take precedence over XML values).

`qemu-tool` is a command and has no library interface. For people working
on its code, the [site](https://sbates130272.github.io/qemu-minimal/api/)
has a Python API section: every module of the package but `__init__` and
`__main__`, with its docstrings, public members, signatures and source. It
is built from the docstrings when the site is built
([`docs/api/`](docs/api/) holds only the list of modules for each page),
and it promises nothing about those interfaces from one release to the
next.

### check flags

Checks the host and prints a report grouped by area — tools, Docker,
virtualisation, container identity, settings, capacity, images. Lines are
marked `ok`, `warn`, `FAIL` or `skip`; `FAIL` sets exit 1 and stops a stack
from starting, `warn` does not.

| Flag | Default | Description |
|------|---------|-------------|
| `--xml FILE` | (search path) | Fleet XML to check against, repeatable, a later file overriding the elements it sets. Which file the search path found is itself part of the report |

### down flags

Gracefully shuts down guests through the QEMU guest agent, then optionally
tears down the containing stack. `run-vm` execs QEMU, so `docker compose down`
is a power cut — `qemu-tool down` is what keeps guest filesystems clean.

| Flag | Default | Description |
|------|---------|-------------|
| `--all` | off | Power off all guests then tear down every owned stack. `sudo qemu-tool down --all` takes every stack on the system |
| `--dry-run` | off | Show what would happen without doing it |
| `--yes` | off | Skip the teardown confirmation. The prompt never defaults to yes, so this is the only way to proceed unattended |
| `--all-users` | off | With `--all`: include other users' stacks. Needs root |
| `--timeout SECONDS` | 120 | How long to wait for guests to power off |

Stack identity comes from what compose records on each container: a stack is
yours if its containers run as your uid. Stacks started as root are skipped
by a non-root caller and listed with the reason; `sudo qemu-tool down --all`
takes every stack on the system.

### Shared flags (run-vm and gen-vm)

| Flag | Default | Description |
|------|---------|-------------|
| `--vm-name NAME` | `qemu-minimal` | VM name |
| `--arch {amd64,arm64,riscv64}` | `amd64` | Target architecture |
| `--vcpus N` | `2` | vCPU count |
| `--vmem MiB` | `4096` | Memory in MiB |
| `--images DIR` | `/var/lib/qemu-tool/images` | Image directory |
| `--ssh-port PORT` | `2222` | Host port forwarded to guest SSH, or `auto` for the first free port from 2222 up, picked as QEMU starts and printed; `qemu-tool ssh`, `list` and `inspect` find it |
| `--mac ADDR` | (from `--ssh-port`; from 2222 when that is `auto`) | Management NIC MAC; set it for an image that pins its netplan to one, including images this repo built before `3969f07` |
| `--kvm / --no-kvm` | kvm | KVM acceleration |
| `--qemu-path PATH` | (system) | Directory containing QEMU binaries |
| `--domain FILE` | — | Libvirt XML base config (`-` for stdin) |
| `--xml FILE` | search path | Fleet XML settings; repeatable, a later file overriding the elements it sets. A flag outranks the file |

### gen-vm flags

| Flag | Default | Description |
|------|---------|-------------|
| `--release NAME` | `resolute` | Ubuntu codename (`resolute`, `noble`) or `XX.YY` |
| `--size GB` | `64` | Disk size in GB |
| `--username USER` | `ubuntu` | Guest username |
| `--password PASS` | `password` | Guest password |
| `--user-id UID` | `1000` | Guest UID |
| `--ssh-key-file FILE` | a keypair generated for the image | SSH public key to inject instead |
| `--packages FILE` | `packages.d/packages-default` | Package manifest or `none` |
| `--force` | off | Force re-download of cloud image |
| `--compact / --no-compact` | compact | Recompress the backing image after the bake |
| `--no-backing` | off | Create flat image without a backing file |
| `--restore-image` | off | Recreate overlay from existing backing file |
| `--backing-file FILE` | — | Create overlay on top of this existing qcow2 |
| `--backing-image OCI_REF` | — | Pull a published backing qcow2 with `oras`, then overlay on it |
| `--bake` | off | Bake the image the fleet XML describes in its `<bake-*>` elements, from the cloud image, instead of pulling `<backing-ref>` |
| `--ansible-playbook FILE` | — | Path to an Ansible playbook to run against the VM image after cloud-init |
| `--cache-dir DIR` | `<bake-cache-dir>`, else none | With `--bake`: carry the bake's compiler cache in this directory between bakes |
| `--cache-replace` | off | The cache in that directory was saved for other pins: save this bake's over it |

### run-vm flags

| Flag | Default | Description |
|------|---------|-------------|
| `--nvme VALUE` | — | NVMe: positive int=count, negative=null_blk, string=literal |
| `--nvme-trace EVENT` | — | NVMe tracing: `doorbell`, `all`, or event name |
| `--nvme-trace-file FILE` | — | Redirect trace output to file |
| `--nvme-lbaf-mask HEX` | — | 16-bit hex LBA format mask (e.g. `0x1f`) |
| `--nvme-block-size {512,4096}` | `<vm-nvme-block-size>`, else 512 | Logical and physical block size of every NVMe namespace `run-vm` creates: `logical_block_size` and `physical_block_size` on the device that holds the drive. Not applied to a literal `--nvme` string |
| `--nvme-recreate` | off | Delete and recreate NVMe qcow2 files on start |
| `--nvme-vram-dev ID` | — | Let the emulated NVMe DMA into the BAR of the vfio-user device with this qdev id (its socket's file name without `.sock`); fork QEMU only. `<vm-nvme-vram>` in a fleet file |
| `--nvme-vram-bar N` | 0 | Which BAR of that device |
| `--filesystem DIR` | — | Host directory to share via 9p/VirtFS |
| `--pci-testdev` | off | Enable `pci-testdev` |
| `--pci-hostdev BDF[,BDF]` | — | VFIO PCI passthrough (repeatable or comma-sep) |
| `--vram-dev-index N` | — | 1-based index into `--pci-hostdev` for VRAM DMA |
| `--vram-bar N` | `0` | BAR index on the VRAM device |
| `--vfio-userdev SOCK[,SOCK]` | — | libvfio-user socket paths |
| `--pci-mmio-bridge` | off | Enable `pci-mmio-bridge` for CXL-style testing |
| `--data-nic-queues N` | `0` | Multi-queue TAP NIC (queue count) |
| `--mcast-group IP:PORT` | — | Multicast socket NIC |
| `--qmp-socket [PATH]` | — | QMP socket (bare flag = auto path) |
| `--no-qemu-guest-agent` | off | Omit guest agent channel |
| `--backing-shared` | off | Disable image locking for shared backing files |
| `--extra-hostfwd RULE` | — | Extra hostfwd rule e.g. `tcp::9150-:9100` (repeatable) |
| `--cpu-vendor VENDOR` | `<vm-cpu-vendor>`, else `AuthenticAMD` | CPUID vendor stated on `-cpu` (amd64); 12 characters, or `''` for the host's |
| `--qemu-arg=WORD` | the words of `<vm-qemu-args>` | One word appended to the QEMU command line as it is (repeatable, one word each) |
| `--dry-run` | off | Print QEMU command instead of running |
| `--convert-to-libvirt [FILE]` | — | Emit libvirt domain XML to FILE (default `<vm>.xml`) |

### gen-compose flags

| Flag | Default | Description |
|------|---------|-------------|
| `--xml FILE` | a `fleet.xml` on the search path, else the stack's own file | Fleet XML. Repeatable: a later file overrides the elements it sets |
| `--stack NAME` | the stack named after the first `--xml` file, else `rocm-golden` | Stack to generate: `rocm-golden`, `helios-tray`, `mi355x-server`, `xio` |
| `--output-dir DIR` | the stack directory | Where to write |
| `--check` | off | Exit non-zero if regenerating would change anything; writes nothing |
| `--dry-run` | off | Print the generated files instead of writing them |

`--output-dir` will not default into `/usr/share/qemu-tool/compose`; that tree
belongs to the Debian package, so overwriting it has to be asked for by name.
In a checkout it defaults to the committed stack directory instead, so any
render that layers an overlay must name an `--output-dir` of its own or it
overwrites the committed stack and breaks `--check`.

### compose flags

| Flag | Default | Description |
|------|---------|-------------|
| `--stack NAME` | `rocm-golden` | Shipped stack to run: `rocm-golden`, `helios-tray`, `mi355x-server`, `xio` |
| `--stack-dir DIR` | `$QEMU_TOOL_STACK`, else the cwd if it is a rendered stack | Run a stack rendered by `gen-compose --output-dir DIR`. Mutually exclusive with `--stack` |
| `--images DIR` | `<vm-images-dir>` | Guest images directory, passed to `docker compose` as `VM_IMAGES_DIR` |
| `--xml FILE` | none for a stack found by its directory, else the search path | Fleet XML. Repeatable: a later file overrides the elements it sets |

Everything else on the line goes to `docker compose` in the order typed —
`--profile metrics up -d` needs no separator.

`compose` never regenerates a stack or edits a settings file. Resizing a fleet
is `gen-compose --output-dir` after editing `<vm-count>` yourself. To run a
command across all fleet guests use ansible against `gen-compose`'s rendered
inventory:

```bash
ansible -i <stack>/fleet-inventory.yml fleet -f <N> -m raw \
    --private-key <key> -a '…'
```

`down -v` is worth typing in full: without `-v` a killed run leaves its
sockets in `/run/vfu` and the next `up` dies on `Address already in use`.

### ernic-stats flags

| Flag | Default | Description |
|------|---------|-------------|
| `--stats-dir DIR` | `/run/ernic-stats` | Directory of `*.stats` files |
| `--port PORT` | `9840` | Listen port |
| `--addr ADDR` | `0.0.0.0` | Listen address |

Runs inside the generated stack; rarely invoked by hand. Metric and label names
match rocm-ernic's own `prometheus/ernic-exporter`, so that project's Grafana
dashboard works against this endpoint unmodified.

### list flags

| Flag | Default | Description |
|------|---------|-------------|
| `--json` | off | Emit JSON instead of a table |
| `--qemu-tool-only` | off | Omit VMs not started by qemu-tool |

`qemu-tool list` shows every `qemu-system-*` process on the node — including
VMs running inside containers, which it attributes back to the container name —
and says which ones qemu-tool started:

```console
$ qemu-tool list
PID    NAME         SOURCE            ARCH   VCPU  MEM    SSH   KVM  UPTIME  CONTAINER                     NVME      VFIO-USER
37374  rocm-golden-1  external        amd64  4     4096M  2222  yes  53m     rocm-golden-qemu-1-1          00:05.0   rocjitsu-1.sock
79805  marker-test  qemu-tool/run-vm  amd64  1     1024M  2223  yes  0m      -                             -         -
```

#### The NVME column

Every NVMe attached to the VM, emulated or passed through, comma-joined, with
identical labels collapsed as `label*N` (`<vm-nvme>2</vm-nvme>` is ordinary, and two
identical labels say no more than one plus a count). `-` when the VM has none.

| Entry | Meaning |
|-------|---------|
| `00:05.0` | **Guest** BDF of an emulated NVMe, read from the VM's QMP monitor. The `0000:` domain is dropped for table width; `--json` keeps the canonical form |
| `emul` | An emulated NVMe whose guest BDF is not knowable — the VM has no QMP socket, or the query failed |
| `host:03:00.0` | A passed-through device, by its **host** BDF |
| `host:03:00.0?` | The same, with no sysfs entry on this host to confirm it is an NVMe |

Examples: `emul*2`, `emul,host:03:00.0`, `00:05.0,00:06.0`, `-`.

**A host BDF and a guest BDF are different address spaces**, which is why a
passthrough entry is labelled rather than printed bare: `host:03:00.0` is the
address on *this* machine, the address `lspci` inside the guest reports is a
different one, and sending someone to the wrong namespace's device is worse
than telling them nothing. Only the guest BDF is actionable inside the VM, and
it is only available when the VM was started with `--qmp-socket` — without a
monitor to ask, an emulated NVMe is reported as `emul` and nothing more,
because `run-vm` emits `-device nvme,...` with no `addr=` and QEMU picks the
slot at realize time, so the guest address never appears on the command line.

Passthrough entries come from `-device vfio-pci,host=...`, which is also how a
GPU or a NIC is passed through, so the device class is checked in
`/sys/bus/pci/devices/<bdf>/class` and only `0x0108xx` (NVM Express) is
claimed. A BDF this host has no sysfs entry for is reported with a trailing
`?` rather than dropped — dropping would silently hide a real NVMe.

`--json` adds the disk image path, full vfio-user socket paths, owning user and
uptime in seconds, plus an `nvme` array of objects per VM — `bdf` (canonical
`dddd:bb:dd.f`, or `null` for an emulated device whose guest BDF is unknown),
`kind` (`emulated` or `passthrough`), `source` (`argv` or `qmp`, i.e. whether
the BDF was upgraded from the monitor), `qdev_id`, and `serial` or
`class_verified` respectively:

```bash
# PID of a VM by name
qemu-tool list --json | jq -r '.[] | select(.name=="myvm") | .pid'
```

#### How VMs are identified

qemu-tool stamps every VM it launches with two generic QEMU options:

```
-name guest=<vm-name>,debug-threads=on
-uuid <uuid5(namespace, "<role>:<vm-name>")>
```

`list` recomputes the UUID from the parsed guest name and compares it against
the `-uuid` on the command line. An exact match identifies the VM as
qemu-tool's and reveals which subcommand started it, so a transient `gen-vm`
image-build VM is distinguishable from a running `run-vm` VM.

Both options are generic rather than machine-specific, so they work on all
supported architectures — including riscv64, where `-smbios` would not.

VMs shown as `external` were either started by something other than qemu-tool,
or by a version predating these markers; restart such a VM for it to be
identified. This is operator convenience, not a security boundary — the markers
are ordinary command-line arguments and anyone can set them.

### Libvirt XML round-trip

`--convert-to-libvirt` emits a domain XML that can be fed back as
`--domain` input to reproduce the identical QEMU command. QEMU-specific
features not natively representable in libvirt (NVMe emulation, tracing,
libvfio-user, pci-mmio-bridge) are stored as `<qemu:commandline>` entries
and round-trip hint attributes.

```bash
# Emit XML
qemu-tool run-vm --vm-name myvm --nvme 2 \
  --convert-to-libvirt myvm.xml

# Parse XML back and run
qemu-tool run-vm --domain myvm.xml

# Define and start in libvirt instead
virsh define myvm.xml && virsh start myvm
```

### --restore-image

When `--restore-image` is set, `qemu-tool gen-vm` skips cloud-init and
only recreates the final qcow2 from the existing backing file. Useful when
the VM image is corrupted or deleted but the backing file remains.

### --backing-file

When `--backing-file FILE` is set, `qemu-tool gen-vm` creates
`<vm-name>.qcow2` as a thin overlay on top of the specified file. Cloud-init
provisioning is skipped — the backing file must already be provisioned (e.g.
by a prior `gen-vm` run). See [Shared Backing Files](#shared-backing-files)
for a full workflow example.

## Shared Backing Files

Multiple VMs can share a single read-only backing file.
Each VM gets its own thin overlay where all writes land,
so the backing file is never modified after provisioning.

1. Create the base VM (runs cloud-init and provisions the
   backing file):

```bash
qemu-tool gen-vm --vm-name base
```

2. Create per-VM overlays from the shared backing file:

```bash
qemu-tool gen-vm --vm-name vm1 \
  --backing-file images/base-backing.qcow2
qemu-tool gen-vm --vm-name vm2 \
  --backing-file images/base-backing.qcow2
```

3. Run each VM with `--backing-shared` and a unique
   `--ssh-port` so QEMU disables file locking on the
   backing chain:

```bash
qemu-tool run-vm --vm-name vm1 \
  --ssh-port 2222 --backing-shared &
qemu-tool run-vm --vm-name vm2 \
  --ssh-port 2223 --backing-shared &
```

`--backing-shared` adds `file.locking=off` and
`backing.file.locking=off` to the root disk `-drive`.
The first disables QEMU's OFD locking on the overlay;
the second disables it on the backing file that QEMU
opens internally. Both are needed to prevent lock
conflicts across instances sharing the same backing
file. Each VM must still use a distinct `--vm-name` (and
therefore a distinct overlay) to avoid data corruption.

<!-- References -->

[batesste-galaxy]: https://galaxy.ansible.com/ui/repo/published/sbates130272/batesste/
[rocm-ernic]: https://github.com/ROCm/rocm-ernic
[rocjitsu]: https://github.com/sbates130272/batesste-ci-images
