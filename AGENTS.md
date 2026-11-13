# Agent Context for qemu-minimal

Practical facts for AI agents (and humans) working in this repo. Keep this
file up to date as infrastructure changes.

---

## Repository layout

- `qemu/` — qemu-tool Python package (installed as a Debian package or via pipx)
- `qemu/compose/vfio-user-ernic-vm/` — single-VM ernic-only compose stack
- `qemu/compose/vfio-user-rocjitsu-vm/` — single-VM rocjitsu-only compose stack
- `qemu/compose/vfio-user-ernic-rocjitsu-vm/` — single-VM ernic + rocjitsu compose stack
- `qemu/compose/vfio-user-ernic-2vm/` — two-VM ernic mesh stack; rocjitsu GPUs opt-in via `--profile rocjitsu-vm1/vm2`
- `qemu/compose/vfio-user-ernic-rocjitsu-scale-out/` — N-VM ernic mesh + one GPU per VM. **Generated** by `qemu-tool gen-compose` from `qemu/xml/scale-out.xml` plus whichever overlay applies; do not hand-edit it. `--profile metrics` adds Prometheus+Grafana
- `qemu/compose/vfio-user-mi355x-node/` — MI355X-shaped PCIe topology: one EPYC socket, 8 GPUs, 8 NICs and 8 PCIe switches per VM, one NIC + one GPU below each switch. Also **generated**, from `qemu/xml/mi355x-node.xml`. 8 ernics per VM means 8 *mesh nodes* per VM, so the fleet caps at 4 VMs, not 40 — see its README
- `qemu/compose/vfio-user-helios-tray/` — one VM with 4 GPUs, 8 ernics and an SDXI device. **Generated**, from `qemu/xml/helios-tray.xml`, and the stack behind `qemu-tool quick-start --flavor helios`. It needs `--output-dir` to regenerate, because `gen-compose` otherwise writes into the scale-out directory
- `qemu/docker/` — Dockerfiles this repo owns, one directory per image (`sdxi/` so far). Built by hand and referenced by tag, unlike the stack-local `tapsetup/` which compose builds itself
- `qemu/xml/` — every settings file (XML fleet configs). It is a sibling of `qemu/compose/` and `qemu/packages.d/`
  and is mapped in place by `package-dir`, for the same reason they are: these
  are data the wheel ships, not Python source, so they do not belong under
  `qemu/qemu-tool/`
- `qemu/xml/scale-out.xml` + `qemu/xml/overlay-ci.xml` + `qemu/xml/overlay-mi100.xml` — the
  fleet settings: one golden file and the committed overlays that sit on it
- `qemu/xml/rocm-golden.xml` — the performance-tracking fleet: two guests from the published rocm-golden qcow2, each with a GPU, an NVMe, an ernic with P4 in guest scope and an SDXI device. `report-for-vm-rocm-golden` runs every tracked benchmark against it
- `qemu/xml/quickstart.xml` + `qemu/qemu-tool/quick_start.py` + `ansible/playbooks/fleet-quickstart.yml` — the README's Quick Start, which is now `qemu-tool quick-start`: two guests from the published qcow2, each with a GPU, an NVMe and an ernic, Prometheus and Grafana beside them, plus a GPU test and an inter-guest RDMA test. The `report-for-quickstart` lane runs *that subcommand*, not a copy of its steps, so the documented path cannot rot. It replaced the shell script that used to live under `scripts/`, which is deleted; do not reintroduce a shell wrapper beside it. The guests are sized from the host by default (4 vCPU floor, 8 ceiling), and every prompt it asks takes its default answer when stdin is not a terminal, so CI needs no flag to stay non-interactive. Its settings file runs AHEAD of `qemu/xml/scale-out.xml` on purpose — the stamp check is per file, not across them
- `images/` — qcow2 VM disk images (backing + overlay pairs, **not** `/var/lib/qemu-tool/images`)
- `ansible/playbooks/` — three playbooks and four roles, and nothing else:
  - `vm-basic.yml` — a complete bake on its own, and the base of the golden
  - `vm-rocm-golden.yml` — the one image beyond it: imports `vm-basic.yml`, then the mainline kernel and ernic prerequisites, ROCm, the rocjitsu roles, SRQ + P4, SDXI and one slim pass. `--tags configure` sets up the NIC on a live guest and `--tags test` runs the GPU workloads. The two ernic pins (`ionic_kernel_pin`, `ernic_source_pin`) are defined once on its first play and shared with the others through YAML anchors
  - `fleet-quickstart.yml` — configures and tests a running fleet; what `qemu-tool quick-start test` runs
  - roles `rocm_ernic_setup` (image prep, device naming, boot-time addressing), `rocjitsu_setup` (firmware, amdgpu, tools, live tests), `sdxi_setup` and `image_slim`. Each has task files callers reach with `tasks_from`; all four are linted at the `production` profile in CI
- `scripts/install-vfio-rules` + `scripts/99-qemu-minimal-vfio.rules` — host-side udev rules for PCI passthrough. There is no top-level `udev/` any more: guest rules live in their role's `files/`
- `rocm-ernic-enablement.md` — running log of rocm-ernic integration status and bugs

## VM images

All qcow2 images live at **`<repo-root>/images/`** (i.e.
`/home/stebates/Projects/qemu-minimal/images/`), not at the default
`/var/lib/qemu-tool/images`. Set `VM_IMAGES_DIR` accordingly in `qemu/.env`.

Active images for the 2-VM ernic stack:

| VM   | Overlay                      | Backing                       |
|------|------------------------------|-------------------------------|
| VM1  | `stebates-ernic-vm-1.qcow2`   | `stebates-ernic-base.qcow2`   |
| VM2  | `stebates-ernic-vm-2.qcow2`   | `stebates-ernic-base.qcow2`   |

Base image is 12G (compressed from 28G used filesystem, 95G virtual).
Both VMs share the same backing; only delta writes go into their overlays.
Kernel: `7.0.0-31-generic` (HWE). Recreate overlays with:
```bash
qemu-img create -f qcow2 -b stebates-ernic-base.qcow2 -F qcow2 stebates-ernic-vm-1.qcow2
qemu-img create -f qcow2 -b stebates-ernic-base.qcow2 -F qcow2 stebates-ernic-vm-2.qcow2
```

SSH access (when the stack is running):

```
ssh -p 2222 stebates@localhost   # VM1
ssh -p 2223 stebates@localhost   # VM2
```

Password: see cloud-init user-data (plain_text_passwd field). GPG key in VM
is not unlocked (no private key injected), so git-crypt and signed commits
are not available inside the VM.

## Two-VM compose stack

```bash
cp qemu/xml/fleet-example.xml my-fleet.xml
# Edit my-fleet.xml: set VM_IMAGES_DIR to the repo images/ absolute path
# Add --profile rocjitsu-vm1 --profile rocjitsu-vm2 to enable GPUs
qemu-tool compose --xml my-fleet.xml --stack vfio-user-ernic-2vm \
  --profile rocjitsu-vm1 --profile rocjitsu-vm2 up -d
```

Key fleet XML values that differ from the example defaults:

| Variable       | Correct value                                        |
|----------------|------------------------------------------------------|
| `VM_IMAGES_DIR`| `/home/stebates/Projects/qemu-minimal/images`        |
| `VM1_NAME`     | `stebates-ernic-vm-1`                                 |
| `VM2_NAME`     | `stebates-ernic-vm-2`                                 |

The ernic hub serves VM1 (`ernic-1.sock`); the worker serves VM2
(`ernic-2.sock`). VMs should be assigned IPs in `192.168.100.11/24` and
`192.168.100.12/24` on `enp1s0` — avoid `.1` (reserved as the hub DHCP
`server_ip`; ARP for `.1` is intercepted by the hub).

## Scale-out fleet stack

`vfio-user-ernic-rocjitsu-scale-out` is an N-VM version of the 2-VM stack: one
emulated RDMA NIC and one rocjitsu GPU per VM, VM 1 the mesh manager and 2..N
workers dialling it.

**Its `docker-compose.yml`, `prometheus.yml` and `fleet-inventory.yml` are
generated.** Edit `qemu/xml/scale-out.xml` and re-run `qemu-tool gen-compose`; a
hand edit is lost on the next regeneration and `gen-compose --check` fails in
CI — the `Gen Compose Check` lane, which also runs `docker compose config`
against the committed stack, a tap-enabled render, and a
`--profile metrics` render. That last one is not optional
polish: profiles *filter* services out of `config` output, so until it existed
every observability service was validated by nothing, and
`config -q` on a render with all services filtered away still exits 0 — hence
the step asserts the seven services are present first. All three files are
committed so the stack stays reviewable in a diff and usable with plain
`docker compose`.

