# vfio-user-ernic-rocjitsu-scale-out

An N-VM rocm-ernic mesh, one emulated RDMA NIC and one rocjitsu GPU per VM.
Same topology as `vfio-user-ernic-2vm`, sized by a variable instead of by hand.

**`docker-compose.yml`, `prometheus.yml` and `fleet-inventory.yml` in this
directory are generated.** Edit [`qemu/env/scale-out`](../../env/scale-out)
and regenerate; do not edit them directly. All three are committed so the
stack stays reviewable in a diff and usable with plain `docker compose`, but a
hand edit is lost on the next regeneration and the `Gen Compose Check` CI lane
will fail.

`grafana/` is **not** generated — nothing in it depends on `VM_COUNT`, because
every query groups by a label instead of naming a VM.

```bash
qemu-tool gen-compose --env-file qemu/env/scale-out
```

## One golden file, plus overlays

`qemu/env/scale-out` is the **golden** settings file and the only one that
documents every key. A fleet that differs from it — a CI lane, a particular
host — states only the keys it differs on, in a small committed overlay named
after the golden file on the command line:

```bash
qemu-tool gen-compose \
    --env-file qemu/env/scale-out --env-file qemu/env/overlay-mi100 \
    --output-dir /var/lib/qemu-tool/stacks/mi100-fleet
qemu-tool compose --stack-dir /var/lib/qemu-tool/stacks/mi100-fleet \
    --env-file qemu/env/scale-out --env-file qemu/env/overlay-mi100 \
    --profile metrics up -d
```

`--env-file` is repeatable and layers left to right, key by key: a key the
overlay sets wins, a key it omits keeps the golden value. `qemu-tool` passes
each layer through to `docker compose` as its own `--env-file` in the same
order, which applies the same last-wins rule, so both see identical values.

The two directory flags are not decoration. `gen-compose` writes to this
directory by default, so an overlay render without `--output-dir` replaces the
committed 8-VM stack with the overlay's fleet and the `Gen Compose Check` lane
fails. And the layers do not size a running stack: `VM_COUNT` and the VM names
are baked into the YAML at generation time, so `compose` without `--stack-dir`
brings up the committed stack and reports success.

This replaces four full copies of the same file —
`env.scale-out{,-ci,-mi100,-strix}` — that shared about twenty identical keys
and were hand-synced. The committed overlays are
[`qemu/env/overlay-ci`](../../env/overlay-ci) and
[`qemu/env/overlay-mi100`](../../env/overlay-mi100).

The generated files in this directory come from the golden file alone, so they
record one `# env-file:` line. A render with an overlay records one line per
layer and a single `# env-sha256:` over all of them concatenated — an edit to
the overlay makes the stack stale exactly as an edit to the golden file does.

## Why generated

The 2-VM stack hand-writes every socket path, MAC, SSH port and qcow2 name.
That does not survive 8 VMs, let alone the mesh's 64-node ceiling
(`rdma_backend_tcp.c`), and hand-editing 24 services is exactly how two VMs end
up sharing a MAC — which presents as an intermittent RDMA fault, not as a
config error.

Per-VM values in the settings file are comma-lists, not numbered variables:
`VM1_NAME`/`VM2_NAME` does not scale. A bare value broadcasts to every VM, a
list of length `VM_COUNT` applies positionally, and any other length is an
error. The exception is a key naming a field that is *already* a list
(`VM_EXTRA_HOSTFWD` and friends), where the commas mean several values for one
VM and the whole value goes to every VM. The golden `env/scale-out` documents
this at length because getting it backwards fails silently — and it is the
only one of the settings files that does, which is most of why there is now
one of them and not four.

## Bringing it up

The VMs need qcow2 overlays. Fetch the published backing image once and
overlay the rest on it — `gen-compose` prints these commands with the names
filled in:

```bash
qemu-tool gen-vm --env-file qemu/env/scale-out \
    --vm-name stebates-fleet-1 --backing-image "$VM_BACKING_IMAGE"
for n in $(seq 2 8); do
  qemu-tool gen-vm --env-file qemu/env/scale-out \
      --vm-name stebates-fleet-$n \
      --backing-file images/stebates-fleet-1-backing.qcow2
done

qemu-tool compose --env-file qemu/env/scale-out \
    --stack vfio-user-ernic-rocjitsu-scale-out --profile metrics up -d
```

