# The `rocm-golden` stack

Two guests from the published rocm-golden qcow2, each with one emulated
rocjitsu GPU, one NVMe, one rocm-ernic RDMA NIC with P4 in guest scope and one
SDXI device, the NICs joined by a TCP mesh. It is the default stack: the one
`qemu-tool compose` runs when given no `--stack`, and the one `qemu-tool quick-start` renders.

The same generator renders any guest count, so most of this README is about
fleets larger than the two guests committed here: the measurements below were
taken on 8- to 64-guest renders of this stack.

**`docker-compose.yml`, `prometheus.yml` and `fleet-inventory.yml` in this
directory are generated.** Edit
[`qemu/stacks/rocm-golden.xml`](../../stacks/rocm-golden.xml) and regenerate; do
not edit them directly. All three are committed so the stack stays reviewable in a
diff and usable with plain `docker compose`, but a hand edit is lost on the
next regeneration and the `Gen Compose Check` CI lane will fail.

`grafana/` is **not** generated — nothing in it depends on `<vm-count>`,
because every query groups by a label instead of naming a VM.

```bash
qemu-tool gen-compose --xml qemu/stacks/rocm-golden.xml
```

## Settings are fleet XML, and a larger fleet is an overlay

`qemu/stacks/rocm-golden.xml` is the settings file for this stack and the template
for a fleet of your own. A fleet that differs from it states only the elements
it differs on, in a short fleet XML file of your own, layered after it on the
command line:

```bash
qemu-tool gen-compose \
    --xml qemu/stacks/rocm-golden.xml --xml ~/fleets/mine.xml \
    --output-dir /var/lib/qemu-tool/stacks/mine
qemu-tool compose --stack-dir /var/lib/qemu-tool/stacks/mine \
    --profile metrics up -d
```

`--xml` is repeatable and layers left to right, element by element: an element
the overlay sets wins, one it omits keeps this file's value. No overlay is
committed to the repository; a host's or a CI lane's fleet is a local file.

The two directory flags are not decoration. `gen-compose` writes to this
directory by default, so an overlay render without `--output-dir` replaces the
committed two-guest stack with the overlay's fleet and the `Gen Compose Check`
lane fails. And the layers do not size a running stack: `<vm-count>` and the VM
names are baked into the YAML at generation time, so `compose` without
`--stack-dir` brings up the committed stack and reports success.

Each generated file records one `# xml-sha256:` line, a digest over every
layer concatenated — an edit to an overlay makes the stack stale exactly as an
edit to `rocm-golden.xml` does.

## What plain `docker compose` needs

Everything in the fleet XML is baked into the YAML at render time, with four
exceptions that differ per host and so are left for `docker compose` to
interpolate from its environment:

| Variable | Meaning | If unset |
|---|---|---|
| `CONTAINER_USER` | `uid:gid` the containers run as | compose refuses |
| `CONTAINER_KVM_GID` | gid that opens `/dev/kvm` | compose refuses |
| `VM_IMAGES_DIR` | the guest images directory | `/var/lib/qemu-tool/images` |
| `QEMU_TOOL_SRC` | qemu-tool source tree the qemu containers mount | `../../..` from this directory |

`qemu-tool compose` sets all four in the environment of the `docker compose`
it starts — the first two from who ran it, the others from `--images`,
`<vm-images-dir>` and `<qemu-tool-src>` — without overriding one already
exported. It writes no env file and passes no `--env-file`.

## Why generated

Hand-writing every socket path, MAC, SSH port and qcow2 name
does not survive 8 VMs, let alone the mesh's 64-node ceiling
(`rdma_backend_tcp.c`), and hand-editing 24 services is exactly how two VMs end
up sharing a MAC — which presents as an intermittent RDMA fault, not as a
config error.

Three elements take a per-VM comma-list, `<vm-shm-size>`, `<rocjitsu-config>` and `<vm-filesystem>`:
a bare value broadcasts to every VM, a list of length `<vm-count>` applies
positionally, and any other length is an error. `<vm-extra-hostfwd>` is the
exception, because its field is *already* a list: the commas mean several
rules for one VM and the whole value goes to every VM. Getting it backwards
fails silently.

## Bringing it up

`qemu-tool quick-start` does all of this. By hand, the VMs need qcow2
overlays: fetch the published backing image once and overlay the rest on it —
`gen-compose` prints these commands with the names filled in:

```bash
qemu-tool gen-vm --xml qemu/stacks/rocm-golden.xml --vm-name rocm-golden-1
qemu-tool gen-vm --xml qemu/stacks/rocm-golden.xml --vm-name rocm-golden-2 \
    --backing-file /var/lib/qemu-tool/images/rocm-golden-1-backing.qcow2

qemu-tool compose --stack rocm-golden --profile metrics up -d
```

The first `gen-vm` pulls `<backing-ref>`, and `up` pulls the device server
images. One backing file serves the whole fleet, which
is why the generated stack starts every guest with `run-vm --backing-shared`:
all N VMs open it at once, and that needs `backing.file.locking=off`. No
fleet element controls it.

For a fleet with an overlay, append its `--xml` to the `gen-vm` commands too —
it is where `<vm-images-dir>` comes from, so a layer dropped from one of them
silently builds the overlays in the wrong directory. `<vm-name-prefix>` is not
something `gen-vm` reads: only `gen-compose` does, and it reaches `gen-vm`
through the `--vm-name` arguments you type — which is why `gen-compose` prints
the whole list with the names already filled in. Run the `compose` command with
`--stack-dir` pointing at the `--output-dir` the overlay was rendered to, and
with the same `--xml` layers if `<vm-images-dir>` is not the default: the
images directory is the one fleet value a rendered stack does not bake in.

## Topology

| Service | Role |
|---|---|
| `ernic-1` | mesh manager, `tcp:manager:listen:<ernic-tcp-port>` |
| `ernic-2..N` | workers, dialling the manager's **address**, not its name |
| `rocjitsu-1..N` | one emulated GPU per VM |
| `sdxi-1..N` | one emulated SDXI device per VM, when `<sdxi-per-vm>` is 1 |
| `tapsetup-1..N` | short-lived sidecar per ernic, when `<ernic-tap>` is true |
| `qemu-1..N` | the guests, each attached to its own sockets |
| `ernic-stats-exporter` | `--profile metrics`, serves `:9840/metrics` |
| `prometheus` | `--profile metrics`, scrapes every source below |
| `grafana` | `--profile metrics` and `<grafana-enable>`, provisioned dashboards on `:3000` |
| `loki`, `alloy` | `--profile metrics` and `<logs-enable>`, container logs |

Derived per VM `n`, with no element to override any of them: name
`<vm-name-prefix>-<n>`, SSH port `<ssh-port-base> + n - 1`, MAC
`72:6f:63:6d:<n>>8>:<n&0xff>`, guest IP `192.168.100.<10+n>`. The MAC prefix
and the network are constants of the generator (`gen_compose.ERNIC_MAC_PREFIX`,
`ERNIC_SUBNET`): the guest image addresses its NICs on that network at boot,
and the quick-start playbook finds a guest's NIC by that prefix. Guests start
at `.11`: the manager claims `.1`
as its DHCP `server_ip` and intercepts ARP for it, so a VM placed there is
unreachable in a way that looks like a driver fault.

**Published ports.** Two kinds of service publish a host port: each `qemu-N`
its guest's SSH port, and `grafana`. Both bind `<publish-bind-addr>`, which
is `127.0.0.1` unless the fleet file says otherwise, so by hand a guest is

```bash
ssh -i <vm-images-dir>/keys/id_rsa -p 2222 ubuntu@localhost   # guest 1; 2223 is guest 2
```

from the host the stack runs on, and from nowhere else; `qemu-tool ssh
<vm-name>` resolves the same thing. Inside, the guest's hostname is that
`<vm-name>` (`rocm-golden-1`, `rocm-golden-2`): `run-vm` passes it in the
SMBIOS serial number and the image sets it at boot. Both halves have to be
there, or the guests are `ernic-vm-1`, `ernic-vm-2` as before: an image
published after `20261117.g481c892`, and a `run-vm` in the qemu containers
that passes the name. The containers build qemu-tool from the tree
`QEMU_TOOL_SRC` (or `<qemu-tool-src>`) names, and with none there they
install it from PyPI, where `2.0.0` does not pass one; a stack run without
a checkout needs the first release after it. `sudo cat
/sys/class/dmi/id/product_serial` in a guest says which half is missing:
empty, and the host passed no name. Nothing else is published: the
`<vm-extra-hostfwd>` rules listen inside each qemu container and Prometheus
scrapes them over the stack's own network.