The same lane is where the two cheap guards live that no guest-booting lane
can replace. It runs the unit suite with `CI=true` after installing udevadm,
pciutils, PyYAML and Jinja2, because `tests/test_ernic_identity.py` is the
only thing that asks udev to *parse* `99-rocm-ernic.rules` and the only
thing that reads `rocm_ernic_setup`'s wiring, and under `CI=true` those checks fail instead of skipping. And it
renders a 3-VM stack to a scratch directory and runs `qemu-tool compose
config` inside it with no flags, asserting the compose project name (taken
from the working directory) and the service list, because resolving to the
*wrong* stack also exits 0. Note the render passes `--xml` absolute: a
provenance header records layer paths as typed, and relative ones resolve
against the stack directory.

```bash
qemu-tool gen-vm --xml qemu/xml/scale-out.xml \
    --vm-name stebates-fleet-1 --backing-image "$VM_BACKING_IMAGE"
for n in $(seq 2 8); do
  qemu-tool gen-vm --xml qemu/xml/scale-out.xml --vm-name stebates-fleet-$n \
      --backing-file images/stebates-fleet-1-backing.qcow2
done
qemu-tool gen-compose --xml qemu/xml/scale-out.xml
qemu-tool compose --xml qemu/xml/scale-out.xml \
    --stack vfio-user-ernic-rocjitsu-scale-out --profile metrics up -d
```

**A rendered stack names its own inputs, so driving one takes no flags.** A
`docker-compose.yml` written by `gen-compose` records its env layers in its
provenance header, and `compose` reads them back, so after the stack exists:

```bash
cd /var/lib/qemu-tool/stacks/mi100-fleet && qemu-tool compose ps
export QEMU_TOOL_STACK=/var/lib/qemu-tool/stacks/mi100-fleet   # or, from anywhere
```

The directory comes from `--stack-dir`, then `$QEMU_TOOL_STACK`, then the cwd
if it carries that header — no walk-up, because a walk-up from inside this
checkout lands on the committed 8-VM stack. Explicit `--xml` still wins,
a recorded layer that has moved is an error rather than a fall-back to the
search path, and the digest is still re-hashed, so a stack rendered from an
edited layer still warns. `compose` does not regenerate and does not edit
`VM_COUNT`: a re-render stays a typed `gen-compose --output-dir`.

**One golden file plus overlays, not a file per host.** `qemu/xml/scale-out.xml` is
authoritative and is what packaging ships; a CI lane or a particular host adds
a short committed overlay naming only the keys it changes. `--xml` is
repeatable on `gen-vm`, `run-vm`, `gen-compose`, `compose` and `inspect`, and
later files win key by key, so the overlay goes last:

```bash
qemu-tool gen-compose --xml qemu/xml/scale-out.xml --xml qemu/xml/overlay-mi100.xml \
    --output-dir /var/lib/qemu-tool/stacks/mi100-fleet
qemu-tool compose --stack-dir /var/lib/qemu-tool/stacks/mi100-fleet \
    --xml qemu/xml/scale-out.xml --xml qemu/xml/overlay-mi100.xml \
    --profile metrics up -d
```

An overlay render needs both directory flags. `gen-compose` defaults its output
to the committed stack directory, so without `--output-dir` the overlay's fleet
lands on top of the golden 8-VM one and `Gen Compose Check` fails. `VM_COUNT`
and the VM names are baked into the generated YAML rather than read from the
settings file at run time, so without `--stack-dir` `compose` runs that
committed stack whatever layers it is handed — an 8-VM stack standing in for
the 32-VM one just rendered, with no error.

This replaced `env.scale-out-ci`, `env.scale-out-mi100` and
`env.scale-out-strix` — three near-copies of the golden file, sharing ~20
identical keys, hand-synced and reliably out of step. `env.scale-out-strix` was dropped outright and has no
overlay. Do not reintroduce a standalone per-host settings file; add an
overlay, or add the key to the golden file if every fleet wants it.
`compose` passes each layer through to `docker compose --env-file` in order and
compose applies the same last-wins rule, so the tool and the containers cannot
disagree about a value. The staleness digest covers every layer, so editing an
overlay marks the generated stack stale just as editing the golden file does.

Per-VM values are comma-lists, not numbered variables. The trap worth knowing
before editing that file: keys naming a field that is *already* `list[str]`
(`VM_PCI_HOSTDEV`, `VM_VFIO_USERDEV`, `VM_EXTRA_HOSTFWD`) keep their existing
meaning — the commas are several values for **one** VM, so the whole value goes
to **every** VM. `VM_EXTRA_HOSTFWD=a,b` is two hostfwd rules per guest, not one
rule each for two guests. Do not "fix" such a key by padding it to `VM_COUNT`.

**`ERNIC_TAP=true` is what makes inter-node anything possible.** The TCP mesh
carries RDMA payload only; without a TAP the guests have no Ethernet, ARP goes
unanswered, and `ib_send_bw` cannot do its out-of-band exchange — so no
inter-node RDMA test runs at all. It costs one short-lived sidecar per VM
(holding the `CAP_NET_ADMIN` the long-running server does not need) and a
shared `l2` network. Each ernic also gets its own `ERNIC_TCP_GUEST_GIDS`,
derived from its MAC; that one is per-node and must never be hoisted into the
shared anchor (issue 18).

Two other keys worth knowing: `PROM_DATA_DIR` bind-mounts the Prometheus TSDB
to a path you can actually find — create it and `chown 65534:65534` first, as
the container runs as `nobody` — and `LOG_MAX_SIZE`/`LOG_MAX_FILE` bound every
service's log, because Docker's default is unlimited and a wedged manager with
no backoff on its error path wrote 295 GB in two days.

`ernic-1` is `restart: "no"` on purpose (issue 7 below): a manager restart
wedges every guest on a DSR timeout that only `down && up` clears, and
`on-failure` would turn a visible crash into a quiet wedge.

Ceilings, nearest first: **the mesh stops registering new workers at about 40
nodes** (issue 17 — the 64-node topology payload cap in `rdma_backend_tcp.c` is
not reachable), and `VM_VCPUS × VM_COUNT` should not exceed `nproc` plus about
25% (issue 14). Memory never binds in practice: 48 VMs used 107G of 503G.
`VM_VCPUS` must stay ≥ 4 or `ionic_rdma` never probes.

**`VM_SHM_SIZE × VM_COUNT` against `/dev/shm` is not the memory ceiling, and
reading it as one looks reassuring while measuring nothing.** Guest RAM is a
`memory-backend-memfd` (`-object memory-backend-memfd,share=on`), and a memfd
is anonymous — it is charged to the container's cgroup as `shmem`, never to
`/dev/shm`. Measured on the 32-VM fleet, 8 GiB per guest: host `/dev/shm`
489G with **64K used**, each container's own `/dev/shm` an 8G tmpfs with
**0 used**, while `memory.stat shmem` across the qemu containers was **40.6
GiB** — the guest RAM, exactly where the accounting says it is. `shm_size`
still has to be ≥ `VM_VMEM` or QEMU refuses to allocate the backend, so the
setting matters; what does not hold is the idea that watching `/dev/shm` tells
you how much of it is in use.

Two corollaries for anyone reading memory numbers off this stack. The
container's `anon` is **not** fleet memory — it was 2.0 GiB across all 32,
20× below the real figure, because the guest RAM is shmem. And
`memory.current` folds in qcow2 page cache, so it overstates: it answers
"what can the host not reclaim cheaply", not "how much RAM do the guests
hold". `qemu-tool list --summary` reports the shmem figure and labels it.

## `qemu-tool check`

One command that asks every host question the stacks depend on and prints a
line per answer: tools on PATH, the Docker daemon and your access to it, KVM
(the ioctl, not the node), `/dev/net/tun`, the uid the containers will run as,
which settings file is in force, the vCPU budget against `nproc`, and whether
`VM_IMAGES_DIR` has room and is owned by you. Read-only, no `sudo`, writes
nothing — safe on a host nobody has committed to yet.

`FAIL` sets exit 1; `warn` does not. That split is deliberate and worth
keeping: a checker that failed on a merely tight host would get disabled in
whatever runs it, and then catch nothing.

**It is the same code `quick-start` runs in its preflight, not a copy.**
`quick_start` imports `kvm_usable`, `docker_compose_ok` and `free_gb` from
`check_system`, and `tests/test_check_system.py` asserts *identity* (`assertIs`)  
rather than equality, so a second copy that happens to agree today still fails
the test. This repo has been bitten by a documented path and its CI equivalent
drifting apart — it is why `report-for-quickstart` runs `qemu-tool quick-start`
itself rather than a reimplementation of its steps.

The one thing deliberately **not** shared is the tool list.
`quick_start._TOOL_HINTS` requires oras, ansible-playbook and ssh outright,
because the quickstart pulls a published qcow2, provisions the guests and
reaches them. `check_system` treats those as optional, because a host that
only runs `qemu-tool compose` against an image it already has needs none of
them and failing it would be wrong. Two lists because there are two
requirements; there is a test pinning that too, so nobody "fixes" it.

## GPU BARs, and rocjitsu's VRAM aperture

`qemu-tool inspect` reports each GPU's PCIe BARs under its row:

```
GPU:       gfx1250 (emulated, rocjitsu-1.sock)
             BAR0 256 MiB (64-bit prefetchable)
             BAR2 2 MiB (64-bit prefetchable)
GPU:       gfx908 (passthrough, 63:00.0)
             BAR0 32 GiB (64-bit prefetchable)
             BAR2 2 MiB (64-bit prefetchable)
             BAR5 512 KiB (32-bit)
```

Two sources, because there is no single one. A **passthrough** GPU's BARs come
off the host's own sysfs and need no agent. An **emulated** one's come from the
guest, reached through the KFD node's `location_id` — KFD packs the PCI address
as `(bus << 8) | (device << 3) | function` with `domain` separate, verified
against real hardware (`location_id 25344` = `0x6300` = `0000:63:00.0`, and
that device is there). Deriving it is necessary because the guest agent has no
readdir, so `/sys/bus/pci/devices` cannot be walked. rocjitsu's own config is
not an option: it lives inside the server's container, where `inspect` cannot
reach it.

Three things about a sysfs `resource` file are easy to get wrong and all three
are silent — `tests/test_inspect_devices.py::Bars` pins each, against a
verbatim MI100 fixture. Bounds are **inclusive**, so size is `end - start + 1`;
off by one reports every BAR one byte under a power of two, which looks
precise. A 64-bit BAR occupies **two** slots with the upper reading as zeros,
so anything inferring the index from position misnumbers every BAR after the
first 64-bit one. And only the **first six** lines are BARs; the rest are the
expansion ROM and bridge windows.

**Enabling the large BAR on rocjitsu, for P2P DMA.** It is a config-file field,
not a CLI flag — `rocjitsu --help` has no knob for it. From the shipped schema
(`/usr/local/share/rocjitsu/schemas/simulation_config.fbs`):

```
vram_aperture_bytes: uint64 = 0; // 0 = an implementation-chosen window, <= 256 MiB.
doorbell_aperture_bytes: uint64 = 2097152;
register_aperture_bytes: uint64 = 524288;
```

`gfx1250_mi455x.json` ships `vram_aperture_bytes: 268435456` — 256 MiB, already
the small-BAR ceiling. That 2 MiB doorbell BAR is the "2 MiB second BAR not
mapped at all" of `.artifacts/vfio-user-p2p-dma.md`. Set the field to the
largest power of two **≤ `local_mem_size`**; for that config
(`local_mem_size: 463856467968`, ~432 GiB) it is **256 GiB = 274877906944**.

Measured against the real binary rather than inferred, by serving each value
on a vfio-user socket:

| `vram_aperture_bytes` | rocjitsu |
|---|---|
| 128 GiB | clean, serves |
| 256 GiB | clean, serves |
| 512 GiB | `warn: the video memory aperture is larger than the memory itself` |
| 300 GiB | `warn: the video memory aperture is ... not a power of two` |

Both are warnings, not refusals — it still serves — so a wrong value here is
the quiet kind. Two things have to travel with a large BAR: `VM_PCI_HOLE64_SIZE`
must cover it (`512G` is the phys-bits ceiling, see the MI355X README), and
vfio-user P2P DMA needs the fork QEMU image (issue 19) — `qemu/xml/overlay-mi100.xml`
pins it, the golden `env/scale-out.xml` does not.

## Tearing a stack down

**`qemu-tool down`** gracefully shuts down guests via the QEMU guest agent and,
optionally, tears down the containing stack. With no argument it powers off
every running guest. With a VM name it powers off that guest. With a stack
name (the `STACK` column in `qemu-tool list`) it powers off that stack's guests
then tears it down. With `--all` it powers off everything and tears down every
owned stack. `--dry-run` lists what it would do and the exact commands; `--yes`
skips the teardown confirmation prompt, which never defaults to yes.

Both of its questions are answered from what compose records on each
container, so neither is a guess:

| Question | Answered by |
|---|---|
| Is this a qemu-tool stack? | `com.docker.compose.project.config_files` — the file carries the `gen-compose` provenance header, or sits in one of the six stack directories this tool ships |
| Is it yours? | `.Config.User`, the uid the containers really run as — which is what the `CONTAINER_USER` work put there |

**Do not "simplify" the first to a service-name match.** `qemu-1` is a name
anyone might use, and this command removes containers. Verified: an unrelated
compose project with a service called `qemu-1` is ignored, while a real fleet
beside it is found.

Ownership is *any* container as your uid, not all — a stack legitimately
mixes them, since the stats exporters are pinned to 65534 and
tapsetup/dockerapi stay root. The limitation worth knowing: a stack started
**before** the containers ran as the caller has every container as root, so a
non-root caller sees it as root's and skips it. That is correct — you cannot
stop them without root — and skipped stacks are listed with the reason rather
than passed over. `sudo qemu-tool down --all` takes everything, which is what
makes it the system-wide form.

The teardown pins the project with an explicit `-p` and rebuilds the
invocation from the recorded `working_dir`, `config_files` and
`environment_file`. That `-p` is the point: the project name is otherwise
derived from the working directory, which is the leading theory for the
41-hour survivors below.


**`qemu-tool compose down` and `stop` enable every profile**, which plain
`docker compose` does not, and say so on stderr. Pass `--profile` (or set
`COMPOSE_PROFILES`) to scope it deliberately; an explicit choice is respected.

**And a teardown that exits 0 while containers in its own project are still
running now warns**, naming them. That check is the useful half, because the
cause is not established. A fleet was found with prometheus, grafana and the
stats exporter alive **41 hours** after a `down --remove-orphans` that
reported success.

The obvious explanation — that profile-gated services are filtered out of
`down` — is **wrong**, and was tested rather than assumed: on compose v5.3.1,
`up --profile metrics` followed by a plain `down`, `down --remove-orphans` or
`stop` removes the gated container every time. So `--profile "*"` is
belt-and-braces; it costs nothing and makes `down` unambiguous, but it is not
the fix for what was seen.

The likeliest remaining candidate is a **project-name mismatch**. Compose
derives the project from the working directory, `compose.run` never passes
`-p`, and the stack-directory discovery order (`--stack-dir`, then
`$QEMU_TOOL_STACK`, then the cwd) can resolve differently between the `up` and
the `down` — in which case the down tears down an *empty* project and reports
success. If this happens again, the thing to capture before cleaning up is
`docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}'` on
a survivor, next to the directory the `down` ran from. That one comparison
settles it.

## Container privilege

**Nothing in any stack runs as root by default, and nothing holds a
capability.** This changed on 2026-10-24; before that every service in all six
stacks ran as uid 0 with Docker's full default capability set, and the
`CONTAINER_USER` opt-in that existed to fix it was set by exactly two
committed overlays, both pinning one operator's ids.

The mechanism, and why it was off for so long: the value cannot be committed.
It differs per host, and `gen-compose --check` compares the rendered stack byte
for byte in CI, so a uid in the YAML fails that check everywhere but the
machine that generated it. What the render carries is a *reference* —
`user: ${CONTAINER_USER:?set CONTAINER_USER=<uid>:<gid>}` — and compose
resolves it at `up`. That is exactly what makes it safe to emit
unconditionally, which is the fix: the render stays machine-independent either
way, and `qemu-tool compose` now answers it from `compose._caller_identity()`
(`os.getuid()`/`os.getgid()`, and `CONTAINER_KVM_GID` from `stat /dev/kvm`).
`quick_start._compose` calls the same helper, because it builds its own
`docker compose` command rather than going through `compose.run`.

Precedence matches compose's own: the process environment first, then the
`--env-file` layers, and only an unset key is filled in. Get that backwards and
qemu-tool and the containers disagree about the value, which is the failure the
repeated-`--env-file` handling exists to prevent.

**`:?` and never `:-`.** Measured on compose v5.3.1: an unset variable
interpolates to the empty string, `user: ":"` is not an error to Docker, and
the container runs as root with one warning line as the evidence. A plain
`docker compose up` against a committed stack therefore *refuses*:

```
error while interpolating services.qemu-3.user: required variable
CONTAINER_USER is missing a value: set CONTAINER_USER=<uid>:<gid>
```

That is the designed behaviour, not a regression. Supply both and it works:

```bash
CONTAINER_USER=$(id -u):$(id -g) \
CONTAINER_KVM_GID=$(stat -c %g /dev/kvm) docker compose up -d
```

`CONTAINER_KVM_GID` is not optional with a uid set: a container given
`--user <uid>:<gid>` holds **no** supplementary groups, and `/dev/kvm` is
`crw-rw---- root:kvm`. Without it the guest silently falls back to TCG and runs
~20x slower, which presents as "the guests are hanging". The gid is read off
the device rather than from the group *name*, because a host can have the group
without the device or the device without that name — and what the container
needs is whichever gid opens that node.

**A uid is not a capability set.** A container can be uid 1000 and still hold
`NET_RAW`: Docker grants the default permitted set independently of the user.
So every service also takes `cap_drop: [ALL]` and
`security_opt: ["no-new-privileges:true"]`, which matters most in the
configuration where someone has deliberately pinned `CONTAINER_USER=0:0`.