One backing file serves the whole fleet, which is why `VM_BACKING_SHARED=true`
is not optional here: all N VMs open it at once, and that needs
`backing.file.locking=off`.

For a fleet with an overlay, append `--env-file qemu/env/overlay-<name>` to
**every** command above — `gen-vm` takes it repeatedly too, and it is where
`VM_IMAGES_DIR` comes from, so a layer dropped from one of them silently builds
the overlays in the wrong directory. `VM_NAME_PREFIX` is not a `VMConfig` field
and `gen-vm` never reads it: only `gen-compose` does, and it reaches `gen-vm`
through the `--vm-name` arguments you type — which is why `gen-compose` prints
the whole list with the names already filled in. Run the `compose` command with
`--stack-dir` pointing at the `--output-dir` the overlay was rendered to.

## Topology

| Service | Role |
|---|---|
| `ernic-1` | mesh manager, `tcp:manager:listen:$ERNIC_TCP_PORT` |
| `ernic-2..N` | workers, dialling the manager's **address**, not its name |
| `rocjitsu-1..N` | one emulated GPU per VM |
| `qemu-1..N` | the guests, each attached to its own two sockets |
| `ernic-stats-exporter` | `--profile metrics`, serves `:9840/metrics` |
| `prometheus` | `--profile metrics`, scrapes every source below |
| `grafana` | `--profile metrics`, provisioned dashboard on `:3000` |
| `lmcache-coordinator` | `--profile lmcache`, the controller the in-guest workers register with |

Derived per VM `n`: name `<VM_NAME_PREFIX>-<n>`, SSH port
`VM_SSH_PORT_BASE + n - 1`, MAC `<ERNIC_MAC_PREFIX>:<n>>8>:<n&0xff>`, guest IP
`<ERNIC_GUEST_SUBNET>.<10+n>`. Guests start at `.11`: the manager claims `.1`
as its DHCP `server_ip` and intercepts ARP for it, so a VM placed there is
unreachable in a way that looks like a driver fault.

## Addressing

Every service has a fixed address on the `fleet` network —
`$FLEET_NET_PREFIX.<family>.<n>`, family `0` infra, `1` ernic, `2` rocjitsu,
`3` qemu — and **nothing in the stack resolves a service name at run time**.
Workers dial the manager's address, Prometheus scrapes addresses.

That is a scaling fix, not tidiness; see the ramp below.

Sockets (`/run/vfu/{ernic,rocjitsu}-<n>.sock`) are derived only, with no
override — they are internal wiring between services generated together, so an
override could only desynchronise them. Each guest is passed its own two
explicitly rather than globbing the directory, which the single-VM stacks do
and which would attach every device to every VM here.

## `ernic-1` does not restart

Deliberate, and the one thing worth knowing before reading the logs. If the
mesh manager restarts, every guest driver enters a DSR initialization timeout
on the next `modprobe ionic_rdma`, and the vfio-user sessions QEMU holds are
stale — reloading guest modules does not recover them. The only fix is
`docker compose down && up` for the whole stack. `restart: on-failure` would
turn that into a quiet wedge instead of a visible crash. See AGENTS.md issue 7.

## Nothing here runs as root

Every service carries `user: ${CONTAINER_USER:?...}` and `cap_drop: [ALL]`,
and `qemu-tool compose` supplies the uid from whoever ran it — your own
`uid:gid`, plus `CONTAINER_KVM_GID` from the gid that owns `/dev/kvm`. You do
not have to configure anything; set `CONTAINER_USER` only to pin a *different*
identity, including `0:0` to go back to root deliberately.

A plain `docker compose up` has no caller to ask and refuses rather than
guessing:

```
error while interpolating services.qemu-3.user: required variable
CONTAINER_USER is missing a value: set CONTAINER_USER=<uid>:<gid>
```

