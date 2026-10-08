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
- `qemu/compose/vfio-user-ernic-rocjitsu-scale-out/` — N-VM ernic mesh + one
  GPU per VM. **Generated** by `qemu-tool gen-compose` from `qemu/env.scale-out`;
  do not hand-edit it
- `images/` — qcow2 VM disk images (backing + overlay pairs, **not** `/var/lib/qemu-tool/images`)
- `ansible/` — Ansible playbooks for VM provisioning
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
cp qemu/env.example qemu/.env
# Edit qemu/.env: set VM_IMAGES_DIR to the repo images/ absolute path
# Add --profile rocjitsu-vm1 --profile rocjitsu-vm2 to enable GPUs
qemu-tool compose --stack vfio-user-ernic-2vm \
  --profile rocjitsu-vm1 --profile rocjitsu-vm2 up -d
```

Key `qemu/.env` values that differ from the example defaults:

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

**Its `docker-compose.yml` and `prometheus.yml` are generated.** Edit
`qemu/env.scale-out` and re-run `qemu-tool gen-compose`; a hand edit is lost on
the next regeneration and `gen-compose --check` fails in CI — the
`Gen Compose Check` lane, which also runs `docker compose config` against
both the committed stack and a tap-enabled render. Both files are
committed so the stack stays reviewable in a diff and usable with plain
`docker compose`.

```bash
qemu-tool gen-vm --env-file qemu/env.scale-out \
    --vm-name stebates-fleet-1 --backing-image "$VM_BACKING_IMAGE"
for n in $(seq 2 8); do
  qemu-tool gen-vm --env-file qemu/env.scale-out --vm-name stebates-fleet-$n \
      --backing-file images/stebates-fleet-1-backing.qcow2
done
qemu-tool gen-compose --env-file qemu/env.scale-out
qemu-tool compose --env-file qemu/env.scale-out \
    --stack vfio-user-ernic-rocjitsu-scale-out --profile metrics up -d
```

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
not reachable), `VM_VCPUS × VM_COUNT` should not exceed `nproc` plus about 25%
(issue 14), and `VM_SHM_SIZE × VM_COUNT` must fit `/dev/shm`. Memory never
binds in practice: 48 VMs used 107G of 503G. `VM_VCPUS` must stay ≥ 4 or
`ionic_rdma` never probes.

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
| emulated device `1022:8000` | Pensando `1dd8:1002` (subsystem `1dd8:5400`) |

`ionic_rdma` needs kernel ≥ 6.18 (`drivers/infiniband/hw/ionic` merged there)
and `ib_umem_get_va`, which landed after 7.0. No Ubuntu release ships one, so
the guest runs a pinned mainline kernel.

None of this is done by hand in this repo. Two pieces of automation own it:

- [`ansible/playbooks/roles/ionic_image_prep/`](ansible/playbooks/roles/ionic_image_prep/)
  prepares the guest image — installs the pinned mainline kernel from
  `kernel.ubuntu.com/mainline`, the build toolchain, the `1dd8:1002` pci.ids
  entry, and `/etc/modules-load.d/rocm-ernic-ionic.conf`. Upstream assumes a
  guest already prepared this way (their published `ionic` qcow2); this repo
  builds its own, so it owns the preparation.
- `sbates130272.rocm_ernic.ernic_guest_setup` from the collection builds and
  installs the DKMS package, the rdma-core provider and the NIC config.

The kernel ref is pinned to `v7.2.4` (via `ionic_kernel_pin` in
[`playbooks/vars/ernic-pins.yml`](ansible/playbooks/vars/ernic-pins.yml)) as
`ernic_ionic_kernel_ref` in [`vm-ernic.yml`](ansible/playbooks/vm-ernic.yml),
which also feeds `ionic_image_kernel_ref` so the sources and the guest kernel
cannot drift apart — `driver_ionic.yml` fails the build if they disagree on
major.minor.

Device names are unchanged: Ethernet `rocm-ernic0`, IB `rocm-rdma-ernic0`, both
still set by `99-rocm-ernic.rules` (see udev section below), now keyed on
`1dd8:1002` (subsystem `1dd8:5400`) and `DRIVERS=="ionic|ionic_rdma"`.

## udev rules (in-VM)

`99-rocm-ernic.rules` gives the ernic devices stable names: Ethernet interface
→ `rocm-ernic0`, IB device → `rocm-rdma-ernic0`. Use these with
`ib_send_bw -d rocm-rdma-ernic0`.

There is nothing to install by hand. `ernic_guest_setup` ships the rules and
reloads udev, and both renames are confirmed working in CI — the ionic lane's
`Show NIC details` reports `rocm-ernic0` carrying `altname enp0s5np0`, which is
the pre-rename kernel name, and `ibv_devinfo` reports `hca_id: rocm-rdma-ernic0`.
The IB rename shells out to `/usr/bin/rdma`, which the gen-vm guest has.

If a guest somehow comes up with the kernel names instead, check that the rules
matched rather than re-copying them: they key on `ATTR{device/vendor}=="0x1dd8"`,
`ATTR{device/device}=="0x1002"`, and subsystem `0x1dd8:0x5400`, so a guest still
being served the old `1022:8000` device (or `1dd8:100a`) will not rename anything.

## PCI ID (in-VM)

`ionic_image_prep` owns this; there is nothing to do by hand. Two traps make it
less straightforward than it looks.

**`1dd8:1002` (subsystem `1dd8:5400`) is not yet in the upstream pciids database.**
rocm-ernic PR #190 moved the emulated device from `1dd8:100a` (a real Pensando
DSC Serial Port Controller that hwdata already named) to `1dd8:1002`, the device
ID the real ionic hardware presents. Neither `update-pciids` nor the upstream
collection's own `driver_common.yml` call injects a custom entry — `ernic_guest_setup`
runs `update-pciids` and then warns if the subsystem label is absent, which it
always is until AMD submits the entry upstream. `ionic_image_prep` fills the gap:

```
1dd8  AMD Pensando Systems
	1002  ROCm Emulated RDMA NIC (ionic)