Exemptions, each for its own reason:

| Service | Why |
|---|---|
| `tapsetup-N` | **Cannot** take a uid. `cap_add` raises only the bounding and permitted sets; Docker sets no AMBIENT capabilities, so a non-root uid holds nothing effective and `ip link add` dies with `RTNETLINK answers: Operation not permitted`. Compose cannot express ambient caps. What *was* fixable is the rest of the set: it now runs `cap_drop: [ALL]` then `cap_add: [NET_ADMIN]`, read-only rootfs, where it used to carry Docker's thirteen defaults as well. |
| `dockerapi` | Holds the host Docker socket, which is root's. |
| `*-stats-exporter` | Pinned `65534`. They create nothing on the host, so there is no ownership to fix and `nobody` beats "you". |

The two pairs that must not diverge: `x-ernic-common` and `x-qemu-common` carry
the same uid because **rocjitsu does not chmod its vfio-user socket**. It stays
0755 owned by its creator and `connect(2)` needs write on the inode, so a qemu
on a different uid cannot attach the GPU at all. rocm-ernic hides this by
chmodding its own to 0666 — do not generalise from it. And `TAP_OWNER` on the
tapsetup sidecar must be that same uid: `TUNSETIFF` on a *persistent* tap needs
a matching owner or `NET_ADMIN`, and the ernic deliberately holds neither.

**`:ro` on a Docker socket mount restricts the inode, not the API.** Anything
that can `connect()` to `/var/run/docker.sock` can create a privileged
container and own the host, whatever the mount flags say — so the old
`alloy` service, a large pipeline that parses untrusted container log content,
held a root credential. There is now a `dockerapi` service (haproxy,
`tecnativa/docker-socket-proxy`) with `CONTAINERS`/`EVENTS`/`PING`/`VERSION`
on and **`POST=0`**; that last is the line that matters, since without it the
allowlist still permits container create, start and exec. Alloy reaches the
daemon through `DOCKER_HOST` over the fleet network, holds no socket, and so
takes `CONTAINER_USER` like everything else. The proxy publishes no port. This
does not remove the root-equivalent container — anything speaking to the daemon
needs the socket — it shrinks it from a log pipeline to a proxy whose whole job
is saying no.

**Migrating a fleet that ran as root.** `qemu-tool compose up` creates
`PROM_DATA_DIR`/`LOKI_DATA_DIR`/`GRAFANA_DATA_DIR` as the caller (Docker would
otherwise create a missing bind-mount source root-owned, and the service that
cannot write it reports the problem several minutes later) and warns about
anything it finds owned by someone else. The one it cannot fix is a
`<vm>-nvmeN.qcow2` from a root run: 0644 root-owned, and QEMU opens a drive
read-write, so the guest does not boot **at all** rather than merely owning the
wrong file. Delete them; they are scratch.

Three layers report this now, at the three moments it can be noticed.
`qemu-tool check` flags a `VM_IMAGES_DIR` that is unwritable or holds
files owned by someone else, before anything starts. `qemu-tool compose up`
warns and names them. And `run-vm` **refuses**, because by then it is the
last chance: it checks the images directory and every drive QEMU will open
read-write, and prints the owner, the mode and a fix. A backing image is
deliberately exempt from the write check -- a fleet shares one across every
guest and QEMU opens it read-only, so demanding write there would reject a
correct setup.

The wording distinguishes two causes, which is not cosmetic. A file owned by
someone else wants `chown` (or `rm`, if it is a `-nvmeN.qcow2`, which is
scratch and recreated -- and needs no sudo, since unlinking depends on the
directory). A file that is **yours** but mode 0444 wants `chmod`, and the
earlier wording for it read "owned by uid 1000, not read-write for uid 1000"
-- a true sentence that looks like a contradiction and names the wrong
remedy.

**Host-side `sudo` in `run_vm.py` is unchanged except for one thing.** The TAP
path still shells out to `sudo` for `ip`, `iptables` and a long-lived
`dnsmasq`. What changed is `net.ipv4.ip_forward`: it used to be an
unconditional `sysctl -w ...=1` with no matching restore, so running one VM
once left the host forwarding packets forever. It is now set only if it was
off, recorded as ours, and put back on teardown — and only when no other
`qemu-br*` bridge is left, because a second `run-vm` that found it already on
will not have recorded it and switching it off under them breaks a running
guest's networking.

## rocm-ernic driver and userspace provider (in-VM)

**rocm-ernic is now ionic-based.** Upstream deleted `driver/` and `rdma-core/`
on 2026-09-16. There is no `rocm_ernic_eth`, no `rocm_ernic_rdma`, and no
patched rocm_ernic verbs provider any more; anything still referring to those
is describing a tree that no longer exists.

What replaces them:

| Was | Is |
|-----|-----|
| out-of-tree `rocm_ernic_eth` + `rocm_ernic_rdma` | upstream `ionic` + `ionic_rdma`, two AMD patches on top |
| DKMS package `rocm-ernic` | DKMS package `ionic-ernic`, built by `scripts/setup-ionic-dkms.sh` |
| rdma-core ≥ 62 + `apply-rocm-ernic-dv.sh` | stock rdma-core ≥ 61, whose `providers/ionic` is upstream |
| emulated device `1022:8000` | Pensando `1dd8:1002`, subsystem `1dd8:5400` (was `1dd8:100a` before rocm-ernic@3fb74d1) |

`ionic_rdma` needs kernel ≥ 6.18 (`drivers/infiniband/hw/ionic` merged there)
and `ib_umem_get_va`, which landed after 7.0. No Ubuntu release ships one, so
the guest runs a pinned mainline kernel.

None of this is done by hand in this repo. Two pieces of automation own it:

- [`ansible/playbooks/roles/rocm_ernic_setup/`](ansible/playbooks/roles/rocm_ernic_setup/)
  prepares the guest image. Its `image_prep` tasks install the pinned mainline
  kernel from `kernel.ubuntu.com/mainline` and the build toolchain; `naming`
  installs the udev rules and the `modules-load.d` entry; `network` installs
  the boot-time addressing unit. It writes no pci.ids entry by default —
  upstream hwdata carries the device, see the PCI ID section. Upstream assumes
  a guest already prepared this way; this repo builds its own, so it owns the
  preparation.
- `sbates130272.rocm_ernic.ernic_guest_setup` from the collection builds and
  installs the DKMS package, the rdma-core provider and the NIC config.

The kernel ref is `ionic_kernel_pin`, defined once at the top of
[`vm-rocm-golden.yml`](ansible/playbooks/vm-rocm-golden.yml) and currently
`v7.2.6`. It feeds both `ernic_ionic_kernel_ref` and
`rocm_ernic_setup_kernel_ref`, so the sources and the guest kernel cannot
drift apart — `driver_ionic.yml` fails the build if they disagree on
major.minor.

Device names are unchanged: Ethernet `rocm-ernic0`, IB `rocm-rdma-ernic0`, both
still set by `99-rocm-ernic.rules` (see udev section below), now keyed on
`1dd8:1002` with the `1dd8:5400` subsystem pair, and
`DRIVERS=="ionic|ionic_rdma"`.

## udev rules (in-VM)

`99-rocm-ernic.rules` gives the ernic devices stable names: Ethernet interface
→ `rocm-ernic0`, IB device → `rocm-rdma-ernic0`. Use the Ethernet one with
`ib_send_bw -d ...`; **do not use the IB one with anything built on UCX** —
see the UCX issue below.

**This repo carries the rules**, at
[`99-rocm-ernic.rules`](ansible/playbooks/roles/rocm_ernic_setup/files/99-rocm-ernic.rules),
and `rocm_ernic_setup`'s `naming` tasks install them.
`rocm_ernic_setup_udev_rules_src` defaults to that file. It used to default to
`""` and no playbook ever set it, so the role installed nothing and every guest
inherited whatever its image baked in; that is how a fleet ended up matching an
id upstream had retired. An empty `src` is still allowed, and says so in the
play output instead of skipping silently. `tests/test_ernic_identity.py`
asserts both stanzas match all four fields.

**Naming is never conditional.** The copy task once lived in a task file that
was included only when a patched-modules directory was set, and that directory
is empty by default — in-tree `ionic` binds the device natively. So the
documented setup installed no rules and no `modules-load.d` entry: on a
32-guest fleet, 64 NICs, none renamed, none addressed. Every id assertion in
the tests stayed green throughout, because they read the task and never asked
whether it runs; there is now a test that the `naming.yml` include carries no
`when:`.

**The published qcow2 ships no rules of its own either**, as of
`20261021.g742e8e2`. Read straight off the image over `qemu-nbd`, not
inferred: `/etc/udev/rules.d/` has no `99-rocm-ernic.rules`. The note below
about the Oct-14 image carrying one no longer holds, so a guest that has had
no Ansible run against it has no rules from any source.