That is the point of the `:?` spelling. `${CONTAINER_USER:-}` interpolates to
the empty string, `user: ":"` is not an error to Docker, and the fleet runs as
uid 0 with one warning line as the evidence — measured on compose v5.3.1.
Supply both and plain compose works:

```bash
CONTAINER_USER=$(id -u):$(id -g) \
CONTAINER_KVM_GID=$(stat -c %g /dev/kvm) docker compose up -d
```

Three services are exempt, each for its own reason:

| Service | Why |
|---|---|
| `tapsetup-N` | Cannot. Docker sets no **ambient** capabilities, so a non-root uid holds no effective `NET_ADMIN` and `ip link add` fails with `RTNETLINK answers: Operation not permitted`. Compose cannot express ambient caps. It is short-lived, holds `NET_ADMIN` and nothing else (`cap_drop: [ALL]` first, then that one back — it used to run with Docker's full default set *plus* `NET_ADMIN`), and runs read-only. |
| `dockerapi` | Speaks to the host Docker socket, which is root's. |
| `*-stats-exporter` | Pinned to `65534`. They create nothing on the host, so there is no ownership to fix and `nobody` is the stronger position. |

A uid is not a capability set — a container can be uid 1000 and still hold
`NET_RAW` — so `cap_drop: [ALL]` and `no-new-privileges:true` are on
everything, independently of who it runs as.

**Migrating a fleet that ran as root.** `qemu-tool compose up` creates
`PROM_DATA_DIR`, `LOKI_DATA_DIR` and `GRAFANA_DATA_DIR` as you, and names any
file it finds that an earlier root run left behind. The one it cannot fix is a
`<vm>-nvmeN.qcow2`: 0644 root-owned, and QEMU opens a drive read-write, so the
guest will not boot at all rather than merely owning the wrong file. Delete
them — they are scratch.

## Alloy holds no Docker socket

`:ro` on a socket mount restricts the *inode*, not the API behind it. Anything
that can `connect()` to `/var/run/docker.sock` can create a privileged
container and own the host, whatever flags the mount carries — so the old
`- /var/run/docker.sock:/var/run/docker.sock:ro` on `alloy` was a root
credential held by the one process in the stack that parses untrusted
container log content.

The `dockerapi` service is an haproxy in front of that socket with a path
allowlist: `CONTAINERS`, `EVENTS`, `PING` and `VERSION` on, `POST=0` and
everything else off. `POST=0` is the line that matters — without it the
allowlist still permits container create, start and exec. Alloy reaches the
daemon over the fleet network through `DOCKER_HOST`, holds no socket, and
therefore takes `CONTAINER_USER` like everything else.

This does not remove the root-equivalent container — anything that speaks to
the daemon needs the socket — it shrinks it from a log pipeline to a proxy
whose whole job is saying no. The proxy publishes no port, so it is reachable
from the fleet network and nowhere else.

## Guest Ethernet (`ERNIC_TAP`)

Off by default. Turn it on and the guests can talk IP to each other; leave it
off and they cannot, at all.

**rocm-ernic's TCP mesh carries RDMA payload only.** The emulated NIC has no
Ethernet datapath unless it is attached to a TAP (`-T`). Without one the guest
driver transmits happily — `ethtool -S` counts the packets — the device drops
them, `ernic_ip_bytes_tx_total` never moves, ARP goes unanswered and ping gets
100% loss. Since `ib_send_bw` exchanges queue-pair parameters out of band over
TCP, that also means **no inter-node RDMA test can run at all**.

With `ERNIC_TAP=true` each ernic gets `tap0`, and a `tapsetup-<n>` sidecar
sharing that container's network namespace creates the tap, creates a bridge,
and enslaves both the tap and the container's `l2` interface. Every ernic is
on the same `l2` network, so every guest lands on one L2 segment. Measured on
a 32-VM fleet: 31/31 peers reachable, ARP resolving to the real ernic MACs.

Three things about the shape are deliberate:

- **`CAP_NET_ADMIN` is in the sidecar, which exits.** Creating a tap or a
  bridge needs it; *attaching* to a pre-created, owned tap does not — only
  `/dev/net/tun`. Verified: an ernic at Docker's default capability set
  reports `Ethernet attached to TAP tap0`. Nothing privileged stays running.
- **The bridge takes `l2`, never the mesh interface.** The mesh interface
  carries the address workers dial; moving it into a bridge would break
  registration.
- **The ernic entrypoint waits for `tap0` before exec.** The sidecar shares
  the ernic's namespace, so it cannot run until that container exists — the
  tap is not there at the instant `rocm-ernic` would start.

The sidecar image is built by compose from `tapsetup/` rather than pulled. It
is three lines and not worth publishing, and 32 containers each running
`apk add` at start is exactly the start-burst that cost twelve guests when
`pipx install` raced pypi.org.

### Per-node GIDs

Each ernic is given `ERNIC_TCP_GUEST_GIDS` naming the GIDs its guest owns:
the EUI-64 link-local derived from the fleet MAC, and the guest IPv4. The mesh
resolves a destination GID to a node by asking each node what it owns, so this
is inherently per-node — it cannot be set once in the shared anchor. It is
derived from the MAC rather than restated in the settings file, because the
MAC already says it.

Getting this wrong is not a startup error. The mesh routes the payload to
whichever node answers, the receiving node has no matching queue pair, and the
transfer fails with `Completion with error at client` — a long way from the
cause.

## Metrics

Opt-in with `--profile metrics`. Two sources:

- **Guest node-exporter**, scraped at the qemu container's fleet address on
  `:9100`. The guests are behind
  QEMU SLIRP inside their containers and have no address on this network, so
  the *container* is the target and `VM_EXTRA_HOSTFWD=tcp::9100-:9100` forwards
  through to the guest. Each container has its own netns, so every VM uses the
  same port with no arithmetic and nothing published to the host.
- **ernic stats**, from the dumps each `rocm-ernic -S` writes to the shared
  `ernic-stats` tmpfs about once a second: per-QP bytes, doorbells, CQEs, WQEs
  by opcode, MMIO and interrupt counts. `qemu-tool ernic-stats` parses them;
  its metric and label names match rocm-ernic's own exporter, so that project's
  `grafana/ernic-dashboard.json` works here unmodified.

The in-guest GPU exporters (`amd-metrics-exporter` on :5000,
`hsa-snoop-prometheus` on :9488) are deliberately *not* scraped: they poll the
emulated device, and rocjitsu serves single-threaded, so every scrape comes out
of the same thread as the workload being measured.

### Grafana

Rides the same `metrics` profile — Grafana without Prometheus is an empty
page, so it does not get a third switch. `:3000`, anonymous admin, dark.

Datasource and dashboard are provisioned from `grafana/` rather than clicked
in: a dashboard that exists only in Grafana's own sqlite dies with the
container, which defeats the point of a stack that is reproducible from one
env file. The dashboard directory is mounted read-only and `allowUiUpdates` is
off, because a UI that accepts an edit it then silently fails to persist is
worse than one that refuses it. Edit the JSON and `docker compose restart
grafana`.

Two shape rules in the dashboard are worth keeping if you extend it:

- **Per-VM panels are bar gauges, not one line per VM.** A line per VM stops
  being readable long before this stack's ~40-node ceiling, and a palette that
  cycles hues past eight makes two VMs the same colour.
- **No panel has two y-axes.** Where two measures do not share a unit they get
  two panels.

## LMCache

Opt in with `--profile lmcache`, on top of `--profile metrics`.

```bash
docker build -t qemu-tool/lmcache-rocm:0.5.6-gfx1250 ../../docker/lmcache
qemu-tool compose --env-file qemu/env/scale-out \
    --stack vfio-user-ernic-rocjitsu-scale-out \
    --profile metrics --profile lmcache up -d

docker save qemu-tool/lmcache-rocm:0.5.6-gfx1250 -o /tmp/lmcache-rocm.tar
cd ansible && ansible-playbook \
    -i ../qemu/compose/vfio-user-ernic-rocjitsu-scale-out/fleet-inventory.yml \
    playbooks/fleet-lmcache.yml \
    -e lmcache_guest_image_archive=/tmp/lmcache-rocm.tar
```

The image does not exist until you build it. There is no published LMCache
wheel for gfx1250 — see [`qemu/docker/README.md`](../../docker/README.md).

### Why half of it is not in this file

One coordinator container beside the device servers, and one worker container
**inside** each guest. Compose has no reach into a VM, so the workers cannot
be services here no matter how much tidier that would be. They are started by
a systemd unit that
[`roles/lmcache_guest`](../../../ansible/playbooks/roles/lmcache_guest/)
installs.

That unit's `ExecStartPre` is the part that matters. It probes `amdgpu`, waits
for `rocm-rdma-ernic0`, **and reassigns the address on `rocm-ernic0`** —
because only module loading survives a guest reboot (AGENTS.md issue 12), so a
unit gated on the device node alone would come up cleanly against an
unconfigured NIC every time the guest restarted. It also fails loudly when
`ibv_devinfo` cannot open the device, which is the issue 6/7 shape: that needs
a host-side `docker compose down && up`, and is otherwise diagnosed from
inside the guest for an hour.

`amdgpu` is non-fatal by default and the RDMA NIC is fatal. A guest with no
GPU is still a useful mesh member here, because L1 is host memory and the
P2P reads land in it; a guest with no RDMA device is not.

### MP mode, and why that matters

Both halves are upstream entrypoints: `lmcache coordinator` on the host and
`lmcache server` in each guest. **Not `lmcache_controller`** — that is the
legacy in-process mode's controller, and it waits on a ZMQ pull/reply pair
for `LMCacheWorker` registrations an MP server never sends. Pair the two and
both come up cleanly with every guest registered to nothing.

An MP server needs no vLLM and no engine config file; it is configured
entirely by the CLI flags the unit passes.

L1 is host memory and L2 is the emulated NVMe. Keeping KV out of device
memory is the same decision as not scraping the in-guest GPU exporters:
rocjitsu serves single-threaded, so anything asked of the device comes out of
the same thread as whatever else is using it.

### P2P serves L1 only

A peer reads what is resident in another node's *memory*; it cannot reach
that node's L2. So a cross-node hit depends on the key still being in the
peer's L1, and L1 is small — at `LMCACHE_L1_SIZE_GB=1` with 8 MiB chunks that
is ~128 chunks, about 16 sequences of 8.

Measured here: `lmcache_coord_l1_keys` sat at ~95 per node while the
coordinator's directory held 6464 keys. Everything else had spilled to the
local NVMe and was no longer P2P-reachable, so a cross-read of an older range
returns `0/8` — which looks like P2P being broken and is not.

The serving node does not count the transfer either. LMCache publishes the
requesting side only (`l2_load_completed_requests`, labelled
`l2_name="p2p"`). `l2_store_completed_requests{l2_name="p2p"}` is not the
counterpart: it increments on both guests roughly equally and tracks the
adapter being offered stores, not reads served.

### How a guest reaches the coordinator

The guests reach the coordinator at its **fleet address**, `172.31.0.12`, not
at SLIRP's `10.0.2.2` gateway and not by service name. A guest has no
interface on the fleet network, but QEMU's user-mode networking NATs its
egress out through the qemu container's own stack — and that container is on
that network. So the ZMQ pull/reply ports need not be published at all. Only
`LMCACHE_COORD_PORT` is, because the REST API is what an operator queries:

```bash
curl -s localhost:9000/query_worker_info \
    -H 'content-type: application/json' \
    -d '{"instance_id": "stebates-fleet-1"}'
```

Worth doing, because **a worker that is up is not necessarily a worker that
registered.** The engine builds either way and an unreachable controller is a
log line, not an exit — so the dashboard's "LMCache workers up" is scrape
reachability, and this is the registration check.

### Two switches

`LMCACHE_ENABLE` decides whether the services and scrape jobs are
**generated**; `--profile lmcache` decides whether they **run**. Both exist
because `prometheus.yml` has no notion of a profile, so a job emitted for a
profile nobody selected is just a permanently-down target.

`LMCACHE_WORKER_METRICS_PORT` has to match `lmcache_guest_metrics_port` in the
role, and has to appear in `VM_EXTRA_HOSTFWD`. Nothing cross-checks those
three; a mismatch shows up only as a scrape target that never comes up.

## What a regeneration owns in the output directory

`gen-compose` writes the three generated files and then **mirrors** the stack's
non-generated assets — `grafana/`, `logging/`, `tapsetup/` — into the output
directory. Mirrors, not copies: after a regeneration those directories match
this one exactly. Anything in them that this directory does not have is
deleted, and every deletion is printed by name.

Everything else in the output directory belongs to whoever put it there and is
never read or written: `prom-data/`, `grafana-data/`, `loki-data/`, results,
scripts, tarballs, a `docker-compose.override.yml`. A directory qualifies only
if the rendered compose file names it as `./name` **and** this directory has
something of that name, so a relative `PROM_DATA_DIR` cannot make a TSDB look
like an asset.

That holds at any depth, not just at the top. A relative bind mount nested
under an owned name — `LOKI_DATA_DIR=./logging/data`, Loki's data next to
`logging/loki-config.yml` — is the operator's too, and is skipped by the
prune. Absolute paths are still what the settings file documents for the three
`*_DATA_DIR` keys, and still what the fleet uses.

The rule the mirror will not break: **it deletes nothing outside the output
directory.** A symlink would take it there — `ln -s /srv/grafana-shared
<stack>/grafana` to keep dashboards on a big disk — so a symlink on an owned
path is refused rather than followed, at the top level and inside:

```
Refused /srv/fleet/grafana: symlink to /srv/grafana-shared, not this stack's
Error: the stack directory was not fully mirrored:
  /srv/fleet/grafana is a symlink to /srv/grafana-shared; it was left alone
```

Nothing under the link is read, written or removed; the other asset
directories are still mirrored, `--check` reports the asset as a symlink, and
`gen-compose` exits non-zero. Replace the link with a real directory to get
that asset mirrored again.

A removal that fails — a read-only directory, a file owned by another uid —
is collected and reported at the end rather than raised on the spot, and the
copies all run first. A regeneration that fails part way therefore leaves the
stack no emptier than it started.

Additive copying is what this replaced, and it cost the 32-VM fleet two hours
of metrics. `grafana/provisioning/datasources/` ended up holding a hand-made
`prom.yml` *and* this directory's `prometheus.yml`, both `isDefault: true`:

```
Datasource provisioning error: datasource.yaml config is invalid.
Only one datasource per organization can be marked as default
```

Grafana crash-looped on that for two hours with nothing alerting; it surfaced
only as `infra 1/3` in `qemu-tool list --summary`. An extra file breaks
nothing until something downstream reads the whole directory, which is why
`--check` now compares the asset directories as well as the three generated
files — it is the gate that is supposed to notice exactly this.

The comparison is symmetric, and names what it found:

```
Error: stack assets missing from the output directory: logging/loki-config.yml
Error: stack assets modified from the repo's: tapsetup/Dockerfile
Error: extra stack assets the repo does not have: grafana/provisioning/datasources/prom.yml
```

Reporting only the extras would answer "would a regeneration delete
anything", which is not the question: `rm <stack>/logging/loki-config.yml`
left `--check` printing `Up to date` for a stack that cannot boot, because
Docker recreates that bind-mount source as a directory and loki dies on
`read /etc/loki/config.yml: is a directory`.

The first regeneration against a directory that has been hand-managed will
therefore remove hand-added dashboards, provisioning files and anything
renamed aside during a repair. That is the intent. Read the `Removed` lines;
move anything worth keeping into this directory and commit it.

## Staleness

`docker-compose.yml` carries an `# env-sha256:` header recording the settings
file it was generated from. `qemu-tool compose` recomputes it and warns on
stderr when they disagree, naming the regeneration command. It never blocks —
`down` against a stale tree is how you recover from a bad edit.

## Ceilings

### Static limits

| Limit | Value |
|---|---|
| **mesh registration, measured** | **~40 nodes — the real ceiling** |
| rocm-ernic mesh topology payload | 64 nodes (`rdma_backend_tcp.c:2570`) |
| `VM_SHM_SIZE` × `VM_COUNT` ≤ `/dev/shm` | ~30 VMs at 8g on a 252G host |
| `VM_VCPUS` × `VM_COUNT` ≤ `nproc` | 32 VMs at 4 vCPU on 128 cores |

The vCPU row is the one that actually binds; see *Measured* below. It tolerates
about 25% of overcommit and then the fleet starts losing guests.

`VM_VCPUS` must not drop below 4 on an ernic stack: `ionic_lif_size()` derives
its EQ count from `num_online_cpus()` and `ionic_create_rdma_admin()` rejects
fewer than `IONIC_EQ_COUNT_MIN`, so `ionic_rdma` never probes. The only symptom
is "Failed to register ibdev" in the guest log.

### Measured

Ramped on a 128-core / 503G / 252G-`/dev/shm` host, 4 vCPU and 8G per VM, timed
from `compose up` to every guest's sshd banner. Nothing was overcommitted at
the top of this range: 48 VMs used 107G of 503G.

| VMs | containers | vCPU allocated | guests at sshd | lost |
|---|---|---|---|---|
| 4 | 12 | 16 | 26s | 0 |
| 8 | 24 | 32 | 28s | 0 |
| 16 | 48 | 64 | 31s | 0 |
| 32 | 96 | 128 | 46s | 0 |
| **40** | **120** | **160** | **117s** | **0** |
| 40 (repeat) | 122 | 160 | 37 of 40 | 3 |
| 44 | 132 | 176 | 41 of 44 | 3 |
| 48 | 144 | 192 | 41 of 48 | 7 |
| 64 | 196 | 256 | 40 of 64 (12 min) | 24 |

Sub-linear to 32 — 8× the VMs for 1.8× the time, with vCPU allocation exactly
matching the host's 128 cores. **32 VMs is the largest size that booted clean
every time. 40 is marginal**: the same stack on the same host booted 40/40
once and lost 3 on a repeat, so treat the 40 row as a coin flip rather than a
supported size. The boot burst is what decides it — load average peaked at
**417** on 128 cores during the losing run, and fell back to 54 once the
survivors were up.

Memory is not what runs out: 48 VMs used 107G of 503G, and the fleet at 44 had
396G free. What runs out is CPU. Every VM needs 4 vCPU (`ionic_rdma` will not
probe below `IONIC_EQ_COUNT_MIN`), so 32 VMs already saturate a 128-core host
and 40 is 25% over. Guests idle after boot, which is why 40 works at all — but
the boot burst is not idle, and load peaked at 174 during the 40-VM start.

### What breaks first, and why it is not memory

Two of the three failure modes found at 48 were name resolution, and both are
fixed above; the third is upstream's and is not.

1. *(fixed — static addressing)* Docker runs one embedded resolver per network
   and a 144-container start burst saturates it. Workers died with `Failed to
   resolve host 'ernic-1': Temporary failure in name resolution`. Worse, the
   mesh advertises each node by `gethostname()` and peers resolve that name to
   dial it, so every one of the fleet's N×(N−1) peer links went through the
   same resolver. Each container is now named after its own address, which
   makes those a numeric `getaddrinfo`. Measured effect at 48: **4497 → 0**
   resolution failures.
2. *(fixed — retry)* The entrypoint's `pipx install` reaches pypi.org, and 48
   of those racing killed 12 guests before QEMU started. Retried five times.
3. *(upstream, unfixed)* The manager's heartbeat timeout is 20 s and is not
   tunable — `rocm-ernic --help` exposes no knob. During a 48-VM start the
   manager slips past it and declares live nodes dead (`Node N failed health
   check (last heartbeat: 20 seconds ago)`). The evicted worker exits, its
   vfio-user socket disappears, and the guest that had attached to it dies
   with `vfio-user: timed out waiting for reply`. At both 44 and 48 the lost
   guests matched the restarted ernics one for one — 3 and 7 respectively.

   This is the CPU limit wearing a protocol's clothes. The heartbeat is a wall
   clock, so oversubscribing the host does not merely slow the fleet down, it
   makes the manager mistake slow nodes for dead ones. Which is why 40 VMs
   boot cleanly in 117 s and 44 lose three: nothing is short of memory at
   either size.

Failure 3 compounds because **the mesh allocates a new node id on every
reconnect and never reclaims the old one**. The 64-node cap is therefore a cap
on *connection events*, not on VMs. The same 48-VM fleet reached id 61 before
the fixes above and 50 after them — so churn, not `VM_COUNT`, is what consumes
the budget, and a fleet that flaps its way past 64 stops working for reasons
that have nothing to do with its size.

### What actually kills the fleet

The node id is not the only thing a reconnect consumes. **The manager does not
close the socket of a node it evicts**, so each eviction leaks a file
descriptor, and Docker's default soft `nofile` is 1024. On the 40-VM fleet the
manager evicted eighteen live workers before its own guest had even attached;
at roughly one leaked fd per eviction the limit arrives well inside a normal
run. The first thing to fail is the manager reopening its own stats file, then
every `accept()`:

```
ERROR: rdma: Failed to open stats file /run/ernic-stats/ernic-1.stats: Too many open files
ERROR: rdma: TCP: Failed to accept connection: Too many open files
```

There is no backoff on that path — it spins, and produced 3.3 GB of log in 12
hours. The manager then goes unhealthy, and being `restart: "no"` it stays
down, so its guest dies on an assertion in QEMU's own error handling
(`error_setv: Assertion *errp == NULL failed`).

Every ernic service therefore gets `ulimits.nofile` from `ERNIC_NOFILE`
(default 65536). **This does not fix the leak** — that is upstream's — it moves
the wall far enough out that a fleet-length run finishes first. Raise it
further, or reduce churn, for a fleet that flaps.

Re-measured at 40 VMs with the limit raised: manager steady at 165 fds, **0**
EMFILE, max node id 31 of the 64 budget (it reached 50–61 before). So the
long-run death is gone. The *boot* ceiling is unchanged — that run still lost
3 guests to 24 heartbeat evictions, and the three lost guests were exactly the
three ernics that restarted. Raising the fd limit buys longevity, not scale.

### The mesh saturates at ~40 nodes, and 64 is not reachable

Asking for 64 VMs on this host settles at **40 of 64**. The other 24 workers
never register; they sit in a permanent restart loop on

```
ERROR: rdma: TCP: Registration timeout
Failed to realize PVRDMA device with backend 'tcp:worker:172.31.1.1:6320'
```

This is **not** the boot burst. Load peaked at 843 on 128 cores — 64 VMs ask
for 256 vCPU — and the fleet took 12 minutes to settle. But the stragglers
still fail identically with the host back at load 16, and stopping a healthy
worker to free a slot lets others in. It is a capacity wall, not a delay.

The cause is that this is a **full mesh**. A worker holds about N+2 sockets
where N is the *registered* size, not `VM_COUNT` — 42 sockets measured on both
the 40-VM and the 64-VM fleet. Registering node N+1 therefore costs N new
connections and touches every existing node, through a single-threaded manager,
against a fixed wall-clock registration timeout. The cost per registration
climbs with the mesh until registration stops completing.

The 64-node topology cap never came into it: **max node id reached 30**. Plan
around ~40, expect it to move with host speed, and note two consequences:

- **A stuck worker retries forever; its guest gets one chance.** `ernic` is
  `restart: on-failure`, `qemu` is `restart: "no"`. By the time a worker on its
  fifteenth attempt registers, the VM it exists to serve has been dead for ten
  minutes. The 24 stuck workers matched the 24 dead guests exactly.
- **Failed registrations leak manager fds too, and faster than evictions do.**
  Measured over a six-hour run parked above the wall: 8006 worker restarts and
  50 fds/min at the manager, reaching 18164 open descriptors. That is Docker's
  1024 default in 20 minutes and `ERNIC_NOFILE=65536` in about 22 hours — a
  working day, not an indefinite reprieve. Size the fleet below the wall; no
  fd limit makes sitting above it survivable.