```

**pciutils rejects a duplicate device id outright.** Not "ignores the second
entry" — it refuses to parse the file, and then resolves no names for *any*
device on the bus:

```
$ lspci -i /tmp/merged.ids -nn
lspci: Duplicate entry at /tmp/merged.ids, line 27685
$ echo $?
1
```

So the role's presence check is a block-scoped `awk` scan asking whether *this
vendor's block* already lists the device — a bare `grep 1002` matches the same
four hex digits under dozens of other vendors, and a `grep` for the entry text
misses any future upstream spelling. When it does insert, the line goes directly
under the vendor line (`pci.ids` is parsed sequentially, so an append at the end
of the file lands under the class section and never matches), and a post-merge
`lspci` parse check fails the play rather than quietly breaking every later `lspci`.

See `ionic_image_pciids_*` in
[`ionic_image_prep/defaults/main.yml`](ansible/playbooks/roles/ionic_image_prep/defaults/main.yml).

**Note:** `update-pciids` will overwrite these files — re-apply after each run.
Nothing in a bake runs it any more, though: `rocm_setup` used to call it
unconditionally, and `sbates130272.batesste` 3.0.0 defaults
`rocm_setup_update_pciids` to false. So the hazard is now a hand-run
`update-pciids`, or a lane that sets that variable back to true, rather than
something every ROCm guest does to itself on the way past.

## Known issues / open bugs

See `rocm-ernic-enablement.md` for the full tracking list. Short version:

1. Worker `server_ip` ARP hijack when VM IP = `192.168.100.1` — use `.11`/`.12`
2. *(was ARP log printf aliasing in `pvrdma_eth.c:483-493` — file is gone
   with the pre-ionic `driver/` tree deleted 2026-09-16)*
3. Collection 0.2.0 is not on Galaxy — only 0.1.0 is published, and 0.1.0 is
   the pre-ionic collection. `requirements.yml` pins the upstream git SHA
   instead; move back to a Galaxy version pin once 0.2.0 ships there
4. *(fixed upstream — `driver_ionic.yml` fail_msg no longer references the
   dead `ernic_image_kernel_mainline` variable; resolved in rocm-ernic@1484557)*
5. *(was an `apply-rocm-ernic-dv.sh` / rdma-core / `docs/testing.rst` bug —
   all three files are gone with the pre-ionic tree)*
6. **Killing perftest mid-run breaks VM device context** — killing `ib_send_bw`
   or any perftest process mid-operation (SIGTERM/SIGKILL) leaves the guest
   RDMA driver in a broken state. `ibv_devinfo` reports "Failed to open device"
   or "wasn't found". `rmmod`/`modprobe` hangs for 2 min (DSR timeout) and the
   device does not recover. **Full stack restart required** (`docker compose down
   && up`). Avoid killing tests; let them run to completion or use `ib_send_bw
   --iters 100` to keep runs short.

7. **ernic-hub crash requires full stack restart** — if `ernic-hub` crashes and
   restarts (visible in `docker compose logs ernic-hub` as a re-initialization
   from the top), the guest driver enters a DSR initialization timeout
   (`-ETIMEDOUT`) on the next `modprobe ionic_rdma`. The vfio-user session
   held by QEMU becomes stale and cannot be recovered by reloading modules alone.
   **Fix: `docker compose down && docker compose up`** (all containers, not just
   qemu-1/qemu-2). Restarting only the QEMU containers is insufficient because
   the ernic-worker also needs a fresh TCP mesh connection to the new hub.

8. **perftest server dies if SSH session closes** — `nohup`/`disown` is not
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

9. **`ernic_source_repo_version` defaults to `main`** — so `ernic_source` clones
   upstream HEAD inside the guest even when `requirements.yml` installs the
   collection from a fixed SHA, and the roles can end up building sources from
   a different tree than they came from. Pinned locally in
   [`playbooks/vars/ernic-pins.yml`](ansible/playbooks/vars/ernic-pins.yml);
   the default is still upstream's.
10. **`ernic_nic_subnet` is now a role default upstream** (rocm-ernic@1484557),
    but it still has to be repeated at play-var level in `vm-ernic.yml`'s
    configure play because role defaults are not in scope when play vars are
    evaluated. The upstream commit documents this explicitly.
11. *(fixed upstream — `ernic_guest_setup` now includes `preflight.yml` which
    checks `ansible_processor_nproc >= IONIC_EQ_COUNT_MIN` before any expensive
    apt/DKMS work and fails with a clear message; resolved in rocm-ernic@1484557)*
12. **A rebooted ernic guest is device-present but not fully test-ready.**
    As of rocm-ernic@1484557, `ernic_guest_setup` writes persistent
    `/etc/modules-load.d/` entries for `ionic` and `ionic_rdma` (via
    `modprobe persistent:`), so the RDMA device comes back after a reboot.
    `rocm_xio` is deliberately excluded — it is hand-copied into the running
    kernel's `extra/`, not DKMS-managed, so a persistent entry would outlive
    the module across a kernel upgrade. The address on `rocm-ernic0`, bringing
    the netdev up and the counters symlink are still run-time only; re-run the
    configure play to restore full test-readiness after a reboot.
13. **The mesh allocates a new node id on every reconnect and never reclaims
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
14. **The mesh manager's 20 s heartbeat timeout turns CPU oversubscription into
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
15. **The manager leaks a file descriptor per evicted node, and Docker's
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
16. **Stock in-tree `ionic` drives the emulated device; only the PCI ID is
    missing.** On a mainline kernel (verified on 7.2.4) no DKMS build and no
    AMD patch is needed to bring the NIC and the RDMA device up — the shipped
    `ionic.ko`/`ionic_rdma.ko` bind to `1dd8:1002` (subsystem `1dd8:5400`) and
    work as soon as the id is added to the driver's table:

    ```
    echo "1dd8 1002" > /sys/bus/pci/drivers/ionic/new_id
    ```

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
17. **The mesh saturates at about 40 registered nodes — the 64-node protocol
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

18. **The TCP backend resolved every GID to the same node — fixed upstream.**
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

    Two things worth keeping from the hunt. **Mesh size is the trigger, not
    the cause** — 2 nodes passes everything, 4/8/32 fail identically, so a
    two-node reproduction will never show it. And **node-id assignment order
    is a red herring**: ids are handed out in connection order and do not
    match node index unless starts are serialised, which is true and
    irrelevant, because the resolver never consulted the id. Serialising
    worker startup costs about 56 s of boot at 32 nodes and fixes nothing.

## Git / GitHub

- Default GitHub account: `sbates130272` (verify with `gh auth status`)
- GPG signing required on all commits (`-S`), signoff required (`-s`)
- Main branch: `main`; current work branch: `feat/pypi-package`
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
(root)), not from a Pages artifact. `.github/workflows/qemu-minimal-publish-pages.yml` is
the only workflow that writes that branch; report lanes upload a named
artifact and nothing else.

That split is the whole point. `report-for-vm-basic` and `report-for-vm-ernic` each used
to call `actions/deploy-pages` with their own full `_site/`, and a Pages
deploy replaces the entire site, so whichever ran last won and the other
lane's report disappeared — with both workflows green.

`publish-pages` fetches each lane's latest artifact *by name*, not from the
run that triggered it, so a lane that has not run for a week still
contributes. Each lane's subtree is rsynced with its own scoped `--delete`,
so an expired artifact leaves that lane's last report in place and anything
else on the branch (a future `perf/`) survives untouched.

Adding a lane is four edits: upload an artifact under a new name, add a
`fetch <name> <dir>` line, add the lane's workflow `name:` to
`on.workflow_run.workflows`, and add a row to the landing page. Miss the
third and the lane publishes nothing until some *other* lane finishes, which
looks like the site being stale rather than a missing trigger. Do **not** add
`.nojekyll` — the reports are markdown that GitHub's Jekyll build renders,
unlike rocm-ernic's pre-built Sphinx HTML.