**`NAME="rocm-ernic%n"` could only ever name one NIC per guest.** `%n` is the
kernel number — the trailing digits of the sysname — and every emulated NIC
comes up as `...np0`, so `%n` is 0 for all of them and the second rename fails
with the name already in use. Unreachable while every fleet had one NIC per
guest; `ERNIC_PER_VM` made it reachable and the MI355X node shape, at eight
NICs per VM, makes it certain. The rule now searches for the first free index,
which is what the InfiniBand stanza below already did. Verified on a running
two-NIC guest: `enp1s0np0` → `rocm-ernic0`, `enp2s0np0` → `rocm-ernic1`.
Note udev applies `NAME=` only at device-add and will not rename a live
interface, so a re-trigger needs the links down first.

**`rocm-ernic-netcfg.sh` configures every matching NIC**, not just the first.
It walks all of them and derives each address from that NIC's own MAC, which
matches `gen-compose` keying MACs and IPs on the global NIC ordinal — so VM
*n*'s devices land on consecutive addresses from `.11` up. The hostname still
comes from the first NIC only: it names the guest, not the device.

`ernic_guest_setup` ships its own copy and reloads udev, and both renames are
confirmed working in CI — the ionic lane's
`Show NIC details` reports `rocm-ernic0` carrying `altname enp0s5np0`, which is
the pre-rename kernel name, and `ibv_devinfo` reports `hca_id: rocm-rdma-ernic0`.
The IB rename shells out to `/usr/bin/rdma`, which the gen-vm guest has.

**On the published `rocm-golden` qcow2 only the Ethernet rename happens.**
Measured 2026-10-16 on both guests of a `quickstart` stack from
`20261014.geea0485`: the netdev comes up as `rocm-ernic0`, and `ibv_devinfo`
reports `hca_id: rocep1s0` — the kernel name. The rules file is in the image
(`/etc/udev/rules.d/99-rocm-ernic.rules`, from the build, not from Ansible),
so this is the IB half of it not firing rather than the rules being absent.
The consequence is practical: anything on a fleet guest that passes
`-d rocm-rdma-ernic0` fails there, so resolve the name from `ibv_devinfo -l`
rather than hardcoding it — which is what `fleet-quickstart.yml` does, and
why its RDMA test works on a guest whose device has either name.

As of rocm-ernic@3fb74d1 the rules match on four attributes, not two —
`0x1dd8`/`0x1002` plus the subsystem pair `0x1dd8`/`0x5400`:

```
SUBSYSTEM=="net", ACTION=="add", \
  ATTR{device/vendor}=="0x1dd8", \
  ATTR{device/device}=="0x1002", \
  ATTR{device/subsystem_vendor}=="0x1dd8", \
  ATTR{device/subsystem_device}=="0x5400", \
  DRIVERS=="ionic", NAME="rocm-ernic%n"
```

The subsystem pair is what distinguishes this device from a real Pensando
NIC, because `1dd8:1002` is a real one's id — see the PCI ID section. So a
guest that comes up with kernel names has a device that failed one of four
matches, not two; check all four before re-copying anything.

## PCI ID (in-VM)

**Nothing to do on a current guest: upstream hwdata already names this
device.** `rocm_ernic_setup_pciids` defaults to `false` and the entry stays
unwritten — not because writing it is unsafe, but because it is already
there.

What changed is the id. rocm-ernic@3fb74d1 moved the emulated NIC from
`1dd8:100a` to `1dd8:1002`, and `1dd8:1002` is **a real Pensando device id**
that hwdata has always carried. What identifies *this* device is the
subsystem pair, and that is what upstream now names:

```
1dd8  AMD Pensando Systems
	1002  DSC Ethernet Controller
		1dd8 5400  ROCm Emulated RDMA NIC
```

Confirmed three ways, rather than assumed: the entry is in
<https://pci-ids.ucw.cz/v2.2/pci.ids>; a fleet guest's
`/usr/share/misc/pci.ids` already contains the `5400` line; and `lspci -nn`
on that guest reports `AMD Pensando Systems DSC Ethernet Controller
[1dd8:1002]` with nothing injected. `update-pciids` refreshes it like any
other entry — so the old warning that `update-pciids` *destroys* our entry is
inverted: it now delivers it.

**The entry is three levels deep, and the device line is not ours.** Vendor
→ device (one tab) → subsystem (two tabs), tabs and not spaces. hwdata has
named `1dd8:1002` for years, so the only line this repo could ever contribute
is the `1dd8 5400` subsystem line nested under it. `rocm_ernic_setup` writes
exactly that shape, and its presence check asks for the **subsystem** line: a
check that stopped at the device would see upstream's `1002`, skip forever,
and never write the one line that is ours.

Four parse rules, measured against pciutils 3.14 on a fleet guest and 3.10 on
the dev host rather than assumed, and pinned by `tests/test_ernic_identity.py`:

| Input | Result |
|---|---|
| correct three-level entry | parses, `-nn` names the device, `-v` names the subsystem |
| duplicate at any level | `lspci: Duplicate entry at <file>, line N`, rc 1 |
| subsystem line with no device line above it | `lspci: Parse error`, rc 1 |
| spaces instead of tabs | `lspci: Parse error`, rc 1 |

**The three refusals are version-dependent, and only the first row is a
property to rely on.** A GitHub runner's `lspci` returned 0 for the duplicate
case, which turned those three assertions red on a PR that touched none of
this. The test now probes the local `lspci` and skips the refusal cases,
naming the version, when it accepts a malformed file — and that skip is worth
reading, because on such a host `rocm_ernic_setup`'s "validate the merged
file with `lspci -i` before installing it" step cannot catch a duplicate. The
positive row stays asserted everywhere: it is the behaviour this repo
depends on.

A failure is never local to the entry — after any of those, `lspci` resolves
no names for *any* device on the bus:

```
$ lspci -i /tmp/merged.ids -nn
lspci: Duplicate entry at /tmp/merged.ids, line 27685
$ echo $?
1
```

That is why the role builds the merged file beside the real one, runs `lspci
-i` against the candidate, and only then installs it — and still parse-checks
the installed file afterwards. The merge `awk` exits 3 rather than succeeding
if it inserted nothing, because a silent no-op reads as a successful merge.

`rocm_ernic_setup_pciids` defaults to `false` all the same — on a current
guest hwdata is right and there is nothing to add. The machinery is kept,
off, because the id has moved twice already (`1022:8000` → `1dd8:100a` →
`1dd8:1002`). If it moves again to something hwdata does not carry, set
`rocm_ernic_setup_pciids` true and the four `rocm_ernic_setup_pciids_*` ids to
the new ones; the role inserts whichever of the device and subsystem lines is
missing, directly under its parent, since `pci.ids` is parsed sequentially and
an append at the end of the file lands under the class section and never
matches.

**A second thing follows from the id move.** The in-tree `ionic` driver's PCI
table has always held `1dd8:1002` and `1dd8:1003`, so the device it would not
previously bind — `100a` — is now one it claims natively:

```
$ modinfo ionic | grep 1DD8
alias: pci:v00001DD8d00001002sv*sd*bc*sc*i*
alias: pci:v00001DD8d00001003sv*sd*bc*sc*i*
```

So the `new_id` workaround described under known issues is obsolete on a
current image. The DKMS modules are still required, but for SRQ rather than
for the device appearing at all.

## Known issues / open bugs

See `rocm-ernic-enablement.md` for the full tracking list. Short version:

1. Worker `server_ip` ARP hijack when VM IP = `192.168.100.1` — use `.11`/`.12`
2. Collection 0.2.0 is not on Galaxy — only 0.1.0 is published, and 0.1.0 is
   the pre-ionic collection. `requirements.yml` pins the upstream git SHA
   instead; move back to a Galaxy version pin once 0.2.0 ships there
3. **Killing perftest mid-run breaks VM device context** — killing `ib_send_bw`
   or any perftest process mid-operation (SIGTERM/SIGKILL) leaves the guest
   RDMA driver in a broken state. `ibv_devinfo` reports "Failed to open device"
   or "wasn't found". `rmmod`/`modprobe` hangs for 2 min (DSR timeout) and the
   device does not recover. **Full stack restart required** (`docker compose down
   && up`). Avoid killing tests; let them run to completion or use `ib_send_bw
   --iters 100` to keep runs short.

4. **ernic-hub crash requires full stack restart** — if `ernic-hub` crashes and
   restarts (visible in `docker compose logs ernic-hub` as a re-initialization
   from the top), the guest driver enters a DSR initialization timeout
   (`-ETIMEDOUT`) on the next `modprobe ionic_rdma`. The vfio-user session
   held by QEMU becomes stale and cannot be recovered by reloading modules alone.
   **Fix: `docker compose down && docker compose up`** (all containers, not just
   qemu-1/qemu-2). Restarting only the QEMU containers is insufficient because
   the ernic-worker also needs a fresh TCP mesh connection to the new hub.