`<publish-bind-addr>0.0.0.0` puts the stack on every interface of the host.
That is root in every guest for whoever can reach it: the published image's
key ships beside it, the password of its account is in its `vm-info.json`,
the guest's sshd accepts either, and the account has sudo. Grafana has
anonymous admin as well. Prefer `ssh -L` from the other machine, or one
address of the host in place of `0.0.0.0`. A stack rendered before the SSH
ports took the bind address still publishes them everywhere; `qemu-tool
compose up` warns about one, and re-rendering it is the fix.

## Addressing

Every service has a fixed address on the `fleet` network —
`<fleet-prefix>.<family>.<n>`, family `0` infra, `1` ernic, `2` rocjitsu,
`3` qemu — and **nothing in the stack resolves a service name at run time**.
Workers dial the manager's address, Prometheus scrapes addresses.

That is a scaling fix, not tidiness; see the ramp below.

Sockets (`/run/vfu/{ernic,rocjitsu,sdxi}-<n>.sock`) are derived only, with no
override — they are internal wiring between services generated together, so an
override could only desynchronise them. Each guest is passed its own
explicitly rather than globbing the directory, which would attach every
device to every VM.

## `ernic-1` does not restart

Deliberate, and the one thing worth knowing before reading the logs. If the
mesh manager restarts, every guest driver enters a DSR initialization timeout
on the next `modprobe ionic_rdma`, and the vfio-user sessions QEMU holds are
stale — reloading guest modules does not recover them. The only fix is
`docker compose down && up` for the whole stack. `restart: on-failure` would
turn that into a quiet wedge instead of a visible crash. See AGENTS.md issue 7.

Every other service is `restart: "no"` as well. Docker applies `on-failure`
when its daemon starts, so a stack left up over a host reboot used to come
back as device servers with no manager and no guests. A server that exits
stays down; bring the stack back with `down` and `up`.

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

Three kinds of service are exempt, each for its own reason:

| Service | Why |
|---|---|
| `tapsetup-N` | Cannot. Docker sets no **ambient** capabilities, so a non-root uid holds no effective `NET_ADMIN` and `ip link add` fails with `RTNETLINK answers: Operation not permitted`. Compose cannot express ambient caps. It is short-lived, holds `NET_ADMIN` and nothing else (`cap_drop: [ALL]` first, then that one back — it used to run with Docker's full default set *plus* `NET_ADMIN`), and runs read-only. |
| `alloy` | Mounts the host Docker socket, which is root's, to discover containers and tail their logs. All capabilities dropped. |
| `*-stats-exporter` | Pinned to `65534`. They create nothing on the host, so there is no ownership to fix and `nobody` is the stronger position. |

A uid is not a capability set — a container can be uid 1000 and still hold
`NET_RAW` — so `cap_drop: [ALL]` and `no-new-privileges:true` are on
everything, independently of who it runs as.

**Migrating a fleet that ran as root.** `qemu-tool compose up` creates
`<prom-data-dir>`, `<loki-data-dir>` and `<grafana-data-dir>` as you, when the
fleet XML it is given sets them, and names any
file it finds that an earlier root run left behind. The one it cannot fix is a
`<vm>-nvmeN.qcow2`: 0644 root-owned, and QEMU opens a drive read-write, so the
guest will not boot at all rather than merely owning the wrong file. Delete
them — they are scratch.

## Alloy holds the Docker socket

`:ro` on a socket mount restricts the *inode*, not the API behind it. Anything
that can `connect()` to `/var/run/docker.sock` can create a privileged
container and own the host, whatever flags the mount carries — so the
`- /var/run/docker.sock:/var/run/docker.sock:ro` on `alloy` is a
root-equivalent credential, held by the one process in the stack that parses
untrusted container log content.

That is accepted here: this is a lab stack, and Alloy needs the socket for
container discovery and log tailing. The service runs as root with
`cap_drop: [ALL]` and `no-new-privileges:true`, and exists only with
`--profile metrics` and `<logs-enable>`. A read-only API proxy used to sit in
front of the socket; it was removed because its access log flooded
`quick-start logs`. Leave `<logs-enable>` off on a host where a
root-equivalent log collector is not acceptable.

## Guest Ethernet (`<ernic-tap>`)

On in every shipped fleet file; the element's own default is off. With it on
the guests can talk IP to each other; with it off they cannot, at all.

**rocm-ernic's TCP mesh carries RDMA payload only.** The emulated NIC has no
Ethernet datapath unless it is attached to a TAP (`-T`). Without one the guest
driver transmits happily — `ethtool -S` counts the packets — the device drops
them, `ernic_ip_bytes_tx_total` never moves, ARP goes unanswered and ping gets
100% loss. Since `ib_send_bw` exchanges queue-pair parameters out of band over
TCP, that also means **no inter-node RDMA test can run at all**.

With `<ernic-tap>true` each ernic gets `tap0`, and a `tapsetup-<n>` sidecar
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

Each ernic is given `ERNIC_TCP_GUEST_GIDS` — an environment variable of the
server container, not a fleet setting — naming the GIDs its guest owns:
the EUI-64 link-local derived from the fleet MAC, and the guest IPv4. The mesh
resolves a destination GID to a node by asking each node what it owns, so this
is inherently per-node — it cannot be set once in the shared anchor. It is
derived from the MAC rather than restated in the fleet XML, because the
MAC already says it.

Getting this wrong is not a startup error. The mesh routes the payload to
whichever node answers, the receiving node has no matching queue pair, and the
transfer fails with `Completion with error at client` — a long way from the
cause.

## Metrics

Opt-in with `--profile metrics`. The sources:

- **Guest exporters**, scraped at the qemu container's fleet address. The
  guests are behind QEMU SLIRP inside their containers and have no address on
  this network, so the *container* is the target and a `<vm-extra-hostfwd>`
  rule forwards the port through to the guest. Prometheus is given two
  jobs for them — `fleet-node` for node-exporter (`:9100`) and
  `rdma-exporter` (`:9879`) — and `rocm-golden.xml` forwards both; a fleet
  that forwards fewer shows the rest as down. Those are the two exporters
  the guest image runs. The GPU's presence comes through the first:
  each guest writes `amdgpu_kfd_present` and `amdgpu_devices_total` for
  node-exporter's textfile collector, and the home dashboard's "GPU Up"
  counts guests with a device bound.
  The SDXI device's counters come the same way: `sdxi_up` and
  `sdxi_<counter>_total{context="N"}`, written every 15 s, which the home
  dashboard's "SDXI" row reads. Client work is counted in context 1.
  What the optional tests of `quick-start test` measured comes the
  same way, written once by the test and not by a timer:
  `quickstart_p4_fp8_bytes_per_second`, `quickstart_p4_fp8_operation_seconds`,
  `quickstart_p4_fp8_elements`, `quickstart_p4_fp8_iterations` and
  `quickstart_p4_fp8_timestamp_seconds` after `--p4-test`, on the first
  guest, which the home dashboard's "P4" row reads, and
  `quickstart_sdxi_copy_bytes` and `quickstart_sdxi_copy_timestamp_seconds`
  after `--sdxi-test`, on every guest, and
  `quickstart_hipfile_read_bytes_per_second`,
  `quickstart_hipfile_read_bytes`, `quickstart_hipfile_timestamp_seconds`
  and, on a guest whose image has the fio with the `libhipfile` engine,
  `quickstart_hipfile_fio_bytes_per_second` after `--hipfile-test`, on
  every guest, which the "hipFile" row reads. A guest no test has passed
  on has none of them, and the tiles say "no test run" ("no fio result"
  for fio); a test that fails
  writes nothing, so the tiles keep the last pass.
  **There is no amd-metrics-exporter (`:9488`) and no hsa-snoop (`:9490`)
  job any more.** Both used to be rendered and forwarded, and both were
  down: measured on two guests of `20261117.g80c17f8` after a passing
  `quick-start test`, `up` was 0 for `amd-gpu` and `hsa-snoop` and 1 for
  `fleet-node`, `rdma-exporter` and `ernic-stats`. Neither can run against
  an emulated GPU. `gpuagent` assertion-fails on a gfx1250 that answers
  amdsmi with "not supported" and wedges `/dev/kfd`, so the image does not
  install it; hsa-snoop's collector deadlocks GPU dispatch on a vfio-user
  device in its default mode and holds `trace_pipe`, so the image disables
  it. Each container has its own netns, so every VM uses the same ports with
  no arithmetic and nothing published to the host.
- **ernic stats**, from the dumps each `rocm-ernic -S` writes to the shared
  `ernic-stats` tmpfs about once a second: per-QP bytes, doorbells, CQEs, WQEs
  by opcode, MMIO and interrupt counts. `qemu-tool ernic-stats` parses them;
  its metric and label names match rocm-ernic's own exporter, so that project's
  `grafana/ernic-dashboard.json` works here unmodified.

`scripts/check-guest-exporters.py` asks Prometheus for exactly these: for
every guest and every TCP port the fleet file forwards it wants a target
that is up, and it names the ones that are not. The daily benchmark lane
runs it after `quick-start test`.

### Grafana

Rides the same `metrics` profile — Grafana without Prometheus is an empty
page, so it does not get a third switch. `:3000`, anonymous admin, dark,
and bound to `<publish-bind-addr>`: loopback by default, because whoever can
reach the port is an admin.

Datasource and dashboard are provisioned from `grafana/` rather than clicked
in: a dashboard that exists only in Grafana's own sqlite dies with the
container, which defeats the point of a stack that is reproducible from one
fleet file. The dashboard directory is mounted read-only and `allowUiUpdates` is
off, because a UI that accepts an edit it then silently fails to persist is
worse than one that refuses it. Edit the JSON and `docker compose restart
grafana`.

Three shape rules in the dashboard are worth keeping if you extend it:

- **Per-VM panels are bar gauges, not one line per VM.** A line per VM stops
  being readable long before this stack's ~40-node ceiling, and a palette that
  cycles hues past eight makes two VMs the same colour.
- **No panel has two y-axes.** Where two measures do not share a unit they get
  two panels.
- **Every band fills the page.** Under each row heading the panels sit in
  bands: one `y`, one height, and widths that run from column 0 to 24 with
  no gap; each band starts where the one above ends. Grafana does not
  refuse an overlap, it pushes panels down until there is none, so a file
  that breaks this draws as something other than what it says.
  `tests/test_quickstart_dashboard.py` fails on an overlap, a short band, a
  gap, a panel above its row and a duplicate or missing `id`, for
  `fleet.json` and `quickstart.json` in every stack.

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
something of that name, so a relative `<prom-data-dir>` cannot make a TSDB look
like an asset.

That holds at any depth, not just at the top. A relative bind mount nested
under an owned name — `<loki-data-dir>./logging/data`, Loki's data next to
`logging/loki-config.yml` — is the operator's too, and is skipped by the
prune. Absolute paths are still the right thing to put in the three
`<*-data-dir>` elements.

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

`docker-compose.yml` carries an `# xml-sha256:` header, the digest of the
fleet XML layers it was generated from. `qemu-tool compose --xml ...`
recomputes it from the layers it is given and warns on stderr when they
disagree, naming the regeneration command. A stack driven by its directory
with no `--xml` has nothing to compare against and is not checked. It never blocks —
`down` against a stale tree is how you recover from a bad edit.

## Ceilings

### Static limits

| Limit | Value |
|---|---|
| **mesh registration, measured** | **~40 nodes — the real ceiling** |
| rocm-ernic mesh topology payload | 64 nodes (`rdma_backend_tcp.c:2570`) |
| `<vm-shm-size>` × `<vm-count>` ≤ `/dev/shm` | ~30 VMs at 8g on a 252G host |
| `<vm-vcpus>` × `<vm-count>` ≤ `nproc` | 32 VMs at 4 vCPU on 128 cores |

The vCPU row is the one that actually binds; see *Measured* below. It tolerates
about 25% of overcommit and then the fleet starts losing guests.

`<vm-vcpus>` must not drop below 4 on an ernic stack: `ionic_lif_size()` derives
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
the fixes above and 50 after them — so churn, not `<vm-count>`, is what consumes
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

Every ernic service therefore gets `ulimits.nofile` from `<ernic-nofile>`
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
where N is the *registered* size, not `<vm-count>` — 42 sockets measured on both
the 40-VM and the 64-VM fleet. Registering node N+1 therefore costs N new
connections and touches every existing node, through a single-threaded manager,
against a fixed wall-clock registration timeout. The cost per registration
climbs with the mesh until registration stops completing.

The 64-node topology cap never came into it: **max node id reached 30**. Plan
around ~40, expect it to move with host speed, and note two consequences:

- **A stuck worker retried forever; its guest got one chance.** When this was
  measured `ernic` was `restart: on-failure` and `qemu` `restart: "no"`. By
  the time a worker on its fifteenth attempt registered, the VM it existed to
  serve had been dead for ten minutes. The 24 stuck workers matched the 24
  dead guests exactly. Workers are `restart: "no"` now: one that fails to
  register exits and stays down.
- **Failed registrations leak manager fds too, and faster than evictions do.**
  Measured over a six-hour run parked above the wall: 8006 worker restarts and
  50 fds/min at the manager, reaching 18164 open descriptors. That is Docker's
  1024 default in 20 minutes and an `<ernic-nofile>` of 65536 in about 22 hours — a
  working day, not an indefinite reprieve. Size the fleet below the wall; no
  fd limit makes sitting above it survivable.
