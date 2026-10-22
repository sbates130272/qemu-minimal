# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- **`qemu-tool ssh <vm>`**, which reaches a guest through exactly the
  resolution `inspect` prints: `records()` is factored out of `inspect.run()`
  so the two cannot disagree. The username comes from the image's own sidecar
  and an `-i` only when the guest names a key whose private half is on this
  host; when it does not, no identity is offered -- that only buys a refusal --
  and a note says why *before* the password prompt, because an unexplained
  prompt reads as a broken VM rather than a missing key. Host-key checking is
  off by default: guests built from one backing image share a host key and the
  fleet reuses localhost ports, so strict checking fails as a matter of course
  and `known_hosts` entries would poison the next fleet. `--strict-host-key`
  restores it. It `execvp`s, so ssh gets the real tty and the caller sees ssh's
  own exit status.

- **`qemu-tool inspect <vm>` takes the VM positionally**, alongside
  `--vm-name`. Naming it both ways with different values is an error rather
  than a precedence rule nobody can remember.

- **`qemu/env/overlay-dense`**, a 32-VM fleet with two GPUs, two ernics and
  eight NVMe per guest on the sbates-fork QEMU, for exercising
  `ERNIC_PER_VM`/`ROCJITSU_PER_VM` at the 64-node mesh cap.

- **A report lane for the MI355X node shape**, published at
  `/mi355x/` on the site. `gen-compose --check` already proves the committed
  render is byte-identical, and that says nothing about the tree the guest
  enumerates — a flat PCIe tree boots exactly as happily as a switched one
  and nothing in the guest complains. So the lane boots a node and publishes
  its `lspci -tv`, alongside the bound GPU count, the RDMA devices, the NVMe
  and what `inspect` reports about the live guest.

  It runs `scripts/mi355x-node.sh`, the script the stack's README names, for
  the reason report-for-quickstart runs `quickstart-2vm.sh`: a documented
  path that nothing executes rots.

  **One VM where the lab stack is four**, via the committed
  `qemu/env/overlay-mi355x-ci` — a runner has 4 cores against the stack's 32.
  The overlay reduces the fleet and the guest's memory and *nothing* about
  the topology: `ERNIC_PER_VM`, `ROCJITSU_PER_VM` and `VM_PCIE_SWITCH_WIDTH`
  keep their golden values, because a lane that quietly halved them would
  publish a green report about a shape nobody runs. The corollary is stated
  in the report itself: with one guest there is no second node, so its RDMA
  figure is a **loopback** run that proves the verbs, never the mesh.

- **`VM_NVME=2` on the MI355X stack.** It had none, where scale-out and
  quickstart each take one, so `inspect` reported no storage on a node shape
  meant to resemble a real one. Two rather than one is the smallest count
  that is visibly a *set*: anything that enumerates drives, stripes across
  them or picks one by index behaves differently at two, and at one those
  paths are never taken.

- **An MI355X-shaped PCIe topology stack.** `vfio-user-mi355x-node` gives each
  VM one EPYC socket, 8 gfx1250 GPUs, 8 ernic NICs and 8 PCIe switches, with
  one NIC and one GPU below each switch and each switch on its own root port.
  Every other stack here hangs one NIC and one GPU flat off the root complex,
  so there was nothing to exercise software that reads the PCIe tree — ROCm
  topology detection, affinity heuristics, P2P path selection — against.

  `run-vm` grows `--vfio-user-switch-width N`, which groups `--vfio-userdev`
  into `x3130-upstream`/`xio3130-downstream` switches N at a time in the order
  given; `gen-compose` round-robins the NIC and GPU socket lists so each
  switch gets one of each. Two things that are easy to lose and silently
  break the result: `hotplug=off` on all 24 ports, without which their
  hotplug MMIO reservations exhaust the 32-bit hole and the guest fails BAR
  assignment for devices already present, and `--pci-hole64-size`, without
  which eight GPUs' BARs do not fit. The hole is bounded by the CPU's
  physical address width — `-cpu EPYC` gives 40 bits, and QEMU 10.2.1 accepts
  512G but refuses 1T outright.

  `gen-compose` grows `ERNIC_PER_VM`/`ROCJITSU_PER_VM`. Both default to 1, so
  the committed scale-out stack regenerates byte-identical. Above 1, per-NIC
  identity (MAC, guest IP, GID, fleet address) is keyed on a global NIC
  ordinal rather than the VM index, and the mesh registration chain
  serialises across VM boundaries. **Each ernic is its own mesh node**, so 8
  per VM caps the fleet near 4 VMs rather than 40 — the ceiling check now
  counts nodes and names the product, because `VM_COUNT` is not the number
  that is too large.

- **`run-vm --cpu-sockets N`.** Bare `-smp cpus=N` leaves QEMU to infer
  `sockets=N, cores=1`, so every guest this repo has booted reports N
  single-core EPYC sockets rather than one CPU. Off by default; the MI355X
  stack sets 1.

- **A playbook that bakes the ionic SRQ stack into a guest image**,
  `ansible/playbooks/vm-ernic-srq.yml`. Stock v7.2.4 has no SRQ in
  `drivers/infiniband/hw/ionic`, and no rdma-core release — nor master — has
  `create_srq` in `providers/ionic`, so a guest built from either reports
  `max_srq = 0`. UCX then declines `rc_verbs` and offers only `ud_verbs`: no
  one-sided RDMA. Measured, NIXL at **0.064 GB/s** against MORI_IO's
  **0.355** at 1 MiB on a device where `ib_write_bw` reaches **594 MB/s**,
  and nothing in that failure names SRQ.

  rocm-ernic already carries both halves — `patches/srq` for the kernel
  driver, `patches/rdma-core` for the provider — and this repo already pins
  that collection. What was missing was a way to apply them to an **image**
  rather than to a running fleet: `srq_driver.yml` does the kernel half only,
  inside `lmcache_guest`, because the provider that creates the SRQ there
  lives in a container.

  Three things differ from the fleet path, each only sensible at build time:
  no module reload (nothing is attached to reload for, and `rmmod ionic` on
  the emulated device can hang on a DSR timeout and take the build with it);
  the kernel pin is asserted rather than warned about; and the artefact is
  checked with `nm` rather than `strings`, because `strings ionic_rdma.ko |
  grep -i srq` returns a dozen BTF hits on a module with no SRQ support and
  zero on a build without BTF — a difference that is not there.

  Both halves or neither: with only the kernel one the device advertises an
  SRQ and `ibv_create_srq()` returns `EOPNOTSUPP`, which is a more confusing
  guest than one that never claimed to do SRQ. It is deliberately not run
  from `gen-vm --ansible-playbook`, which happens before a consumer installs
  its pinned kernel.

- **SRQ detection is checked on every PR**, in `tests/test_srq_detection.py`,
  44 ms. The capability itself needs a booted guest, so these cover what
  fails silently instead: the detection pipeline is lifted out of
  `srq_driver.yml` *by pattern* and run against real `ibv_devinfo` shapes, so
  editing the role without editing the test fails here; and
  `ernic_srq_dkms_reload` is asserted on, because upstream defaults it false,
  which installs the modules while the running kernel keeps the old ones —
  indistinguishable from never building, since `max_srq` stays 0. Before
  this, SRQ was exercised only by `report-for-lmcache`: weekly, behind an
  11 GB container build, and red at the time.

- **`qemu-tool man`, because only the `.deb` ever put the page on
  `MANPATH`.** The manual ships as package data in the wheel, and neither
  pipx nor uv has any mechanism for installing a man page — measured, not
  assumed: `uv tool install ./qemu` leaves it at
  `…/site-packages/qemu_tool/man/qemu-tool.1` with no `share/man` anywhere.
  So every non-Debian install carried a manual nothing could read.
  `qemu-tool man` hands that bundled file to `man(1)` directly, and
  `qemu-tool man --path` prints its location.

- **The manual page is now checked against argparse.** It had drifted exactly
  as unread prose does: `inspect` and `lmcache-stats` were missing from the
  SYNOPSIS and had no OPTIONS section at all, `list --summary` — the largest
  addition of this release — was not mentioned anywhere, and `--mgmt-tap`,
  `--vfio-user-root-port` and `--vm2-name` had never been written up. All are
  documented now, and `tests/test_man_page.py` asserts that every subcommand
  appears in the SYNOPSIS, has an OPTIONS section, and has every long option
  documented. It caught `qemu-tool man` itself while that command was being
  added.

- **`qemu-tool compose` remembers the stack.** From inside a directory
  rendered by `gen-compose --output-dir`, `qemu-tool compose ps` now works
  with no flags at all, the way `docker compose` works beside a
  `compose.yaml`. The memory was already on disk: every generated
  `docker-compose.yml` has always recorded its env layers in its provenance
  header, and until now nothing but a unit test read them back, so each
  invocation had to repeat the directory and both layers — which is the only
  reason a wrapper script existed.

  The stack directory is found by `--stack-dir`, then `$QEMU_TOOL_STACK`
  (the same shape as `$QEMU_TOOL_ENV`, so a fleet is selected once per
  shell), then the current directory if it is itself a rendered stack.
  Recognition is the provenance header, not the file name, so an unrelated
  docker project is never adopted, and there is deliberately **no walk-up**:
  `docker compose` can walk up because what it finds is what it runs, whereas
  here a walk-up from inside the checkout reaches `qemu/compose/<default>/`
  and would drive the committed 8-VM stack under the name of a 32-VM fleet —
  the exact silent substitution `--stack-dir` was added to prevent.

  Recorded layers are resolved as typed: absolute as themselves, relative
  against the stack directory and then the cwd. A layer that is at neither,
  or a generated stack whose header names no layers, is an error naming the
  path and where it was looked for. It never falls back to the ambient env
  search path, because that is how a 32-VM YAML gets driven by an 8-VM
  `qemu/.env`.

  The staleness check matters more, not less, now that the inputs need not be
  typed, so it still re-hashes the layers' current bytes — adopting the
  recorded names is not taken as satisfying the recorded digest. It also now
  distinguishes the two faults: content drift ("generated from a different
  version of …") from being handed layers that are not the ones the stack was
  rendered from ("was rendered from A, B; you passed A, C").

  Not absorbed, deliberately: `compose` does not regenerate and does not edit
  settings files (resizing a fleet stays an explicit `VM_COUNT` edit plus
  `gen-compose --output-dir`; a tool that rewrote `VM_COUNT` as a side effect
  of `up` could write it into the tracked `qemu/env/scale-out` and break
  `Gen Compose Check` from a command that looks like a start), and it is not
  a command runner — a whole fleet is `ansible -i <stack>/fleet-inventory.yml
  fleet`, which `gen-compose` already renders, and one guest is the `ssh`
  command `inspect` already prints. See the README and the man page.

  `Gen Compose Check` renders a 3-VM stack to a scratch directory and runs
  `qemu-tool compose config` inside it with no flags. The assertion is on the
  project name — which `docker compose` derives from the working directory —
  and on the service list, not on the exit status: resolving to the wrong
  stack also exits 0, and that is the failure worth catching. A second run
  one directory up asserts the stack is *not* adopted from outside it.

- **`qemu-tool list --summary`** — a per-fleet aggregate of the same bytes
  `list` already prints, plus a measured load picture across CPU, memory,
  storage, the RDMA mesh, GPUs and logged-in users. VMs are grouped by Docker
  Compose project, so two stacks on one node stay two fleets.

  Four sections: a header roll-call (`qemu 32/32 ernic 32/32 rocjitsu 32/32
  infra 1/3`), `HEADROOM` (how many more VMs fit by each ceiling and which one
  binds), `LOAD`, and `OUTLIERS`. The headroom ceilings are the measured walls
  this repo records rather than nominal limits: the mesh stops registering at
  about 40 nodes (issue 17, *not* the 64-node payload cap) and vCPU is budgeted
  at `nproc` plus 25% (issue 14). On the 32-VM fleet the mesh binds at +8 VMs
  while vCPU still shows +28, which is the point of printing all four.

  Several numbers are deliberately not the obvious ones:

  - CPU is split by service class, because the ernic mesh sidecars burn as much
    host CPU as the VMs they serve — a summary reporting only QEMU understates
    the fleet's cost on the node by over 2x.
  - Guest RAM resident is cgroup `memory.stat` **shmem**, not `anon`. Guest RAM
    is a `memory-backend-memfd` with `share=on`, so anon across 32 qemu
    containers is 2.0 GiB against 45.5 GiB of shmem — anon is 20x low.
  - Ethernet is the **tx** sum only. The `l2` bridge floods every frame to every
    guest, so summing rx counts the same ~1 MB once per guest and reports 31 MB
    of traffic on a fleet that offered 1.
  - Disk is the **host** figure. Asking the guests gives 301 GiB used across 32
    guests against the 26.7 GiB this node holds, because every guest counts the
    whole shared backing file as its own. Both are right; only one decides
    whether another VM fits.
  - GPUs are reported as count and gfx target with *utilisation not measurable*
    spelled out. rocjitsu exposes no stats interface at all and these guests
    have no amdgpu, so a busy% would be a number nobody could falsify.

  Every collector has a defined unavailable state and renders it rather than a
  zero, including the unknown bucket for guests whose agent could not answer:
  an idle fleet and a fleet whose stats files could not be read must not look
  the same. An ernic instance whose stats file is caught mid-rewrite shrinks
  the denominator (`31/32 ernic stats read`) instead of contributing a zero.

  `--no-guests` drops the one collector that contacts a guest; the users line
  then reads `not collected`. Measured on the live 32-VM fleet: 7.0 s with
  guests, 6.9 s without, of which 5 s is the sample window, which cannot shrink
  below ~2 s because the ernic servers rewrite their stats at 1 Hz. `docker
  stats` is used nowhere — it costs 4.2 s for 100 containers against 45 ms for
  the same figures out of cgroup v2 sysfs. Plain `list` is unchanged and still
  never opens a guest socket.

  `--summary --json` emits one object whatever the VM count — deliberately not
  `inspect --json`'s shape, which is a dict for one VM and a list for many —
  carrying `sampled_seconds`, a `source` per metric group and units in the key
  names.

- `qemu-tool list --json` gains an `image` field on each `nvme` entry: the
  scratch qcow2 backing an emulated controller, or null for a passed-through
  one, which has no host-side image at all. Those images carry the bulk of a
  fleet's virtual size (1 TiB each against the root overlay's 512 GiB) and were
  parsed out of `argv` and discarded.