5. **perftest server dies if SSH session closes** — `nohup`/`disown` is not
   sufficient; the server exits with no output when the parent SSH session ends.
   Workaround: keep the SSH session alive (run server in foreground in a
   background job `&` and let the shell wait) or use `screen`/`tmux` inside
   the VM. Example:
   ```bash
   # terminal 1 — server (keep session open)
   ssh -p 2223 local-vm "ib_send_bw -d rocm-rdma-ernic0"
   # terminal 2 — client
   ssh -p 2222 local-vm "ib_send_bw -d rocm-rdma-ernic0 192.168.100.12"
   ```

6. **`ernic_source_repo_version` defaults to `main`** — so `ernic_source` clones
   upstream HEAD inside the guest even when `requirements.yml` installs the
   collection from a fixed SHA, and the roles can end up building sources from
   a different tree than they came from. Pinned locally in
   [`vm-rocm-golden.yml`](ansible/playbooks/vm-rocm-golden.yml) as `ernic_source_pin`;
   the default is still upstream's.
7. **`ernic_nic_subnet` is now a role default upstream** (rocm-ernic@1484557),
   but it still has to be repeated at play-var level in `vm-rocm-golden.yml`'s
   configure play because role defaults are not in scope when play vars are
   evaluated. The upstream commit documents this explicitly.
8. **A rebooted ernic guest is device-present but not fully test-ready.**
    As of rocm-ernic@1484557, `ernic_guest_setup` writes persistent
    `/etc/modules-load.d/` entries for `ionic` and `ionic_rdma` (via
    `modprobe persistent:`), so the RDMA device comes back after a reboot.
    `rocm_xio` is deliberately excluded — it is hand-copied into the running
    kernel's `extra/`, not DKMS-managed, so a persistent entry would outlive
    the module across a kernel upgrade. The address on `rocm-ernic0`, bringing
    the netdev up and the counters symlink are still run-time only; re-run the
    configure play to restore full test-readiness after a reboot.
9. **The mesh allocates a new node id on every reconnect and never reclaims
    the old one.** This is the fact that decides how large a fleet can be. A
    worker that dies and is restarted by Docker does not resume its old id; it
    consumes the next one. The topology payload caps at 64 nodes
    (`rdma_backend_tcp.c:2570`), so a fleet does not have 64 VMs' worth of
    headroom — it has 64 *connection events*. Measured: a 48-VM fleet whose
    workers flapped during start reached node id 61 before settling.

    The corollary is that anything causing worker churn shortens the fleet. The
    one that bit here was Docker's embedded DNS: one resolver per network, and a
    144-container start burst saturates it, so workers died with `Failed to
    resolve host 'ernic-1': Temporary failure in name resolution`, restarted,
    and burned ids. `vfio-user-ernic-rocjitsu-scale-out` therefore pins a static
    address on every service and resolves no service names at run time. A fleet
    that reintroduces name resolution on the start path will hit this again.

    Secondary symptom of the same churn: `depends_on: service_healthy` against
    a flapping ernic makes compose abort the whole `up` with `dependency failed
    to start: container fleet-ernic-10-1 is unhealthy`, leaving containers in
    `Created` and guests dead with `vfio-user: timed out waiting for reply`.
10. **The mesh manager's 20 s heartbeat timeout turns CPU oversubscription into
    lost VMs, and it is not tunable.** `rocm-ernic --help` exposes no knob. When
    a fleet allocates more vCPU than the host has cores, the manager slips past
    20 s and declares live nodes dead (`Node N failed health check (last
    heartbeat: 20 seconds ago)`). The evicted worker exits, its vfio-user socket
    disappears, and the guest attached to it dies with `vfio-user: timed out
    waiting for reply` — lost guests match restarted ernics one for one.

    Measured on a 128-core / 503G host at 4 vCPU per VM: 40 VMs boot clean in
    117 s, 44 lose 3, 48 lose 7. Memory is never the constraint (48 VMs used
    107G of 503G). Budget by `VM_VCPUS * VM_COUNT <= nproc` plus about 25%,
    not by RAM, and remember `VM_VCPUS` cannot go below 4 on an ernic stack.
11. **The manager leaks a file descriptor per evicted node, and Docker's
    default soft `nofile` is 1024.** This, not CPU directly, is what actually
    kills a large fleet — issues 13 and 14 are the two halves of the mechanism
    and this is where they land.

    The cascade, observed end to end on a 40-VM fleet: the manager slips its
    20 s heartbeat under CPU pressure and evicts live workers (issue 14) —
    eighteen of them before its own guest had even attached. Each evicted
    worker reconnects, takes a fresh node id (issue 13) and a fresh socket,
    and **the manager never closes the old one**. At about one leaked fd per
    eviction the 1024 default is reached well inside a normal run. The first
    casualty is the manager reopening its own stats file:

    ```
    ERROR: rdma: Failed to open stats file /run/ernic-stats/ernic-1.stats: Too many open files
    ERROR: rdma: TCP: Failed to accept connection: Too many open files
    ```

    after which it spins in an `accept()`→EMFILE loop with no backoff — **3.3 GB
    of log in 12 hours**, measured. The manager then goes unhealthy, and because
    it is `restart: "no"` (issue 7) the guest attached to it dies:
    `qemu-system-x86_64: ../util/error.c:62: error_setv: Assertion *errp == NULL failed`.

    `gen_compose.py` now sets `ulimits.nofile` on every ernic service from
    `ERNIC_NOFILE` (default 65536). That does **not** fix the leak, which is
    upstream's; it moves the wall out far enough that a fleet-length run
    finishes first. A fleet that churns hard enough will still get there.

    **Correction: evictions were not the main source.** The manager also
    never closes a connection that disconnects *without registering*, and our
    own manager healthcheck connected to the mesh port every 2 seconds. That
    leaked a descriptor per probe regardless of churn: measured at **28/min on
    a 32-VM fleet with zero evictions and zero restarts**, 1118 sockets in
    `CLOSE_WAIT`, all from `127.0.0.1:<tcp_port>`. On Docker's 1024 default
    that alone is EMFILE in about 37 minutes. The healthcheck now reads
    `/proc/net/tcp` for a `LISTEN` rather than connecting; re-measured after,
    the manager sits at **50 descriptors, flat, zero CLOSE_WAIT**. Anything
    that periodically opens and closes the mesh port will reintroduce this.

    Re-measured at 40 VMs with the limit raised: manager steady at 165 fds,
    zero EMFILE, max node id 31 of 64 (it reached 50–61 before). The long-run
    death is gone. **The boot ceiling is not** — the same run still lost 3
    guests to 24 heartbeat evictions, with the lost guests matching the
    restarted ernics one for one, and load peaked at 417 on 128 cores. Budget
    by issue 14 regardless of what `ERNIC_NOFILE` says.
12. **Stock in-tree `ionic` drives the emulated device — and since
    rocm-ernic@3fb74d1 it binds with no help at all.** On a mainline kernel
    (verified on 7.2.4) no DKMS build and no AMD patch is needed to bring the
    NIC and the RDMA device up. This used to need the id adding to the
    driver's table by hand:

    ```
    echo "1dd8 100a" > /sys/bus/pci/drivers/ionic/new_id   # NO LONGER NEEDED
    ```

    The device moved to `1dd8:1002`, which the in-tree table already held
    (alongside `1dd8:1003`), so it binds natively. `new_id` is now a no-op.
    What the DKMS modules are still needed for is SRQ — see below.

    That yields `ionic 0000:00:05.0: FW: rocm-ernic-1.0`, `Link up - 100 Gbps`,
    an `enp0s5np0` netdev and an `ibv_devinfo` reporting `PORT_ACTIVE` over
    Ethernet (RoCE). `ib_send_bw` then runs and the ernic byte counters move.
    Whether the AMD patches matter for correctness beyond the id table has not
    been established here — but "needs the DKMS package to come up at all" is
    not true on a mainline guest.

    Two caveats on a guest that has only had `new_id` done to it: the udev
    rules are not installed, so the devices keep kernel names (`rocep0s5`, not
    `rocm-rdma-ernic0`), and **IP traffic does not cross the mesh** — ARP goes
    unanswered and ping gets 100% loss even with static neighbours seeded. So
    inter-VM `ib_send_bw` does not work; a loopback run on one guest does, and
    is enough to exercise the QPs and light up the counters. Full inter-VM
    testing still needs the `ernic_guest_setup` configure play.
13. **The mesh saturates at about 40 registered nodes — the 64-node protocol
    cap is not reachable.** Measured by asking for 64 VMs on a 128-core host.
    It settled at **40 of 64**; the other 24 workers never registered, dying in
    a permanent restart loop on:

    ```
    ERROR: rdma: TCP: Registration timeout
    ERROR: rdma: Backend tcp init failed: -1
    Failed to realize PVRDMA device with backend 'tcp:worker:172.31.1.1:6320'
    ```

    **This is not CPU starvation.** Load peaked at 843 during the boot burst,
    but the stragglers still failed identically once the host was back to load
    16, and freeing a slot by stopping a healthy worker let others in — so it
    is a capacity wall in the mesh, not a scheduling delay.

    The cause is that the mesh is a **full mesh**. A worker holds ~N+2 sockets
    where N is the *registered* size, not `VM_COUNT`: 42 sockets measured at
    both the 40-VM and the 64-VM fleet. So registering node N+1 costs N new
    connections and touches every existing node, through a single-threaded
    manager, against a fixed wall-clock registration timeout. The cost per
    registration grows with the mesh until registration cannot finish — around
    40 on this host.

    The 64-node topology payload cap (`rdma_backend_tcp.c:2570`) never came
    into it: max node id reached **30**. The mesh dies of connection
    establishment cost well before its own advertised limit, so treat ~40, not
    64, as the real ceiling and expect it to move with host speed.

    Two consequences worth knowing:

    - **A stuck worker retries forever; its guest gets one chance.** `ernic` is
      `restart: on-failure` and `qemu` is `restart: "no"`, so by the time a
      worker on its fifteenth attempt registers, the VM it exists to serve has
      been dead for ten minutes. The 24 stuck workers matched the 24 dead
      guests exactly.
    - **Failed registrations leak manager fds too, and faster than evictions
      do.** Measured over a six-hour run parked above the wall: **8006 worker
      restarts** and **50 fds/min** at the manager, reaching 18164 open
      descriptors. Extrapolated, that is Docker's 1024 default in **20
      minutes** and issue 15's `ERNIC_NOFILE=65536` in about **22 hours** — so
      raising the limit buys a working day, not an indefinite reprieve. A
      fleet left above the wall will eventually reach EMFILE whatever the
      limit is; the fix is to size the fleet below it.

14. **The TCP backend resolved every GID to the same node — fixed upstream.**
    This is why inter-node RDMA never worked on a fleet larger than two nodes,
    and why `vfio-user-ernic-2vm` was the only stack that ever appeared
    healthy: with a single peer, the wrong answer is also the right one.

    Caught by comparing what the manager assigned against what the resolver
    returned, in the same run:

    ```
    TCP: Registered node 1 at 172.28.1.2        <- ernic-2
    TCP: Registered node 2 at 172.28.1.3        <- ernic-3
    TCP: Registered node 3 at 172.28.1.4        <- ernic-4

    TCP: Resolved GID 254.109.0.3 -> node 1     <- should be node 2
    TCP: Resolved GID 254.109.0.4 -> node 1     <- should be node 3
    ```

    Three distinct GIDs, one answer each time: the first other node that
    instance knew about, regardless of which GID was asked for. The payload
    reached a node with no matching queue pair and the client saw `Completion
    with error at client` (`ib_send_bw` exit 17).

    Fixed upstream by resolving against a per-node advertised GID set, which
    this repo now supplies as `ERNIC_TCP_GUEST_GIDS` — see the scale-out
    README. Re-measured on 32 nodes against the fix: **16 concurrent disjoint
    pairs pass, 8-to-1 incast passes 8/8 at ~11.4 GB/s into one node, 1.72 TB
    moved in a 25-minute soak with zero evictions.**

    Measured performance once it works, so there is a baseline to regress
    against: `ib_send_bw` scales 3.19 MiB/s at 64 B to 4028 MiB/s at 4 MB;
    Above about 256 KB the three bandwidth verbs are
    indistinguishable: repeated sampling shows up to 39% run-to-run spread,
    which swamps any difference between them, so do not quote verb ordering
    or a per-size ranking from a single run. Concurrency is close to linear -- 16 disjoint
    pairs (every node at once) give **20.51 GB/s** aggregate, per-pair sagging
    only ~23% from one pair.
    Latency is 260 us typical at 1 KB; quote `t_typical`, not `t_max`, which
    is nearly 3x higher. TCP/IP over the same L2 runs 200-270 MiB/s at 0.66 ms
    RTT -- about 6x slower than RDMA, since Ethernet crosses the TAP and
    bridge while RDMA goes over the mesh.

    Two things worth keeping from the hunt. **Mesh size is the trigger, not
    the cause** — 2 nodes passes everything, 4/8/32 fail identically, so a
    two-node reproduction will never show it. And **node-id assignment order
    is a red herring**: ids are handed out in connection order and do not
    match node index unless starts are serialised, which is true and
    irrelevant, because the resolver never consulted the id. Serialising
    worker startup costs about 56 s of boot at 32 nodes and fixes nothing.

15. **vfio-user P2P DMA needs a QEMU patch the stock image does not carry.**
    Anything that has one vfio-user device DMA into another's BAR — a peer
    reading GPU memory over the ernic, most obviously — does not work on
    the stock QEMU this repo pins, and does not complain: the BAR window is
    simply never advertised to peer servers.

    The fix is `31e3d06a` on `sbates130272/qemu`, branch
    `dev/stephen/pci-mmio-bridge-submit`: *"hw/vfio: assign region fd to
    RAMBlock for vfio-user P2P DMA"* (`hw/vfio/region.c`, +17).
    `vfio_user_dma_map` skips the fd in DMA_MAP when
    `memory_region_get_fd()` returns -1, and for vfio-user mmap regions the
    fd arrives from GET_REGION_INFO into `vbasedev->region_fds[]` but is
    never propagated to the RAMBlock.

    **The fork image now carries it.** This used to say that every tag of
    `...-qemu-libvfio-user-sbates-fork` was built from `qemu.d757fa6`
    (2026-09-22), one commit short, and needed a rebuild. That rebuild has
    happened: the tag reads `qemu.31e3d06`, which is this patch. So the
    choice is now a real one — the `qemu/xml/overlay-mi100.xml` overlay pins the
    fork and gets P2P DMA; the golden `env/scale-out.xml`, and so everything
    without that overlay, pins the stock image and does not.

    What has NOT changed is that the stock image cannot do it, so a stack
    that needs one vfio-user device to DMA into another's BAR must pin the
    fork deliberately. Do not be misled by `pci-mmio-bridge`, which the fork
    images *also* have and stock does not — it is a different mechanism in
    the same branch, not the one P2P DMA needs.

    Untested here. The fork tag carrying the patch is read from the tag name;
    nothing in this repo has yet run a P2P DMA workload against it.

16. **`ernic_guest_setup` does not build SRQ, and without SRQ there is no
    one-sided RDMA through UCX.** SRQ ships as two separate upstream roles —
    `ernic_srq_dkms` (kernel) and `ernic_srq_provider` (rdma-core) — driven by
    a playbook the galaxy build of the collection does not package.
    `vm-rocm-golden.yml` runs both, together with `ernic_p4_dkms`, in its
    "Build SRQ + P4 ionic stack" play, so the published image has them.

    The failure on a guest without them names nothing it is about:
    `max_srq: 0`, so UCX's `rc_verbs` (which is built on a shared receive
    queue) is declined and only `ud_verbs` is left, which has no one-sided
    RDMA — with the RDMA device present, `PORT_ACTIVE` and perfectly healthy.

    `ernic_srq_dkms_reload` defaults to **false**, and the bake leaves it
    there because no device is attached: the modules land in `updates/dkms`
    and are what the guest loads on its next boot. On a live guest, building
    without the reload is indistinguishable from the build never having
    happened. With it, `max_srq` goes to 256 and `rc_verbs` appears — and
    `rc_verbs` appearing is the proof. The reload drops the netdev's address
    and link.

    Both halves must come from one tree: the device half lives in the ernic
    server container, so the collection SHA and the `ernic.<sha>` in
    `ERNIC_IMAGE` move together.

17. **UCX cannot open an IB device whose name contains `-` or `_`, so the
    udev rename silently disables RDMA.** `ernic_guest_setup`'s rule names the
    IB device `rocm-rdma-ernic0`; UCX derives a memory-domain name from it and
    fails to find it:

    ```
    ib_md.c:1122 IB device rocm-rdma-ernic not found
    # < failed to open memory domain rocm-rdma-ernic for component ib >
    ```

    and then enumerates **no verbs transport at all**. Nothing notices:
    `ibv_devinfo` works, the port is `PORT_ACTIVE`, and NIXL comes up and
    moves every byte over TCP — a cache hit on every lookup and zero RDMA
    bytes on the ernic counters. Observed by accident, as 4/4 chunks hit with
    a counter delta of zero.

    Measured by renaming the same device and re-running `ucx_info -d`:
    `rocm-rdma-ernic0` and `rocm_rdma_ernic0` give no verbs transport;
    `rocmernic0` and `ernic0` give `rc_verbs` + `ud_verbs`. So it is the
    separators, not the trailing digit. Anything using UCX has to rename the
    device first; nothing in this repo does so any more.

    This conflicts with the `ib_send_bw -d rocm-rdma-ernic0` the udev section
    above recommends: a guest set up for UCX will not answer to that name.