- **An `NVME` column on `qemu-tool list`.** Every NVMe attached to a VM,
  emulated or passed through, from the same per-PID `argv` parse the GPU, RDMA
  and VFIO-USER columns already come from. Repeats collapse (`emul*2`), and a
  VM with none prints `-`.

  The column says which address space it is quoting, because the two are not
  interchangeable and a BDF alone does not say which one it is. A passthrough
  entry is a **host** BDF — `-device vfio-pci,host=0000:03:00.0` names a
  device on this machine — so it is printed labelled, as `host:03:00.0`.
  Since a GPU and a NIC are passed through by exactly that same argument, the
  class is read from `/sys/bus/pci/devices/<bdf>/class` and only `0x0108xx`
  is claimed as NVMe; a BDF this host has no sysfs entry for keeps a trailing
  `?` rather than being dropped, so an unverified entry is visible but never
  mistaken for a confirmed one.

  An emulated NVMe prints its **guest** BDF, `00:05.0`, but only where the VM
  has a QMP monitor to ask: `run-vm` emits `-device nvme,...` with no `addr=`,
  so QEMU assigns the slot at realize time and the guest address is simply not
  on the command line. With `--qmp-socket` the entry is upgraded from
  `query-pci` (proxied with `docker exec` for a containerised VM, since the
  socket lives in the container's filesystem namespace); without one it reads
  `emul`. `--json` gains an `nvme` array carrying the canonical BDF, the kind,
  and whether the address came from `argv` or `qmp`.

- **CI assertions on what `list` and `inspect` report about a live guest.**
  `scripts/check-vm-devices.sh` takes a running VM's name and demands the
  positive value for every field the device reporting produces: the `NVME`
  column is not `-`, `inspect` names a block device on the `nvme` bus with a
  non-zero capacity and a resolved origin, the GPU names a gfx version and an
  origin, the SSH key is the one the guest's own `authorized_keys` carries
  (`ssh_key_source == "guest"`, accepted, found locally) rather than the
  configured default labelled unverified, and `Login` names the expected user.

  Positive values rather than exit status, because every one of these fields
  degrades to a plausible-looking placeholder — `-`, a missing line,
  `configured default, unverified`, `unknown` — with the command still
  exiting 0, so a lane that only ran it would stay green through a total
  regression. It reads `--json` rather than the rendered lines, which are
  prose and get reworded; the sole exception is the `NVME` column, which
  exists only in the table and is read by field index off the header, never
  by character offset. On failure it prints the full `list` and `inspect`
  output, since a CI failure nobody can diagnose from the log gets rerun
  rather than fixed.

  Two lanes run it, for a few seconds each. `report-for-quickstart` covers
  all four fields daily on a guest that has an emulated NVMe, an emulated
  gfx1250 and an ernic at once; it gained `--pid=host` on its job container,
  without which `list` and `inspect` enumerate a `/proc` holding none of the
  sibling containers' QEMU processes and print an empty table.
  `smoke-test-qemu-tool` covers the half no compose stack can reach, on
  every PR: it
  passes `--qmp-socket` to `run-vm` and requires a guest BDF rather than
  `emul`, which `gen-compose` can never emit. Both open their own SSH session
  first — nothing else in either lane holds one, and these guests have no
  `/var/run/utmp`, so the login comes from logind state and is only non-empty
  while somebody is logged in.

  `quickstart-2vm.sh` now also writes the pulled guest key's public half to
  `<images>/keys/`, one of the places `inspect` looks; without it the Key line
  reads "accepted by guest, not found locally" and the printed `ssh` command
  carries no `-i`.

- **A Quick Start that CI runs every day.** `scripts/quickstart-2vm.sh` takes a
  machine with KVM and Docker to two guests — each with an emulated gfx1250, an
  emulated NVMe and an emulated rocm-ernic RDMA NIC — and then proves both
  devices: `vector_add` on each GPU, `ib_send_bw` between the guests, and the
  NIC's own `total_bytes_sent` counter as the acceptance test, because a
  passing `ib_send_bw` proves the verbs worked while only the counter proves
  the bytes crossed the mesh.

  Nothing is built. The guest is the published `ernic-rocjitsu` qcow2, which
  already carries the mainline kernel, the ionic-ernic DKMS modules, the udev
  naming rules, rdma-core, perftest and ROCm 10.1, and the device servers are
  the tagged CI images. What the image does *not* carry is the per-rocjitsu
  `ip_discovery.bin`, an address on the ernic and a loaded `amdgpu` — so
  `ansible/playbooks/fleet-quickstart.yml` stages those three and nothing else,
  reusing `lmcache_guest`'s `amdgpu_firmware` and `netplan_ernic` task files
  and `rocjitsu_gpu_test`'s `vector_add.hip` rather than copying them.

  The stack comes from `qemu/env/quickstart`, a two-VM cut of the scale-out
  settings generated to a scratch directory, with `VM_NVME=1` and
  `ERNIC_TAP=true` — the TAP is not optional, since the TCP mesh carries RDMA
  payload only and without Ethernet `ib_send_bw` cannot do its out-of-band
  exchange at all.

  `report-for-quickstart` runs that same script daily and publishes to
  `/quickstart/` on the site. One script for the README and the lane, on
  purpose: a documented path nothing executes rots, and a lane that executes
  something else proves nothing about the documentation.

- **`qemu/docker/`, and an LMCache image for gfx1250 in it.** One directory
  per image, so the next Dockerfile has somewhere to go.
  `qemu/docker/lmcache/` builds LMCache v0.5.5 from source with
  `BUILD_WITH_HIP=1` and `PYTORCH_ROCM_ARCH=gfx1250`. A source build rather
  than a wheel because upstream publishes neither: PyPI's `lmcache` is a CUDA
  wheel, and the official ROCm wheels cover `gfx942,gfx950` only.

  The base is `rocm/pytorch:rocm10.1.0_ubuntu24.04_py3.12_...`, and both
  halves of that are forced rather than preferred. A PyTorch base because
  torch is needed *before* the build runs (`--no-build-isolation`, hipcc
  against torch's own headers) and cannot be installed — the newest
  `download.pytorch.org/whl/rocm*` channel is `rocm7.2`, with `rocm10.0` and
  `rocm10.1` both 403. Ubuntu 24.04 rather than 26.04 because every ROCm 10.1
  / 26.04 tag is py3.14 and LMCache 0.5.5 pins `requires-python <3.14`.

- **An `lmcache` compose profile on the scale-out stack, in MP mode.** An MP
  coordinator (`lmcache coordinator`) beside the vfio-user device servers, and
  an MP cache server (`lmcache server`) inside each of the N guests, with L1 in
  host memory, L2 on an emulated NVMe, and P2P reads between guests over the
  ernic.

  Both are upstream entrypoints. Deliberately **not** `lmcache_controller`,
  which is the legacy in-process mode's controller: it waits on a ZMQ
  pull/reply pair for `LMCacheWorker` registrations an MP server never sends,
  so pairing the two leaves every guest registered with nothing and nothing
  erroring.

  The split across the VM boundary is forced: compose has no reach into a VM,
  so the per-VM servers cannot be services. They are started by a systemd unit
  that the new `lmcache_guest` role installs, whose `ExecStartPre` is where the
  interesting failures live. It runs `amdgpu-probe` rather than `modprobe
  amdgpu` — the image keeps amdgpu blacklisted and ships that helper with the
  emulation parameters, and unloading to retry spins at 100% CPU indefinitely.
  It finds the ernic by **MAC and PCI association** rather than by name,
  because the udev rules that produce `rocm-ernic0`/`rocm-rdma-ernic0` ship
  with `ernic_guest_setup` and without them the kernel names stand (measured:
  `enp1s0np0`, `rocep0s7`). It registers `1dd8:100a` with the in-tree ionic via
  `new_id`, which otherwise claims only `1002`/`1003` and binds nothing with no
  dmesg line at all. And it **reassigns the ernic address**, because only
  module loading survives a guest reboot (AGENTS.md issue 12) — with P2P, a
  guest that comes back without its address advertises an endpoint no peer can
  reach.

  KV stays out of device memory on purpose: rocjitsu serves single-threaded,
  the same reason the stack already declines to scrape the in-guest GPU
  exporters.

  Images reach the guests over a 9p host share (`VM_FILESYSTEM`) rather than
  being copied — one file read by all N guests instead of ~2.8 GB per VM, which
  is 22 GB of transfer at `VM_COUNT=8` and 112 GB at 40.

  `LMCACHE_ENABLE` gates *emission*, `--profile lmcache` gates *running*. Both
  exist because `prometheus.yml` has no notion of a profile, so scrape jobs
  emitted for an unselected profile would be permanently-down targets.

- **Grafana in the `metrics` profile, with a provisioned fleet dashboard.**
  Datasource and dashboard come from files under the stack directory rather
  than from clicks, so they survive `down`; the dashboard directory is mounted
  read-only and `allowUiUpdates` is off, since a UI that accepts an edit it
  cannot persist is worse than one that refuses it. Per-VM panels are bar
  gauges keyed by `vm_name` rather than one line per VM — a line per VM stops
  being readable well before this stack's ~40-node ceiling — and every
  timeseries panel is single-axis.

- **`fleet-inventory.yml` is generated alongside the compose file.** The
  fleet's ansible inventory is derived from the same `VM_COUNT`, SSH ports and
  guest IPs as everything else, because the failure mode of a hand-kept one is
  quiet: a playbook against an inventory listing six of eight guests
  configures six and reports success. VM name, `lmcache_instance_id` and the
  Prometheus `lmcache_instance_id` label are all the same string, so a panel
  can join a scraped series to a controller query with no lookup table.

- **`ERNIC_TAP` gives fleet guests working Ethernet.** rocm-ernic's TCP mesh
  carries RDMA payload only, so without a TAP the guest driver transmits, the
  device drops the frames, ARP goes unanswered and `ib_send_bw` can never
  complete the out-of-band exchange that precedes an inter-node transfer —
  inter-node RDMA was simply not possible on any generated fleet. Each ernic
  now gets a TAP that a short-lived sidecar bridges onto a shared `l2`
  network, putting every guest on one L2 segment. Measured after: 31/31 peers
  reachable from one guest on a 32-VM fleet, where it had been 100% loss.

  `CAP_NET_ADMIN` lives in that sidecar, which exits once the wiring is up.
  The long-running server needs only `/dev/net/tun` — attaching to a
  pre-created, owned TAP needs no capability, verified against a fleet running
  at Docker's default capability set. The sidecar image is built by compose
  from a committed context rather than pulled: 32 containers each running
  `apk add` at start is the same start-burst that already cost twelve guests
  when `pipx install` raced pypi.org.

- **Per-node `ERNIC_TCP_GUEST_GIDS`.** The mesh resolves a destination GID to
  a node by asking each node which GIDs it owns, so the set is inherently
  per-node and cannot live in the fleet-wide anchor. Derived here — EUI-64
  link-local from the fleet MAC plus the guest IPv4 — rather than restated in
  the settings file, and canonicalised through `ipaddress` so a MAC with small
  leading octets cannot produce a non-canonical address the mesh will not
  match.

- **`PROM_DATA_DIR` bind-mounts the Prometheus TSDB.** A named volume buries
  the record of a run under `/var/lib/docker/volumes` as root, invisible to
  whoever ran the fleet and silently reused by the next run of a different
  size. The container runs as `nobody`, so the directory needs
  `chown 65534:65534` before `up`.

- **A `Gen Compose Check` CI lane.** AGENTS.md and the stack README have both
  claimed "`gen-compose --check` fails in CI" since the generator landed. It
  did not — no workflow ran it, so a hand edit to the generated tree, or a
  settings change without a regenerate, would have shipped undetected. The
  lane now runs the unit tests, `--check`, and `docker compose config` against
  both the committed stack and a tap-enabled render, since `ERNIC_TAP` is off
  by default and the committed copy only exercises one of the two shapes.

- **`--env-file` is repeatable, and layers last-wins.** Pass it more than once
  to `run-vm`, `gen-vm`, `gen-compose`, `compose` or `inspect` and the files
  apply left to right, key by key: a key a later file sets wins, a key it
  omits keeps the earlier value.

  ```bash
  qemu-tool gen-compose \
      --env-file qemu/env/scale-out --env-file qemu/env/overlay-mi100
  ```

  `compose` passes each layer through to `docker compose` as its own
  `--env-file` in the same order rather than merging them itself, because
  `docker compose` already takes the flag repeatedly and applies the same
  rule — verified on compose v5.3.1 — so both sides resolve every key
  identically instead of nearly so.

  Only `--env-file` layers. The search path (`$QEMU_TOOL_ENV`, `./.env`,
  `qemu/.env`, …) still resolves to exactly one file: layering is asked for
  on the command line and never inherited from the ambient environment.

  A generated stack records one `# env-file:` line per layer and a single
  `# env-sha256:` over all of them concatenated, so an edit to the overlay
  makes the stack stale exactly as an edit to the golden file does. The digest
  of a single layer is unchanged, so stacks generated before this still parse.

### Fixed

- **`ernic_fleet_guest` installed no udev rules under its own defaults.** The
  rules copy and the `modules-load.d` entry lived in `driver.yml`, which
  `main.yml` includes only `when: ernic_fleet_ionic_modules_dir | length > 0`.
  That dir is empty by default and is *meant* to be -- in-tree `ionic` binds
  the emulated device natively since rocm-ernic@3fb74d1 -- so the documented
  configuration installed neither, while AGENTS.md stated this role installs
  them. Measured on a 32-guest fleet: 64 NICs, none renamed, none addressed.
  Naming moves to `naming.yml` and is included unconditionally; `driver.yml`
  keeps its gate, because the patched modules really are optional. Confirmed
  over `qemu-nbd` that the published `g742e8e2` qcow2 ships no rules of its
  own either, so such a guest had none from any source.

- **The udev netdev rule could only name one NIC per guest.**
  `NAME="rocm-ernic%n"` takes the index from the kernel number, and every
  emulated NIC comes up as `...np0`, so `%n` is 0 for all of them and the
  second rename failed with the name in use. Now a free-index search -- the
  fix the InfiniBand stanza already carried -- plus a `KERNEL!="rocm-ernic*"`
  guard for idempotency. Unreachable while fleets had one NIC per guest;
  certain at the MI355X node shape's eight.

- **`rocm-ernic-netcfg.sh` configured only the first NIC.** It now walks every
  matching device, each deriving its address from its own MAC, which matches
  `gen-compose` keying MACs and IPs on the global NIC ordinal. The hostname
  still comes from the first NIC only: it names the guest, not the device.

- **`memlock` half-applied the RDMA limit.** The drop-ins go to
  `/etc/systemd/{system,user}.conf.d` in a loop, `copy` does not create the
  parent, and a stock Ubuntu cloud image has `system.conf.d` but not
  `user.conf.d` -- so system units got `infinity` and the `systemd-run --user`
  perftest servers, the ones that needed it, kept the 8192 KB default. That
  surfaces as "Couldn't create CQ" at 4 MB, naming neither the limit nor the
  directory.

- **`gen-vm` waited out a QEMU that had already exited.** QEMU refuses to start
  on a hostfwd whose host port is taken, which a fleet settings file invites:
  the fleet runs QEMU in a per-VM netns where every guest can bind 9100, while
  `gen-vm` runs it on the host where that port may be a service. The error
  scrolled past and the run failed ten minutes later with "VM did not accept
  SSH in time", blaming the guest. `_wait_for_ssh` now polls the process and
  reports its exit status immediately.

- **`inspect` truncated the disk list at six**, in guest-agent order, then
  `+N more` -- hiding a third of the devices on an 8-NVMe guest and possibly
  the root disk. It now lists every disk, boot disk first and labelled
  `Boot Disk:`, found by mountpoint rather than position, with the rest in
  natural order so `nvme10n1` follows `nvme9n1`.

- **A mounted NVMe showed no mountpoint at all.** `_mountpoints` found a
  partition's parent by stripping trailing digits -- right for `vda1` -> `vda`,
  wrong for every NVMe, where `nvme1n1` became `nvme1n` and named no device.


### Changed

- **Image pins moved to the `20261021.g742e8e2` build** in
  `qemu/env/scale-out` and `qemu/env/overlay-mi100`. Component SHAs are
  unchanged, so this is a CI base-layer refresh; both files move together or
  `_check_image_coherence` errors on the mixed build stamp.

- **Every pinned image moves to the 2026-10-21 build, `g0f1f43f`.** The pins
  were a coherent 2026-10-14 set and a week stale. Only two components
  actually changed — `qemu-libvfio-user` and `rocm-rocjitsu` are the same
  builds restamped — but the ernic move, `3fb74d1` to `31d3114`, is 132 files
  and lands on limits this repo documents as live:

  - *close and replace a worker's dead connections to its peers* (#287) —
    the manager fd leak of AGENTS issue 15, which wrote 3.3 GB of log in
    twelve hours and is what eventually kills a long fleet run;
  - *take a worker's node id only from its manager's first answer* (#286) —
    the id churn of issue 13, the mechanism behind the ~40-node ceiling;
  - *lock down the vfio-user socket* (#285) — owner-only by default, with
    `--socket-group`. Safe here because `CONTAINER_USER` is applied to both
    `x-ernic-common` and `x-qemu-common`, so the server and QEMU already
    share a uid; CI lanes set no user at all and run as root.

  `ansible/requirements.yml` moves with `ERNIC_IMAGE` to
  `31d311417ff74e914f1f4c3b08833c2fe98a2117`, as AGENTS issue 20 requires —
  the two halves of SRQ ship from one tree.

  **The mi100 fork pin needed checking rather than bumping.** Its branch,
  `dev/stephen/pci-mmio-bridge-submit`, was rebased between builds, so
  `qemu.aa4344e` *diverges* from the `31e3d06a` the overlay used to name —
  106 ahead, 12 behind, that id in neither history. The P2P DMA change
  survived the rebase as `5de56a35c`, verified by subject, so the capability
  is intact; the comment now says to check for the change and not the id.

- **vfio-user devices go behind a PCIe root port by default.** Off is not
  cosmetic. Without a root port the device is a conventional PCI endpoint
  with no Express capability and a 32-bit prefetchable BAR; measured on the
  emulated rocjitsu GPU, same guest and image, only this changed:

  ```
  off: Region 0: fd000000   (32-bit, prefetchable) [size=16M]
  on:  Region 0: c040000000 (64-bit, prefetchable) [size=256M]
       Capabilities: [40] Express (v2) Endpoint
  ```

  16M cannot be a VRAM aperture, so `amdgpu`'s memory controller never
  initialises and the probe unwinds with `-22`. The module itself still
  loads, so `lsmod` looks healthy and no KFD node is created — and the first
  complaint arrives from HIP as `no ROCm-capable device is detected`, three
  layers from the cause. That has already produced one wrong diagnosis.

  `--no-vfio-user-root-port` and `VM_VFIO_USER_ROOT_PORT=false` keep the old
  topology, and `gen-compose` now **warns** when a stack renders GPUs with it
  off, naming the BAR rather than leaving HIP to misreport it. The CLI gained
  the negative form because with the default flipped the positive flag could
  only restate the default, which would have made the settings file the only
  way to ask for the old behaviour.

  `qemu/env/overlay-mi100` no longer forces it off. That override was
  deliberate and transitional — it kept the mi100 fleet's enumeration stable
  while NVMe was added — but env files layer, so anything built on
  `scale-out` **plus** that overlay silently inherited a GPU that could not
  bind. A two-VM cluster hit exactly that.

  Necessary, not sufficient: 256M is still short of a gfx1250 aperture, and
  the remaining limit is the rocjitsu device model's BAR size.

- **The emulated ernic is identified by its full PCI tuple — vendor `0x1dd8`,
  device `0x1002`, subsystem vendor `0x1dd8`, subsystem device `0x5400` — and
  by nothing shorter.** rocm-ernic@3fb74d1 moved the device from `1dd8:100a` to
  `1dd8:1002`, which is a real Pensando DSC Ethernet Controller id that
  physical cards ship with, so vendor+device no longer distinguishes the
  emulation.

  A fleet was found broken for exactly this reason: the guests'
  `99-rocm-ernic.rules` still matched `0x100a`, udev renamed nothing, and
  `rocm-ernic-netcfg.service` — which hardcoded `NIC=rocm-ernic0` — exited 1 at
  boot, leaving 32 guests with no address for 18 hours while sshd answered
  normally.

  - `udev/99-rocm-ernic.rules` is carried in the repo and installed by
    `ernic_fleet_guest`, matching all four fields in both the netdev and the
    InfiniBand stanza. `ernic_fleet_udev_rules_src` no longer defaults to `""`,
    which installed nothing and skipped silently.
  - `rocm-ernic-netcfg.sh` discovers the device by tuple and addresses it under
    whatever name it has, warning when the rename is missing instead of exiting.
  - `ionic_image_prep` writes the three-level pci.ids entry (the `1dd8 5400`
    subsystem line nested under upstream's `1002` device line) and its presence
    check asks for that subsystem line. The merge is parse-checked with `lspci
    -i` before it replaces the real file. It stays off by default because
    current hwdata already carries the entry.
  - The `ernic_fleet_guest` module check no longer greps the alias table for
    `d0000100A` — a hard failure on any current module, which aborted guest
    provisioning before the udev rules were installed.
  - `scripts/fleet.sh` is now a repo artifact, takes its paths from
    `FLEET_ENV`/`FLEET_STACK`/`FLEET_KEY`, and its `wait` requires the tuple and
    an address rather than sshd. `fleet.sh check <N>` reports each guest's
    device and address.
  - `tests/test_ernic_identity.py` pins the rules, the discovery script and the
    pci.ids awk programs, including the pciutils parse rules they depend on. It
    also evaluates the fleet role's install task — a `when:` that is false
    under the role's own defaults installs nothing while every id assertion
    stays green — and `Gen Compose Check` now installs the tools it needs and
    runs it with `CI=true`, under which a missing `udevadm`, `lspci`, `awk`,
    PyYAML or Jinja2 fails rather than skips. The check that asks udev to
    parse the file is the only one that can catch a rule rejected at parse
    time, and it was free to not run: a skip reads as coverage.

  Driver binding is deliberately left loose: `ionic`'s match table wildcards
  the subsystem because the same driver drives real Pensando cards, so udev is
  the only layer that can discriminate.

- **`gen-compose` now mirrors a stack's asset directories instead of adding to
  them.** `grafana/`, `logging/` and `tapsetup/` in the output directory are
  made to match the repo: a file the repo does not have is removed, and every
  removal is printed with the source directory it is missing from.

  The additive version cost the live 32-VM fleet two hours of metrics. A
  hand-made `grafana/provisioning/datasources/prom.yml` survived a
  regeneration alongside the repo's `prometheus.yml`, both `isDefault: true`,
  and Grafana refuses to provision two default datasources
  (`Only one datasource per organization can be marked as default`). It
  crash-looped unnoticed, because an extra file breaks nothing until something
  downstream reads the whole directory; it surfaced as `infra 1/3` in
  `qemu-tool list --summary`.

  The deletion is scoped per asset directory — the same shape as
  `publish-pages` rsyncing each lane's subtree with its own `--delete`.
  Everything else in a stack directory belongs to the operator and is neither
  read nor written: on the live fleet that is 2.4 GB of TSDB under
  `prom-data/`, plus `grafana-data/`, `loki-data/`, `results/`, scripts,
  tarballs and a `docker-compose.override.yml`. A name is owned only if the
  rendered compose file references it as `./name` **and** the repo's stack
  directory has something of that name, so a relative `PROM_DATA_DIR` can
  never make a data directory look like an asset.

  Ownership holds at any depth: a relative bind mount nested under an owned
  name, such as `LOKI_DATA_DIR=./logging/data`, is the operator's data and is
  skipped by the prune, not just a `./prom-data` at the top. And the mirror
  deletes nothing outside the output directory — a symlink on an owned path,
  at the top level or inside, is refused with the link and its target named
  rather than followed. Following one emptied the directory the link pointed
  at while printing paths through the link, so the log never said which
  directory had actually been emptied.

  Copies run before the prune, and a removal that fails is collected and
  reported at the end rather than raised on the spot, so a regeneration that
  fails part way leaves the stack no emptier than it started.

  `--check` compares the asset directories as well as the three generated
  files, and compares them symmetrically: `missing`, `modified` and `extra`
  are reported separately. Reporting only the extras answered "would a
  regeneration delete anything" — `rm <stack>/logging/loki-config.yml` left it
  printing `Up to date` for a stack that cannot boot, since Docker recreates
  that bind-mount source as a directory and loki dies on
  `read /etc/loki/config.yml: is a directory`.

  The first regeneration against a hand-managed directory will remove
  hand-added dashboards and provisioning files, including anything renamed
  aside during a repair. That is intended; the `Removed` lines are there so it
  is not the next mystery.

- **The settings files moved into `qemu/env/`, dropping the `env.` prefix.**
  Five files that were `qemu/env.<name>` are now `qemu/env/<name>`:

  | Was | Is |
  |-----|----|
  | `qemu/env.example` | `qemu/env/example` |
  | `qemu/env.scale-out` | `qemu/env/scale-out` |
  | `qemu/env.quickstart` | `qemu/env/quickstart` |
  | `qemu/env.overlay-ci` | `qemu/env/overlay-ci` |
  | `qemu/env.overlay-mi100` | `qemu/env/overlay-mi100` |

  **Every documented command that names a settings file has to change.** The
  flag and the file contents are untouched; only the path moves:

  ```bash
  qemu-tool gen-compose \
      --env-file qemu/env/scale-out --env-file qemu/env/overlay-mi100
  ```

  A gitignored local file follows the same rule — `qemu/env.local-*` is now
  `qemu/env/local-*`. `qemu/.env` and the rest of the search path
  (`$QEMU_TOOL_ENV`, `./.env`, `/etc/qemu-tool/env`) are unaffected, so a
  machine that never passes `--env-file` needs no change.

  The .deb follows the layout: the packaged template is
  `/usr/share/qemu-tool/env/example`, not `/usr/share/qemu-tool/env.example`.
  `postinst` still seeds `/etc/qemu-tool/env` from it on first install, so an
  upgrade keeps its existing settings.

  `qemu/env/` is a third sibling of `qemu/compose/` and `qemu/packages.d/`,
  mapped into the wheel in place for the same reason they are: the files are
  referenced by path from CI and the docs, so there is one copy and no
  working-directory churn. They are data, not Python, so they do not belong
  under `qemu/qemu-tool/`.

- **One golden fleet settings file plus committed overlays**, replacing
  `qemu/env.scale-out{,-ci,-mi100,-strix}`. The four were full copies of each
  other that agreed on about twenty keys and differed on about ten, hand-synced
  — and only the first of them documented what any key meant, so the other
  three drifted silently and a reader of one of them had no way to tell a
  deliberate difference from a missed sync.

  `qemu/env/scale-out` is now authoritative. The fleets that differ from it
  state only their differing keys in `qemu/env/overlay-ci` and
  `qemu/env/overlay-mi100`, layered over it with a repeated `--env-file`.
  `qemu/env.scale-out-strix` is deleted with no
  replacement overlay.

  The overlays are committed, not generated and not site-local, so a fleet
  that differs is a diff someone can review. They are deliberately **not**
  packaged: each names one CI lane or one host, so it is meaningless away from
  the checkout that explains it. `env/scale-out` ships as before.

- **The golden fleet settings file sets `VM_USERNAME=ubuntu` and
  `VM_PASSWORD=ubuntu`**, where it used to set `VM_USERNAME=batesste` and
  leave the password at the built-in `password`. `batesste` is the account on
  the published CI image and means nothing to anyone outside it; the four
  files disagreed on both keys (`batesste`, `ubuntu`, `changeme`) for no
  reason anyone recorded. This is visible beyond the guest login: `gen-compose`
  writes `ansible_user` into the generated `fleet-inventory.yml` from
  `VM_USERNAME`, so a fleet regenerated after upgrading addresses its guests
  as `ubuntu`.

  One path needs an explicit override. `gen-vm` returns before cloud-init on
  `--backing-image` and `--backing-file`, so `VM_USERNAME` is never applied
  to a published qcow2, whose only account is `batesste` — and
  `ansible/playbooks/vm-ernic-fleet.yml` is what creates `ubuntu` on it. Bake
  that image with `--username batesste` or the run dies at "VM did not accept
  SSH in time", which reads like a boot failure and is a login failure.

- **The mesh-manager healthcheck no longer opens a connection.** rocm-ernic
  never closes a connection that disconnects without registering, leaving it
  in `CLOSE_WAIT` forever, so probing the port every 2 s leaked a manager file
  descriptor every time it ran: measured at 28/min on a 32-VM fleet with zero
  evictions and zero restarts, which alone reaches Docker's 1024 default in
  about 37 minutes. It now reads `/proc/net/tcp` for a `LISTEN` instead —
  the same guarantee for the workers that gate on it, without the connection.
  Re-measured after: 50 descriptors, flat, zero `CLOSE_WAIT`.

- **Every service bounds its log** via `LOG_MAX_SIZE` and `LOG_MAX_FILE`
  (50m × 3). Docker's json-file driver is unlimited by default and a manager
  that reaches EMFILE spins in an `accept()` error loop with no backoff: one
  wrote **295 GB** in two days before anyone looked.

- **The stats exporter runs unprivileged** (`user: "65534:65534"`). It reads
  world-readable dumps off a sticky tmpfs and binds an unprivileged port, so
  it never needed root. The other services are root only because their base
  images declare no `USER`, which is a default rather than a decision; this
  one was cheap to fix and is verified end to end as `nobody`.

- **Image refs with no build stamp are excluded from the coherence check**
  with a warning, instead of being refused. That allows a locally built image
  carrying a fix under test to be run against a fleet, which is how the
  upstream GID-resolution fix was validated at 32 nodes. The check still
  catches what it exists for: two *different* real builds mixed together.

## [v1.4.1] - 2026-10-02

### Fixed

- **`report-for-lmcache` ran to the end for the first time**, after four
  distinct faults, none of which was the one the lane's comments assumed.
  It had been red for over a week and never reached the assertion it exists
  for.

  The last of them was the cause: the lane's QEMU containers were installing
  qemu-tool **from PyPI**. The generated entrypoint builds from
  `/qemu-tool-src` when a `pyproject.toml` is there and falls back to the
  release otherwise, and this lane set `QEMU_TOOL_SRC` nowhere — so every
  guest container died on

      qemu-tool: error: unrecognized arguments: --vfio-user-root-port

  because released 1.4.1 has never heard of that flag, which this stack has
  passed all along. `scripts/quickstart-2vm.sh` has copied the package beside
  the stack since #188, which is why the quickstart lane survived the same
  flag.

  Before that could be seen, three others had to go: the lane addressed
  guests by compose **service name**, which cannot resolve from a container
  joined to the network after it was created, so it now uses the generator's
  fixed addresses the way `--in-network` does; the Ansible inventory took its
  `ansible_user` from the settings layers rather than the image; and the
  report step used `${GITHUB_SHA:0:7}`, a bash substring expansion, under
  `dash`.

  The run that found the real cause did so because the step finally printed
  what it saw — the network it joined, this container's addresses, and the
  network's members, which listed every service **except** the two guests.
  `No route to host` was exactly true. One diagnostic attempt had to be
  thrown away first: it used `ip(8)`, which that image does not have, and so
  killed the step it was meant to explain.

  End to end afterwards: both guests up, Ansible configured, the SRQ driver
  built and reloaded, and `RDMA_DELTA: 16777216` — the exact figure the mesh
  assertion wants, so NIXL used RDMA rather than falling back to TCP.

- **The guest account comes from the image's own manifest, not from a
  settings file that guesses it.** Setting `VM_USERNAME=ubuntu` in the golden
  file (above) turned `report-for-quickstart` red the hour it merged
  (`report-for-lmcache` was already red for an unrelated reason, below): the
  published qcow2 is built with `batesste`, so every SSH
  went to an account that does not exist there. The guests were entirely
  healthy, and the symptom was `guest 1 did not answer SSH` after ten minutes
  of retries — which says nothing about the account.

  The answer was already published and unread. The qcow2 carries an ORAS
  referrer of type `application/vnd.batesste.vm-info.v1`, and both lanes
  already pulled it for the guest keypair while ignoring the `username`,
  `password` and `ssh_keys` sitting beside it in `vm-info.json`.
  `quickstart-2vm.sh` now adopts that username (`adopt_login`), and both lanes
  prefer it over the settings file, announcing the disagreement rather than
  correcting it silently — the settings file is then wrong for everyone else
  too. `VM_USERNAME` remains what the settings declare and is used whenever
  the image names nothing.

  This is the same rule the SSH key verification follows: ask the thing
  itself. An image is the authority on which accounts exist in it.

- **CI covered none of the LMCache work, and three gaps each hid a live
  bug.** Adding the lanes found them, which is the argument for the lanes:

  - **Gen Compose Check validated no observability service.** Profiles
    *filter* services out of `docker compose config`, and both existing
    validation steps ran with none enabled — so `prometheus`, `grafana`,
    `loki`, `alloy`, `lmcache-coordinator` and the two stats exporters were
    parsed by nothing. A new step enables `lmcache` and `metrics` and asserts
    the seven services are present before validating, because `config -q` on
    a render whose services were all filtered away also exits 0.
  - **Shell Check skipped `qemu/compose/*/tapsetup/*.sh`.** Adding it
    immediately failed on SC2045 in `setup.sh` — `for dev in $(ls
    /sys/class/net)` — on the very line that picks the interface to bridge,
    which is the line the 2-VM mesh bug was traced to. Now a glob.
  - **`fleet-lmcache.yml` was syntax-checked by nothing**, being imported by
    none of the five playbooks the lane names. Checking it found a duplicate
    `when` key in `lmcache_guest/tasks/main.yml`, where the second silently
    replaced the first and the image copy lost its "only when the 9p share is
    off" guard — a ~2.8 GB write per guest in the configuration the role
    exists to avoid. The job now sets `ANSIBLE_DUPLICATE_YAML_DICT_KEY=error`,
    because a duplicate key is a warning by default and `--syntax-check`
    exits 0 regardless: measured on this instance, 0 → 4.

- **`VM_NVME` could not carry a literal QEMU argument string, and said it
  could.** The key names a `str` field, so commas are the per-VM split. At
  `VM_COUNT=8` `4096,logical_block_size=4096` is a loud count mismatch; at
  `VM_COUNT=2` it is a silent one, giving guest 1 `4096` and guest 2
  `logical_block_size=4096`. Behaviour unchanged — a fix needs a quoting
  convention across every `str` field — but the source comment claiming all
  three forms "pass straight through" is corrected and both halves are pinned
  by tests.

- **`gen-vm` first-boot timeout for arm64 and riscv64** — without KVM,
  cloud-init's rootfs expansion (growpart + resize2fs) can stall for 30+
  minutes under software emulation, causing the CI job to be silently
  cancelled at its ceiling with no diagnostic output. `_first_boot` now
  caps the QEMU subprocess at 55 minutes and exits with a message pointing
  at the architecture and cloud-init step when the deadline is exceeded.

- **riscv64 trailing comma in QEMU machine string** — `f"virt,{kvm}"` with
  an empty `kvm` string produced `"virt,"`. Fixed to emit `"virt"` or
  `"virt,accel=kvm"` cleanly.

## [v1.4.0] - 2026-10-02

### Added

- **`qemu-tool gen-compose` generates an N-VM rocm-ernic mesh stack** from a
  settings file, with one emulated RDMA NIC and one rocjitsu GPU per VM. Same
  topology as `vfio-user-ernic-2vm`, sized by a variable rather than by hand.
  Per-VM values are comma-lists, not numbered variables — `VM1_NAME`/`VM2_NAME`
  does not survive eight VMs, let alone the mesh's 64-node ceiling. A bare value
  broadcasts, a list of length `VM_COUNT` applies positionally, and any other
  length is an error naming the key and both lengths. Keys naming a field that
  is already `list[str]` (`VM_EXTRA_HOSTFWD` and friends) keep their existing
  meaning and go whole to every VM; the two cases are told apart from
  `VMConfig`'s own type hints rather than a hand-kept list of names, so a scalar
  field added later becomes per-VM-overridable for free. Settings template in
  `qemu/env.scale-out`, generated stack in
  `qemu/compose/vfio-user-ernic-rocjitsu-scale-out/`.

  The generator refuses an image set that cannot have been built together: all
  four image tags must share one `<date>.<ci-sha>` stamp, and the qemu and ernic
  images must share one `vfu.*` revision. Both guard the same failure, which is
  worth an up-front check because it does not present as a version problem — a
  guest driver and a host server from different SHAs wedge at the vfio-user/DSR
  boundary and look like a broken device.

  Every service gets a static address on the stack's own `fleet` network and
  nothing in the stack resolves a service name at run time. That is a scaling
  fix, not tidiness: Docker runs one embedded resolver per network, and the
  mesh advertises each node by `gethostname()` so that peers resolve it to
  dial — which puts that one resolver in the path of all N×(N−1) peer links.
  At 48 VMs it saturated, workers died on `Temporary failure in name
  resolution`, and because the mesh takes a new node id on every reconnect and
  never reclaims the old one, the restarts walked the fleet toward its 64-node
  protocol ceiling. Naming each container after its own address makes those
  numeric lookups: 4497 resolution failures at 48 VMs became zero.

  Measured ceiling on a 128-core / 503G host at 4 vCPU and 8G per VM: 32 VMs
  boot clean every time, 40 is marginal (40/40 once, 37/40 on a repeat), 44
  lose 3 guests, 48 lose 7. Memory is never the constraint — 48 VMs used 107G
  of 503G. Two things bind instead. The manager's heartbeat timeout is 20 s of
  wall clock and is not tunable, so an oversubscribed host makes it mistake
  slow nodes for dead ones and evict them, taking their guests down with
  `vfio-user: timed out waiting for reply`. And the mesh stops registering new
  workers at about 40 nodes: asking for 64 VMs settles at 40 of 64, with the
  rest dying on `TCP: Registration timeout` on an idle host as much as a busy
  one, because the mesh is a full mesh and registering node N+1 costs N new
  connections against a fixed timeout. The 64-node protocol cap in
  `rdma_backend_tcp.c` is therefore not reachable — the 64-VM fleet peaked at
  node id 30. Full write-up in the stack's README; the fleet-wide consequences
  are AGENTS.md issues 13, 14 and 17.

  Every ernic service now sets `ulimits.nofile` from a new `ERNIC_NOFILE`
  setting (default 65536). Docker's default soft limit is 1024, and the manager
  does not close the socket of a node it evicts — so each eviction leaks a file
  descriptor and a 40-VM fleet reaches the limit inside a normal run. Past it
  the manager cannot even reopen its own stats file, spins in an
  `accept()`/EMFILE loop with no backoff (3.3 GB of log in 12 hours, measured),
  and the guest attached to it dies on a QEMU assertion. Raising the limit does
  not fix the leak, which is upstream's; it buys the run enough headroom to
  finish. AGENTS.md issue 15.

  The generated YAML records the hash of the settings file it came from, and
  `qemu-tool compose` warns on stderr when they disagree. It never blocks:
  `down` against a stale tree is how a bad edit is recovered from.
  `gen-compose --check` turns the same comparison into a CI gate.

- **`qemu-tool ernic-stats`** serves the stats dumps written by
  `rocm-ernic -S` as Prometheus metrics — per-QP bytes, doorbells, CQEs, WQEs
  by opcode, MMIO and interrupt counts. Metric and label names are taken
  verbatim from rocm-ernic's own `prometheus/ernic-exporter`, so that project's
  `grafana/ernic-dashboard.json` works against this endpoint unmodified. The
  implementation is separate because that exporter is systemd-shaped: it needs
  an `instances.json` manifest ernicctl writes, only reads files whose stem
  parses as an integer, and checks liveness with PIDs from its own namespace —
  none of which survive a compose stack where each server is PID 1 in its own
  container. The CI image also ships only the `rocm-ernic` binary.

- **`qemu-tool gen-vm --backing-image OCI_REF`** pulls a published backing
  qcow2 from a registry with `oras`, decompresses it and creates the overlay —
  the same job as the cloud-image download, from a different source. The
  artifact is not a runnable image (`artifactType`
  `application/vnd.batesste.vm-image.v1`, one zstd layer), so `docker pull`
  will not fetch it. `zstd` is now a `Recommends`.

### Changed

- **`qemu-tool list` reports the SSH port a containerised VM is actually
  reachable on.** It read the port out of the QEMU command line, but a
  containerised guest's `-hostfwd` port is the port inside that container's
  network namespace — and the scale-out stack gives every guest 2222 there, so
  a forty-VM fleet listed forty VMs on port 2222. True, and useless. The
  listing now also resolves the published mapping and shows `host->guest` when
  the two differ (`2231->2222`); JSON output gains `ssh_port_host` alongside
  the existing `ssh_port`. An uncontainerised listing is unchanged, still one
  port per row.

- **`sbates130272.batesste` is pinned to 3.0.0** (was `>=2.4.0`), and the
  workarounds that release makes unnecessary are gone. `vm-rocm.yml` loses the
  four hand-rolled pre_tasks that wrote `/etc/dkms/no-autoinstall` and diverted
  `update-initramfs` — `rocm_setup_amdgpu_dkms_defer_build` does that in the
  role, immediately before the `amdgpu-dkms` install rather than a play
  boundary away. `vm-rocjitsu.yml` loses its ROCm-prefix fixup
  (batesste-ansible#248) and reads `rocm_setup_therock_prefix` instead; the
  `setup-vm-job` action loses its `jmespath` install (2.4.0's `check_platform`
  was the only `json_query` caller) and its `~/.gnupg` step (`git_setup` no
  longer deploys a GnuPG config by default). The pin is exact, not a floor:
  3.0.0 is a breaking release, and the next one should arrive as a PR rather
  than as a lane that broke overnight when the collections cache was evicted.

- **A local ROCm bake still runs its distro-wide `apt upgrade`.** 3.0.0 flipped
  `rocm_setup_skip_system_upgrade` to true, which would have taken that pass
  away from every bake; `vm-rocm.yml` holds it at false, so the behaviour is
  unchanged and CI's explicit `true` is a real override again rather than a
  restatement of the default. Pass `vm_rocm_skip_system_upgrade=true` to opt a
  local bake out — CI does it for its own reasons, documented in the workflows:
  that pass is the largest source of flake in those lanes, since it is what
  meets the `security.ubuntu.com` mirror skew, and a reproducibility hole
  against the pinned guest kernel. In the same
  release `rocm_setup` stopped running `update-pciids` and stopped installing
  `rocm-cli` and `xrocmtop` by default; this repo already disabled the latter
  two, and the `ionic_image_prep` pci.ids entry is no longer clobbered by a
  bake.

- **Guests get a generic git identity.** 3.0.0's `git_setup` asserts on an
  unset `user.name`/`user.email` instead of leaving them blank, so
  `vm-basic.yml` sets `qemu-minimal VM <qemu-minimal@localhost>`. Deliberately
  impersonal: these images are published as qcow2s and nothing in a guest makes
  a commit anyone reads. CI additionally passes
  `user_setup_deploy_ssh_key=false`.

- **The VM playbooks are layered** the way the README has always described
  them, instead of four standalone files re-copying the shared parts.
  `vm-basic.yml` is the base for every guest; `vm-rocm.yml` imports it and is
  the base for every guest that needs ROCm; `vm-rocjitsu.yml` imports
  `vm-rocm.yml`; `vm-ernic.yml` imports `vm-basic.yml`. What had been
  duplicated: the GitHub-token `module_defaults` block (four verbatim copies,
  now one expression in `vars/github-api.yml`), the
  `user_setup`/`fave_packages`/`git_setup` trio (four copies), and the ROCm
  apt pin plus three stale-source cleanups (two copies, ~60 lines each).
  `vm-ernic.yml` sits on `vm-basic.yml` rather than `vm-rocm.yml` because it
  installs no ROCm at all — its `rocm_setup_*` vars, the ROCm apt pin and the
  ROCm keyring cleanup were configuring a role that never ran, and are gone
  with it. `ansible-playbook --list-tasks --tags install` is unchanged for the
  combined lane and changes only as intended for the other two.
  `vm-ernic-rocjitsu.yml` is untouched.

- **Every CI lane that bakes a guest now runs `resolute`.** The `vm-rocm`,
  `ansible-setup-test`, `report-for-vm-basic`, `report-for-vm-ernic` and
  `smoke-test-rocm-ernic` lanes were still on `noble`, so half the fleet was
  being proved on a release nothing else used. `smoke-test-qemu-tool` keeps its
  `noble` × `resolute` matrix deliberately — it is the one lane whose subject
  *is* release coverage, and collapsing it would leave `noble` advertised by
  the CLI and untested everywhere.

- **`gen-vm` defaults to `--release resolute`** (was `noble`), so a bare
  `qemu-tool gen-vm` builds what CI proves. README, the man page and
  `env.example` follow. Pass `--release noble` to get the old default back;
  `noble` remains supported and matrix-tested.

- **`vm-rocm.yml` stops installing `rocm-cli` and `xrocmtop`.** Both
  `rocm_setup_install_rocm_cli` and `rocm_setup_install_xrocmtop` default to
  true in the role and were set nowhere here, so every ROCm lane — plain
  rocjitsu and ernic-rocjitsu alike — baked them in by accident. Nothing in
  this repo runs either and no lane asserts on them. `rocm-cli` is the bigger
  reason: its installer is an unpinned `curl … rocm-cli/main/install.sh | sh`
  executed at bake time. `xrocmtop` is an interactive terminal UI in a
  headless CI guest. `rocm_setup_install_metrics_exporter` stays true —
  `vm-rocjitsu.yml` deleted its own exporter install in favour of it, and the
  guest that had no GPU and no use for it, `vm-ernic.yml`, no longer runs
  `rocm_setup` at all.

- `vm-rocm.yml`'s `rocm_setup_extra_kernel_packages` now defaults to `[]`. It
  named `linux-generic-hwe-24.04`, which was correct only while the plain ROCm
  lane ran noble; on resolute that is another release's metapackage, and there
  is no resolute equivalent worth naming because resolute's GA kernel already
  is the current one. Naming one there installs a kernel nothing boots, which
  is the trap `vm-rocjitsu.yml` already documents on its own `[]` override.

- `vm-rocjitsu.yml` installs the pinned mainline kernel (`ionic_kernel_pin`,
  currently `v7.2.4`) before it touches DKMS, so the standalone rocjitsu lane
  boots the same kernel as the combined lane and as the published
  `ernic-rocjitsu` qcow2. It had been booting resolute GA, 7.0.0-34, while
  carrying two amdgpu patches — the ptrace gate and the `panel_type` probe —
  that are version-gated to >= 7.2 and are therefore deliberate no-ops there.
  The combined lane never had the problem because `vm-ernic.yml` runs first and
  `ionic_image_prep` puts the guest on 7.2.4; the standalone lane is now the
  same guest. The kernel comes from `ionic_image_prep`'s `kernel.yml` via
  `tasks_from`, reusing one installer and one pin rather than a second copy,
  and `kernel.yml` short-circuits when the pinned kernel is already running so
  the combined lane does not install it twice. Its closing `ib_umem_get_va`
  assert is now gated on a new `ionic_image_kernel_assert_ionic`, which the GPU
  lane sets false — that check is `ionic_rdma`'s precondition, not a reason to
  fail a lane that never builds it. Position in `pre_tasks` is load-bearing:
  the kernel `.debs` run `update-initramfs` themselves, so the install has to
  precede the `dpkg-divert` that replaces it with a no-op, and it has to
  precede the DKMS build, which must target the kernel the guest boots.

  This is the one remaining difference between the guest the rocjitsu lane
  builds per run and the published qcow2, which runs `sgemm-bench` to
  completion — 8/8, including under a GitHub-runner CPU and memory envelope —
  where the lane's own guest hangs in `hipFuncGetAttributes`.

- `vm-rocjitsu.yml` builds the amdgpu DKMS module **once**, from patched
  source, instead of building it for every installed kernel and then throwing
  the result away. The four source patches report `changed` on every run, so
  the module the `amdgpu-dkms` postinst produced was always discarded by the
  rebuild that follows them — while costing 23m55s of a 47m48s standalone lane
  and 28m20s of a 55m46s combined lane, against ~12m for the rebuild that
  produces what the guest loads. The postinst built "for 7.0.0-31-generic,
  7.0.0-34-generic and 7.2.4-070204-generic"; the guest boots one of those.
  `/etc/dkms/no-autoinstall` now stops it before it starts, replacing the
  `no-autoinstall-errors` marker that let the doomed build run and then ignored
  its exit status. That marker's fallout goes with it: `dpkg --configure -a`
  has nothing left to settle, and genuine DKMS failures elsewhere in the image
  stay fatal rather than being swallowed for the rest of the run. Because
  `no-autoinstall` short-circuits above `dkms add` as well as the build loop,
  the rebuild task registers the module itself and takes the version from
  `/usr/src/amdgpu-<version>` — `dkms status` names nothing until something has
  been added, and the old `awk` over its first line was producing an empty
  version.

  That empty version turned out to have a second consumer. `amdgpu-dkms`'s own
  postinst runs after `common.postinst` returns and derives a kernel version
  from the DKMS state that the early exit never wrote, so it called
  `update-initramfs -u -k` with nothing after the `-k` and exited 2 — a
  maintainer script this playbook does not own, on an install that belongs to
  `rocm_setup`, so dpkg's failure became apt's rc 100 and failed the play.
  `/usr/sbin/update-initramfs` is now diverted to a no-op for exactly the
  window the marker covers (`dpkg-divert`, not a PATH shim, because the
  postinst calls the absolute path). Nothing is lost by skipping that call: the
  initramfs it would build describes a guest with no amdgpu module and no
  blacklist yet, and the existing "Regenerate initramfs" task rebuilds it once
  both exist — the undivert is placed before that task so it gets the real
  binary.

- RVS and the rocBLAS host library it links against are no longer baked into
  the rocjitsu image. The check that consumes them is off (no `amdrocm10` RVS
  tarball exists, and the `amdrocm7` one cannot run against this guest's ROCm
  10 tree), but the install tasks were ungated and every bake still downloaded
  and unpacked the tarball and pulled in `amdrocm-blas-host<ver>`. They are now
  gated on `vm_rocjitsu_install_rvs`, default false; turn it on in the same
  change that sets `rocjitsu_gpu_test_rvs` true. The role-side RVS tasks were
  already gated and are unchanged.

- `rocm_setup_extra_kernel_packages` is gone from `vm-rocjitsu.yml`. Its
  comment claimed `linux-generic-hwe-26.04` resolved to the running
  `7.0.0-31-generic` and so was a no-op; it reported `changed` on every run,
  and by the time it ran the guest was already on `7.0.0-34-generic`, so it
  installed a metapackage for a kernel nothing here boots — and fed that kernel
  to the postinst build above. In the combined lane the guest runs the pinned
  mainline `7.2.4` from `ionic_image_prep`, where a 7.0.x HWE kernel is pure
  weight. `vm-ernic.yml` had already dropped it for the same reason.

- Three no-op tasks removed from `vm-rocjitsu.yml`, each confirmed `ok` on
  every run of both lanes. The device-metrics-exporter apt source and
  `amdgpu-exporter` install duplicated what `rocm_setup_install_metrics_exporter`
  already does, adding a second apt source for the same repo; the suite pin
  they carried ("no resolute suite yet") was stale, since the role's own source
  resolves on resolute. The purge of five distro ROCm runtime packages named
  exactly what the `rocm-no-distro-packages` pin already pins to Priority -1
  before any role runs, so apt cannot install them to begin with.

- Workflow triggers now follow from the fact that `main` is protected and every
  commit arrives as a PR merge. No workflow runs on both `pull_request` and
  `push: main` any more — `shell-check`, `spell-check`,
  `rocm-gemm-benchmark-compile`, `build-packages` and `ansible-setup-test` each used to run twice per change with
  byte-identical path filters. Those five lost their `push` trigger *and* their
  `paths` filter, because they are intended to become required checks and a
  path-filtered required check never reports on a PR outside its filter,
  leaving that PR blocked on a status that never arrives. The trade is that a
  docs-only PR now also pays `build-packages` (~7m) and `ansible-setup-test`
  (~7m). The four report lanes converged on one shape — `push: main` plus a
  daily cron plus manual — which gives `vm-report-ernic` a push trigger it
  never had, and their crons moved from weekly to daily.
  `smoke-test-rocm-ernic` moved from Monday 04:00 to Monday 07:07.

- The four report lanes are all scheduled at **01:00 MST (`0 8 * * *`)**
  rather than staggered across 02:47–06:29 UTC. GitHub cron is UTC-only and
  MST is a fixed `-07:00` with no DST, so one expression holds year round;
  under Mountain *Daylight* Time it lands at 02:00 local. Each lane gets its
  own hosted runner, so the nightly window contracts from ~3h45m of wall clock
  to roughly the ~57m of the slowest lane. `publish-pages` consequently sees
  four `workflow_run` completions inside an hour rather than spread across the
  night; it sets `cancel-in-progress: false`, so they queue and none is
  dropped, but the Pages branch is rewritten four times in succession and the
  site only settles after the last drains. Scheduled runs at `:00` are also the
  most likely to start late, `:00` being the most contended minute on the
  platform.

- `ansible-playbook-test-rocjitsu` and `ansible-playbook-test-ernic-rocjitsu`
  gate pull requests again. The rocjitsu lane had become dispatch-only while
  still being documented as the PR gate for the emulated GPU, which is how the
  RVS regression in #154 reached `main` and was found an hour later by
  `vm-report-rocjitsu` instead of before merge. Neither is a required check.

- Every file in `.github/workflows/` is now named for the workflow it
  contains, slugified: `qemu-minimal - Shell Check` lives in
  `qemu-minimal-shell-check.yml`. Most were a missing `qemu-minimal-` prefix;
  eight more were renamed a second time on 2026-09-23 once the display names
  settled, so that the rule now has no exceptions: lowercase the workflow name,
  turn each ` - ` and each space into `-`, append `.yml`. The four playbook
  tests gained the `vm-` their names carry (`...-ansible-playbook-test-rocm.yml`
  → `...-ansible-playbook-test-vm-rocm.yml`), and the four report lanes moved
  from `vm-report*` to `report-for-*`. Five had drifted further still and were
  renamed harder — `shell-check.yml` →
  `qemu-minimal-shell-check.yml`, `bench-compile.yml` →
  `qemu-minimal-rocm-gemm-benchmark-compile.yml`, `qemu-tool-dry-run.yml` →
  `qemu-minimal-dry-run-qemu-tool.yml`, `qemu-tool-smoke-test.yml` →
  `qemu-minimal-smoke-test-qemu-tool.yml`, and `smoke-test-rocm-two-vms.yml` →
  `qemu-minimal-smoke-test-rocm-ernic.yml`. Nothing about what any workflow
  *does* changed. The self-referential entries in each `paths:` filter, the
  `workflow_run` list in `publish-pages`, and the README badges were all
  updated to match. Concurrency groups and uploaded
  artifact names were deliberately *not* renamed: `publish-pages` fetches each
  report by artifact name, so those are load-bearing across workflows.
  See **Upgrading**.

- Workflow `name:` fields are now title-cased prose rather than a second copy
  of the filename: `qemu-minimal - Shell Check`, `qemu-minimal - Spell Check`,
  `qemu-minimal - ROCm GEMM Benchmark Compile`, `qemu-minimal - Ansible
  Playbook Test - vm-rocjitsu`, `qemu-minimal - Smoke Test - rocm-ernic`. The four report lanes
  are named for the playbook they exercise — `qemu-minimal - Report for
  vm-basic`, `- Report for vm-rocjitsu`, `- Report for vm-ernic` — except
  `Report for hipfile-fio`, which runs `vm-rocjitsu.yml` but reports on the
  hipFile/fio storage benchmark. `publish-pages` selects its upstream lanes by
  workflow *name* in its `workflow_run` trigger, so all four were updated there
  in the same commit; a mismatch there fails silently and freezes the site.
  `qemu-minimal - Package` became `qemu-minimal - Build Packages` (and
  `qemu-minimal-build-packages.yml`), since "Package" named a noun where every
  other lane names an action.

- Job `name:` fields follow the workflow names. They were kebab-case slugs that
  had drifted from their lanes — `shell-check.yml` declared `shellcheck`,
  `rocm-gemm-benchmark-compile.yml` declared `bench-compile`. Two rules now
  cover all 28 jobs: a single-job workflow names its job after the workflow
  minus the `qemu-minimal - ` prefix (`Shell Check`, `Report for hipfile-fio`,
  `Ansible Playbook Test - vm-rocm`), and a multi-job workflow title-cases each
  job's own identity (`Build Packages` holds `Build Deb`, `Install Smoke Test
  (<kvm-group>)`, `Build Wheel`, `Wheel Smoke Test`). Job **ids** are
  unchanged, so every `needs:` edge still resolves. Branch protection matches
  job names unqualified by workflow, which is why the single-job rule keeps the
  profile suffix — four lanes all naming a job `Report` would collapse into one
  required context. See **Upgrading**.

- `vm-report-two-vms` is now `vm-report-ernic`, and its report moves from
  `/two-vm/` to `/ernic/` and `/two-vm/vm2/` to `/ernic/vm2/`. The old name
  described the *shape* of the lane — two guests — while every other report
  lane is named for the thing it exercises, and what this one actually proves
  is the ernic RDMA NIC across a pair of guests. The workflow file, workflow
  name, job id, concurrency group, guest names (`ernic-report-1` and
  `-2`), cloud-image cache key and uploaded artifact (`vm-report-ernic`) all
  follow. The compose stack keeps its `vfio-user-ernic-2vm` name, since
  `smoke-test-rocm-ernic` shares it. See **Upgrading**.

### Fixed

- `render-site-perf.py`'s "Current report freshness" table read its stamps from
  `history[-1]`, but `append_history` dedupes on `(sha, metrics)` — so a publish
  whose benchmarks produced identical numbers against an unchanged sha appended
  no row, and the table printed the *previous* publish's timestamps directly
  underneath an all-green badge computed live from the reports on disk. It now
  takes a reports dict read off disk, which is what `update_green` already does
  and says why in its docstring: report freshness moves independently of
  benchmark values. Covered by three new cases in `tests/test_render_site_perf.py`.
- The man page documented `--ansible-profile FILE` ("the specified profile
  directory"); the flag is and always was `--ansible-playbook FILE`, and it takes
  a playbook. Anyone following `man qemu-tool` got `unrecognized arguments`.

### Removed

- The `slash-command-dispatch` workflow, and with it the `/run-ci-full` comment
  command. It existed so a reviewer could ask for the four
  `ansible-playbook-test-*` lanes on a pull request back when those lanes had
  no `pull_request` trigger at all. All four are now PR-triggered within their
  path filters and all four carry `workflow_dispatch`, so the remaining case —
  forcing a run on a PR whose changes fell outside the filter — is served by
  the Actions "Run workflow" button. Two things do change for reviewers: that
  button is per-workflow, so it is four selections rather than one comment, and
  it needs write access to the repository, where the comment path accepted any
  commenter who passed the workflow's own permission check. See **Upgrading**.

### Upgrading

- `/run-ci-full` comments are no longer answered by anything. A comment
  containing it is now inert rather than an error, so the only signal that it
  stopped working is the absence of the acknowledgement reply. Use the Actions
  "Run workflow" button on each `ansible-playbook-test-vm-*` workflow instead.

- `main` has no required status checks configured: the `contexts` list under
  `required_status_checks` is empty, which is why the post-merge `push`
  triggers removed here were the only guaranteed validation. Nothing in this release changes branch
  protection. Once the renamed workflows have landed and each gate has reported
  once, add `Shell Check`, `Spell Check`, `ROCm GEMM Benchmark Compile`,
  `Ansible Syntax Check`, `Generate VM Ansible Setup`, `Build Deb`,
  `Build Wheel` and `Wheel Smoke Test` as required contexts. Adding them
  *before* the first run leaves PRs waiting on contexts GitHub has never seen —
  and because the job renames above mean GitHub has seen *none* of these eight
  strings, that sequencing is not optional here.

- Renaming a workflow file orphans its run history: GitHub keys runs on the
  file path, so each renamed workflow starts empty and its README badge reads
  "no status" until it next runs. The old runs are still reachable in the
  Actions tab under the retired filename. Any bookmark of the form
  `/actions/workflows/<old>.yml` needs updating. Branch protection is not
  disturbed by the *file* renames — required checks key on job name — but the
  job renames in this release do change every context string, so any protection
  rule, external status consumer or merge-queue config pinned to the old
  kebab-case names must be updated in step.

- The two-guest ernic report moved from `/two-vm/` to `/ernic/`. Links to
  `https://sbates130272.github.io/qemu-minimal/two-vm/` will keep serving the
  last report published under the old name until that directory is removed
  from `gh-pages` by hand — `publish-pages.yml` scopes its `--delete` to the
  lane directories it knows about, so a retired one is left untouched rather
  than silently erased. `/ernic/` itself stays empty until the renamed lane
  next runs; it is cron- and dispatch-only, so trigger it if you want the new
  path populated sooner.

- A guest built by an older `gen-vm` may come up unreachable after a bump.
  `run-vm` now always presents a MAC derived from `--ssh-port`, and a guest
  whose netplan pins the MAC it saw at *its* first boot will not match it. SSH
  reports `Connection timed out during banner exchange` rather than `refused`,
  because slirp's `hostfwd` still completes the TCP handshake with nothing
  behind it.

  Check what the guest actually pinned, rather than reasoning from versions:
  `grep -A4 ethernets /etc/netplan/50-cloud-init.yaml` in the guest, or on the
  mounted qcow2. If there is a `macaddress:` line, pass that value as `--mac`
  (or `VM_MAC`) and the guest runs as-is; otherwise the guest matches on
  interface name and needs nothing. Rebuilding the guest also fixes it
  permanently. Which MAC got baked in depends on both the commit and whether
  the guest was built with `--mgmt-tap`, so the file in the guest is the only
  reliable answer.

### Added

- **Every bake now ends by slimming the guest it just built.** A new
  `image_slim` role, invoked from a one-play `vm-slim.yml` that each leaf
  playbook imports as its final line, empties the apt cache and package lists
  (the largest single item on a ROCm guest, and nothing cleaned them before),
  removes leftover DKMS `build/` trees, vacuums archived journals to 16M,
  drops rotated and cloud-init logs, sweeps `/tmp` and `/var/tmp`, and then
  `fstrim -av`s — which, with the bake boot's `discard=unmap`, punches all of
  it back out of the qcow2 in place. It prints the reclaimed MiB. Two opt-in
  flags go further at the cost of a fallback: `image_slim_purge_old_kernels`
  removes the GA kernel the guest no longer boots, and
  `image_slim_purge_dkms_sources` removes `/usr/src/amdgpu-*`; both default
  false so a local bake keeps the ability to boot back and to rebuild.
  `/var/tmp/ionic-src` is kept by default and only removed under
  `image_slim_drop_ionic_src`, because the ernic lanes' post-boot
  `--tags configure` run re-invokes `setup-ionic-dkms.sh` against it and would
  otherwise re-clone a kernel from inside the VM. `cloud-init clean` is
  deliberately *not* run: these images have no datasource on a later boot, so
  wiping instance state would make that boot a first boot and regenerate the
  MAC-pinned netplan `gen-vm` goes out of its way to neutralise.

  Where it runs is the subtle part. `import_playbook` splices plays inline and
  does not dedupe, so an ungated import from all five leaves would slim six
  times on `vm-ernic-rocjitsu.yml` — five of them mid-chain, with real work
  still to come. That is a bug, not waste: `vm-rocjitsu.yml` installs rocBLAS
  with `update_cache: false` against lists an earlier pass would have deleted,
  and both DKMS builds read trees the opt-in purges remove. Each importing
  playbook therefore passes `image_slim_enable: false`, and because import
  vars propagate through nested imports, one `false` silences every pass
  beneath it. Exactly one slim pass runs per entry point and it is the last
  play in the run.

- **`gen-vm` compacts the backing image after the bake**, with
  `--no-compact` (or `VM_COMPACT=false`) to opt out. Once Ansible has
  finished, the backing image is rewritten with `qemu-img convert -O qcow2
  -c` and the before/after allocated size and ratio are printed into the bake
  log — that number is what the rest of the image-size work is measured
  against. zlib compression is safe here precisely because this is a
  *backing* file: in normal use every guest write lands in the overlay, so no
  compressed cluster is rewritten, and a ROCm tree is mostly ELF. The one
  exception is `--ansible-only`, which boots an existing backing as the
  writable root disk with no overlay in front; that mode compacts again at the
  end, and `--no-compact` is there for a rebuild loop that would rather stay
  uncompressed throughout. Nothing is
  flattened — the backing image is a byte copy of a standalone Ubuntu cloud
  image, so the convert is a pure recompress. It writes a sibling temp file
  and `os.replace`s only after `qemu-img info` confirms the result, so an
  interrupted convert cannot destroy an image that took an hour to bake, and
  a failed one warns and leaves the original in place rather than failing the
  bake. Skipped under `--no-backing`, where the image becomes the overlay and
  will be written to.

  **Measured, across all four bake lanes.** The two stages are complementary
  rather than redundant: the slim removes ~1.9 GiB of real files, and the
  compaction then finds a further 2.3–2.5x in what remains, because what
  remains is mostly ELF.

  | Lane | before | after slim | after compaction | total |
  |---|---|---|---|---|
  | `vm-ernic` | 6.41 GiB | 4.84 GiB | **2.89 GiB** | −55% |
  | `vm-rocm` | 10.5 GiB | 8.42 GiB | **3.44 GiB** | −67% |
  | `vm-rocjitsu` | 10.8 GiB | 8.80 GiB | **3.59 GiB** | −67% |
  | `vm-ernic-rocjitsu` | 11.2 GiB | 9.15 GiB | **3.90 GiB** | −65% |

  The ROCm guests compress at 2.45x against ernic's 1.67x, so the lanes that
  were worst off benefit most. Four images that totalled 38.9 GiB now total
  13.8 GiB.

- **The `vm-rocm` and `vm-ernic-rocjitsu` lanes now trigger on
  `ansible/playbooks/roles/**`.** They did not, and both run roles from there —
  `vm-rocm.yml` includes `ionic_image_prep`'s `kernel.yml` to install the pinned
  mainline kernel it boots, and the new `image_slim` runs last in every bake.
  Every bake and report lane also now lists `vm-slim.yml`. These filters
  enumerate playbooks by name rather than globbing `ansible/playbooks/*.yml`,
  so a newly added playbook is invisible to them until it is named: the lanes
  that actually execute a change were not the lanes being run on it, which is
  the same class of drift as a recipe kept in two places.

- **The performance badges now encode health rather than branding.** They used
  to carry the AMD/NVIDIA/Intel brand colours, so rocjitsu GEMM rendered red on
  its best day and none of the three ever changed — the colour half of each
  badge carried no information at all. Each badge now compares the latest
  reading against the mean of the previous five *distinct* readings and colours
  green at or above 95% of it, amber down to 80%, red below, with the delta
  appended to the message. Distinct matters: a history row is a *publish*, not a
  benchmark run, and any lane finishing re-records the other lanes' unchanged
  numbers, so a naive mean over the last five rows weights whichever plateau
  happened to be republished most. A reading with nothing to compare against is
  blue, not green — no verdict has been made — and a metric with no badge at all
  is grey `n/a`. The 95/80 dead band is wide on purpose: these benchmarks run
  against an emulated GPU over vfio-user and swing hard between runs.

- **An "all reports green" date badge**, served from `perf/badge-all-green.json`
  and shown on the README's report row and the site landing page. It is green
  with today's date while every one of the five report lanes has published a
  fresh passing report, amber carrying the last such date once any lane stops,
  and red `never` before the first one. Green needs both halves: a `pass` stamp
  *and* a report generated within 36 hours. A failing lane uploads nothing and
  its last good report stays on the branch, so the stamp alone would read green
  forever; freshness alone would call a fresh failure green. The date lives in
  its own `perf/green.json` rather than in `perf/history.jsonl`, because the
  history appends only when a benchmark number changes and the date has to be
  able to advance on a publish where nothing did.

- **`generate-vm-report.sh` stamps `Status: **pass**`** into each report's meta
  line, which is what the badge above reads. It is earned rather than
  decorative: the script runs under `set -e` and every benchmark path ends in
  `bench_failed`, so reaching the report heredoc at all means nothing failed.
  Reports already on `gh-pages` predate the stamp and parse as `unknown`, which
  is treated as not-green rather than as a pass. Override with `REPORT_STATUS`
  only if a caller has a verdict the script cannot see.

- **A `Site Script Tests` lane and `tests/test_render_site_perf.py`** — the
  repo's first Python tests, stdlib `unittest`, no dependency to install. Run
  them with `python3 -m unittest discover -s tests`. `render-site-perf.py` runs
  exactly once per publish, on a runner, against state that only exists on
  `gh-pages`, and `workflow_run`/`schedule` only ever run the default branch's
  copy of a workflow — so every bug in it used to be a post-merge bug, found by
  reading the published site afterwards. The lane covers the colour thresholds,
  the distinct-value collapsing, the green/stale/unstamped cases and the badge
  JSON shape in about a second, with no VM. It also drives
  `generate-vm-report.sh` against a dead SSH port and feeds the result to the
  renderer, so the two scripts cannot drift on the meta-line format without a
  PR going red.

- **Two opt-out knobs on `vm-rocjitsu.yml`'s firmware generation**, so the
  playbook can drive a build whose Ansible controller has no Docker daemon.
  `rocjitsu_generate_firmware` (default `true`) gates the seven controller-side
  tasks — the generation step is a `delegate_to: localhost` `docker run`, and a
  BuildKit `RUN --security=insecure` step has the CLI on `PATH` with no socket
  behind it. `rocjitsu_firmware_ip_discovery` (default `true`) appends
  `--no-ip-discovery` to the generator argv when false: `ip_discovery.bin` is
  per-config and has to match the rocjitsu build the *consumer* serves the
  device from, not the one that built the disk, so a published qcow2 must not
  bake one in. Defaults preserve current behaviour exactly. The two
  driver-packaged firmware assertions stay ungated — they only stat what
  `amdgpu-dkms-firmware` installed and need no controller Docker.

- **`.github/actions/report-image-size`**, wired into all four bake lanes.
  Prints the baked qcow2's allocated size and an in-guest `du`/`dpkg-query`
  breakdown. Purely diagnostic and non-fatal. The ROCm images are ~19–20 GiB
  of real allocation and nothing had ever measured why, so any attempt to
  shrink them would have been judged against a guess.

- `pciutils` in `packages.d/packages-default`, which backs the new PCI
  devices section below. See **Fixed** for why the report lanes now use
  that manifest at all.
- A **PCI devices** section in every VM report, from `lspci -nn`. This
  repository exists to run emulated PCI devices — ernic NICs, rocjitsu GPUs —
  and the reports described the guests without ever listing them.
- A repository-owned Pages site under `site/`, so the published `gh-pages`
  branch is no longer just a generated landing page plus raw report subtrees.
  The new layout is documentation-first, closer to the ROCm/rocm-ernic site:
  it adds a styled home page, report index, publishing-flow documentation, and
  a generated performance-trends page backed by retained badge history on
  `gh-pages`. `publish-pages.yml` now checks out the repository, stages the
  site content, seeds the trend renderer from the existing `perf/history.jsonl`
  record on `gh-pages`, regenerates the badges at `/perf/`, and then syncs the
  static site content and report artifacts together.
- `bench-compile.yml`, which compiles both `scripts/vm-report/*.hip` files on
  every pull request that touches them. Until now the only thing that ever
  compiled them was the rocjitsu report lane, which runs on push to `main` and
  needs a GPU, a VM and two and a half hours — so a benchmark that was not
  valid C++ could sit on `main` indefinitely, and did. This lane installs
  hipcc and the hipFile SDK from the same TheRock `stable` channel the guests
  use — pinned to the `ubuntu2404` path, because the runner is `ubuntu-24.04`
  while the rocjitsu guest is built `--release resolute` and takes
  `ubuntu2604` — and runs the compile only: no GPU, no VM, and it is
  path-filtered to `scripts/vm-report/**` and the workflow itself, so it costs
  nothing on the PRs that do not touch the benchmarks. It compiles with
  `-Wall -Wextra`, which is stricter than the report script's own line, and
  without `-Werror`, so only hard errors fail it.

- `--mac ADDR` (`VM_MAC`) on `run-vm` and `gen-vm`, which sets the management
  NIC MAC instead of deriving it from the SSH port. Images this repo's
  `gen-vm` builds today do not need it — their netplan matches on interface
  name — but images it built before `3969f07` pin `match: macaddress:` to
  whatever MAC they saw at their own first boot, which is not necessarily one
  qemu-tool will pick again; an image built elsewhere may do the same. The NIC
  then comes up unconfigured and only SLIRP's fallback eventually gets an
  address on it, slowly. `--mac` makes qemu-tool present the MAC such an image
  expects, so it can be run as-is rather than rebuilt. See **Upgrading** above
  for how to find out what a given guest pinned. The value is validated as a
  unicast MAC up front, because qemu rejects a malformed one at
  device-creation time, long after `gen-vm` has fetched an image and built a
  seed.

- `publish-pages.yml`, now the only workflow that writes the site. The report
  lanes upload a named artifact and stop; this assembles them onto a
  `gh-pages` branch behind a landing page at `/`, with the single-VM report
  at `/1vm/` and the two-VM pair at `/two-vm/` and `/two-vm/vm2/`. It fetches
  the latest artifact *by name* rather than from the run that triggered it,
  so a lane that has not run for a week still contributes its last report and
  the site is never partial. Each lane's subtree is synced with its own
  scoped `--delete` instead of one delete over the whole site, so a lane
  whose artifact has expired keeps the report it last published, and a
  `perf/` record written by a future perf lane survives without needing to be
  named. A push rejected by a concurrent writer is retried from the new tip.
- PyPI publishing, so `pipx install qemu-tool` needs no checkout and no
  downloaded `.deb`. `release.yml` uploads via Trusted Publishing against a
  `pypi` environment rather than a stored API token, and runs last: a PyPI
  version can never be reused even after a delete, so publishing it before
  the deb has built would burn the version for a release that then has to be
  cut again as the next one. `qemu/pyproject.toml` gains the metadata a
  project page needs — readme, classifiers, keywords and URLs. The readme is
  written inline because the sdist root is `qemu/`, so `../README.md` is
  outside the project and cannot be packaged, and because the repo README
  documents compose stacks, Ansible and libvirt that a `pip install` does not
  install. `package.yml` and `release.yml` both run `twine check --strict`,
  so metadata that PyPI would reject fails on a PR rather than at tag time.
- `scripts/release.sh <version>`, which cuts a release from a clean `main`:
  it stamps `qemu/pyproject.toml`, generates the `qemu/debian/changelog`
  stanza from the `[Unreleased]` section of this file — preserving the
  Added/Changed/Fixed/Removed grouping as dpkg `[ Section ]` markers, since
  flattened into one list a removal reads exactly like an addition —
  retitles that section as the new version, commits signed-off and makes a
  signed tag. `--dry-run` shows the generated stanza and touches nothing. It
  never pushes; it prints the command that does. A failed commit restores
  the tree, which is safe because it refuses to start on a dirty one.
- A `verify-version` job gating `release.yml`. Nothing is built or published
  unless the tag, `qemu/pyproject.toml` and `qemu/debian/changelog` agree on
  the version and `CHANGELOG.md` has a heading for the tag. v1.3.0 shipped
  with no tag at all and every check stayed green, because the tag was the
  only thing that would have disagreed and nothing compared it to anything.
- A self-contained wheel. The compose stacks, both package manifests,
  `env.example` and the man page now ship inside the Python distribution, so
  `pipx install qemu-tool` is a working tool rather than a degraded one —
  previously a non-editable install could not locate a compose stack or the
  default package manifest at all. Bringing a stack *up* still needs
  `QEMU_TOOL_SRC` pointing at a checkout, because every stack builds
  qemu-tool from source inside its container and neither a wheel nor the
  `.deb` is a source tree; the README says so now. `pyproject.toml` maps
  them in from where they already live via
  `[tool.setuptools.package-dir]`, so `qemu/compose/`
  and `qemu/packages.d/` stay put and there is no second copy to keep in
  sync. They are namespaced under `qemu_tool.share` rather than directly
  under `qemu_tool`, because a data package named `qemu_tool.compose` would
  collide with the `compose` module and `importlib.resources` would silently
  resolve to the wrong directory.
- Per-user fallbacks for the two paths a rootless install cannot have:
  `$XDG_CONFIG_HOME/qemu-tool/env` for settings (searched between `qemu/.env`
  and `/etc/qemu-tool/env`) and `$XDG_DATA_HOME/qemu-tool/images` for images.
  The images default falls back only when `/var/lib/qemu-tool/images` is not
  *writable*, so a user outside the `kvm` group gets a usable directory
  instead of a permission failure on the default.
- `package.yml`, replacing `deb-package.yml`, now covering the wheel as well
  as the deb: it builds both, rebuilds the wheel from the sdist to prove the
  sdist carries the same data, and installs the wheel into a bare
  `python:3.12-slim` with no `/usr/share/qemu-tool` and no checkout to prove
  a pipx install resolves every stack and manifest. See `ci.md`.

- `rocjitsu_gpu_test`, a repo-local role that runs HIP workloads against a live
  rocjitsu vfio-user server: a scratch-free `vector_add`, a four-kernel
  private-segment repro, and an `hsa-snoop` trace of a dispatch on the emulated
  device. It runs from `vm-rocjitsu-test.yml` against a booted guest rather
  than at image build, because the server has to be serving. The repro carries
  a per-kernel watchdog and is run with `async`/`poll`: a scratch-starved
  dispatch hangs rather than failing, and the wedged ROCr thread leaves an
  unreapable zombie whose stdout never closes, which blocks a plain `command:`
  forever regardless of `timeout(1)`.
- hsa-snoop 1.1.1 in the rocjitsu guest image, with `bpftrace`. Pinned to a
  release tag rather than the rolling `latest` pre-release, so two builds of
  the same commit install the same bits. The packaged system-wide collector is
  disabled: it polls queues for its `:9488` exporter, which is free on real
  hardware but competes with the workload on a device that serves
  single-threaded. The test role invokes hsa-snoop per-workload instead.
- `ionic_image_prep`, a repo-local Ansible role that prepares a guest image for
  rocm-ernic's ionic mode: a pinned mainline kernel from `kernel.ubuntu.com`,
  the DKMS and rdma-core toolchain, the `1dd8:100a` `pci.ids` entry, the
  `modules-load.d` drop-in and the rdma-core provider stamp. Collection 0.2.0
  removed `ernic_image_prep` and now assumes an already-prepared guest; this
  repo builds its own with `gen-vm`, so it owns that preparation.
- `smoke-test-rocm-two-vms` and `vm-report-two-vms` workflows, replacing
  `2vm-smoke-test` and `2vm-vm-report`. Guest vCPU and memory are derived from
  the runner rather than fixed, with a floor of 4 vCPUs per guest.
- `ansible/playbooks/vars/ernic-pins.yml`: the mainline kernel ref and the
  rocm-ernic source commit, each written once and read by both plays.
- `qemu/env.example`, one settings template for the whole tool, replacing the
  four per-stack `env.example` files. Copy it to `qemu/.env`; `gen-vm`,
  `run-vm` and `compose` all read that one file. Every `VM_*` key maps to the
  flag of the same name, so `VM_VCPUS` is `--vcpus` and `VM_IMAGES_DIR` is
  `--images`, and the file is searched for at `--env-file`, `$QEMU_TOOL_ENV`,
  `./.env`, `qemu/.env`, then `/etc/qemu-tool/env`. Values apply lowest to
  highest from the defaults, a `--domain` XML, the file, then explicit flags,
  so a flag always beats the file. One-shot actions (`--dry-run`, `--force`,
  `--restore-image`, `--ansible-only`, `--nvme-recreate`) are deliberately not
  readable from it.
- `--env-file` on `gen-vm`, `run-vm` and `compose`.

### Changed

- The rocjitsu benchmark numbers pick their own SI prefix instead of always
  being reported in G-units. The GPU is attached over vfio-user, so the same
  benchmark spans five orders of magnitude between bare metal and the guest —
  231 GFLOP/s against 1.88 MFLOP/s — and a fixed unit renders one of the two
  unreadably, as `0.00187734275 GFLOP/s` on a shields.io badge. The value now
  selects between T, G, M, k and none, rounded to three significant figures,
  for the report table and both badges. The benchmarks themselves still emit
  `gflops=` and `read_gbs=`, so the parse and the raw output block are
  unchanged. The table carries a note saying the low absolute numbers are
  expected of a vfio-user guest and that the trend is what to watch.

- `vm-report` and `vm-report-two-vms` no longer publish the site themselves.
  Both called `actions/deploy-pages` with their own full `_site/`, and a Pages
  deploy replaces the whole site, so whichever ran last won and the other
  lane's report vanished — with both workflows reporting success. They now
  upload an artifact and `publish-pages.yml` assembles the site. The second
  guest's report moves from `site/2vm` to `site/vm2`, since nested under
  `/two-vm/` the old name read as a duplicate of its parent.

- The `.deb` no longer ships the bundled copy of the data the wheel carries.
  It installs it at the FHS locations as before, and `debian/rules` strips the
  duplicate from `dist-packages` at `dh_installdeb` — not at
  `dh_auto_install`, where `dh_python3` re-stages the package afterwards and
  silently undoes the removal. The `/usr/share/qemu-tool` copies keep
  precedence at runtime, so editing the installed files still takes effect.
- **rocm-ernic is now ionic-based.** Upstream deleted the out-of-tree
  `rocm_ernic_eth`/`rocm_ernic_rdma` drivers and the patched verbs provider on
  2026-09-16. The guest now builds upstream `ionic` + `ionic_rdma` as the
  `ionic-ernic` DKMS package and uses stock rdma-core's `providers/ionic`; the
  emulated device moved from `1022:8000` to Pensando `1dd8:100a`.
- `ansible-playbook-test` split into per-profile workflows (`-rocm`,
  `-rocjitsu`, `-ernic`, `-ernic-rocjitsu`) sharing a `setup-vm-job` action,
  and every workflow renamed to a consistent `qemu-minimal - <name>` form.
- `requirements.yml` pins the rocm-ernic collection to an upstream git SHA.
  Galaxy publishes only 0.1.0, which is the pre-ionic collection, so the
  previous `>=0.1.0` could never have resolved to 0.2.0.
- Container images move off the previous pins: rocm-ernic to
  `20260919.g959f0cf` (`ernic.6ca9a46` → `ernic.0b48aa1`), qemu to
  `20260924.ge1cd640`, and rocjitsu to `20260924.gad7a357-rocjitsu.c85bb75`
  (`rocjitsu.20d4ce1` → `rocjitsu.2d8a73f` → `rocjitsu.8e01a5a` →
  `rocjitsu.c85bb75`).

  rocjitsu was built from rocm-systems `develop` at
  `8e01a5a3fbee92f2b570dde97f314000d5226327`, which is where the vfio-pci work
  landed — 14 commits ahead of the pin before it under `emulation/rocjitsu`,
  including the `vram_store.cpp` 16-bit `atomic_load` and `compare_exchange`
  fixes. It now tracks `c85bb752577b6608745f4ff7835e3da91a190795`, a further
  100 commits on (`ahead 100, behind 0` — a straight fast-forward), of which 22
  are `[rocjitsu]`. Those are host-side footprint and throughput work rather
  than device-model changes: lazily constructed wavefronts instead of 16,384
  built at VM init (#11952), pooled KFD page-table nodes (#11951), lazy SGPR
  chunks (#11948), cached MTYPE lookups and sharded reader counters
  (#11949, #11950).

  Nothing guest-visible moved with it, which is the point worth recording.
  `gfx1250_mi455x.json` is byte-identical across the two images, as is the
  output of `vfio_guest_firmware.py --set gap` and the installed package set;
  the only config edits anywhere in the image are to `gfx1100_w7900.json`
  (`sgprs_per_wf` 104 → 106) and a new `gfx1251_synthetic.json`, neither of
  which this repo serves. In particular **none of this touches the guest
  scratch path**, so `vramlimit=1024` in `amdgpu-probe` stays exactly as it is
  — see the note there.

  `vfio_guest_firmware.py` did gain a `--generation gfx1250` flag, mutually
  exclusive with `--config`, for a caller with no rocjitsu config on disk (it
  leaves `config` null in the manifest). This repo generates stubs inside the
  container on the controller, where the config does exist, so it is unused
  here; it is the flag to reach for if firmware generation ever moves into the
  guest.

  Note that rocjitsu and qemu no longer share a CI build sha. That is fine and
  deliberate: they only have to agree on libvfio-user, which is unchanged at
  `vfu.8039244`. Do not "fix" the mismatch by rebuilding qemu unless
  libvfio-user itself moves.
- `spell-check` runs `codespell` instead of `pyspelling`/aspell. codespell
  matches a fixed list of known misspellings rather than validating every word
  against a dictionary, so the 441-entry `.wordlist.txt` is gone: hostnames,
  flags, image tags and hex fragments are no longer words anyone has to
  allow-list. Configuration is `.codespellrc`, and the workflow runs a bare
  `codespell` so a local run is the same command. It also covers the whole
  tree, not just `**/*.md`.
- ernic guests now get 4 vCPUs. `ionic_lif_size()` derives its EQ count from
  `num_online_cpus()` and `ionic_create_rdma_admin()` rejects fewer than
  `IONIC_EQ_COUNT_MIN`, so at 2 vCPUs `ionic_rdma` could never probe.

### Fixed

- Both rocjitsu-family report lanes failed on every push to `main` since RVS
  was added, at `Install ROCm Validation Suite`: the task asked apt for
  `amd-smi-lib` and `rocm-validation-suite`, which are `repo.radeon.com`
  names, while the guest resolves against TheRock. TheRock carries amd-smi as
  `amdrocm-amdsmi` and does not package RVS at all, under any name. The
  amd-smi request is renamed, and RVS now comes from AMD's standalone tarball
  at `repo.amd.com/rocm/rvs/tarball/`, unpacked into its own `/opt/rvs`
  prefix — not over the versioned ROCm tree, which apt owns and whose `bin`,
  `lib`, `include` and `share` the tarball would collide with. The task also
  had no `become: true`, so it would have failed on permissions next; the
  availability error simply fired first. `rocjitsu_gpu_test` follows the new
  prefix. Only `amdrocm7` tarballs are published, but every soname RVS needs
  is one TheRock 10.0 ships, and TheRock's own libraries pull `rocm_sysdeps`
  and `llvm` in through their `$ORIGIN` rpaths — verified by running the
  1.5.125 tarball against a bare 10.0.0-4 tree with nothing else on the
  library path. See #153.
- The published `/1vm/` report carried a nested copy of the whole site —
  `1vm/index.md` alongside `1vm/reports.md`, `1vm/documentation.md`,
  `1vm/publishing.md`, `1vm/assets/` and `1vm/perf/`. `vm-report.yml` checks
  out the repository, which populates `site/` with the repository-owned pages,
  then generated its report into that same `site/` and uploaded the whole tree
  as the `vm-report-1vm` artifact. The duplicated pages declare absolute
  permalinks (`/reports/` and friends), so they silently contended with the
  real root pages for those URLs. Both `vm-report` and `vm-report-two-vms` now
  generate into a lane subdirectory and upload only that — `site/1vm/`, and
  `site/two-vm/` plus `site/two-vm/vm2/` — the shape the rocjitsu and
  hipfile-fio lanes already used. `publish-pages.yml` syncs with `--delete`,
  so the stale files clear on the next publish without touching `gh-pages`
  by hand.
- Every published report said `nvme-cli unavailable` under **NVMe**, three of
  them next to a real device. `gen-vm` defaults `--packages` to
  `packages-default`, which carries `nvme-cli`, but all eleven `gen-vm` call
  sites in CI override that to `none` — a choice that arrived unexplained with
  the first `vm-report` workflow and was copied ever since. `vm-report` passes
  `--nvme 1`; `vm-report-rocjitsu` and `vm-report-hipfile-fio` reach the same
  place through `VM_NVME: "1"` in their compose stacks. All four report lanes
  now pass `--packages qemu/packages.d/packages-default`, so a report describes
  the guest a plain `gen-vm` gives you rather than a stripped-down one. That
  manifest is longer than `none`, so the lanes' SSH waits are worth watching if
  cloud-init ever slows. `packages-minimal` is left alone.
- Every published VM report linked its commit to `/commit/unknown`. All four
  pages — `1vm`, `two-vm`, `two-vm/vm2` and `rocjitsu` — carried the literal
  text `Commit: unknown` wrapped in a dead link, because
  `generate-vm-report.sh` resolved the SHA with `git rev-parse --short HEAD`
  and every report lane runs the script inside a container, where that call
  fails. `2>/dev/null` hid the reason, so it silently fell through to the
  `unknown` fallback; whether git is absent from the container images or its
  dubious-ownership check rejects the checkout was not pinned down, and the
  fix short-circuits both. The
  script now prefers `GITHUB_SHA`, which Actions always sets, and keeps git
  only for local invocation; the link carries the full SHA while the text
  shows the short form. When there is genuinely no commit to point at it
  emits plain text instead of a link, so the dead-link case cannot come back.

- `gemm-hipfile-bench` double-counted its device offset and aborted the whole
  rocjitsu report at block 16 of 32, reporting
  `warm hipFileRead block 16 returned -5022 of 1048576`.
  `hipFileRead`'s second
  argument is `buffer_base` and its fifth is `buffer_offset`, applied to that
  base; both read loops passed an already-advanced `dev + blk * kBlock` *and*
  the same increment again as the offset, so the effective destination was
  `dev + 2 * blk * kBlock`. The buffer is registered for 32 MiB, which makes
  block 15 the last one fully in range and block 16 the first to start past
  the registration, which the library reports as `-5022`,
  `hipFileInvalidValue`. Both loops now pass `dev`, the pointer
  `hipFileBufRegister` was given. Note that the benchmark's own correctness
  gate could not have caught this: it fails only when `failures` — elements
  exceeding `kTolerance`, 1e-3 — is non-zero, and `kBOffset` is 64 KiB, so
  both GEMM operands sit inside block 0, the one block the old arithmetic
  addressed correctly. Verified instead with an ad-hoc harness on the
  development VM, reading a 32 MiB random file from its NVMe and comparing
  device memory against `pread`: all 32 blocks match byte-for-byte. The
  benchmark then ran to completion for the first time — it had only ever been
  compile-verified before. Ten runs on that VM gave a `read_gbs` between 0.115
  and 0.151, which the report renders as 115–151 MB/s; note that the ~30%
  run-to-run spread is wider than the trend the badge is meant to show, so the
  single published figure should not be read as precise.

- Neither rocjitsu benchmark had ever compiled. `sgemm-bench.hip` and
  `gemm-hipfile-bench.hip` both used `goto cleanup` to reach a single cleanup
  block, and every one of those jumps crossed the initialisation of a variable
  declared later in the same scope — `block`, `grid`, `max_abs_error`,
  `start`, `seconds` and the rest. That is ill-formed C++, not a warning, and
  clang rejected it: 11 errors for `sgemm-bench.hip` against gfx1250. The
  failure stayed invisible for as long as the lane reported a blank line,
  because hipcc writes its diagnostics to stderr and the report script only
  captured stdout. Both files now release their device buffers, file
  descriptor, hipFile handle and driver through scope guards and return
  directly, so no jump crosses an initialisation. Guard declaration order is
  load-bearing: reverse-destruction order reproduces exactly what the
  `cleanup:` block did. Exit codes and every diagnostic string are unchanged.

- The hipFile benchmark reported "hipFile headers or library unavailable" on a
  guest that had the SDK installed. `run_rocjitsu_hipfile` probed only for the
  nested `include/hipfile/hipfile.h`, but TheRock — the stream
  `vm-rocjitsu.yml` uses — ships the header flat at `include/hipfile.h`, so the
  probe never matched and the lane exited before producing a `read_gbs`
  metric. The probe now accepts either layout, as the compile line below it and
  the benchmark's own `__has_include` already did.

- The rocjitsu guest never had the hipFile SDK in the first place, so fixing
  the probe alone would only have made it report the truth. `vm-rocjitsu.yml`
  listed `amdrocm-runtime-dev`, which depends on `amdrocm-sysdeps`,
  `amdrocm-runtime` and `amdrocm-llvm-dev` and nothing else — the development
  VM that the benchmark was proven on had `amdrocm-hipfile-dev` installed by
  hand. It is now in `rocm_setup_minimal_packages`, so a freshly generated
  image can run `gemm-hipfile-bench` and produce a `read_gbs` metric instead of
  aborting the report.

- The management and multicast NICs of one guest were given the same MAC.
  `_mcast_args` derived its address from the SSH port with the same
  arithmetic as `_mgmt_mac` rather than calling it, so `--mcast-group` put
  two interfaces on one address and the guest answered ARP for whichever
  came up first. The NIC index is now a byte of its own —
  `52:54:00:<index>:<hi>:<lo>` — so the management, multicast and data NICs
  cannot collide. The management NIC keeps the address it already had, so
  existing images that pin it still match. The data NIC gained an explicit
  MAC at the same time: it had none, which meant QEMU's built-in default
  `52:54:00:12:34:56` — exactly the address older guest images pin their
  netplan to, so a guest with both NICs could match its management netplan
  against the data NIC.

- The rocjitsu report lane reported its failures as a blank line. The
  guest-side benchmark scripts run under `set -euo pipefail`, and their
  `hipcc=$(command -v hipcc || ls /opt/rocm*/bin/hipcc ... | head -1)` probe
  fails the whole assignment when no `hipcc` exists: the unmatched glob makes
  `ls` exit non-zero and `pipefail` carries that out, so `set -e` killed the
  script one line above the `hipcc unavailable` guard written for exactly
  that case — with nothing printed, `ls` stderr being discarded. The
  `rocm=$(for ...; done | head -1)` hipFile probe had the same shape. Both
  now tolerate the empty result and reach their guard, and a benchmark that
  fails with no output at all is reported by name rather than as an empty
  `printf`. This is why `vm-report-rocjitsu` failed on its first run, took
  the `publish-pages` artifact with it, and left `/rocjitsu/` a 404 with two
  broken shields.io endpoint badges on the README.
- `scripts/release.sh` dropped every paragraph after the first in a
  multi-paragraph `CHANGELOG.md` bullet. A blank line ended the bullet, so
  the indented paragraph that followed matched the continuation rule but
  found an empty buffer and was discarded — silently, because the shortened
  stanza still parses. Blank lines no longer terminate a bullet, and a
  nested markdown item now becomes an entry of its own instead of being
  appended to its parent with a literal `- ` left mid-sentence.
- The wheel and sdist ship the MIT licence text. `license-files` was unset
  and `LICENSE` lives above the sdist root, so the PyPI artifacts carried
  only the `License: MIT` metadata string — not the text MIT requires be
  included in all copies. `qemu/LICENSE` is a symlink to the repo's, so
  there is no second copy to drift.
- `build-essential` is installed before `dpkg-buildpackage` in both
  `package.yml` and `release.yml`. `dpkg-checkbuilddeps` treats it as an
  implicit build dependency of every source package, even an arch-all
  Python one that compiles nothing, and the runner image does not ship it.
  Neither deb build had ever run on a GitHub runner to find out.
- The container jobs in `package.yml` pin `shell: bash`. The runner chose
  `sh -e` for the `ubuntu:24.04` container despite bash being installed
  there, and dash has no `set -o pipefail`, which every assertion step
  opens with.

- `vm-rocjitsu.yml` no longer stubs over the real gfx1250 firmware.
  `vfio_guest_firmware.py` changed contract: its default output is now the
  "gap" set -- `gc_12_1_0_imu.bin`, `gc_12_1_0_mes.bin`, `gc_12_1_0_mes1.bin`
  and `ip_discovery.bin`, the files no driver release ships -- which lands
  beside the packaged blobs instead of over them. As of amdgpu 31.60,
  `amdgpu-dkms-firmware` ships real `gc_12_1_0_mec.bin`,
  `gc_12_1_0_mec_1.bin`, `gc_12_1_0_rlc.bin`, `gc_12_1_0_rlc_1.bin`,
  `gc_12_1_0_uni_mes.bin` and `sdma_7_1_0.bin`, and `amdgpu-dkms` depends on
  it. That, plus `imu` from the generator, covers what upstream's
  `docs/qemu-vfio.md` calls for.

  Those blobs install under `/lib/firmware/updates/amdgpu/`, not
  `/lib/firmware/amdgpu/`. The kernel searches `updates/` first, so they take
  precedence over anything of the same name written into the base tree — which
  is why the gap set is safe to copy in alongside them.

  The playbook asserted on `gc_12_1_0_rlc_1.bin`, now deliberately absent, so a
  working generator failed the build. It asserts on `imu` instead, which is in
  both sets and in neither package. The separate `rj-ip-discovery` call is gone
  — the generator emits `ip_discovery.bin` itself, so that call overwrote what
  had just been produced. The `uni_mes` → `mes`/`mes1` copy is gone too, and
  had become actively wrong: it overwrote the generator's real `mes`/`mes1`
  stubs with the packaged `uni_mes` blob. A new guest-side check names any
  missing packaged blob, because the driver otherwise reports only `MES
  firmware reports incorrect version in ucode binary` or a bare `-2`. Set
  `vm_rocjitsu_firmware_set=full` for a guest on a pre-31.60 driver release.

  That check initially looked in `/lib/firmware/amdgpu/`, where it found only
  the stubs an earlier `--set full` run had left behind. It therefore passed on
  a guest carrying stale debris and failed on a freshly built one that was
  entirely correct. It now checks `/lib/firmware/updates/amdgpu/`, against the
  package's own file list.
- HIP kernels with a private segment (scratch) now complete on the emulated
  gfx1250 instead of hanging forever. `amdgpu-probe` passed
  `amdgpu.vramlimit=256`; upstream's `qemu-vfio.md` says 1024, and it is not a
  performance knob. ROCr provisions queue scratch out of that budget at the
  occupancy the device advertises, so a kernel with no private segment is
  unaffected — `vector_add` always passed in under a second — while one with a
  private segment waits on an allocation that never arrives. The dispatch never
  fails, so `hipDeviceSynchronize()` simply does not return and no ordinary
  timeout catches it; the wedged ROCr thread then sits in an uninterruptible
  KFD ioctl holding a KFD mutex for the rest of the boot, so only the first
  such result in a boot means anything.

  This is independent of `vram_aperture_bytes` in the rocjitsu config, which is
  the BAR window — upstream leaves that at 256 MiB alongside a 1 GiB
  `vramlimit`. Conflating the two sent this investigation down a dead end for
  some time, so it is worth stating plainly. The A/B is clean: the same server
  build, with only `vramlimit` changed, hangs at 256 and passes all four repro
  kernels at 1024.
- `amdgpu` now probes on the rocjitsu emulated device instead of oopsing the
  guest. Two independent faults, both of which took the machine down hard
  enough that `sudo amdgpu-probe` returned only `Killed`:
  - The device is exposed with `rombar=0`, so there is no option ROM and
    `amdgpu_device_init` takes its "VBIOS image optional" path, leaving
    `adev->mode_info.atom_context` NULL. `amdgpu_ras_init` then queries the RAS
    capability from the VBIOS and the atomfirmware helpers dereference that
    field unguarded, oopsing in `amdgpu_atom_parse_data_header`.
    `vm-rocjitsu.yml` now patches the DKMS source to return early when
    `atom_context` is NULL, matching the guard `amdgpu_ras_get_quirks` already
    has on the same field one function above.
  - `amdgpu-probe` passed `ip_block_mask=0x3f`, taken from upstream's
    `qemu-vfio.md`. That is correct for upstream's driver, where the compute IP
    blocks enumerate as indices 0..5 ending in `mes`; this DKMS build
    enumerates an extra `ras_v1_0` at index 5, so `mes_v12_1` lands at 6 and
    `0x3f` masks it off. `gfx_v12_1` needs MES to resume the command processor,
    so the probe oopsed in `gfx_v12_1_xcc_cp_resume`. Now `0x7f`.

  With both applied the probe completes, `/dev/kfd` appears, and `rocminfo`
  reports `gfx1250` with 32 CUs.
- `gen-vm` guests no longer lose networking when the MAC qemu-tool presents
  changes. The guest MAC is derived from the SSH port, and guests were ignoring
  the seed's `network-config` and falling back to cloud-init's own, which pins
  the interface to the MAC seen at creation time. Booting such an image again
  then left the NIC `unmanaged` with no address — on a different port, but also
  on the same one, including the default: before `4532f91` the slirp path
  passed no `mac=` at all, so QEMU's built-in `52:54:00:12:34:56` applied and
  got baked into the guest, while every later boot presents the derived
  address. A guest built with `--mgmt-tap` already got the derived MAC and so
  only broke when the port changed. In every case slirp's `hostfwd` still
  completed the TCP handshake, so SSH reported `Connection timed out during
  banner exchange` rather than `refused`, and `gen-vm` `--ansible-only` sat in
  `_wait_for_ssh` for its full 600 s without ever reaching the playbook. First
  boot now overwrites the rendered
  `/etc/netplan/50-cloud-init.yaml` with a `name: "en*"` match, so the MAC
  stops being load-bearing. Existing images keep the old pin; regenerate, or
  repoint netplan in the guest.
- `vm-rocjitsu.yml` now resolves the ROCm install prefix and writes
  `/etc/ld.so.conf.d/rocm.conf` and `/etc/profile.d/rocm.sh` against it.
  TheRock installs to a versioned `/opt/rocm/core-10.0`, but `rocm_setup`
  hardcodes the pre-TheRock `/opt/rocm/lib` and never sets `PATH`, so the
  linker resolved no ROCm libraries (`ldconfig -p | grep -c hsa-runtime` was 0)
  and `rocminfo` was `command not found` despite being installed. Apt then
  helpfully offers the universe 5.2.3 package, which the play already pins to
  priority `-1`. This is a workaround for
  [batesste-ansible#248](https://github.com/sbates130272/batesste-ansible/issues/248)
  and should be dropped once the role derives the prefix itself.
- The ernic configure play supplies `ernic_guest_vm_ip`, which
  `ernic_guest_setup` asserts on and defaults to empty. Derived as
  `192.168.200.<10 * vm_index>`, matching upstream `vm-register.yml`.
- `ernic_source_repo_version` is pinned to the same commit as the collection.
  It defaults to `main`, so the sources built in the guest could come from a
  different tree than the roles building them.
- `ansible-playbook-test-ernic` now triggers on `ansible/playbooks/roles/**`
  and `vars/ernic-pins.yml`. Both are inputs to `vm-ernic.yml`, but neither was
  in the workflow's `paths`, so a change to `ionic_image_prep` or to the kernel
  and source pins reported all checks green without the ernic lane having run
  at all.
- `ionic_image_prep` no longer corrupts `pci.ids`. hwdata already lists
  `1dd8:100a` (as `DSC Serial Port Controller` — the emulated NIC reuses a real
  pair), and the presence check grepped for our own entry text, so it never
  matched and appended a duplicate device id. pciutils refuses to parse a file
  containing one, which left `lspci` resolving no names for any device on the
  bus. The check is now a block-scoped `awk` scan for the device under its
  vendor, and a post-merge `lspci` parse check fails the play if a merge ever
  does break the file.

### Removed

- `qemu/gen-vm` and `qemu/run-vm`. `qemu-tool gen-vm` and `qemu-tool run-vm`
  have been the maintained path for some time and the two bash scripts had
  drifted; keeping both meant every flag had to be added twice. `shell-check`
  now lints only the two `libvirt/` scripts.
- The four per-stack `qemu/compose/*/env.example` files, superseded by
  `qemu/env.example`.
- The `smoke-test` workflow, which drove the removed bash scripts.

## [v1.3.0] - 2026-09-15

### Added

- `qemu-tool list` subcommand: lists every `qemu-system-*` process on the node,
  including VMs inside containers (attributed back to the container name), with
  vCPU count, memory, forwarded SSH port, KVM state, uptime and attached
  vfio-user sockets. `--json` for machine-readable output, `--qemu-tool-only`
  to filter.
- VM identity markers: `run-vm` and `gen-vm` now stamp each VM with
  `-name guest=<vm-name>,debug-threads=on` and a deterministic
  `-uuid <uuid5(namespace, "<role>:<vm-name>")>`. `list` recomputes the UUID
  from the guest name to identify qemu-tool's VMs exactly, and to distinguish a
  transient `gen-vm` build VM from a running `run-vm` VM. Both options are
  generic, so they work on amd64, arm64 and riscv64 alike.

### Changed

- `--vm-name` now rejects a comma. A comma terminates a QEMU option value, so
  it silently corrupted `-name`, `-uuid`, `-drive file=` and `-chardev path=`.

### Fixed

- The Debian package declared no dependencies beyond the Python interpreter.
  It now `Depends` on a QEMU system emulator and `qemu-utils`, `Recommends`
  `cloud-image-utils`, `openssh-client` and `wget` for `gen-vm`, and `Suggests`
  `ansible`, `dnsmasq-base`, `docker.io` and `iproute2` for the optional paths.

## [v1.2.0] - 2026-08-31

### Added

- `python3-qemu-tool` Debian package for system-wide installation via `dpkg`/`apt`
- Man page `qemu-tool(1)` installed to `/usr/share/man/man1/`
- `qemu/packages.d/packages-default`: package manifest moved into `qemu/` so it
  is self-contained within the deb build tree; installed to
  `/usr/share/qemu-tool/packages-default`
- CI: parallel `build-deb` job in `release.yml` uploads the `.deb` to GitHub Releases

### Changed

- Default image directory changed from `../images` (repo-relative) to
  `/var/lib/qemu-tool/images`; the installer creates the directory owned
  `root:kvm` with mode `2775` (setgid for kvm group inheritance)
- Default package manifest path changed from the repo-relative path to
  `/usr/share/qemu-tool/packages-default`

## [v1.0.0] - 2026-08-05

### Added

- `qemu/gen-vm`: cloud-init based VM creation for x86_64, ARM64, and RISC-V
- `qemu/run-vm`: flexible QEMU runner with NVMe, VFIO, CXL, VirtFS, QMP, and
  multicast NIC support
- `libvirt/virt-install-ubuntu`: libvirt VM creation via `virt-install`
- `libvirt/create-nvme`: helper to generate NVMe XML fragments for libvirt
- `packages.d/packages-default`: broad development and debugging package manifest
- `packages.d/packages-minimal`: minimal package manifest (`emacs-nox`, `fio`,
  `sysstat`, `tree`)
- `ansible/playbooks/vm-setup.yml`: post cloud-init Ansible playbook using the
  `sbates130272.batesste` Galaxy collection
- `udev/99-qemu-minimal-vfio.rules`: udev rules for VFIO device permissions
- `udev/install-vfio-rules`: installer script for the udev rules
- Shared backing-file workflow allowing multiple VMs to share a single
  read-only qcow2
- `RESTORE_IMAGE` mode to recreate a VM overlay from an existing backing file
- CI workflows: shell-check, smoke-test (x86/arm64, Noble/Resolute),
  ansible-setup-test, spell-check, and release