18. **A single RDMA READ of 16 MiB tears down the mesh connection.**
    `TCP_MAX_PAYLOAD_LEN` in `rdma_backend_tcp.c` bounds one wire message at
    16 MiB, and the receiver drops the connection on anything over it. The
    initiator sees `NIXL_ERR_REMOTE_DISCONNECT`, and the responder's log fills
    with `SEND from node N to unknown QP M` as the connection is rebuilt.

    Bisected on fresh servers, one READ per request: 8, 12, 14 and 15 MiB all
    pass; 16 MiB (2^24) fails. It is a **per-request total**, not per-chunk —
    16 MiB fails both as 4 × 4 MiB and as 2 × 8 MiB, while 14 MiB passes as
    2 × 7 MiB. So chunking does not get you under it.

    The device advertises `max_msg_sz: 0x80000000` (2 GiB) while the mesh can
    carry 16 MiB, so UCX sizes transfers to a figure the device cannot honour
    and is given a connection teardown rather than a completion with error.
    Keep per-request payloads under ~15 MiB until this is fixed upstream;
    note the fleet's default KV geometry is far larger.

19. **The emulated NIC's PCI id moved and the guests' udev rules did not —
    32 guests sat unusable for 18 hours.** Found 2026-10-20 on `mi100-fleet`.
    The mechanism is worth keeping because every step of it was silent.

    rocm-ernic@3fb74d1 moved the device from `1dd8:100a` to `1dd8:1002`. The
    guests' `99-rocm-ernic.rules` still said `ATTR{device/device}=="0x100a"`,
    so udev matched nothing and renamed nothing. The netdev came up as
    `enp0s6np0` and the IB device as `rocep0s6`. `rocm-ernic-netcfg.service`
    hardcoded `NIC=rocm-ernic0`, waited 30 s for a name that was never going
    to appear, and exited 1 — so no address was assigned, no counters symlink
    was made, and every `-d rocm-rdma-ernic0` in the test drivers failed. The
    guests were up, SSH answered, and `fleet.sh wait` called them ready,
    because it polled sshd. Nothing reported a wrong id; the only symptom was
    a missing device name, which says nothing about why.

    **The subsystem is now part of the identity, and that is the lasting
    change.** `1dd8:1002` is a real Pensando DSC Ethernet Controller id that
    physical cards ship with — hwdata lists ~23 real subsystem variants of it
    — so vendor+device no longer means "the emulation". Only
    `1dd8:5400` does. A rule that matched just the new device id would be
    correct on this host and would rename a real Pensando NIC on a host that
    had one.

    What changed in the repo:

    - `99-rocm-ernic.rules` is carried here now, matching all four
      fields in both stanzas, and `rocm_ernic_setup` installs it rather than
      defaulting to `""` and inheriting the image's copy.
    - `rocm-ernic-netcfg.sh` finds the device by tuple and addresses it under
      whatever name it has, warning when the rename is missing. A missed
      rename degrades; it no longer takes the network down.
    - The `rocm_ernic_setup` module check no longer greps the alias table for
      `d0000100A`. That had become a hard failure that aborted provisioning
      before the rules were installed — and it never proved what it claimed:
      in-tree `ionic` advertises `1002` too, so no PCI id distinguishes the
      patched build. SRQ does.
    - `scripts/fleet.sh wait` requires the tuple *and* an address, not sshd.
      `fleet.sh check <N>` prints each guest's device and address.
    - `tests/test_ernic_identity.py` pins all of it.

    **Where a looser match is still right:** driver binding. `ionic`'s table
    is `pci:v00001DD8d00001002sv*sd*bc*sc*i*` — it wildcards the subsystem
    because the same driver drives real Pensando cards, and it cannot tell
    them apart. That is upstream's and should not be tightened. udev is the
    only layer that can discriminate, so the tuple discipline belongs there
    and in anything scanning sysfs or `lspci`, not in the driver.

## Git / GitHub

- Default GitHub account: `sbates130272` (verify with `gh auth status`)
- GPG signing required on all commits (`-S`), signoff required (`-s`)
- Main branch: `main`
- Never use `--no-verify` or `--no-gpg-sign`

## Cutting a release

`scripts/release.sh <version>` from a clean `main`. It stamps the version in
`qemu/pyproject.toml`, generates the `qemu/debian/changelog` stanza from the
`CHANGELOG.md` `[Unreleased]` section, retitles that section as the new
version, commits signed-off, and makes a signed tag. Run it with `--dry-run`
first to see the generated stanza.

It never pushes; it prints the `git push origin main v<version>` that does.
Pushing the tag is what triggers `qemu-minimal-release.yml`, whose `Verify Version` job
refuses to build anything unless the tag, `pyproject.toml` and
`debian/changelog` agree and `CHANGELOG.md` has a heading for the tag. That
gate exists because v1.3.0 shipped with no tag at all and nothing noticed.

That workflow then builds the wheel, the sdist and the `.deb`, attaches all
three to one GitHub Release, and publishes to PyPI last — a PyPI version can
never be reused, so it goes after everything repeatable has succeeded. PyPI
uses Trusted Publishing against the `pypi` environment, so there is no API
token anywhere in the repo.

## The published site

<https://sbates130272.github.io/qemu-minimal/> is served from the `gh-pages`
branch (Settings > Pages > Source = "Deploy from a branch", `gh-pages` /
(root)), not from a Pages artifact. `.github/workflows/qemu-minimal-publish-pages.yml`
is the only workflow that writes that branch.

**The site carries documentation and performance trends, and no guest
reports.** It is built with MkDocs from `docs/`, plus a `perf/` page that
`scripts/render-site-perf.py` writes from the numbers the benchmark workflow
uploads. MkDocs adds `.nojekyll` itself.

There are five tracked metrics and four artifacts, all uploaded by
`report-for-vm-rocm-golden`:

| Artifact | Metric |
|---|---|
| `perf-rocjitsu` | rocjitsu GEMM, rocjitsu hipFile |
| `perf-hipfile-fio` | hipFile fio |
| `perf-ernic` | Ernic P4 FP8 bandwidth |
| `perf-helios` | SDXI copy bandwidth |

Each artifact holds nothing but `badge-*.json`. `publish-pages` fetches each
*by name* from the most recent run that produced it, into a scratch `perf-in/`
that is never published, so a benchmark that failed today keeps its last good
number. The renderer appends to `perf/history.jsonl` on the branch and writes
the endpoint JSON the README's shields read.

`publish-pages` runs on any non-skipped conclusion of the benchmark workflow,
not only on success. That is deliberate: each benchmark step is
`continue-on-error` with its own upload, and the job then fails on purpose if
any of them failed, so a red run routinely carries good numbers.

Adding a benchmark is five edits: upload its `badge-*.json` under a stable
`perf-<name>` artifact, add a `fetch` line in `publish-pages`, add the
workflow's `name:` to `on.workflow_run.workflows` if it is a new workflow, add
the metric to `METRICS` in `render-site-perf.py`, and add the endpoint badge to
the README. Miss the trigger and the number publishes only when some other
workflow finishes, which looks like a stale site rather than a missing trigger.

**`report-for-vm-rocm-golden` runs the stack the way a person would.** It
drives `qemu/xml/rocm-golden.xml` through `qemu-tool quick-start up` and
`quick-start test`, then runs the four benchmark steps with
`scripts/generate-vm-report.sh`. The markdown report that script writes is
left on the runner. No settings live in the workflow's `env:`.

`report-for-vm-basic` and `report-for-quickstart` still exist as tests, and
still upload report artifacts, but nothing fetches them.
report-for-quickstart runs `qemu-tool quick-start`, the same command the
README's Quick Start names, so a change that breaks the documented path turns
that lane red. It also checks that Prometheus and Grafana are running and that
Prometheus answers a query: they are part of the documented quickstart, and a
lane that only looked at the guests would pass with half the stack down.

**`quickstart-boot` is the PR-time half of that lane.** report-for-quickstart
runs on push to main and on a cron, so a broken quickstart was found the
morning after the merge; `Gen Compose Check` renders `qemu/xml/quickstart.xml`
on every PR but starts nothing. This one boots **one** guest and asserts the
emulated NIC is there by its full `1dd8:1002` + `1dd8:5400` tuple and that
`ibv_devinfo` reports `PORT_ACTIVE`, then stops. It publishes nothing.

Three choices in it are load-bearing. **One guest, not two**: the 4 vCPU floor
is real and two guests on a 4-core runner is 2x oversubscribed against the
mesh manager's untunable 20 s heartbeat — tolerable once a day, not on a check
that gates merges. At `VM_COUNT=1` the mesh is just the manager and
`gen_compose` omits the worker branch entirely. **Its settings are an
overlay**, so `qemu/xml/quickstart.xml` stays the file a reader diffs and the
file the daily lane runs. **The IB device name is resolved from
`ibv_devinfo -l`**, never hardcoded: the published image ships no udev rules,
so the device keeps its kernel name.

A badge for a workflow that does NOT run on every push to main wants no
`?branch=main`: shields pins that query to main's latest run, and a weekly or
dispatch-only workflow can have none for days, which renders as "no status"
rather than as its real result.
