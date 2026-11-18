# Agent Context for qemu-minimal

Practical facts for AI agents (and humans) working in this repo. Keep this
file up to date as infrastructure changes.

---

## Repository layout

- `qemu/` — qemu-tool Python package (installed as a Debian package or via pipx); the sources are in `qemu/qemu-tool/`, imported as `qemu_tool`
- `qemu/stacks/` — the fleet XML settings files, and the only settings files in the repo: `rocm-golden.xml`, `helios-tray.xml`, `mi355x-server.xml`. It is a sibling of `qemu/compose/` and `qemu/packages.d/` and is mapped in place by `package-dir`, for the same reason they are: these are data the wheel ships, not Python source, so they do not belong under `qemu/qemu-tool/`
- `qemu/compose/` — exactly three stacks, each **generated** by `qemu-tool gen-compose` from the fleet file of the same name; do not hand-edit one. `--profile metrics` adds Prometheus+Grafana to any of them
  - `rocm-golden/` — the default stack: two guests from the published rocm-golden qcow2, each with a GPU, an NVMe, an ernic with P4 in guest scope and an SDXI device. The stack `qemu-tool compose` runs when given no `--stack`, and the one `quick-start` renders
  - `helios-tray/` — one VM with 4 GPUs, 8 ernics and an SDXI device; the stack behind `quick-start --flavor helios`
  - `mi355x-server/` — MI355X-shaped PCIe topology: one EPYC socket, 8 GPUs, 8 NICs and 8 PCIe switches per VM, one NIC + one GPU below each switch. 8 ernics per VM means 8 *mesh nodes* per VM, so the fleet caps at 4 VMs, not 40 — see its README. Nothing in CI boots it: `Gen Compose Check` renders it and checks the switch topology, and `quick-start --flavor mi355x` is the way to run it
- `qemu/qemu-tool/settings.py` — the one loader: fleet XML layers in, a `FleetConfig` (or `VMConfig` overrides) out. See "Settings" below
- `qemu/qemu-tool/quick_start.py` + `ansible/playbooks/vm-quickstart.yml` — the README's Quick Start, which is `qemu-tool quick-start`: two guests from the published qcow2, Prometheus, Grafana and log collection beside them, plus a GPU test and an inter-guest RDMA test. It runs `qemu/stacks/rocm-golden.xml` unless given `--xml` or `--flavor`; `--flavor default` is that fleet. The daily `report-for-vm-rocm-golden` lane runs *that subcommand*, not a copy of its steps. It replaced the shell script that used to live under `scripts/`, which is deleted; do not reintroduce a shell wrapper beside it. The guests are sized from the host by default (4 vCPU floor, 8 ceiling), and every prompt it asks takes its default answer when stdin is not a terminal, so CI needs no flag to stay non-interactive
- `images/` — qcow2 VM disk images (backing + overlay pairs, **not** `/var/lib/qemu-tool/images`)
- `ansible/playbooks/` — three playbooks and six roles, and nothing else:
  - `vm-basic.yml` — a complete bake on its own, and the base of the golden
  - `vm-rocm-golden.yml` — the one image beyond it: imports `vm-basic.yml`, then the mainline kernel and ernic prerequisites, ROCm, the rocjitsu roles, SRQ + P4, SDXI and one slim pass. Every bake play is tagged `install`; `tests/test_playbook_pins.py` fails a play with no tag, because an untagged play runs in every bake. Three plays need a device and are tagged `never`: `--tags configure` sets up the NIC and builds no driver (known issue 16 says why it must not), `--tags firmware` installs `ip_discovery.bin` from the rocjitsu serving the guest, and `--tags test` runs the GPU workloads. The pins (`ionic_kernel_pin` with `ionic_kernel_pin_commit` and `ionic_kernel_mirror`, `ernic_source_pin`, `rocm_line_pin`, `sdxi_source_pin`) are each defined once and shared between plays through YAML anchors; see "Where the ionic kernel sources come from" for the first three. Its defaults are a published image's, and a bake needs no Docker daemon when given `-e rocjitsu_firmware_script=<path to vfio_guest_firmware.py>`. Given a cache directory (`gen-vm --bake --cache-dir DIR`, or `<bake-cache-dir>` in a fleet file, which reaches the playbook as the extra var `bake_cache_dir`) the module builds go through a compiler cache kept in that directory; there is none by default, and the two workflows that bake the guest, the playbook-test lane and the publish workflow, each pass one and share what it holds — see "The bake's compiler cache". The slim play also purges every kernel but the running one, after asserting that is the pinned one — see "One kernel in the baked image". The acceptance test is `qemu/checks/rocm-golden.sh`, which the playbook-test lane runs against the baked guest and batesste-ci-images runs against the published one
  - `vm-quickstart.yml` — configures and tests a running fleet; what `qemu-tool quick-start test` runs. Three of its plays are optional and off by default: the SDXI test (`--sdxi-test`), the P4 fp8 test (`--p4-test`) and the GPU-memory RDMA test, which has no switch
  - roles `rocm_ernic_setup` (image prep, device naming, boot-time addressing, the P4 client and who may open `/dev/ernic-p4`, the mirror and commit check for the ionic kernel sources, and `driver_check`, which asks a guest whether its ionic driver is still the baked SRQ + P4 build), `rocjitsu_setup` (firmware, amdgpu, tools, live tests), `sdxi_setup`, `image_slim` and `bake_ccache` (the compiler cache: `restore`, `save` and `remove`, no `main`). Each has task files callers reach with `tasks_from`; all five are linted at the `production` profile in CI
  - role `perftest_setup` — perftest built from a pinned commit with ROCm memory support, installed in `/usr/local/bin` ahead of the archive's package; linted with the other five. See "perftest with ROCm memory support"
- `scripts/install-vfio-rules` + `scripts/99-qemu-minimal-vfio.rules` — host-side udev rules for PCI passthrough. There is no top-level `udev/` any more: guest rules live in their role's `files/`
- `rocm-ernic-enablement.md` — running log of rocm-ernic integration status and bugs. Git-ignored: it exists only on a host that keeps one

## VM images

All qcow2 images on this development host live at **`<repo-root>/images/`**
(i.e. `/home/stebates/Projects/qemu-minimal/images/`), not at the default
`/var/lib/qemu-tool/images`. That is a per-host fact, so it is never in a
committed file: pass `--images`, or set `<vm-images-dir>` in a local fleet XML
(`qemu/stacks/fleet.xml` is on the search path and git-ignored).

A local two-guest ernic pair, both on one backing file:

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

SSH access (when a stack is running; `<ssh-port-base>` is 2222 and guest *n*
gets the next port up):

```
ssh -p 2222 stebates@localhost   # VM1
ssh -p 2223 stebates@localhost   # VM2
```

Those two images predate it, but **`gen-vm` no longer authorises
`~/.ssh/id_rsa.pub` by default.** `VMConfig.ssh_key_file` is `None`, and what
happens then depends on what the run creates:

| Guest | Key | Sidecar `ssh_key_generated` |
|---|---|---|
| baked from the cloud image, no key named | generated by `gen_vm.image_key()` into `<images>/keys/<vm-name>_id_rsa` | `true` |
| baked with `--ssh-key-file` or `<vm-ssh-key-file>` | the named public key | `false` |
| overlay on `--backing-file` | the key recorded in the backing image's sidecar | `false` |
| overlay on a pulled `--backing-image` | the pulled keypair, `<images>/keys/id_rsa` | `false` |

`<images>/keys/` is the one place image keys live; `inspect` globs it. A
re-bake under the same name deletes the old generated pair and makes a new
one, because the image it opened has just been overwritten. `--ansible-only`
generates nothing and logs in with the key its own sidecar records.
`qemu-tool ssh <vm>` resolves all four, and its fallback when the guest
agent cannot be asked is the sidecar, then a `.pub` beside the image, then a
key a settings layer names — never a default under `~/.ssh`. `inspect` still
globs `~/.ssh/*.pub` when matching the keys a guest *reports* accepting: that
reads public halves to find the private key of something the guest already
authorises, and decides nothing about what gets authorised or published.

`ssh_key_generated` is `true` only on the image that run baked, never on an
overlay, because it is what licenses `publish-qcow` to publish a key without
being told which. Do not "fix" an overlay to inherit it. The sidecar also
carries `ssh_key_fingerprint`, and a cloud-image bake deletes the sidecar
before it touches the backing file: the record is written only on success,
so one left over from an earlier bake would describe an image that no
longer exists. A re-bake with a named key deletes the earlier generated
pair for the same reason.

**`gen-vm` tells the playbook which key the image has.**
`sbates130272.batesste.user_setup` authorises
`{{ user_setup_ssh_key_src_dir }}/{{ user_setup_ssh_key_name }}.pub` read from
the controller, unconditionally, and both default to `~/.ssh/id_rsa`. Left
alone that put the caller's own key into every `vm-basic.yml` and
`vm-rocm-golden.yml` bake beside the generated one, and failed the bake on
a host with no `~/.ssh/id_rsa.pub`. `gen_vm._key_extra_vars()` passes both
as `-e`, ahead of `ANSIBLE_EXTRA_ARGS` so a caller can still override them.
`vm-basic.yml` on its own also leaves `user_setup_deploy_ssh_key` on, which
copies the private half into the guest; that is now the image's key rather
than yours. A bare `ansible-playbook` run of those playbooks, with no
`gen-vm` in front of it, still gets the role's `~/.ssh` default.

Most CI lanes that bake and then use plain `ssh` pass
`--ssh-key-file "$HOME/.ssh/id_rsa.pub"`, the key the job generated. The
publish workflow does not: it bakes with no key and logs in with
`-i $VM_IMAGES_DIR/keys/rocm-golden_id_rsa`, so it is the lane that runs the
default end to end. The arm64 and riscv64 smoke lanes take the default too
but never log in.

Password: see cloud-init user-data (plain_text_passwd field). GPG key in VM
is not unlocked (no private key injected), so git-crypt and signed commits
are not available inside the VM.

## Settings

**Fleet XML is the only settings format.** A file is one `<fleet>` element in
the namespace `http://qemu-tool/fleet/1.0` with flat children, one per
setting; `qemu/qemu-tool/fleet_xml.py` parses it into a `FleetConfig`.
`--xml` is repeatable on every subcommand that reads settings and later files
win element by element.

**The parser is strict.** An element it does not know, an element repeated in
one file, an attribute, a nested element and text directly under `<fleet>`
are errors naming the file and the element (and the closest known name, for
an unknown one); comments are fine. It used to ignore all of these, so a typo
was an override that silently never applied. The format lives in one table,
`_SETTINGS` in `fleet_xml.py`: parse, `emit()` and `KNOWN_ELEMENTS` all read
it, so a new setting is one row there plus its `FleetConfig` field, and
`tests/test_fleet_xml.py` fails if the two disagree. `tests/test_fleet_files.py`
parses every `qemu/stacks/*.xml` and every `<fleet>` document a workflow
under `.github/` writes inline (heredoc or `printf`), with shell expansions
replaced by `1`, so a typo in either fails `Gen Compose Check` without
booting anything.

Name a setting by its element (`<vm-images-dir>`), not
by an upper-case key: the upper-case keys were the env-format files', and
those are gone.

What no longer exists, so do not reach for it or reintroduce it:

- **No env-format settings file is read, anywhere.** No `KEY=value` parser,
  no `envfile.py`, no `$QEMU_TOOL_ENV`, no `./.env`, `qemu/.env`,
  `~/.config/qemu-tool/env` or `/etc/qemu-tool/env`. A `.env` handed to
  `--xml` is an XML parse error.
- **No XML-to-env detour.** The generator and `gen-vm`/`run-vm` read the
  `FleetConfig` directly; `gen_compose._fleet_to_env` and the `_DEFAULTS`
  table are deleted, and `FleetConfig`'s field defaults are the defaults.
- **No `fleet-to-env` subcommand**, and qemu-tool writes no temporary env file
  and passes `docker compose` no `--env-file`.

`qemu/qemu-tool/settings.py` is the one loader. `settings.find()` resolves the
layers, `load_fleet()` merges them into a `FleetConfig`, `vm_overrides()` maps
that onto `VMConfig` fields for `gen-vm`/`run-vm`/`inspect`/`ssh`/`qcow`, and
`bake_overrides()` does the same for `gen-vm --bake`.

With no `--xml`, discovery is `FleetXml.find()` and nothing else — the first
that exists, used alone: `$QEMU_TOOL_FLEET`, `./fleet.xml`,
`qemu/stacks/fleet.xml` in a checkout (git-ignored), `$XDG_CONFIG_HOME/qemu-tool/fleet.xml`,
`/etc/qemu-tool/fleet.xml`. With none,
dataclass defaults apply. Layering is only ever something typed on the command
line; the search path resolves to one file. Four commands differ:
`gen-compose` and `compose --stack NAME` fall back to the stack's own file in
`qemu/stacks/`, `quick-start` does not search and uses its flavor's file, and
`publish-qcow` reads a settings file only when `--xml` names one.

**The `.deb` does not seed `/etc/qemu-tool/fleet.xml`, and must not.** A file
there is the settings of every command given no `--xml`, and the only
candidate to copy is `rocm-golden.xml`, which is a whole fleet: a bare `gen-vm`
would take its `<backing-ref>` and pull instead of baking, with its password,
and a bare `run-vm` its NVMe, `file.locking=off` and four fixed hostfwd rules,
so a second guest on the host cannot bind them. An empty `<fleet/>` is no
better: `gen-compose` would find it and render from it rather than from the
stack's own file. The postinst creates the directory and nothing in it;
`tests/test_settings.py` pins that.

**`<vm-cpu-sockets>` and `<vm-cpu-phys-bits>` are integers and `~` is expanded
at parse time**, so `qemu-tool check` is the one validator and every reader —
`settings.vm_overrides`, `compose.interpolation_vars`, `check` — sees the same
path. Do not add a per-reader `expanduser()` or a late `int()`.

Environment variables that name a *path* rather than a setting stay:
`$QEMU_TOOL_FLEET`, `$QEMU_TOOL_STACK`, and the `XDG_*` directories.

**How `docker compose` gets its variables.** The irreducible remainder is that
compose itself interpolates `${VAR}` in a compose file. The three generated
stacks interpolate exactly four names, each because it differs per host and so
cannot be committed: `CONTAINER_USER`, `CONTAINER_KVM_GID`, `VM_IMAGES_DIR`
and `QEMU_TOOL_SRC`. qemu-tool supplies them through the **child process
environment** of the `docker compose` it starts (`subprocess` `env=`):

- `compose.interpolation_vars(fleet)` is the one mapping from `FleetConfig` to
  those names — `VM_IMAGES_DIR` from `<vm-images-dir>`, `QEMU_TOOL_SRC` from
  `<qemu-tool-src>`. It is docker compose's interface, not a settings format.
  `tests/test_compose_interpolation.py` holds it to the names the shipped
  stacks really interpolate, so adding a name no stack reads fails a test.
- `compose.interpolation_env(fleet, env)` fills in only a name the caller's
  environment does not already carry. That is compose's own precedence — the
  process environment wins — and present-but-empty counts as set, as it does
  to compose (checked on v5.2.0). `--images` is written into that environment
  first, so it outranks `<vm-images-dir>`.
- With settings in force it also sets `COMPOSE_DISABLE_ENV_FILE=1` in the
  child, so a stray `<stack dir>/.env` is not consulted as well.
- `compose._apply_identity(env)` answers `CONTAINER_USER`/`CONTAINER_KVM_GID`
  the same way; see "Container privilege". Fleet XML has no element for
  either, on purpose.

`compose.run`, `quick_start._compose` and `down_all._teardown_env` all go
through these, so the three cannot disagree. Those four are the only
upper-case names a reader types, and only for plain `docker compose`.

## The stacks, and `qemu/stacks/rocm-golden.xml`

There are three fleet files and three stacks, and the names match:
`qemu/stacks/<name>.xml` renders to `qemu/compose/<name>/` for `rocm-golden`,
`helios-tray` and `mi355x-server`. `rocm-golden` is the default stack: the name
is the fleet's, and "default" is only what it is when nothing else is named. `gen_compose.stack_for()` reads the stack
off the first `--xml` file's basename, so regenerating a committed stack
takes neither `--stack` nor `--output-dir`:

```bash
qemu-tool gen-compose --xml qemu/stacks/rocm-golden.xml
qemu-tool gen-compose --xml qemu/stacks/helios-tray.xml
qemu-tool gen-compose --xml qemu/stacks/mi355x-server.xml
qemu-tool gen-compose --check          # no --xml: the default stack against its own file
```

The hand-written stacks (`vfio-user-ernic-vm`, `-rocjitsu-vm`,
`-ernic-rocjitsu-vm`, `-ernic-2vm`) are deleted, and with them every
variable they alone interpolated and `compose --vm-name`/`--vm2-name`. The
old generated names map `vfio-user-ernic-rocjitsu-scale-out` → `rocm-golden`,
`vfio-user-mi355x-node` → `mi355x-server`, `vfio-user-helios-tray` →
`helios-tray`. One guest with one device of each kind is no longer a stack of
its own: it is `rocm-golden.xml` plus an overlay setting `<vm-count>1`, which
is what the CI lanes that boot a single guest write inline. `gen-compose`
requires `<ernic-per-vm>` and `<rocjitsu-per-vm>` of at least 1, so a NIC-only
or GPU-only guest is not expressible; `<sdxi-per-vm>0`, `<p4-enable>false`,
`<ernic-tap>false` and `<logs-enable>false` all are.

**`rocm-golden.xml` has four jobs, and an edit to it lands on all four:**

1. **The default stack.** `qemu/compose/rocm-golden/` is generated from it, and
   it is what `qemu-tool compose` runs with no `--stack`.
2. **What `quick-start` runs** with no `--xml` and the default flavor, and so
   what the daily benchmark lane and `quickstart-boot` run. Its promises to a
   new user are part of the file: guests at the 4 vCPU floor (`quick-start`
   resizes them for the host), Grafana and logs on, the two guest-exporter
   `<vm-extra-hostfwd>` rules the home dashboard needs (node-exporter on
   9100, rdma-exporter on 9879; see "Guest exporters" below), TAP on, and
   `<prom-data-dir>`/`<grafana-data-dir>` deliberately unset so `quick-start`
   puts them beside the stack with no root and no chown.
3. **The template** for a fleet of your own: copy it, or layer an overlay on
   it. It replaced `fleet-example.xml`.
4. **The bake and publish settings of the rocm-golden image**, in its
   `<bake-*>` and `<publish-*>` elements. Only `gen-vm --bake` and
   `publish-qcow --xml` read them; see "The published guest image".

Its guests are named `rocm-golden-N`. It absorbed `quickstart.xml`, whose
guests were `quickstart-N`; the quick-start flavor is still called `default`
and its state directories (`/var/lib/qemu-tool/stacks/quickstart`,
`quickstart-key`) kept their names.

**Guest exporters: a forward, a scrape job, a panel and a listener, or none
of them.** Prometheus reaches an exporter inside a guest at the address of
the guest's qemu container, through a `<vm-extra-hostfwd>` rule, and four
places have to agree for that to show anything: the fleet file's forward,
the scrape job `gen_compose.render_prometheus` renders, the panels of
`grafana/dashboards/fleet/quickstart.json` (three identical copies, one per
stack), and what the guest image has listening. They did not.
`rocm-golden.xml` forwarded four ports, the generator rendered four jobs for
every stack and the home dashboard had panels for all four, while the image
runs two. Measured on `20261117.g80c17f8` after a successful
`quick-start test`: `up` was 0 for `amd-gpu` (`:9488`) and `hsa-snoop`
(`:9490`) and 1 for `fleet-node` and `rdma-exporter`.

Neither of the two can run in a generated stack, where every GPU is
rocjitsu's, so they were removed rather than started:

| Exporter | Why it cannot run against an emulated gfx1250 | Where that is recorded |
|---|---|---|
| amd-metrics-exporter (`gpuagent`) | assertion-fails where amdsmi answers `AMDSMI_STATUS_NOT_SUPPORTED`, and leaves `/dev/kfd` busy for the next process | `rocjitsu_setup/tasks/tools.yml`; commit `791e5e3`. The bake does not even install it: `rocm_setup_install_metrics_exporter: false` |
| hsa-snoop's collector (`hsa-snoop-prometheus.service`) | its default mode reads each queue's ring with `process_vm_readv`, which "deadlocks GPU dispatch" on "emulated GPU devices (rocjitsu/vfio-user)"; its `kfd-uprobe` mode was tried here and "captures nothing useful"; and it holds `trace_pipe`, which admits one reader, so the role's own `hsa-snoop -- <app>` test dies while it runs | hsa-snoop v1.3.0 `systemd/hsa-snoop.conf`; commit `7897d96`; `rocjitsu_setup/defaults/main.yml` |

The packaged unit also listens on 9488, not 9490: 9490 came from an
`/etc/default/hsa-snoop` the quickstart playbook wrote until `7897d96` and
nothing has written since. So the generator renders `fleet-node`,
`rdma-exporter` and `ernic-stats` and no other job, `rocm-golden.xml`
forwards 9100 and 9879, and the dashboard's amd-metrics-exporter row and
hsa-snoop panels are gone. Its "GPU Up" stat now counts guests with
`amdgpu_devices_total >= 1`, which each guest writes for node-exporter's
textfile collector (`amdgpu-textfile-metrics.sh`) and which therefore
arrives through `fleet-node`; its log panel reads `{job="docker"}`, the
stack's containers, where it used to read a file inside a guest that Alloy
on the host never sees. Bringing either exporter back is four edits and
`tests/test_guest_exporters.py` fails on three of them done alone: a job no
port is forwarded for, a forward with no job, a forward for a port the
image does not listen on, and a panel on a job the stack does not scrape.

**All three shipped fleets forward both ports.** The generator renders the
same guest jobs for every fleet whatever it forwards, and `helios-tray.xml`
and `mi355x-server.xml` forwarded 9100 alone, so their `rdma-exporter`
targets were down by construction, on a port the image does listen on.
Both now carry `tcp::9879-:9879`, and the test holds every shipped stack to
forwards equal to scraped ports, not only the default one. It also names
the unit behind each port (`prometheus-node-exporter`, `rdma_exporter`) and
requires that unit's own task to enable it. Not run: that `rdma_exporter`
answers in a guest with eight NICs. It was seen up only on the one-NIC
default fleet.

**The daily lane now asks for the targets by name.**
`scripts/check-guest-exporters.py` reads the answer to the `up` query and
the fleet file, and for every guest and every TCP port the fleet forwards
requires a target at `<qemu container address>:<port>` with `up == 1`; a
forwarded port no job scrapes fails as well. It prints each target and
names the ones that are not up, and `report-for-vm-rocm-golden` runs it
after `quick-start test`, retrying for two minutes because a target is not
up until it has been scraped once. The step it sits beside only asked
whether Prometheus answered a query, which it did throughout. Not run
against a live stack: the script was run against recorded answers, including
the four-forward state the published fleet file had.

**Every image a fleet names is pulled.** `<sdxi-image>` is a published
`batesste-ci-images-ubuntu-rocm-sdxi` tag.

**The three device-side images share one build stamp. The guest and the
SDXI server are each tied to their pair by commit instead.** `<qemu-ref>`,
`<ernic-ref>` and `<rocjitsu-ref>` must carry one `<date>.<ci-sha>`, the
stamp of one batesste-ci-images build; `_check_image_coherence` refuses a mix
at render time, and a `vfu.<sha>` that differs between QEMU and the ernic.
`<sdxi-image>` is exempt from that comparison
and tied to the guest by commit instead: the `sdxi.<sha7>` in its tag must
equal the first seven characters of `sdxi_source_pin` in `vm-rocm-golden.yml`,
because a guest driver newer than its server waits for a completion MSI-X
that never comes. `tests/test_playbook_pins.py` holds that check —
`gen-compose` cannot, since an installed package does not see the playbook —
and the `Image Tag Consistency` workflow leaves `-sdxi.<sha>` refs out of its
stamp count while requiring them all to name one tag. Moving the SDXI pair is
therefore two edits, the pin and the `<sdxi-image>` in `rocm-golden.xml` and
`helios-tray.xml`, and needs a published server image built from that commit;
it does not need the rest of the fleet to move stamps. `gen-compose` still
refuses a non-zero `<sdxi-per-vm>` with an empty `<sdxi-image>`.

**`<backing-ref>` is the guest this repo publishes, and it is not on the
servers' stamp.** All three fleet files name
`docker.io/sbates130272/qemu-tool-rocm-golden:20261117.g481c892`, a dated tag
of `<publish-ref>`. That stamp is the day of the publish and the qemu-minimal
commit the guest was baked from; it is not a batesste-ci-images build and
never equals one, so the old rule — four refs, one stamp — would refuse
every fleet that pulls the guest this repo makes. Until 2026-11-17 the three
files named a batesste-ci-images qcow2
(`...-qcow2-gen-ernic-rocjitsu:20261112.gdca6903-...-qm.096ddfe-qcow2`),
which did share the stamp and had no SDXI driver, no hipFile SDK and no P4
client; the daily report lane failed all four benchmark steps against it.
The first image of this repo's own that they named was `20261117.g80c17f8`,
for part of that day; `20261117.g481c892` is the second, and the first with
the bake fixes listed under "What has and has not been seen" below.

What `_check_image_coherence` holds a backing ref to is decided by its tag
(`gen_compose._backing_ref_kind`) first, because `<publish-ref>` can be
anyone's, and by its repository only where a layer says which repository
that is:

| Tag | What it is | Rule |
|---|---|---|
| exactly `<YYYYMMDD>.g<sha7>` | `publish-qcow`'s dated tag | accepted, not compared with the servers' stamp |
| none, `latest`, `qm.<sha7>` | a name `publish-qcow` moves | refused, and `<image-allow-guest-lag>` does not excuse it |
| `name@sha256:...` | a digest | accepted |
| a near miss of the dated tag, `latest` in another case, `qm.` plus anything but seven hex digits | a mistake for one of the above | refused ("does not name a published guest"), not excused either |
| any other tag, in the repository `<publish-ref>` names | not something `publish-qcow` pushes | refused the same way |
| `<stamp>-...` | a batesste-ci-images qcow2 | the old rule: the servers' stamp, unless `<image-allow-guest-lag>` |
| anything else | a local build | warned about and left out, like an unstamped server image |

**The two "malformed" rows exist because a typo used to render.** Before
them only the exact strings `latest` and `qm.<7 hex>` and a missing tag were
refused; `:2026117.g80c17f8` (a digit short), `:LATEST`, `:stable` and
`:qm.80c17f8a` in the publish repository each fell through to the last row
and rendered rc 0 with "image(s) with no build stamp, excluded from the
coherence check" — a mistyped pin became an image nothing checked. The
committed files were never exposed, since `tests/test_image_tag_consistency.py`
holds them; a local overlay and an `--output-dir` render were. The
repository row needs `<publish-ref>`, which only `rocm-golden.xml` sets: an
overlay on `helios-tray.xml` or `mi355x-server.xml` naming `:stable` in the
publish repository is still the last row, because nothing there says whose
repository it is. And `<stamp>-dirty` outside the publish repository is
still a stamped tag held to the servers' stamp; the refusal now adds that a
published guest is named by its dated tag alone.

A moving tag is refused for two reasons, neither of them tidiness. `gen-vm`
and `quick-start` cache the disk and the keypair by the *text* of the ref, so
a tag that moves leaves each host on whichever image it pulled first. And a
moving tag names no commit, which the next check needs. The shape is spelled
once, `VM_DATED_TAG` in `vm_artifact.py`; `publish-qcow-prune` deletes only
that shape and `gen-compose` pins only that shape.

**What ties the pulled guest to the servers is the pins at its bake commit,
and `tests/test_playbook_pins.py` (`PulledGuest`) reads them out of git.**
The `g<sha7>` in the tag is a commit of this repository. The guest's drivers
were built at the `ernic_source_pin` and `sdxi_source_pin`
`vm-rocm-golden.yml` had *at that commit*, so the test runs
`git show <sha>:ansible/playbooks/vm-rocm-golden.yml` and compares those two
pins with the `ernic.<sha>` in `<ernic-ref>` and the `sdxi.<sha>` in
`<sdxi-image>` of each fleet file. For `20261117.g481c892` they are
`90434a2` and `dfd76f2`, which is what the three files serve; neither pin
moved between `80c17f8` and `481c892`. This is the
replacement for the stamp comparison, and it is stricter than the stamp ever
was: the stamp said "built in one run", this says "built from the commit the
server was". It needs the commit, so:

- a shallow checkout skips it, with the commit named in the skip. The
  publish workflow's first step runs the file on a depth-1 checkout and
  skips this one test;
- a full checkout that does not hold the commit fails;
- `Gen Compose Check` checks out with `fetch-depth: 0` for this test alone,
  and is the only lane that runs it on a pull request.

**A pin bump opens a window this test will not let pass silently.** The
change that moves `ernic_source_pin` and `<ernic-ref>` (or the SDXI pair)
leaves the fleet pulling a guest baked before it, and no guest at the new
pins can exist until that change is on main and published, because the
publish workflow never runs from a pull request. So that change also sets
`<image-allow-guest-lag>true` in each fleet file, with a comment saying what
it waits for. `PulledGuest` accepts a lagging guest only with that element,
and fails a fleet file that still sets it once the guest matches, so the
follow-up that moves `<backing-ref>` has to remove it. While it is set every
render warns that the guest may be older than its servers, and during that
window the fleet really is a guest and a server from different commits:
expect `quick-start`, `quickstart-boot` and the daily lane to misbehave
until the follow-up lands.

**Moving `<backing-ref>` is a change of its own**, made after a publish:
the same dated tag in `rocm-golden.xml`, `helios-tray.xml` and
`mi355x-server.xml`, then `qemu-tool gen-compose --xml` for each, since the
ref is in every stack's digest. `tests/test_image_tag_consistency.py`
(`GuestImage`) and the `Image Tag Consistency` workflow fail if the three
disagree, if one names a repository other than `<publish-ref>`, or if one
names `latest`.

**A host that already ran `quick-start` has guest disks on the old image,
and `quick-start` stops rather than boot them.** `make_overlays` leaves an
existing overlay alone, and `fetch_key` re-fetches the keypair when the ref
changes, so a moved `<backing-ref>` used to boot the old disks and offer
them the new image's key: healthy guests, refused logins, reported as "did
not answer SSH". `quick_start.retire_stale_overlays` runs first. It compares
the ref `gen-vm` recorded in `<vm-1>-backing.qcow2.image-ref` with the
fleet's, and on a difference:

| Run | What happens |
|---|---|
| a qemu service of this stack is running | stops, names the services, removes nothing; `quick-start down` first |
| Docker cannot be asked | stops, removes nothing |
| at a terminal | asks, default **no**; a yes replaces the guest disks and their NVMe scratch disks |
| `--yes`, or no terminal | takes the default, so it stops and removes nothing |
| `--clean-overlays` | the answer is already yes: no question, and the disks go where that flag always removed them, after the keypair is fetched |

Every stop is before the keypair is touched, so the old guests are still
reachable. No record means nothing was pulled under that name and nothing
is removed.

**This is the one `quick-start` prompt whose default is no.** It still
takes its default when stdin is not a terminal, like every other; the
default is a stop. The first version defaulted to yes and did not look
for a running stack: an unattended `up` on a host with a live stack from
the old image would have unlinked the overlays and NVMe disks under the
running QEMUs, then fetched the new key — guests still running on deleted
inodes, refusing the new key, and losing everything written to them when
they stopped. No CI lane reaches the prompt: a fresh runner has no
`.image-ref`. A lane on a runner that keeps its images directory would stop
here after a ref move, and wants `--clean-overlays`.

`--clean-overlays` on its own is turned away from running guests the same
way, since it unlinks the same files (`_refuse_under_running_guests`); with
no disks to remove it is not. "Running" is a `qemu-N` service of the compose
project `quick-start` itself uses, the stack directory's name: servers left
up after their guests exited do not count, and a stack started from the same
images directory under another project name is not seen. This host has the
state the check is for: `images/stebates-helios-1-backing.qcow2.image-ref`
names a `20261030` image.

A re-fetch also replaces the copy of the keypair published to
`<images>/keys/quickstart_id_rsa`, which used to be written once and kept.
Plain `gen-vm` with a changed ref re-pulls and removes that one guest's
overlay, but not the overlays of other guests made on the same backing file
with `--backing-file`; delete those by hand.

**What has and has not been seen of `20261117.g481c892`, the image the
fleet files pull.** The publish run on main at `481c892` baked it (476 tasks
ok, none failed), ran `qemu/checks/rocm-golden.sh` against it with no
devices, then booted an overlay of it as the single guest of a rendered
default stack and required an active RDMA port, the GPU test stage and a
copy through `/dev/sdxi0`, and pushed it: manifest `sha256:72a0aa0c1ad4...`,
a 2,075,684,547-byte layer, 8,379,301,888 bytes unpacked by its own
annotation, also tagged `qm.481c892` and `latest`. That gate runs with
`<p4-enable>false`, so the P4 client and `/dev/ernic-p4` were not exercised
by it. It is the first image baked with the fixes for what was found wrong
with `20261117.g80c17f8`, and this is what that one run showed of each:

| Fix | In the bake | In the guest check, on the boot with no device |
|---|---|---|
| snapd purged, so `apparmor.service` starts | `Purge snapd` changed, and the restart of `apparmor.service` after it passed | printed `apparmor: apparmor.service is not failed` |
| this repo's `99-rocm-ernic.rules` | `naming_check` found the installed file equal to the role's | printed `udev: 99-rocm-ernic.rules indexes both devices by PCI position` |
| `amdgpu-probe` refuses with no blob, and the `nofirmware` and `unbound` status words | both probe assertions of `rocjitsu_setup`'s `amdgpu.yml` passed | the scratch-tree assertions passed; they print nothing when they do |
| the `rdma_exporter` record | `Record which rdma_exporter tarball is installed` changed | not asserted; nothing has read the record back |

**The published `20261117.g481c892` artifact has been pulled and booted
by hand, on the dev host, in one session; no CI lane has pulled it yet.** Everything
above is the publish run's, on the bake and on overlays of it before the
push. `quickstart-boot` and the daily report lane have not run against it:
their next runs after the `<backing-ref>` move lands will be the first.
The by-hand boot is recorded below, after the one of `20261117.g80c17f8`
that it is compared with: "`20261117.g481c892`, pulled and booted". It
showed a guest whose `systemctl is-system-running` reads `running`, the
login hint naming the host command, and a `quick-start test` that leaves
the naming rules and `rdma_exporter` alone. Still expected and not seen: an
eight-NIC guest with eight renamed NICs, since only the default stack was
booted.

**What was seen of `20261117.g80c17f8`, the image before it.** The publish
run on main at `80c17f8` baked it and put it through the same gate. CI has
since booted the published artifact twice. `quickstart-boot`, one guest,
passed on the pull request whose fleet files named it (#238; read from the
run list, its log was not). And the daily report lane passed on main at
`481c892`, which still pinned it: the lane's first pass on an image this
repo publishes. Two guests at 4 vCPU and 5993 MiB each on a 4-core GitHub
runner, so oversubscribed two to one: GEMM 484 kFLOP/s, hipFile 45.5 MB/s,
hipFile fio 36.2 MB/s, P4 FP8 80.3 MB/s, SDXI copy 737.7 MB/s, and the
exporter check passed. In that run `vm-quickstart.yml` reported the naming
rules, the `rdma_exporter` download, its install and its record changed on
both guests, which is that image without this repo's rules or a record. None
of those five figures is comparable with the dev host's below: other
hardware, half the vCPUs, and a host with no cores to spare.

**`20261117.g80c17f8` was booted as a two-guest fleet once, by hand, on the
dev host, on 2026-11-17, and every step the daily report lane runs passed.**
That is
one run on one machine, not a lane. How: `qemu-tool quick-start preflight`,
`up`, `test` and `down` from this working tree (`80c17f8` plus the
uncommitted `<backing-ref>` move), with `--xml qemu/stacks/rocm-golden.xml`
and `--stack-dir`, `--key-dir` and `--images` all under one scratch
directory, so the images directory was fresh and the artifact was pulled.
The four benchmark steps were then run as the lane runs them:
`scripts/generate-vm-report.sh` as root in the lane's job image, on the
stack's `fleet` network, with the image's published key.

What differed from the lane, each of which limits what this shows:

- 32 cores and 46 GB, so `quick-start` sized the guests at 8 vCPU and
  8192 MiB each; the lane gets the 4 vCPU floor.
- No `--in-network`; the host is not a job container.
- Windows held the default ports, so guest SSH was 2226/2227 and Grafana
  3003. `quick-start` also said Prometheus would be published on 9093. It
  was not: the stack gives Prometheus no host port, and `quick-start` no
  longer asks about one. See "Prometheus has no host port" below.
- The four server images were already in Docker's cache, so the `up` time
  below understates a cold runner.
- This host exports `AMD_CERT`, so `vm-quickstart.yml` installed the
  corporate CA into the two scratch overlays. The fio step's in-guest
  `git fetch` from GitHub ran after that and depended on it here. CI has no
  TLS interception. The overlays are deleted.

Pulled: image manifest `sha256:14f0b5d30e19...`,
keypair referrer `sha256:8cc3ddd6fba4...`. `rocm-golden.qcow2.zst` unpacked
to 8,422,096,896 bytes, 64 GiB virtual. `vm-info.json`: username `ubuntu`,
`kernel_release` `7.2.6-070206-generic`, `qemu_minimal_commit`
`80c17f805eeb...`. The account matches the fleet file, so `quick-start`
wrote no `overlay-image-login.xml`. `up` took 2 m 5.3 s, and both guests
answered SSH with the pulled key on the first probe, about 23 s after it
returned.

On each guest **before any playbook ran**, identical but for MAC, address
and hostname:

| What | Measured |
|---|---|
| kernel | `7.2.6-070206-generic`, and one kernel image installed |
| `/dev/ernic-p4` | `crw-rw---- root ernic-p4` |
| `/dev/sdxi0` | `crw-rw---- root sdxi` |
| the baked account's groups | `ubuntu`, `video`, `users`, `admin`, `sdxi`, `ernic-p4`, `render` |
| on `PATH` | `p4_fp8_client`, `p4_fp8_bench`, `sdxi-cli`, all in `/usr/local/bin` |
| `ibv_devinfo -l` | `rocm-rdma-ernic0`; `PORT_ACTIVE`, Ethernet, `max_srq` 256 |
| netdev | `rocm-ernic0`, up, MAC `72:6f:63:6d:00:01` / `:02` |
| address from `rocm-ernic-netcfg` | `192.168.100.11/24` / `.12`, equal to `fleet_guest_ip` in the generated inventory |
| `/etc/udev/rules.d` | `99-ernic-p4.rules`, `99-sdxi.rules`, and `99-rocm-ernic.rules` at 2928 bytes: rocm-ernic's copy |
| hipFile SDK | `include/hipfile.h` and `lib/libhipfile.so` under `/opt/rocm/core-10.1` |
| modules | `ernic_p4`, `ionic`, `ionic_rdma` from `updates/dkms`, `sdxi_char` from `updates/`, all loaded |
| `/etc/sdxi-guest-ref` | `dfd76f23f6f1...` |
| `lspci` | `[1dd8:1002]`, `[1002:75c1]`, SDXI `[1022:149f]`, NVMe `[1b36:0010]` |
| `/dev/kfd` | absent until the playbook's amdgpu probe, as designed |
| `/opt/fio-hipfile` | absent, so the fio step builds fio in the guest |
| `systemctl --failed` | `apparmor.service`; `is-system-running` says `degraded` |

So both renames fire on this image at boot, which the paragraph on resolving
the IB name below had as unrecorded, and the boot unit's address is the one
`quick-start` assigns.

The lane's steps, all exit 0:

| Step | Time | Result |
|---|---|---|
| `quick-start test` | 1 m 37.5 s | `rocm-golden-1` ok=66 changed=21 failed=0; `rocm-golden-2` ok=62 changed=18 failed=0 |
| sidecars | | prometheus, grafana, loki and alloy running; the `up` query answered; Loki healthy |
| `check-vm-devices.sh --gpu gfx1250 --strict-key`, both guests | | "device reporting checks passed" |
| GEMM + hipFile, guest 1 | 135 s | 643 kFLOP/s; 13.3 MB/s |
| hipFile fio, guest 2 | 59 s | 12.3 MB/s, fio-3.42 built in the guest |
| P4 FP8, guest 1 | 9 s | 206.7 MB/s: 262144 elements x 2000, avg 1.268 ms |
| SDXI copy, guest 1 | 8 s | 2523.0 MB/s: 4194304 bytes x 1204, avg 1.66 ms |
| `quick-start down` | 18.5 s | both guests powered off by name |

Inside `quick-start test`: `vector_add` passed on gfx1250; `ib_send_bw`
65536 B x 1000 averaged 559.27 MiB/s on `rocm-rdma-ernic0` and ernic 1
moved 65,536,000 bytes; NVMe write/read was 117.0/287.0 MiB/s on guest 1
and 119.0/289.0 on guest 2. The P4 step ran with `<p4-enable>true` and
`<p4-guest-scope>load`, as the guest user, with no sudo, which the publish
gate had not exercised. No service restarted and ernic, rocjitsu and sdxi
logged no error line.

**What that run found wrong, none of which failed a step:**

- **`apparmor.service` is failed on a pristine boot of the published
  image.** The journal: `AppArmor parser error for /etc/apparmor.d in
  profile /etc/apparmor.d/usr.lib.snapd.snap-confine.real at line 13: Could
  not open '/var/lib/snapd/apparmor/snap-confine'`.
  `sbates130272.batesste.user_setup` removes the `snapd` package with
  `state: absent` and no purge (`tasks/main.yml:186`), then deletes
  `/var/lib/snapd` (line 208). dpkg keeps a removed package's conffiles,
  that profile is one of snapd's, and its line 13 includes a path under the
  deleted directory. Nothing in the daily lane or
  `qemu/checks/rocm-golden.sh` looked at failed units, so it went out.
  **Fixed in the bake, and so in `20261117.g481c892`, not in that image**:
  see "AppArmor and the snapd leftover" below.
- **Prometheus's `amd-gpu` and `hsa-snoop` targets are down for every
  golden guest.** Only `:9100` and `:9879` have a listener. That is the
  bake's doing, on purpose: `rocjitsu_setup`'s `tools` tasks disable
  `gpuagent` and `hsa-snoop-prometheus.service`. Neither can run against
  an emulated GPU, so the two scrape jobs, their forwards and their panels
  are removed; see "Guest exporters" above. That is a change to the
  generator and the fleet file, so it takes effect at the next render and
  needs no new image. Whether the old backing image behaved the same was
  not compared.
- **The image's `99-rocm-ernic.rules` is rocm-ernic's copy, which renames
  one NIC per guest.** Harmless on this fleet, one ernic a guest; see "udev
  rules (in-VM)". Fixed in the bake, and so in `20261117.g481c892`, not in
  that image.
- **The exporter play was not a no-op**: see "the published image carries
  an `rdma_exporter` with no record" below.
- **`quick-start down` leaves the two named volumes**, `<stack>_ernic-stats`
  and `<stack>_vfu-sockets`. Left as it is; nothing is known to go wrong
  with a stale one.

Not exercised by that run: `helios-tray` and `mi355x-server` were not
booted; the lane's `--in-network` path and 4 vCPU sizing were not
reproduced; and `retire_stale_overlays` was not reached, the images
directory being fresh.

**`20261117.g481c892`, pulled and booted as the two-guest fleet, by hand,
on the dev host, on 2026-11-17: three boots, and every step run passed.**
One machine, not a lane. How: `python3 -m qemu_tool quick-start preflight`,
`up`, `test` and `down` from this working tree (`481c892` plus the
uncommitted `<backing-ref>` move), with `--xml qemu/stacks/rocm-golden.xml`,
`--yes`, and `--stack-dir`, `--key-dir` and `--images` under one scratch
directory, so the images directory was fresh and the artifact came from the
registry. Logins used the image's published key and nothing else.

What differed from the lane is what differed for the `g80c17f8` boot above:
8 vCPU and 8192 MiB a guest on 32 cores, no `--in-network`, guest SSH on
2226/2227 and Grafana on 3003, the server images already in Docker's cache,
and `AMD_CERT` exported, so `vm-quickstart.yml` installed the corporate CA
into the scratch overlays, which are deleted. And less of the lane was run:
not `scripts/check-vm-devices.sh`, and not the GEMM, hipFile or fio steps.

Pulled: image manifest `sha256:72a0aa0c1ad44bf1...`, the digest the publish
run pushed; keypair referrer `sha256:76d3e2c2a9e4...`. `rocm-golden.qcow2.zst`
unpacked to 8,379,301,888 bytes, the manifest's annotation. The run read
`vm-info.json` as username `ubuntu`, `kernel_release`
`7.2.6-070206-generic`, `qemu_minimal_commit` `481c892854b2...`. The first
`up` took 8 m 20.0 s, nearly all of it the 2.08 GB pull; an `up` with a
fresh pull later the same hour took 2 m 15.7 s, and `up --clean-overlays`
on the backing file already there 29.1 s.

On each guest **before any playbook ran**, identical but for MAC, address
and hostname. The rows of the `g80c17f8` table above that are not here read
the same on this image: kernel and the one kernel image, `/dev/ernic-p4`,
`/dev/sdxi0`, the account's groups, the IB and netdev names, the addresses,
`/etc/sdxi-guest-ref`, `lspci`, and no `/dev/kfd`.

| What | `20261117.g80c17f8` | `20261117.g481c892` |
|---|---|---|
| `99-rocm-ernic.rules` | 2928 bytes: rocm-ernic's copy | 9647 bytes, sha256 `2968abe70300...`, the role's file byte for byte |
| `systemctl --failed` | `apparmor.service` | empty |
| `systemctl is-system-running` | `degraded` | `running` |
| snapd | removed, its AppArmor profile left behind | `un` to dpkg; no `usr.lib.snapd.snap-confine.real`, no `/var/lib/snapd`; `apparmor.service` active |
| `amdgpu-probe --status` | not asked: the probe there predates the flag | `nofirmware` |
| the login hint | not looked at; the script there says `sudo amdgpu-probe` | names `qemu-tool quick-start test` on the host; printed at a real pty login on guest 1, above the WELCOME banner |
| `sudo amdgpu-probe`, no blob | not run; the script there loads amdgpu, and the probe fails | refused, exit 1, "Nothing was changed"; `lsmod` counted 0 amdgpu before and after |
| `/usr/local/share/rdma_exporter.sha256` | absent | present, written by the bake |
| listeners | `:9100`, `:9879` | the same two; nothing on 9488 or 9490 |
| `git config --system` | not looked at | no `url.*` entry and no `/etc/gitconfig` |

The installed `amdgpu-probe` was the role's file as of `481c892`, by
sha256. The role's copy has since changed by one sentence, the
`rj-ip-discovery` step below, so the two no longer compare equal.

`quick-start test`, exit 0, 1 m 38.1 s: `rocm-golden-1` ok=65 changed=17
failed=0, `rocm-golden-2` ok=61 changed=14 failed=0, against 66/21 and
62/18 on `g80c17f8`. The difference is the tasks the bake now does first:
`Install the rocm-ernic udev naming rules` reported ok on both guests, and
`Download rdma_exporter`, `Install the rdma_exporter binary` and `Record
which rdma_exporter tarball is installed` were skipped on both, the binary
keeping the bake's mtime. `vector_add` passed on gfx1250 on both guests;
`ib_send_bw` 65536 B x 1000 averaged 515.38 MiB/s on `rocm-rdma-ernic0` and
ernic 1 moved 65,536,000 bytes; NVMe write/read was 140.0/234.0 MiB/s on
guest 1 and 143.0/215.0 on guest 2. Afterwards both guests said `loaded`,
the hint was silent, `/dev/kfd` and `renderD128` to `135` were there, no
unit had failed and `is-system-running` still read `running`. `down` took
18.1 s.

Prometheus, asked from inside its container: `up` was 1 for `fleet-node`
and `rdma-exporter` on both guests and for `ernic-stats`, five targets and
three jobs, with no `amd-gpu` or `hsa-snoop` job; `amdgpu_devices_total`
and `amdgpu_kfd_present` were 1 for each guest.
`scripts/check-guest-exporters.py`, fed that `up` query the way the daily
lane pipes it, printed four `up` lines and exited 0. Grafana answered 200
on 3003.

P4 and SDXI on guest 1, as `ubuntu`, no sudo: `p4_fp8_client` loaded
`fp8_policy.json` (7047 bytes) and reduced 4096 elements;
`p4_fp8_bench` gave 208.0 MB/s, 262144 elements x 2000, avg 1.260 ms.
`sdxi-cli` copied 4 MiB and `cmp` found it equal; the report script's timed
loop, run by hand, gave 2702.0 MB/s, 4194304 bytes x 1289, avg 1.55 ms.
The `g80c17f8` boot's 206.7 and 2523.0 MB/s were the script itself.

**The recovery from a failed probe, run for real on this image, twice.**
Each on a fresh overlay, which has no blob. A bare `sudo modprobe amdgpu`
with the emulation parameters on guest 1 returned 0; dmesg said `discovery
failed: -2` and `probe with driver amdgpu failed with error -2`; the module
stayed loaded with no driver on `0000:02:00.0`; `--status` said `unbound`
and the hint printed its warning. `sudo amdgpu-probe` then said the blob
was what was missing and that nothing was changed, exit 1.

| Path | What happened |
|---|---|
| from the host | `quick-start test` staged the blob and `firmware.yml`'s re-bind bound the GPU; `vector_add` passed on guest 1; recap ok=65 changed=15 and ok=61 changed=15 |
| by hand | `rj-ip-discovery gfx1250 FILE` in the rocjitsu container serving the guest wrote 848 bytes; copied to `/lib/firmware/amdgpu/ip_discovery.bin`; `sudo amdgpu-probe` printed "The discovery blob is staged now; binding without a reload.", `bound 0000:02:00.0 to amdgpu` and "render node present; the GPU is ready.", exit 0; a `quick-start test` after it passed `vector_add` (ok=64 changed=14) |

No `rmmod`, no reboot and no oops in either, and guest 2 was not touched.

**What that boot found wrong, none of which failed a step:**

- **`quick-start` said Prometheus was published on a host port, and it was
  not.** Fixed; see "Prometheus has no host port" below.
- **The by-hand staging step named a command that fails as written.** The
  probe's refusal, `WELCOME.md` and `/lib/firmware/amdgpu/README` said to
  copy "the output of `rj-ip-discovery gfx1250`". In the rocjitsu image the
  fleet pins that prints `Required arguments: <generation> <output-path>`
  and exits 1; with a path it writes the file. All three now say
  `rj-ip-discovery gfx1250 FILE`. They are files the bake installs, so
  `20261117.g481c892` keeps the old sentence and the next published image
  has the new one. `tests/test_amdgpu_probe.py` holds the spelling.
- **A root-owned directory in the caller's Grafana data directory**,
  `<stack>/grafana-data/dashboards`, root:root 0755 and empty, after every
  `up`. Fixed; see "Container privilege".
- **The image carries the bake's `lastlog` entry**: a first login prints
  `Last login: Tue Nov 17 14:27:59 2026 from 10.0.2.2`. Left as it is.

Not exercised by that boot: `helios-tray` and `mi355x-server`, so eight
renamed NICs are still unseen; `--in-network` and 4 vCPU sizing;
`check-vm-devices.sh` and the lane's GEMM, hipFile and fio steps on this
image; and `retire_stale_overlays`. Whether the published image carried a
CA file before `quick-start test` installed one was not looked at.

**Prometheus has no host port, and `quick-start` no longer says it has.**
The generator stopped publishing one on 2026-11-03 (`6e03fe8`, to stop VS
Code forwarding it); only Grafana and guest SSH reach the host, and
`tests/test_gen_compose.py` pins that. `quick-start` went on as before: its
preflight line read `Prometheus :9090`, `_resolve_ports` asked where to move
9090 when it was busy and printed "Prometheus will be published on 9093",
the summary gave `http://localhost:9090`, and the README's table did too.
On the boot above `curl localhost:9093` got connection refused. Now
`_resolve_ports` handles Grafana and guest SSH only, the overlay it writes
has no `<prom-port>`, and the summary and the README give the way that
works, which is the way the daily lane asks:

```bash
docker exec <stack>-prometheus-1 wget -q -O- \
    'http://127.0.0.1:9090/api/v1/query?query=up'
```

`<prom-port>` still parses and nothing reads it (`gen_compose` copies it
into the render's spec and no template uses it). Removing the element would
make every fleet file that sets it an error, so it is left for the owner.

**The published image carries an `rdma_exporter` with no record of which
one.** `rocm_ernic_setup`'s `metrics` tasks downloaded the tarball and
reinstalled the binary on every run: `quick-start test` against the golden
guests reported `Download rdma_exporter` and `Install the rdma_exporter
binary` changed on both, and the binary's mtime became the time of the
test. They now record the tarball's checksum in
`/usr/local/share/rdma_exporter.sha256` and skip the download and the
install when the record names the pinned one. `20261117.g80c17f8` has the
binary and no record, so each guest made from it is installed to once more
by `vm-quickstart.yml`: the daily lane's run on that image reported the
download, the install and the record changed on both guests. The bake of
`20261117.g481c892` wrote the record, and on two pulled guests of it the
play changed nothing: the download, the install and the record were skipped
on both and the binary kept the bake's mtime. The guest check still does not
look for the record.

**`quick-start test` writes its per-guest summaries into the stack,**
`<stack-dir>/artifacts`. It was `/var/tmp/quickstart-artifacts-<flavor>`,
which no flag moved and which `test` empties first: the run above, pointed
entirely at a scratch directory, still wrote into the default stack's.

**`quick-start` has two optional tests, `--sdxi-test` and `--p4-test`, both
off by default.** Each is a play of `vm-quickstart.yml` gated the way the
GPU-memory RDMA play is: a play var that is `false`
(`quickstart_sdxi_test`, `quickstart_p4_test`) and a first task that ends
the play, so a run without the switch executes nothing of it in a guest and
`quick_start._playbook_command` passes the command line it always did. They
sit after the NVMe test and before the facts play, so the per-guest summary
and the report carry their results; the GPU-memory RDMA play stays last.

| Switch | Hosts | What runs, as the login user with no `become` |
|---|---|---|
| `--sdxi-test` | every guest | 4 MiB of `/dev/urandom` through `sdxi-cli -d /dev/sdxi0 copy`, then `cmp` |
| `--p4-test` | `fleet[0]` only | `p4_fp8_client` with the installed `fp8_policy.json`, then `p4_fp8_bench <ib> 262144 2000` |

- **A fleet that cannot run one is refused before anything is started**
  (`quick_start._check_optional_tests`, on the `test` and `all` verbs, from
  the merged settings): `<sdxi-per-vm>` of 0, `<p4-enable>` not true, or a
  `<p4-guest-scope>` other than `load`. The client loads a program
  (LOAD_*, which needs `load`) and installs an entry (ENTRY_UPDATE, which
  needs `write`), and `load` is the widest scope. A flavor with no playbook
  is refused too, and so is `--p4-iters` without `--p4-test`. The message
  pins the value on the one layer that set it — the last that names the
  element, `quick_start._where_set` reading `FleetXml.fields_set` — or
  calls it the default when none does; it used to list every layer, which
  blamed `rocm-golden.xml` for an overlay's `<sdxi-per-vm>0`.
- **The check reads the settings this invocation names, not the stack that
  is running**, as `test` does for the guest count and the name prefix:
  `up --xml overlay.xml` followed by `test --p4-test` with no `--xml`
  checks the packaged file and passes whatever the overlay turned off. That
  stack, and one rendered before an edit to the fleet file, is caught in
  the guest by the play's own check, which names the same elements — after
  the default tests have run, not before. Reading the P4 flags and SDXI
  services back from the rendered `docker-compose.yml` would close that,
  and would be the only setting `quick-start` reads from there.
- **Any other verb ignores the three switches and says so on stderr**
  (``note: --sdxi-test ignored: `up` runs no test``), exit status
  unchanged. `--rdma-duration` and `--nvme-duration` are still ignored in
  silence there.
- **P4 is one guest because the reduction is inside one NIC**; see "The P4
  number is the rate of the fp8 reduction" below. The play asks that guest
  for its IB device with `ibv_devinfo -l` and does not read the fact the
  first play recorded. `--p4-iters N` is the one sizing switch; the element
  count stays a play var, since the rate is not comparable across counts.
- **The P4 program and its entry stay in that NIC until its ernic server
  restarts.** Nothing unloads them. A two-equal-operand RDMA WRITE on that
  NIC is a reduction from then on; a write with one gather entry, which is
  everything else the playbook sends, is untouched.
- **The SDXI play is a functional check and reports no rate.** The daily
  lane's SDXI figure times the copy ioctl for two seconds from a program
  inside `scripts/generate-vm-report.sh`; sharing it means moving it out of
  that script, and a second timer here would publish a number that cannot
  be compared with the lane's.
- **A test that was asked for and left no result fails the run**
  (`quick_start._report_optional_tests`): the playbook exiting 0 says no
  play failed, and a skipped play did not fail.
- **A guest that fails an optional test writes no summary for that run**,
  the same as one that fails the GPU, RDMA or NVMe test: Ansible drops a
  failed host from the plays after it, and the facts play is after both.
  `quick-start` stops at "the configure-and-test playbook failed" before
  the report, so nothing is published from it.
- **There is no `--all-tests` and no switch for the GPU-memory RDMA play.**
  The second fails on every stack this repository ships, and
  `tests/test_perftest_setup.py` pins that `quick_start.py` and `cli.py` do
  not name its gate; the first would have to leave it out, and would change
  meaning with each test added.

`tests/test_quickstart_optional_tests.py` holds all of it, and runs both
plays on the test host against stand-ins for the guest's tools. The SDXI
play makes its scratch directory under `${TMPDIR:-/tmp}`, which is `/tmp`
in a guest and the test's own directory in the suite, so two copies of the
suite do not see each other's files.

**Both plays have run against real guests: 2026-11-18, the development
host, two pulled guests of `20261117.g481c892`.** One boot throughout, on a
scratch stack beside nothing else (`--stack-dir`, `--key-dir`, `--images`
and an overlay setting only `<vm-name-prefix>qslive`). `quick-start` sized
the guests from the 32-core host to 8 vCPU and 8192 MiB each, not the fleet
file's 4 vCPU and 4096 MiB, so every figure below is at that size; nothing
was measured at 4 vCPU. Five `test` runs, in this order, all exit 0:

| Run | Wall | Recap, guest 1 / guest 2 | SDXI | P4, guest 1 | `ib_send_bw` average |
|---|---|---|---|---|---|
| `test` | 112 s, the first after boot | ok=65 / ok=61, failed=0 | skip task only | skip task only | 595.15 MiB/s |
| `test --sdxi-test --p4-test` | 74 s | ok=78 / ok=64, failed=0 | verified on both | 194.1 MB/s, avg 1.351 ms | 615.02 MiB/s |
| the same again | 69 s | ok=78 / ok=64, failed=0 | verified on both | 195.0 MB/s, avg 1.344 ms | 512.97 MiB/s |
| `test --p4-test --p4-iters 200` | 68 s | ok=74 / ok=60, failed=0 | skip task only | 194.5 MB/s, avg 1.348 ms, "x 200 iterations" | 623.53 MiB/s |
| `test` | 66 s | ok=64 / ok=60, failed=0 | skip task only | skip task only | 572.93 MiB/s |

What that settles, row by row of what was open:

- **A default run is unaffected.** In the first and the last run each new
  play printed its `PLAY` line and its one skip task and nothing else, and
  neither guest's summary has an SDXI or a P4 section. The switches add
  four `ok` tasks a guest for SDXI and ten on guest 1 for P4 over the last
  row, and nothing to a run without them.
- **`--sdxi-test` passes on both guests**, in two runs: `sdxi copy: 4194304
  bytes through /dev/sdxi0, source and destination identical`, and the
  summary line `<guest>: SDXI copy of 4194304 bytes verified`. No scratch
  directory was left in either guest's `/tmp`.
- **`--p4-test` passes on guest 1 and the rate is 194 to 195 MB/s** at
  262144 elements, three runs within 0.5%. The client reported `abi=1
  scope=3 max_program=4194304`, loaded `fp8_policy.json` (7047 bytes) and
  checked its one 4096-element reduction each time. That is below the "about
  210 MB/s" of "The P4 number is the rate of the fp8 reduction" below,
  which was the 2026-11-16 local bake and another guest size; the two were
  not measured side by side, so the difference is not attributed.
- **A second load in one boot works.** Three loads of the program over the
  one already there, the rate unchanged, and the `ernic-1` server log
  carries no error or failure line afterwards.
- **`ib_send_bw` between the guests still passes with the program left in
  `ernic-1`**: the last row, 572.93 MiB/s against 595.15 before any load,
  and `ernic 1 moved 65536000 bytes` in all five. The five averages run
  from 513 to 624 MiB/s with the low one in the middle, which is the
  run-to-run spread issue 14 describes and no trend.
- **The summary and the report carry the results.** Guest 1's summary
  gained `### SDXI test` and `### P4 FP8 test`, guest 2's `### SDXI test`
  alone, and `quick-start report` wrote both into `index.md`. Run as its
  own verb, `report` heads that file `RDMA bytes across the mesh: unknown`
  and `Status: **unknown**`; that is `report`'s and predates the switches.
- **The programs resolve from a non-login session**, by `ansible -m shell`
  through the stack's inventory, on both guests: `sdxi-cli`,
  `p4_fp8_client` and `p4_fp8_bench` are found in `/usr/local/bin`. The
  plays' own checks printed `/dev/sdxi0 is root:sdxi mode 660, usable by
  ubuntu` on both guests and `/dev/ernic-p4 is root:ernic-p4 mode 660,
  usable by ubuntu` on guest 1.
- `report --sdxi-test --p4-test --p4-iters 5` exits 0 with the note on
  stderr, and `test --p4-iters 5` exits 1 in under a second.

Still not run:

| | |
|---|---|
| the `all` verb with a switch | not run, so the closing summary line that names the switches, which only `all` prints, has not been seen live |
| the in-guest refusal for a missing device or a driver that is not loaded | read, not triggered |
| the in-guest refusal for a user outside the group | not triggered through the play. The run reported, and its kept logs do not hold, that `test -r -a -w` on `/dev/sdxi0` and on `/dev/ernic-p4` as `nobody` returns 1 and that the play's condition evaluated as `nobody` selects the group message. No membership was changed to make the play itself fail |
| either test at the fleet file's 4 vCPU guests | not run |

The refusals before anything starts were measured the same day through the
CLI against a scratch stack directory: `<sdxi-per-vm>0`,
`<p4-enable>false` and `<p4-guest-scope>read` each exit 1 in under 0.2 s,
before the preflight; `--p4-iters 0` exits 2 and a switch on `--flavor
helios` exits 1. `ansible-playbook --list-tasks` shows ten plays, SDXI
seventh and P4 eighth, the GPU-memory RDMA play still last.

That host has `AMD_CERT` set, so each `quick-start test` there installs the
corporate CA in the guests it configures. It went into the two scratch
overlays and into nothing tracked or published.

The SDXI image has no `ENTRYPOINT` and a shell `CMD`
serving a fixed socket name, so the generated `sdxi-N` services carry
`entrypoint: ["rocm-sdxi"]`; without it compose execs `-s` as the program.
There is no `qemu/docker/` and no locally built image any more.

**The generated files are generated.** `docker-compose.yml`, `prometheus.yml`
and `fleet-inventory.yml` in each stack directory come from the fleet file;
edit that and re-run `qemu-tool gen-compose`. A hand edit is lost on the next
regeneration and `gen-compose --check` fails in CI. All three files are
committed so the stack stays reviewable in a diff and usable with plain
`docker compose`. Each carries a `# xml-sha256:` line, the digest over every
layer it was rendered from; that header is also what identifies a directory
as a qemu-tool stack.

**Editing `rocm-golden.xml` changes that digest even when the edit is to a
`<bake-*>` element the generator never reads**, so every edit to it is
followed by a regeneration of `qemu/compose/rocm-golden`.

**`Gen Compose Check` is the only workflow that runs `tests/`**, on every
pull request and by manual dispatch — not on push to main, and not on a
schedule. A second workflow, `Site Script Tests`, ran the same command on a
bare runner; it is deleted, because this run is a superset of it. The tests
load the package straight from the checkout, so qemu-tool needs no install
for them.

The same lane is where the cheap guards live that no guest-booting lane can
replace:

- It runs the unit suite with `CI=true` after installing udevadm, pciutils,
  PyYAML and Jinja2, because `tests/test_ernic_identity.py` is the only thing
  that asks udev to *parse* `99-rocm-ernic.rules` and the only thing that
  reads `rocm_ernic_setup`'s wiring, and under `CI=true` those checks fail
  instead of skipping.
- It installs the rocm-ernic collection first, for the same reason. Tests
  in `tests/test_ionic_source_mirror.py` read the installed collection
  (the fetch script's default repository, which roles run it, the variables
  they read, and the script itself against a local mirror) and skipped
  without it, which in this lane was always; so do tests in
  `test_configure_keeps_baked_driver.py`, `test_ernic_identity.py` and
  `test_source_clone.py`. They look under
  `~/.ansible/collections/ansible_collections/rocm/ernic`, where a default
  `ansible-galaxy` install puts it; to run them against another install,
  set `HOME` to a directory holding one. The step takes the one
  rocm-ernic entry out of `ansible/requirements.yml`, so the pin has no
  second copy, and installs it with `ansible-galaxy --no-deps`: a git clone
  from GitHub, nothing from Galaxy. Under `CI=true` those tests, and the
  ones in that file that run the role's tasks through `ansible-playbook`,
  fail where they would skip; on a host with neither they still skip, and
  print that they did.
- `gen-compose --check` for all three committed stacks, plus the bare form.
- `docker compose config` against the committed stacks with only
  `CONTAINER_USER` and `CONTAINER_KVM_GID` in the environment, which is the
  proof that nothing else is left to interpolate.
- A `--profile metrics` render. That one is not optional polish: profiles
  *filter* services out of `config` output, so until it existed every
  observability service was validated by nothing, and `config -q` on a render
  with all services filtered away still exits 0 — hence the step asserts the
  services are present first.
- Overlay renders (one guest, eight guests), a tap-less render, and the
  privilege-posture check over every stack.
- A 3-VM stack rendered to a scratch directory, with `qemu-tool compose
  config` run inside it with no flags, asserting the compose project name
  (taken from the working directory) and the service list, because resolving
  to the *wrong* stack also exits 0.

### A fleet larger than the committed ones is a local XML

**One file per shipped stack, plus overlays you do not commit.** An overlay is
a short fleet XML naming only the elements it changes; it goes last on the
command line:

```bash
qemu-tool gen-compose --xml qemu/stacks/rocm-golden.xml --xml ~/fleets/mi100.xml \
    --output-dir /var/lib/qemu-tool/stacks/mi100-fleet
qemu-tool compose --stack-dir /var/lib/qemu-tool/stacks/mi100-fleet \
    --profile metrics up -d
```

The mechanism is unchanged; what changed is that **no overlay is committed any
more**. `scale-out.xml` (the 8-VM golden file), `overlay-ci.xml` and
`overlay-mi100.xml` are deleted. A CI lane writes its overlay inline in the
workflow; a host's fleet is a local file. The rule that outlived them: **do
not add a per-host or per-lane settings file to the repo.** The files this
replaced — `env.scale-out-ci`, `env.scale-out-mi100`, `env.scale-out-strix`,
then the committed overlays — were near-copies sharing ~20 identical values,
hand-synced and reliably out of step. If every fleet wants a value, it goes in
the stack's own file.

**A named stack reads its own fleet file.** `qemu-tool compose --stack NAME`
with no `--xml` takes the first `fleet.xml` on the search path, else
`qemu/stacks/NAME.xml` — the fallback `gen-compose` already had — so the
images directory and the stale check have a source. The stale warning is
three cases, because a shipped stack is rendered from that one file and
nothing else. Layers that *are* that file and no longer match the recorded
digest: "generated from a different version", with the regenerate command.
Any other layers given with `--xml` — another file, or the stack's file plus
an overlay — are a different fleet, and the warning says so and points at
`gen-compose --output-dir DIR` and `compose --stack-dir DIR`; it never
suggests `gen-compose --stack NAME`, which would render that fleet over the
shipped stack. Layers the search path found are not reported at all: nobody
chose them against this stack.

**A host still holding a stack rendered from a deleted layer must re-render
it from a local XML.** `mi100-fleet` was rendered from `scale-out.xml` +
`overlay-mi100.xml`; neither exists, so its recorded regenerate command names
files that are gone, and its header is the old `# env-sha256:` spelling, which
this version does not recognise as a rendered stack at all — `qemu-tool down`
will not identify it and `cd <dir> && qemu-tool compose` falls through to the
committed default stack. Write the overlay's values into a local file (it set
`<vm-count>32`, `<vm-name-prefix>`, `<vm-images-dir>`, the fork
`<qemu-ref>`/`<ernic-ref>` that `mi355x-server.xml` now pins, one
node-exporter `<vm-extra-hostfwd>`, `<prom-data-dir>`, `<qemu-tool-src>` and
`<logs-enable>false`; `git show <old commit>:qemu/xml/overlay-mi100.xml` has
the text), layer it on `rocm-golden.xml` with `<sdxi-per-vm>0` and
`<p4-enable>false` if the old shape is wanted exactly, re-render with
`--output-dir`, and tear the old containers down with
`docker compose -p <project> down` if they are still up.

An overlay render needs both directory flags. `gen-compose` defaults its output
to the committed stack named by the first `--xml` file, so without
`--output-dir` the overlay's fleet lands on top of `qemu/compose/rocm-golden`
and `Gen Compose Check` fails. `<vm-count>` and the VM names are baked into the
generated YAML rather than read from the settings at run time, so without
`--stack-dir` `compose` runs that committed stack whatever layers it is handed
— the two-guest stack standing in for the 32-VM one just rendered, with no
error.

**Inspecting or stopping a rendered stack takes no flags**, because everything
but the four interpolated names is baked in:

```bash
cd /var/lib/qemu-tool/stacks/mi100-fleet && qemu-tool compose ps
export QEMU_TOOL_STACK=/var/lib/qemu-tool/stacks/mi100-fleet   # or, from anywhere
```

The directory comes from `--stack-dir`, then `$QEMU_TOOL_STACK`, then the cwd
if it carries the provenance header — no walk-up, because a walk-up from
inside this checkout lands on the committed default stack. A stack found by
its directory with no `--xml` is driven with **no layers**: the header records
a digest, not the layer paths, and `compose` reads nothing back from it. Two
consequences. The staleness warning needs `--xml`: it re-hashes the layers
you pass and compares, so only then can it say the stack was rendered from an
edited layer. And **the images directory is not baked in** — it is
`${VM_IMAGES_DIR:-/var/lib/qemu-tool/images}` — so a fleet with any other
`<vm-images-dir>` needs `--xml`, `--images` or the variable exported on the
commands that start it, or the guests look for their disks in the default
place. **Nor is the source mount**: `QEMU_TOOL_SRC` defaults to `../../..`
relative to the stack directory, which is the checkout only for
`qemu/compose/<stack>/`. For an `--output-dir` render elsewhere it is an
unrelated directory (`/var/lib` for `/var/lib/qemu-tool/stacks/<x>`), the qemu
containers find no `qemu/pyproject.toml` under it and install qemu-tool from
PyPI — an old `run-vm` against a new stack. Set `<qemu-tool-src>` to an
absolute path and pass `--xml`, or export `QEMU_TOOL_SRC`. And a
`<stack>/.env` is read by compose only in this no-`--xml` mode; with settings
in force `COMPOSE_DISABLE_ENV_FILE` is set. `compose` does not regenerate and does not edit `<vm-count>`: a
re-render stays a typed `gen-compose --output-dir`.

Three elements are per-VM comma-lists — `<vm-shm-size>`, `<rocjitsu-config>`
and `<vm-filesystem>`: one value broadcasts, `<vm-count>` values apply
positionally, any other length is an error naming the element and both
lengths. Each distinct `<vm-filesystem>` share gets its own bind mount on the
qemu anchor; a `run-vm --xml` outside a stack ignores a list, since it cannot
know which guest it is. The trap worth knowing before
editing: `<vm-extra-hostfwd>` names a field that is *already* a list, so it
keeps its existing meaning — the commas are several values for **one** VM, and
the whole value goes to **every** VM. `<vm-extra-hostfwd>a,b` is two hostfwd
rules per guest, not one rule each for two guests. Do not "fix" it by padding
it to `<vm-count>`. (`--pci-hostdev` and `--vfio-userdev` are the same kind of
field on `run-vm`; they have no fleet element.) There is no element for
per-VM names, SSH ports, MACs or guest IPs: all four are derived from the
prefix, the port base and the NIC ordinal.

**`<ernic-tap>true` is what makes inter-node anything possible.** The TCP mesh
carries RDMA payload only; without a TAP the guests have no Ethernet, ARP goes
unanswered, and `ib_send_bw` cannot do its out-of-band exchange — so no
inter-node RDMA test runs at all. It costs one short-lived sidecar per VM
(holding the `CAP_NET_ADMIN` the long-running server does not need) and a
shared `l2` network. Each ernic also gets its own `ERNIC_TCP_GUEST_GIDS` (a
container environment variable the server reads, not a setting), derived from
its MAC; that one is per-node and must never be hoisted into the shared anchor
(issue 18).

Guest addressing: `ernic-1` is the mesh manager and serves VM 1, `ernic-N` are
workers dialling it. Guests take `<ernic-subnet>.11` upward on the ernic
segment — never `.1`, which is the manager's DHCP `server_ip`, and ARP for it
is intercepted.

Two other things worth knowing: `<prom-data-dir>` bind-mounts the Prometheus
TSDB to a path you can actually find — create it and `chown 65534:65534`
first, as the container runs as `nobody` — and `<log-max-size>`/`<log-max-files>`
bound every service's log, because Docker's default is unlimited and a wedged
manager with no backoff on its error path wrote 295 GB in two days.

`ernic-1` is `restart: "no"` on purpose (issue 7 below): a manager restart
wedges every guest on a DSR timeout that only `down && up` clears, and
`on-failure` would turn a visible crash into a quiet wedge.

Ceilings, nearest first: **the mesh stops registering new workers at about 40
nodes** (issue 17 — the 64-node topology payload cap in `rdma_backend_tcp.c` is
not reachable), and `<vm-vcpus>` × `<vm-count>` should not exceed `nproc` plus
about 25% (issue 14). Memory never binds in practice: 48 VMs used 107G of 503G.
`<vm-vcpus>` must stay ≥ 4 or `ionic_rdma` never probes.

**`<vm-shm-size>` × `<vm-count>` against `/dev/shm` is not the memory ceiling,
and reading it as one looks reassuring while measuring nothing.** Guest RAM is a
`memory-backend-memfd` (`-object memory-backend-memfd,share=on`), and a memfd
is anonymous — it is charged to the container's cgroup as `shmem`, never to
`/dev/shm`. Measured on the 32-VM fleet, 8 GiB per guest: host `/dev/shm`
489G with **64K used**, each container's own `/dev/shm` an 8G tmpfs with
**0 used**, while `memory.stat shmem` across the qemu containers was **40.6
GiB** — the guest RAM, exactly where the accounting says it is. `shm_size`
still has to be ≥ `<vm-vmem>` or QEMU refuses to allocate the backend, so the
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
which fleet XML is in force and whether each layer is valid (`FAIL`, one line
per problem, naming file and element), the vCPU budget against `nproc`, and whether
`<vm-images-dir>` has room and is owned by you. (`Gen Compose Check` greps
its output for `FAIL.*floor`, so keep the word in the `<vm-vcpus> floor`
label.) Read-only, no `sudo`, writes
nothing — safe on a host nobody has committed to yet.

`FAIL` sets exit 1; `warn` does not. That split is deliberate and worth
keeping: a checker that failed on a merely tight host would get disabled in
whatever runs it, and then catch nothing.

**It is the same code `quick-start` runs in its preflight, not a copy.**
`quick_start` imports `kvm_usable`, `docker_compose_ok` and `free_gb` from
`check_system`, and `tests/test_check_system.py` asserts *identity* (`assertIs`)  
rather than equality, so a second copy that happens to agree today still fails
the test. This repo has been bitten by a documented path and its CI equivalent
drifting apart — it is why `report-for-vm-rocm-golden` runs `qemu-tool quick-start`
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
the quiet kind. Two things have to travel with a large BAR: `<vm-pci-hole64-size>`
must cover it (`512G` is the phys-bits ceiling, see the MI355X README), and
vfio-user P2P DMA needs the fork QEMU image (issue 19) — `qemu/stacks/mi355x-server.xml`
pins it and sets the hole size, `qemu/stacks/rocm-golden.xml` does neither.

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
| Is this a qemu-tool stack? | `com.docker.compose.project.config_files` — the file carries the `gen-compose` provenance header (`# xml-sha256:`). Nothing else counts: not the directory name, not a service name, and not the project name |
| Is it yours? | `.Config.User`, the uid the containers really run as — which is what the `CONTAINER_USER` work put there |

**Do not "simplify" the first to a service-name match.** `qemu-1` is a name
anyone might use, and this command removes containers. Verified: an unrelated
compose project with a service called `qemu-1` is ignored, while a real fleet
beside it is found.

Ownership is *any* container as your uid, not all — a stack legitimately
mixes them, since the stats exporters are pinned to 65534 and
tapsetup/alloy stay root. The limitation worth knowing: a stack started
**before** the containers ran as the caller has every container as root, so a
non-root caller sees it as root's and skips it. That is correct — you cannot
stop them without root — and skipped stacks are listed with the reason rather
than passed over. `sudo qemu-tool down --all` takes everything, which is what
makes it the system-wide form.

**Containers are grouped by project name *and* compose file.** A stack's
project is its directory basename — `rocm-golden` for the committed default
stack, which no longer collides with the many things called `default`, but
any unrelated compose project in a directory of the same name shares it.
Grouped by name
alone, the foreign containers were counted into the fleet, their uid made a
root-era fleet read as yours, and listed first they hid the fleet altogether.
Each container is now judged by its own `config_files` label, and a stack
whose project name some other compose file also uses is torn down **without**
`--remove-orphans`: to compose the other project's containers are this one's
orphans. `--profile "*"` already covers every service in the file.

**A stack started by an older qemu-tool is not found.** One rendered with the
`# env-sha256:` header has no header this version recognises, and the
hand-written stacks' compose files are gone from the install, so a teardown
`-f` would have nothing to read. Use `docker compose -p <project> down`.

The teardown pins the project with an explicit `-p` and rebuilds the
invocation from the recorded `working_dir` and `config_files`, with
`down_all._teardown_env` supplying the interpolation variables through the
environment. The `environment_file` label is still honoured when a running
stack carries one — only a stack an older version started does, since nothing
passes `--env-file` now. That `-p` is the point: the project name is otherwise
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

## Published ports, and who can reach a stack

**Every port a stack publishes is bound to `<publish-bind-addr>`, and the
default is `127.0.0.1`.** The generator writes exactly two kinds of `ports:`
entry, both through `ports.publish_mapping()`:

| Service | Mapping | What it is |
|---|---|---|
| `qemu-N` | `<addr>:<ssh-port-base + N - 1>:2222` | the guest's sshd |
| `grafana` | `<addr>:<grafana-port>:3000` | Grafana, anonymous admin |

Nothing else is published. Prometheus, Loki and the exporters have no host
port, and a `<vm-extra-hostfwd>` rule is a QEMU forward inside the qemu
container, scraped over the stack's own network.

**"This host only" is Docker Engine's doing, from 28.0.0 up.** The compose
mapping is the only host-facing listener, but QEMU's forward listens on the
qemu container's wildcard at a fixed bridge address (`172.31.3.N`), and
what keeps other machines off both is the engine. Its 28.0.0 release notes
list two fixes (moby/moby#49325): "allowing neighbor hosts to connect to
ports mapped on a loopback address", and "allowing remote hosts to connect
directly to a container on its published ports". On an older engine a
machine on the host's own network segment can reach a port published on
`127.0.0.1`. This host runs 29.1.3. `qemu-tool check` has a `docker engine`
line under Docker for it: `ok` from 28.0, `warn` below, `skip` when the
daemon reports no version, never `FAIL`
(`check_system.loopback_publish_is_local`).

**Measured on a running stack**, 2026-11-17, one guest from
`20261117.g481c892` under `quick-start`, which had moved the ports to 2226
and 3003 because Windows held 2222 and 3000:

| Asked | Answer |
|---|---|
| rendered mappings | `"127.0.0.1:2226:2222"`, `"127.0.0.1:3003:3000"` |
| `docker ps` | qemu `127.0.0.1:2226->2222/tcp`, grafana `127.0.0.1:3003->3000/tcp`; loki and prometheus unpublished |
| `ss -ltn` | `127.0.0.1:2226` and `127.0.0.1:3003`, nothing else on those ports; both `docker-proxy` with `-host-ip 127.0.0.1` |
| `nc -z` from the host | open on `127.0.0.1`; refused on the host's other addresses (`10.0.0.107`, `10.255.255.254`, `172.17.0.1`), both ports |
| Grafana `/api/health` | 200 on `127.0.0.1:3003`, no answer on `10.0.0.107:3003` |
| `qemu-tool list`, `inspect`, `ssh`, the inventory | reach the guest on `localhost`/`127.0.0.1` port 2226 |

All from inside WSL. Nothing was tried from another machine or from
Windows.

Until this was fixed (after v2.0.0) the SSH mapping had no address at all,
`"2222:2222"`, which docker publishes on every interface, and
`<publish-bind-addr>` reached Grafana alone and defaulted to `0.0.0.0`.
What that exposed, read off the published image rather than inferred
(`20261117.g80c17f8`, its root filesystem read without booting it, and the
`vm-info.json` of `20261117.g481c892`):

- the key that logs in is published beside the image, as a referrer;
- `vm-info.json` there carries `username: ubuntu` and the account's
  password, which is `<bake-password>`;
- the guest's sshd accepts that password: `gen_vm.py`'s cloud-config has
  `ssh_pwauth: true` and `lock_passwd: false`, so the image holds
  `/etc/ssh/sshd_config.d/50-cloud-init.conf` saying `PasswordAuthentication
  yes`, which sshd reads before the cloud image's own
  `60-cloudimg-settings.conf` saying `no`, and `ubuntu` has a usable hash in
  `/etc/shadow`. No play changes any of it;
- the account has `NOPASSWD` sudo.

So anything but loopback is root in every guest for whoever can reach the
host. `<vm-password>` plays no part: it never reaches a pulled guest.

The same was then asked of a booted `20261117.g481c892` guest, with no
password submitted: `ssh -v` with public keys off stops at `Authentications
that can continue: publickey,password`; `sshd -T` says
`passwordauthentication yes`; `passwd -S ubuntu` says `P`, a usable
password. **The image is what is open; loopback only keeps people away from
it.** Closing it is a bake change and a republish, not done here: end the
golden bake with password authentication off (a `sshd_config.d` drop-in
that sorts before `50-cloud-init.conf`, or the account's password locked)
and assert it in `qemu/checks/rocm-golden.sh`. Until then a wider
`<publish-bind-addr>`, a stack rendered before this fix and a guest run on
the host (below) all expose that login.

The rules that follow:

- **Never write a `ports:` entry without `publish_mapping()`**, and never
  default the address back to a wildcard. `tests/test_publish_bind.py` fails
  a committed stack with a mapping that has no address or a non-loopback
  one, and a shipped fleet file that does not state
  `<publish-bind-addr>127.0.0.1` itself.
- **The element is an address, validated at parse time** (the `addr` kind in
  `fleet_xml.py`): an IPv4 or IPv6 literal, brackets optional and dropped. A
  host name is a problem naming the element, and so is an IPv6 zone
  (`fe80::1%eth0`): Python's `ipaddress` parses one, the render came out as
  `"[fe80::1%eth0]:2222:2222"`, and `docker compose config` then said
  `invalid IP address`. An empty element is the
  default, loopback, never "every interface". IPv6 is written in brackets,
  `"[::1]:2222:2222"`; `docker compose config -q` accepts `::1`, `::`, a
  ULA and a plain IPv4 address in that position (v5.2.0, checked by
  rendering each).
- **The ports are baked into the rendered YAML.** A stack rendered before
  this change publishes on every interface until it is rendered again,
  whatever its fleet file now says. `compose.exposed_ports()` reads the
  rendered file and `qemu-tool compose up` warns, naming the services; it
  does not refuse.
- **What follows the address.** `quick_start._State.published_host` is where
  quick-start waits for and prints its guests (`localhost` for loopback and
  for a wildcard, the address itself otherwise); `--in-network` is
  untouched, since it uses each qemu container's own address and port 2222.
  The generated inventory's `ansible_host` is `ports.reach_address()` of the
  same value. `list_vms` reads the bound address out of docker's Ports
  column into `ssh_host`, and `inspect` and `ssh` use it. `qemu-tool check`
  has a `<publish-bind-addr>` line under Settings: `ok` for loopback, `warn`
  otherwise, never `FAIL`.
- **quick-start's port probe did not change.** `ports.port_bindable()` asks
  the wildcard and loopback. A listener on any address of a port fails the
  wildcard bind, so a port it calls free is free for a loopback publish.

**No CI lane reaches a published port, and none may start to.** Every lane
that boots a stack runs in a job container, where the docker host's loopback
is not the job's own, so a port on `127.0.0.1` is out of reach and a port on
`0.0.0.0` was reachable only by an address no lane used. Each joins the
stack's network instead and uses the qemu container on port 2222:
playbook-test, publish (its second boot), `quickstart-boot`,
`report-for-vm-rocm-golden` (`--in-network`) and the two-guest smoke lane.
The lanes that say `ubuntu@localhost -p 2222` (`ansible-setup-test`,
`smoke-test-qemu-tool`, `report-for-vm-basic`, publish's first boot) run
`run-vm` inside the job container, where the port is QEMU's own forward and
no docker publish is involved. `compose-tests` starts device servers only.

**Not covered: a guest run on the host.** `run-vm` forwards with
`hostfwd=tcp::<port>-:22`, which binds every interface, and `gen-vm --bake`
boots its guest that way for Ansible: a bake on this host was seen listening
on `0.0.0.0:2226`, with the same public password. Inside a stack that
wildcard is required, since docker forwards to the qemu container's own
address, so narrowing it needs a `run-vm` option the generator does not
pass. Not done.

**WSL2.** The owner reaches Grafana from a Windows browser. Checked on this
host (mirrored networking): a plain listener on WSL's `127.0.0.1` answers
`http://localhost:<port>` and `http://127.0.0.1:<port>` from Windows. A
docker publish on loopback is `docker-proxy` listening the same way
(`userland-proxy` is at its default here; the running stack above showed
it), and NAT mode's `localhostForwarding` is documented to cover a port
bound to localhost. **A Windows browser against a running stack is still
untried**: the stack above was reached from inside WSL only.

## Container privilege

**Nothing in any stack runs as root by default except the two exemptions
below (`tapsetup-N`, `alloy`), and only `tapsetup-N` holds a capability.**
This changed on 2026-10-24; before that every service in every
stack ran as uid 0 with Docker's full default capability set, and the
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

Precedence matches compose's own: the process environment first, and only an
unset name is filled in, from the caller's ids. There is no third source:
fleet XML has no element for either value, because a uid in a settings file is
a uid in a file someone commits. `qemu-tool check` reports which of the two
answered — "the environment" or "this user".

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
| `alloy` | Mounts `/var/run/docker.sock` to discover containers and ship their logs. The socket is root's, so it takes no `CONTAINER_USER`; it runs `cap_drop: [ALL]` and `no-new-privileges`. Only rendered with `--profile metrics` and `<logs-enable>`. |
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
container and own the host, whatever the mount flags say — so `alloy`, a
pipeline that parses untrusted container log content, holds a root-equivalent
credential. That is accepted in a lab stack, and the generated service says so
in its own comment. A `dockerapi` haproxy (`tecnativa/docker-socket-proxy`,
`POST=0`) once sat in front of the socket; it was removed because its access
log, about ten lines per container every 5 s, flooded `quick-start logs`. Do
not describe the stack as having no socket holder, and do not restore the
proxy without solving that: `tests/test_compose_user.py` pins the direct mount.

**Migrating a fleet that ran as root.** `qemu-tool compose up` creates
`<prom-data-dir>`/`<loki-data-dir>`/`<grafana-data-dir>` as the caller. The
paths are read back from the rendered `docker-compose.yml` (the host side of
the `/prometheus`, `/loki` and `/var/lib/grafana` mounts), not from the fleet
XML: the render supplies a default for an element the fleet leaves unset —
`rocm-golden.xml` sets neither of the first two on purpose — and a rendered stack
is driven with no settings at all (Docker would
otherwise create a missing bind-mount source root-owned, and the service that
cannot write it reports the problem several minutes later) and warns about
anything it finds owned by someone else. The one it cannot fix is a
`<vm>-nvmeN.qcow2` from a root run: 0644 root-owned, and QEMU opens a drive
read-write, so the guest does not boot **at all** rather than merely owning the
wrong file. Delete them; they are scratch.

**A mount inside a data directory gets its mount point made first, too.**
Grafana's dashboards are mounted at `/var/lib/grafana/dashboards`, which is
inside the `<grafana-data-dir>` mount. The container runtime makes a missing
mount point itself, as root, and through the bind mount it lands on the
host: every `quick-start up` left `<stack>/grafana-data/dashboards`,
root:root 0755 and empty, in a directory described as needing no root. It
stopped nothing, and an empty directory in a directory you own can be
removed without sudo. `compose._nested_mount_points` reads the rendered
file for any mount whose container path is under a data directory's, and
`_precreate_data_dirs` creates each as the caller; `quick-start` calls it
after rendering, since it never goes through `compose.run`. Not seen since:
`tests/test_compose_user.py` creates the directory, and no stack has been
started to show the runtime leaving an existing one alone.

Three layers report this now, at the three moments it can be noticed.
`qemu-tool check` flags a `<vm-images-dir>` that is unwritable or holds
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
- `rocm.ernic.ernic_guest_setup` from the collection builds and
  installs the DKMS package, the rdma-core provider and the NIC config. Its
  DKMS package is `ionic-ernic` with the **base** patch series, and the bake
  runs it once, in the first play. The "Build SRQ + P4 ionic stack" play then
  builds the same package name and version with the SRQ and P4 series, and
  that build is the one the image keeps. The configure stage runs the role
  for the NIC config alone, with `ernic_install_driver: false`: see known
  issue 16.

### Two pins of rocm-ernic: the roles and the sources

Two commits of rocm-ernic reach a bake, and they are not required to be the
same one. They were until 2026-11-17, when the collection moved alone.

| Pin | Where | What it is | Moves with |
|---|---|---|---|
| the collection | `version:` of the rocm-ernic entry in `ansible/requirements.yml`, now `bd90234` | the **roles**: `rocm.ernic.*` task files and defaults, plus the base ionic series and the two scripts `ernic_guest_setup` stages from the collection's own `files/` | nothing; bump it when the role logic is wanted |
| `ernic_source_pin` | top of `vm-rocm-golden.yml`, now `90434a2` | the **sources**: the checkout `ernic_source` makes on the controller, which the SRQ and P4 series, the scripts that fetch and build them, the rdma-core provider series, the `ernic_p4` module and the P4 client and headers are all read out of | the `ernic.<sha>` in every `<ernic-ref>` and `ERNIC_IMAGE`, and only with it |

**The rule: `ernic_source_pin` is the device's guest half and moves with the
server images; the collection pin is role logic and does not.** A guest
built from sources ahead of its server fails quietly (`ibv_create_srq()`
fails, UCX declines `rc_verbs`, traffic falls back to TCP), so
`tests/test_playbook_pins.py` still requires `ernic_source_pin` to begin
with every `ernic.<sha>` the fleet files and workflows name, with no waiver.
What it no longer requires is that the collection be that commit. In its
place: the collection pin is a 40-hex commit under `type: git` (`Pins`);
every play that runs a `rocm.ernic` role checks the sources out at
`ernic_source_pin`, and the playbook hands the SRQ, provider and P4 roles
paths under `ernic_source_dir` (`ErnicSources`). That last part is the
load-bearing one. The SRQ and provider roles default their scripts and
series to the collection's own `files/`
(`{{ role_path }}/../../files/...`), so an include that lost
`ernic_srq_dkms_scripts_dir` or `ernic_srq_provider_patches_dir` would build
that half from the collection's commit and say nothing.

Why moving the collection alone was safe on 2026-11-17, checked between
`90434a2` and `bd90234` in a clone of upstream:

- **No `.patch` file differs** anywhere under `ansible/files/patches`. The
  one file there that does is `srq/README.md`, a path in prose. So the base
  series the collection supplies to the first play is the one the sources
  have, and `setup-ionic-dkms.sh` is byte-identical too.
- **The roles that drive the sources' scripts changed in spelling only.**
  `ernic_srq_dkms`, `ernic_srq_provider` and `ernic_p4_dkms` differ in the
  form of fact references (`ansible_facts['kernel']` for `ansible_kernel`)
  and an author field, so they pass the scripts at `90434a2` the flags those
  scripts take. Every play that runs them gathers facts.
- **Every variable this repo sets is still read**, and the three paths the
  roles read out of the checkout (`udev/99-rocm-ernic.rules`,
  `scripts/find-rdma-device.sh`, `cmake/ErnicKernelModule.cmake`) exist at
  the older commit. `tests/test_source_clone.py` pins that list against the
  installed collection: a role that starts reading a fourth path needs it to
  exist at `ernic_source_pin`.
- **A mismatch between the two that matters fails loudly.**
  `ernic_srq_provider` takes the rdma-core base commit from its own defaults
  (the collection) and asserts it against the `README.md` beside the series
  it was given (the sources); the two agree today, on `bd3282a`.

What did change, and is from the collection's commit: `fetch-ionic-sources.sh`
for the first play's fetch (see "Where the ionic kernel sources come from")
and `ernic_source`'s clone (see "The controller's rocm-ernic clone"). The SRQ
and P4 fetches still run the script from the checkout, the older one.

**On every bump of either pin, compare `ansible/roles` and `ansible/files`
between the two commits.** The split holds only while the newer roles still
drive the older scripts and series the way those expect. Moving
`ernic_source_pin` is the bigger change: it needs server images at that
commit (stock and fork), the three `<ernic-ref>`s and the `ERNIC_IMAGE`
lines, and a live boot. **This host is the one place the rename can pass by
accident**: a role still named the old way resolves here for as long as the
old collection is installed, to the old roles, with no error.
`CollectionName` in `tests/test_playbook_pins.py` fails any tracked file but
`CHANGELOG.md` and `qemu/debian/changelog` that carries the old name.

The kernel ref is `ionic_kernel_pin`, defined once at the top of
[`vm-rocm-golden.yml`](ansible/playbooks/vm-rocm-golden.yml) and currently
`v7.2.6`. It feeds both `ernic_ionic_kernel_ref` and
`rocm_ernic_setup_kernel_ref`, so the sources and the guest kernel cannot
drift apart — `driver_ionic.yml` fails the build if they disagree on
major.minor. It has a second half, `ionic_kernel_pin_commit`, the commit the
tag peels to, which every fetch of the sources is checked against; the two
move together. See "Where the ionic kernel sources come from".

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

**The published image `20261117.g80c17f8` carried rocm-ernic's copy of the
rules, not this repo's; `20261117.g481c892`, which the fleet files pull,
carries this repo's.** At
commit `80c17f8` `vm-rocm-golden.yml`'s SRQ + P4 play included `naming` with
`rocm_ernic_setup_udev_rules_src` set to
`{{ ernic_source_dir }}/udev/99-rocm-ernic.rules`, the file in the rocm-ernic
checkout at `ernic_source_pin`. At `90434a2` that file matches all four id
fields, as ours does, but its Ethernet stanza is `NAME="rocm-ernic%n"` — the
form the paragraph after next explains can name only one NIC per guest — and
its InfiniBand stanza takes the first free index, which two udev workers
race for. Measured on both guests of the default fleet, one ernic each,
before any playbook ran: the file in `/etc/udev/rules.d` is 2928 bytes (this
repo's is 9647), the netdev is `rocm-ernic0` and the IB device
`rocm-rdma-ernic0`. After `quick-start test` the file is this repo's,
because `vm-quickstart.yml` installs it.

The override is gone, so the include takes the role's default, and three
things keep another copy from coming back. Each ran, and passed, in the
publish run of `20261117.g481c892`: the first in its bake, the second in
the gate's configure stage, where the collection's copy and then this
repo's both reported changed, and the third on the boot with no device:

| Where | What |
|---|---|
| the bake | `rocm_ernic_setup`'s `naming_check` tasks, included by the play that records the bake, after every play that could have written the file. They run the install's own `copy` with `check_mode: true` and fail if it would change anything. The idiom was run on the dev host against two scratch files: it passes on this repo's rules, fails on rocm-ernic's, and leaves the file as it was |
| `--tags configure` | `rocm.ernic.ernic_guest_setup` copies rocm-ernic's rules over the image's whenever its driver path runs (`driver_common.yml`, "Copy the rocm-ernic udev rules"), with no variable of its own to stop it. The configure play now turns that whole path off (`ernic_install_driver: false`, known issue 16), so the copy no longer happens there; the `always` section of the block that runs the role still installs this repo's file, for the caller who turns the path back on with `-e`. In a bake the role never gets that far: it fails at its `lspci` check, which the play's rescue expects, 40 lines earlier |
| the booted guest | `qemu/checks/rocm-golden.sh` reads the installed file between its `ernic naming rules` markers: no `rocm-ernic%n`, no free-index `while`, `NAME="%c"`, and one walk of the PCI devices in each stanza. The playbook-test lane runs the whole check before its configure stage and those lines again after it |

`tests/test_ernic_identity.py` fails a playbook that sets
`rocm_ernic_setup_udev_rules_src` at all, a bake whose last writer of the
file is the collection, a live play that runs the collection's role and does
not end by putting the rules back, and it runs the check's own lines against
both files.

**What the configure play cannot promise**, for a run that turned the role's
driver path back on; with it off, as the play has it, the role copies
nothing. The put-back was in `post_tasks`, which do not run when the role
fails, and such a run does fail on a stack that offers P4 (known issue 16):
measured, it left rocm-ernic's rules on disk, 2928 bytes where this repo's
file is 9647. It is in the block's `always` section now, so it runs
either way; that has not itself been run against a guest. And that copy does not
rename anything while the play runs on a golden guest: the modules are
loaded and the devices named before it starts, and the role's `udevadm
trigger` sends `change` events, which rules matching `ACTION=="add"` ignore.

**batesste-ci-images runs the same check against the published image**, and
`20261117.g80c17f8` fails two assertions of it: these rules, and
`apparmor.service`. That is the check being right about that image.
`20261117.g481c892` passed the whole check in its publish run, before the
push; batesste-ci-images running it against the pulled image has not been
seen.

**On `20261117.g80c17f8`, nothing installed this repo's rules on a
`helios-tray` or `mi355x-server` guest.** Their `quick-start` flavors run no
playbook (`quick_start.VARIANTS` has `None` for both: boot only), so an
eight-ernic guest kept the `%n` rule that image shipped with, for good, and
should have had `rocm-ernic0` and seven NICs under kernel names. That is
read from the rules file and from the next paragraph's analysis. An earlier
version of this section said `vm-quickstart.yml` replaces the rules there.
It does not run there. The repair is the one above, in the bake, and it
reaches those guests now: all three fleet files pull `20261117.g481c892`,
which was baked with this repo's rules; on two pulled one-NIC guests of it
the installed file was the role's byte for byte and the NIC was renamed. No
multi-NIC guest has been booted from either image, so eight renamed NICs on the new one are as unobserved
as one renamed and seven not were on the old; one `helios-tray` boot is
what would confirm it. `rocm-ernic-netcfg.sh` addresses every
NIC by PCI identity under whatever name it has, so a missed rename costs the
name and not the address. The batesste-ci-images qcow2 the fleet files
pulled until 2026-11-17 shipped no rules at all, which is why every consumer
resolves names instead of assuming them, and should go on doing so.

**`NAME="rocm-ernic%n"` could only ever name one NIC per guest.** `%n` is the
kernel number — the trailing digits of the sysname — and every emulated NIC
comes up as `...np0`, so `%n` is 0 for all of them and the second rename fails
with the name already in use. Unreachable while every fleet had one NIC per
guest; `<ernic-per-vm>` made it reachable and the MI355X server shape, at eight
NICs per VM, makes it certain. This repo's rule does not search for the
first free index either, though an earlier version of it did and this
section went on saying so: a free-index search numbers in udev event order,
which is a race, and in the InfiniBand stanza it loses devices outright,
because two workers both settle on index 0 and one rename fails. Both
stanzas now give a device its position among the matching PCI devices, in
bus order, so `rocm-ernic<n>` and `rocm-rdma-ernic<n>` are the same device
by construction and the same one on every boot. The rules file carries the
measurements: 19 of 32 two-NIC guests wrong under the free-index form,
64 of 64 devices right under this one. Note udev applies `NAME=` only at
device-add and will not rename a live interface, so a re-trigger needs the
links down first.

**`rocm-ernic-netcfg.sh` configures every matching NIC**, not just the first.
It walks all of them and derives each address from that NIC's own MAC, which
matches `gen-compose` keying MACs and IPs on the global NIC ordinal — so VM
*n*'s devices land on consecutive addresses from `.11` up. It no longer names
the guest unless the host passed no name, and then from the first NIC only;
see "The guest's hostname".

`ernic_guest_setup` ships its own copy and reloads udev on its driver path —
which is why the configure play, though it now leaves that path off, still
puts this repo's back after the role, see above — and both renames
are confirmed working in CI — the ionic lane's
`Show NIC details` reports `rocm-ernic0` carrying `altname enp0s5np0`, which is
the pre-rename kernel name, and `ibv_devinfo` reports `hca_id: rocm-rdma-ernic0`.
The IB rename shells out to `/usr/bin/rdma`, which the gen-vm guest has.

**Resolve the IB device name from `ibv_devinfo -l`; never hardcode it.**
The rule comes from a guest where only half the rename happened: on
2026-10-16, both guests of a `quickstart` stack from the batesste-ci-images
qcow2 `20261014.geea0485` had the rules file, came up with the netdev as
`rocm-ernic0`, and reported `hca_id: rocep1s0` — the kernel name. Anything
passing `-d rocm-rdma-ernic0` failed there. On the image this repo
publishes the IB rename does fire: both guests of `20261117.g80c17f8`
reported `rocm-rdma-ernic0` at boot, before any playbook, in the one local
run recorded above. That is one image on one host, so the rule stands, and
`quickstart-boot` prints a warning annotation when the name it resolves is
not `rocm-rdma-ernic*`.
`vm-quickstart.yml`, `generate-vm-report.sh` and `quickstart-boot` all
resolve the name, which is why they work on a guest with either.

As of rocm-ernic@3fb74d1 the rules match on four attributes, not two —
`0x1dd8`/`0x1002` plus the subsystem pair `0x1dd8`/`0x5400`. This repo's
Ethernet stanza, with the program that computes the index left out:

```
SUBSYSTEM=="net", ACTION=="add", KERNEL!="rocm-ernic*", \
  ATTR{device/vendor}=="0x1dd8", \
  ATTR{device/device}=="0x1002", \
  ATTR{device/subsystem_vendor}=="0x1dd8", \
  ATTR{device/subsystem_device}=="0x5400", \
  DRIVERS=="ionic", \
  PROGRAM="/bin/sh -c '...'", \
  NAME="%c"
```

rocm-ernic's own copy has the same four matches and ends `NAME="rocm-ernic%n"`.

The subsystem pair is what distinguishes this device from a real Pensando
NIC, because `1dd8:1002` is a real one's id — see the PCI ID section. So a
guest that comes up with kernel names has a device that failed one of four
matches, not two; check all four before re-copying anything.

## The guest's hostname (in-VM)

**A guest's hostname is the name the host calls it by**, and the host tells
it. A stack's guests are `rocm-golden-1` and `rocm-golden-2` to the host:
`<vm-name-prefix>` plus an index, which is the name in `qemu-tool list`, the
`--vm-name` each `qemu-N` service gives `run-vm`, and the `vm_name` label in
Prometheus. Until 2026-11-18 they were `ernic-vm-1` and `ernic-vm-2` to
themselves. Every guest boots an overlay of one image, so nothing inside one
says which it is; `rocm-ernic-netcfg.sh` took an index from the first
emulated NIC's MAC and ran `hostnamectl set-hostname ernic-vm-<n>`, and the
configure stage's `ernic_guest_setup` then made it `rocm-ernic-vm-<n>`.

**With today's published image, `20261117.g481c892`, nothing a user sees
changes.** The guest half is in the image, and reaches guests with the next
one published: `vm-quickstart.yml` installs none of it, and should not start
to. Until then `run-vm` passes a name that nothing in the guest reads, and a
stack's guests are still `ernic-vm-<n>`.

**A stack needs both halves, and the host half is not the `qemu-tool` on the
host.** The `run-vm` that starts a stack's guest is the one inside its qemu
container. `gen_compose._pipx_install` builds it from
`/qemu-tool-src/qemu` when that mount holds a `pyproject.toml`, and
otherwise runs `pipx install qemu-tool`. The mount is `QEMU_TOOL_SRC`,
`../../..` by default, which is a checkout only for `qemu/compose/<stack>/`;
`quick-start` from a checkout stages its own `qemu/` into the stack
directory, and from an installed tool with no checkout stages nothing. PyPI
serves `2.0.0` (asked 2026-11-18), the version string this tree still
carries, and that `run-vm` passes no `-smbios`. So such a stack keeps
`ernic-vm-<n>` on a new image until the first release after `2.0.0` is on
PyPI. Either half without the other changes nothing a user sees — the new
image under an old `run-vm` reads an empty serial and falls back — so the
release and the `<backing-ref>` move can go in either order; the fix shows
when both are in. In a guest, `sudo cat /sys/class/dmi/id/product_serial`
says which half is missing: empty is the host half, and no
`/usr/local/sbin/qemu-tool-hostname.sh` is the image.

**The host half is one argument.** `identity.guest_name_args()` returns
`-smbios type=1,serial=<vm-name>`, and three things add it: `run-vm`, last
on the command line so that a serial in a literal `--nvme` passthrough does
not outrank it, and both of `gen-vm`'s bake boots, so the image's unit meets
a name during the bake. The generated stacks do not change, since the name
was already `run-vm --vm-name`. Read off a guest (QEMU 10.2.1, q35):

| What | Measured |
|---|---|
| `sudo cat /sys/class/dmi/id/product_serial` | the VM name, as typed; mode 0400, root only |
| `sudo dmidecode -s system-serial-number` | the same |
| `sys_vendor`, `product_name` | still `QEMU` and `Standard PC (Q35 + ICH9, 2009)`: only the serial is named |
| `product_uuid` | still the value `-uuid` gave |
| with no `-smbios` | `product_serial` empty, `dmidecode` says `Not Specified` |

- **The system serial number, not `fw_cfg`.** The DMI attributes are in
  sysfs from the first instant with no module; `fw_cfg` would need
  `qemu_fw_cfg`, which is a module on the pinned kernel (it is loaded in the
  guest, so that route works too, it just has one more thing to lose).
- **amd64 only** (`identity.GUEST_NAME_ARCHES`). The one image that reads
  the field is amd64. `qemu-system-riscv64` 10.2.1 on the dev host accepts
  `-smbios` on its `virt` machine; older releases did not (QEMU 9.0 is where
  it arrived, from memory and not checked here), and whether a riscv64 guest
  under U-Boot then sees the table was not tried. No `qemu-system-aarch64`
  is on the dev host to ask. `identity_args()` stays the pair every target
  takes.
- **A comma is doubled**, which is QEMU's escape: `serial=a,b` is refused as
  `Invalid parameter 'b'`. The CLI already refuses a comma in `--vm-name`,
  so this is for a caller that builds a `VMConfig` itself. Spaces and `=` go
  through as they are.
- **cloud-init reads the same field.** `ds-identify` looks at
  `DMI_PRODUCT_SERIAL` for three things: ` ds=nocloud` after a space, a
  serial ending `.brightbox.com`, and one starting `GoogleCloud-`. A VM name
  of one of those shapes turns a datasource on. Any other is ignored:
  measured with the serial set and no seed, `No ds found`, and cloud-init
  disabled by its generator exactly as before; with a NoCloud seed attached,
  `Found single datasource: NoCloud`, by the `cidata` label. The bake's
  first boot is that second case.

**The guest half is `qemu-tool-hostname.service`**, a oneshot that runs
`/usr/local/sbin/qemu-tool-hostname.sh`
([source](ansible/playbooks/roles/rocm_ernic_setup/files/qemu-tool-hostname.sh)).
`rocm_ernic_setup`'s `hostname` tasks install and enable it and run it once,
so a bake fails there if the script cannot write; `network.yml` includes them
first, behind `rocm_ernic_setup_hostname` (on by default) and not behind the
netcfg switch.

- **Its own unit, not a step of netcfg**, which waits thirty seconds for an
  emulated NIC and exits 1 without one: a guest with no NIC has a name too.
  `DefaultDependencies=no`, after `local-fs.target`, wanted by and before
  `sysinit.target`, before `network-pre.target`, cloud-init and
  `rocm-ernic-netcfg.service`. So sshd, the exporters and a DHCP client all
  start under the new name. It ran 1.5 s after systemd started, 6 s into the
  boot.
- **What counts as a name.** Not a placeholder (empty, `Not Specified`,
  `To be filled by O.E.M.`, `0`, all zeros, four or more of one character,
  and the like). On a machine whose `sys_vendor` is `QEMU`: QEMU sets no
  serial of its own, so one that is there was given by whoever started it,
  where a real machine's serial number or a cloud's instance id is not a
  name anyone chose. Then made a hostname by rule: lower case, every
  character outside `[a-z0-9-]` a hyphen, no hyphen at either end, and the
  **last** 63 characters of a longer one, because a fleet's guests differ at
  the end. `Mixed_Case-Name-7` is `mixed-case-name-7`; a name that is
  already a hostname is unchanged.
- **A VM name that is itself a placeholder word is not passed through.**
  `none`, `null`, `empty`, `unknown`, `invalid`, `oem`, `na`, `0` or all
  zeros, four or more of one character (`aaaa`, `1111`) and `123456789` are
  hostnames, and the script cannot tell `run-vm --vm-name none` from a
  serial nobody set. The unit leaves the hostname alone, and the guest
  check fails such a guest, with the script's reason in the error, since it
  does not answer to the name the host calls it by. No stack makes one: its
  guests are `<vm-name-prefix>-<n>`. Run with the worktree script against
  fabricated trees, not on a guest.
- **Written directly**, to `/etc/hostname` and `/proc/sys/kernel/hostname`,
  not through `hostnamectl`: D-Bus is not up that early. `/etc/hosts` gets
  one `127.0.1.1 <name>` line, replacing any that is there. The image had
  none (its hostname resolved through systemd-resolved, and `sudo` did not
  warn either way); the line makes the name resolve without resolved and
  takes out the `127.0.1.1 rocm-ernic-vm-<n>` the collection used to write.
- **Every boot, and the name is kept.** An overlay booted under another
  `--vm-name` follows. A boot with no name leaves the hostname alone, so it
  is whatever `/etc/hostname` holds: the bake's name on a fresh overlay, the
  last name the host gave on a used one.
- **cloud-init is kept off the hostname while the host names the guest.**
  The script writes `/etc/cloud/cloud.cfg.d/99-qemu-tool-hostname.cfg`,
  `preserve_hostname: true`, and removes it on a boot with no name. Usually
  there is nothing to keep off: with no datasource cloud-init is disabled by
  its generator, and for the instance it already knows, `update_hostname`
  logs "assuming user maintained hostname" and changes nothing. A **new**
  instance is the case: its `set_hostname` wrote the datasource's name over
  the host's when forced to run without the file, and did nothing with it.
  Booted for real with a NoCloud seed saying `hostname: from-the-seed`: the
  guest stayed `seeded-guest` with `-smbios`, and became `from-the-seed`
  without.

**`ernic-vm-<n>` is now the name of a guest the host did not name.**
`rocm-ernic-netcfg.sh` asks the script (`qemu-tool-hostname.sh name`, which
changes nothing) before it renames, so a guest started by something that
passes no name, or an image without the script, is named from its first NIC
as before. And **the configure stage no longer renames the guest**:
`ernic_set_hostname: false` in that play's vars, which is the `when:` of
both tasks in the collection's `nic.yml` that touch the name.

What a guest gets from each way of starting it:

| Started by | Hostname |
|---|---|
| `run-vm`, a stack, `quick-start` | the VM name, made a hostname |
| `gen-vm --bake` | the bake's name, which cloud-init had already set |
| plain `qemu-system-x86_64` | what `/etc/hostname` holds, then `ernic-vm-<n>` if it has an emulated NIC |
| a libvirt domain, one from `--convert-to-libvirt` included: the XML it writes carries no serial | the same, by reading; not run |
| any of those with `-smbios type=1,serial=<name>` added by hand | that name |

`qemu/checks/rocm-golden.sh` asks in its `guest hostname` block: the script
is there, the unit is enabled and ran, and the hostname, `/etc/hostname` and
the one `127.0.1.1` line are the name. A serial that is already a hostname
is compared without the script, so a script that wrongly answered "no name"
does not pass as a boot nobody named; a boot with no name is noted and
passes. `ROCM_GOLDEN_EXPECT_HOSTNAME` is for a caller that knows the name:
the playbook-test lane runs the block again after the configure stage with
`$VM_NAME`, which is where the rename used to happen.

**Only the publish lane boots a baked image under a name that is not the
bake's**, and that is the boot that shows the unit renaming anything. The
image's `/etc/hostname` holds the bake's name, so a guest started under that
same name passes the check on what the bake wrote:

| Boot | Baked as | Started as | Shows |
|---|---|---|---|
| playbook-test, behind its stack | `rocm-golden-test-1` | `rocm-golden-test-1` | netcfg and the configure stage leave the name alone; not that the unit set it |
| publish, no device | `rocm-golden` | `rocm-golden` | the unit ran and the name agrees; not that it set it |
| publish, the gate stack, with a NIC | `rocm-golden` | `rocm-golden-1` | the rename, with netcfg and the configure stage after it |

The gate stack's step, "Verify the guest took the name the stack gave it",
runs the block with `ROCM_GOLDEN_EXPECT_HOSTNAME=${VM_NAME}-1` after the
configure stage and ahead of the push, so an image whose guests keep the
bake's name, or take `ernic-vm-1`, is not published. Its qemu container
builds qemu-tool from the staged checkout (`<qemu-tool-src>`), not from
PyPI. The lane never runs from a pull request, so before a merge the rename
is covered by `tests/test_guest_hostname.py` alone; making the playbook-test
lane boot a second overlay under another name would close that, and was not
done. Neither step has run.

Three things to know:

- **The first boot under a new name logs its first seconds under the old
  one.** systemd sets the hostname from `/etc/hostname` before any unit
  runs, so kernel and early-boot lines in that boot's journal carry the
  previous name (715 lines, up to the 6 s mark, on the measured guest).
  Later boots are right from the first line.
- **Two guests can have one name.** A scratch `run-vm --vm-name
  rocm-golden-1` beside a stack shows twice in `qemu-tool list`, and
  `inspect` takes one of them.
- **Proven on one guest with no device**, an overlay of
  `20261117.g481c892` with the role's network tasks applied, booted nine
  times: by `run-vm`, or by hand from its `--dry-run` command line where an
  argument had to change. The name set, followed under a second and third
  `--vm-name`, left alone with the argument removed, with a placeholder and
  under another vendor, and the two seeded boots above. Not run: a bake, a
  stack with two guests and
  NICs, the netcfg path with a NIC (its tests run the script against a
  fabricated sysfs with `ip` and `hostnamectl` stubbed), and
  `ansible-lint`, which does not start on the dev host.

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

## The amdgpu probe (in-VM): firmware first, then the probe

The golden image carries no `/lib/firmware/amdgpu/ip_discovery.bin`, on
purpose: it belongs to the rocjitsu build serving the guest. `amdgpu-probe`
loads the driver with `discovery=2`, which reads it. So the order on a guest
with a GPU is fixed:

1. **Firmware, from the host.** `qemu-tool quick-start test` (it runs
   `vm-quickstart.yml`, whose first GPU step is `rocjitsu_setup`'s `firmware`
   tasks with `rocjitsu_setup_firmware_live`, and then probes), or
   `ansible-playbook -i HOST, vm-rocm-golden.yml --tags firmware`, or a blob
   written by `rj-ip-discovery gfx1250 FILE` in the rocjitsu container
   serving the guest and copied in by hand. The command takes an output
   path and prints nothing to copy: `rj-ip-discovery gfx1250` alone exits 1
   with `Required arguments: <generation> <output-path>`. The probe's
   refusal, `WELCOME.md` and the firmware directory's README in
   `20261117.g481c892` and every earlier image spell it without the path;
   corrected in the role, so in the next published image.
2. **The probe**, `sudo amdgpu-probe` in the guest — which step 1's first
   form has already done.

The login hint used to skip step 1: it saw a GPU and no driver and said
"sudo amdgpu-probe". The probe then failed in the driver (`-22` in dmesg)
and left amdgpu **resident with no device bound**; a second run said only
"no render nodes found", and `rmmod` of this module has been seen to GPF. The
scripts are `roles/rocjitsu_setup/files/amdgpu-probe` and `amdgpu-probe-motd`.

**`amdgpu-probe --status` prints one of six words**, changes nothing and
exits 0. The hint calls it and restates none of the detection:

| Word | State | Hint |
|---|---|---|
| `absent` | no AMD accelerator (`0x1002`, class `0x1200xx`) on the bus, amdgpu not loaded | silent |
| `nofirmware` | a GPU, amdgpu not loaded, no blob | cannot be probed yet; the host command |
| `ready` | a GPU, amdgpu not loaded, blob staged | `sudo amdgpu-probe` |
| `loaded` | amdgpu resident with the script's parameters and no accelerator left without a driver, or a load with those parameters still running | silent |
| `unbound` | resident with the right parameters, an accelerator has no `driver` link: a probe failed | the host command, then `sudo amdgpu-probe`; never rmmod |
| `stranded` | resident with other parameters | reboot |

`stranded` outranks `unbound`. `loaded` used to be printed for `unbound` too.

**A load in progress is `loaded`, not `unbound`.** `/sys/module/amdgpu` is
there for the whole of module init and the device probe runs inside it, so
until `modprobe` returns a GPU has no `driver` link. The script reads
`/sys/module/amdgpu/initstate`: anything but `live` (`coming`, or `going`
during an unload) is `loaded` from `--status`, so the hint stays silent, and
a plain run says the module is not live yet and exits 1 without writing to
`bind`. A tree with no `initstate` file is taken as live. That the kernel
shows `coming` for the length of the probe is its documented behaviour, not
something run here.

What the probe itself does in each:

- **`nofirmware`: refuses**, exit 1, before `sudo` or `modprobe` is reached,
  and prints the three ways to stage the blob. With no GPU on the bus it
  loads as it always did; the guard is about a probe that would fail.
- **`unbound`, blob present: binds without a reload.** It writes each
  unbound accelerator's address to `/sys/bus/pci/drivers/amdgpu/bind` — the
  write the last task of `firmware.yml` makes, limited to the accelerator
  class — and reports per device whether a `driver` link appeared, then
  whether there is a `/dev/dri/render*` node. Exit 1 if any did not bind.
  The write is `sudo tee`, of a name that has the form of a PCI address and
  nothing else.
- **`unbound`, blob absent:** says the blob is what is missing and that
  nothing was changed, exit 1.
- **`stranded`: lists the parameters that differ**, exit 1, and says
  reboot. It used to offer `sudo rmmod amdgpu && sudo amdgpu-probe` first,
  against its own hint for the same state.
- There is no rmmod path and no rmmod advice, and none is to be added. The
  playbook-test lane's retry loop used to `rmmod amdgpu` between attempts;
  it runs the probe again, which is the bind above.

**"Blob present" is four names**: `ip_discovery.bin` and `ip_discovery.bin.xz`
in `/lib/firmware/amdgpu` and in `/lib/firmware/updates/amdgpu`, each a
non-empty regular file (`-f` and `-s`: a directory of that name passes `-s`
alone, and a symlink to a real blob counts).
The role installs only the first. The other three are accepted because the
repo already relies on them for the packaged blobs (`updates/` searched
first, shipped as `.xz`); `.zst` and the per-kernel directories are not,
since nothing here establishes this kernel reads them. A guard that is too
narrow refuses a probe that would have worked, and there is no override
flag, so widen it if a blob is ever installed somewhere else.

**The test seam is `AMDGPU_PROBE_TEST_ROOT`**, a root prefix for every path
both scripts read, honoured only when that directory holds the marker file
`.amdgpu-probe-test-root`; the variable alone is ignored. `sudo` resets the
environment and pam_motd does not pass a caller's, so neither reaches a real
run. With a test root the `bind` write is a plain `tee`, never `sudo tee`,
so the seam cannot have root write into a tree the caller built; the load
is still `sudo modprobe`, which is why every caller asks `--status` before
it runs the probe. `tests/test_amdgpu_probe.py` runs both scripts through it
in every state, with stub `sudo`, `modprobe` and `tee` first on `PATH`. The
same seam is how
an image without the guard is kept from being published: `rocjitsu_setup`'s
`amdgpu.yml` during the bake, and `qemu/checks/rocm-golden.sh` on the booted
image before the push, build a scratch tree with a GPU and no blob and
require `nofirmware`, a refusal that calls nothing, and a hint that names the
host command and does not offer the probe. Each asks `--status` first, so a
copy with no seam fails there and never reaches the run that would load a
driver.

**A with-device run of `rocm-golden.sh` passes
`ROCM_GOLDEN_EXPECT_PROBE=nofirmware`, not `ready`.** The check asserts the
image has no blob, so `ready` cannot be true where it runs. The playbook-test
lane was changed; a caller outside this repo that still passes `ready` will
fail against a new image.

**`vm-quickstart.yml` probes an `unbound` guest, and waits for a GPU, not
for `/dev/kfd`.** amdgpu creates `/dev/kfd` before it registers with the PCI
bus, so the node is there after a probe that bound nothing. The play gated
the probe on that node being absent and then waited for it, so on a guest
that was unbound with a blob that already matched — the firmware tasks
re-bind only when they installed something — it skipped the probe, passed
the wait, and failed in the GPU tests with nothing naming the cause. It now
asks `amdgpu-probe --status` of a resident module and runs the probe when
the answer is `unbound`; any other resident state is left alone, as before,
because a published image's probe exits 1 on a second run when it finds no
render node and a consumer may have loaded the driver with their own
parameters. The wait is for a named node under
`/sys/class/kfd/kfd/topology/nodes`, the test the playbook-test lane and the
report script already use, and its failure prints the status word.
`--status` is asked only when `/dev/kfd` exists: a probe from before the
flag takes it for a plain run, which on a resident module loads nothing.

**This is in `20261117.g481c892`, which the fleet files pull, and not in
the image before it.** The image
published as `20261117.g80c17f8` has the old hint and the old probe: a
guest from it says "sudo amdgpu-probe" with no blob staged, and doing so
strands it as above. `qemu-tool quick-start test` from the host recovers
such a guest without a reboot — the firmware tasks install the blob and
re-bind — as long as nobody has unloaded the module. The exception is a
guest from that image whose blob was then staged by hand: the install
changes nothing, so nothing re-binds, and its probe reports `loaded` and has
no bind of its own. The wait above fails on it and says so; it needs the
guest restarted, or the `bind` write above made by hand.

What the publish run of `20261117.g481c892` ran of this: the bake's two
assertions and the guest check's, each against the installed scripts and a
scratch tree, and the probe's ordinary load of amdgpu in the gate's GPU test
stage, after the firmware stage had staged the blob. Not in that run: the
refusal with a real GPU on the bus and no blob, the hint at a real login,
and the `bind` recovery after a failed probe. The playbook-test lane asks
the first of those, since it runs the check with the GPU attached and
`ROCM_GOLDEN_EXPECT_PROBE=nofirmware`, and it passed on the pull request
that made this change; its log was not read for this. All three have since
been seen on pulled guests of that image, by hand on the dev host: the
refusal with `lsmod` counting no amdgpu before or after, the hint at a real
pty login, and the recovery from a real failed probe both ways, through
`quick-start test` and through the probe's own `bind` after a blob staged
by hand, with no reload and no oops. See "`20261117.g481c892`, pulled and
booted" above. Not seen: more than one GPU on the bus, and the `stranded`
state on a real guest.

## Known issues / open bugs

See `rocm-ernic-enablement.md` for the full tracking list. Short version:

1. Worker `server_ip` ARP hijack when VM IP = `192.168.100.1` — use `.11`/`.12`
2. The collection is not on Galaxy and will not be — upstream renamed it
   `rocm.ernic` (0.3.0) and stopped publishing; what Galaxy still has is
   0.1.0 under the old name, the pre-ionic collection. `requirements.yml`
   pins an upstream git SHA. Upstream means to attach a built collection to
   a GitHub release tagged `ernic-ansible-v<version>`; no such tag exists
   yet, so there is nothing but a commit to pin
3. **Killing perftest mid-run breaks VM device context** — killing `ib_send_bw`
   or any perftest process mid-operation (SIGTERM/SIGKILL) leaves the guest
   RDMA driver in a broken state. `ibv_devinfo` reports "Failed to open device"
   or "wasn't found". `rmmod`/`modprobe` hangs for 2 min (DSR timeout) and the
   device does not recover. **Full stack restart required** (`docker compose down
   && up`). Avoid killing tests; let them run to completion or use `ib_send_bw
   --iters 100` to keep runs short.

4. **A mesh-manager crash requires full stack restart** — if the manager
   (`ernic-1`; `ernic-hub` in the old hand-written two-VM stack) crashes and
   restarts (visible in `docker compose logs ernic-1` as a re-initialization
   from the top), the guest driver enters a DSR initialization timeout
   (`-ETIMEDOUT`) on the next `modprobe ionic_rdma`. The vfio-user session
   held by QEMU becomes stale and cannot be recovered by reloading modules alone.
   **Fix: `docker compose down && docker compose up`** (all containers, not just
   the qemu ones). Restarting only the QEMU containers is insufficient because
   every worker also needs a fresh TCP mesh connection to the new manager.

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

6. **Pinning the collection does not pin the sources** — `ernic_source`
   clones rocm-ernic again on the controller, at `ernic_source_repo_version`,
   which has no default: the role fails when it is unset. Every play that
   runs a `rocm.ernic` role sets it to
   `ernic_source_pin`, in
   [`vm-rocm-golden.yml`](ansible/playbooks/vm-rocm-golden.yml). That is on
   purpose a different commit from the collection's; see "Two pins of
   rocm-ernic".
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

    That is the collection's role on its driver path, which the configure
    stage here no longer runs (issue 16). A guest baked by this repo gets the
    same from the image at every boot: `rocm_ernic_setup`'s `naming` tasks
    write the `modules-load.d` entry and its `network` tasks the unit that
    brings each NIC up and addresses it.
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
    and burned ids. Every generated stack therefore pins a static address on
    every service and resolves no service names at run time. A fleet
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
    107G of 503G). Budget by `<vm-vcpus>` × `<vm-count>` ≤ `nproc` plus about
    25%, not by RAM, and remember `<vm-vcpus>` cannot go below 4 on an ernic
    stack.
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
    `<ernic-nofile>` (default 65536). That does **not** fix the leak, which is
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
    by issue 14 regardless of what `<ernic-nofile>` says.
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
    testing still needed the `ernic_guest_setup` configure play, which built
    the driver in those days. It builds none now, and refuses a guest this
    repo did not bake (issue 16).
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
    where N is the *registered* size, not `<vm-count>`: 42 sockets measured at
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
      minutes** and issue 15's `<ernic-nofile>` of 65536 in about **22 hours** — so
      raising the limit buys a working day, not an indefinite reprieve. A
      fleet left above the wall will eventually reach EMFILE whatever the
      limit is; the fix is to size the fleet below it.

14. **The TCP backend resolved every GID to the same node — fixed upstream.**
    This is why inter-node RDMA never worked on a fleet larger than two nodes,
    and why the hand-written two-VM stack (`vfio-user-ernic-2vm`, since
    deleted) was the only stack that ever appeared healthy: with a single
    peer, the wrong answer is also the right one.

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
    this repo now supplies as `ERNIC_TCP_GUEST_GIDS` — see the default stack's
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
    happened: the tag read `qemu.31e3d06`, which is this patch, and the fork
    has been rebuilt on later tips since. So the choice is now a real one —
    `qemu/stacks/mi355x-server.xml` pins the fork and gets P2P DMA;
    `qemu/stacks/rocm-golden.xml` and `helios-tray.xml` pin the stock image and do
    not. (`overlay-mi100.xml`, which used to carry the fork pin for the 32-VM
    fleet, is deleted; a local overlay for that fleet names the fork itself.)

    What has NOT changed is that the stock image cannot do it, so a stack
    that needs one vfio-user device to DMA into another's BAR must pin the
    fork deliberately. Do not be misled by `pci-mmio-bridge`, which the fork
    images *also* have and stock does not — it is a different mechanism in
    the same branch, not the one P2P DMA needs.

    Untested here. That the fork tag carries the patch was read from the tag
    name, and for the tag pinned today from the comment beside it in
    `mi355x-server.xml`; nothing in this repo has yet run a P2P DMA workload
    against it.

16. **`ernic_guest_setup` does not build SRQ, without SRQ there is no
    one-sided RDMA through UCX, and run after the bake the role takes SRQ and
    P4 away.** SRQ ships as two separate upstream roles —
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

    **Three things must come from one tree, and "both halves" undercounted
    them.** The kernel half is `ionic-ernic` with `patches/srq` and
    `patches/p4`; the provider half is `patches/rdma-core`, installed under
    `/usr/local`; and the device half lives in the ernic server container. The
    first two are built from the checkout at `ernic_source_pin`, so that pin
    and the `ernic.<sha>` in `<ernic-ref>` move together. The collection SHA
    is not one of the three: it is the roles that run those builds, and moved
    alone on 2026-11-17 (see "Two pins of rocm-ernic"). `ernic_p4` hangs off
    the first: it is built
    against that ionic and the kernel compares symbol versions when it loads
    (`CONFIG_MODVERSIONS=y`, read from the `.config` in the 7.2.6-070206
    mainline headers `.deb`, as is `CONFIG_MODULE_SRCVERSION_ALL=y` below).

    **The configure stage replaced the baked kernel half on every run, until
    2026-11-17, and nothing said so.** Read from the collection at `90434a2`,
    and all of it still so at `bd90234`, where
    `tests/test_configure_keeps_baked_driver.py` now reads it:

    - `ernic_guest_setup`'s `main.yml` includes `driver.yml` when
      `ernic_install_driver` is set, which is its default, and `driver.yml`
      is `driver_ionic.yml` then `driver_common.yml` with no switch between:
      the build and the reload come as one.
    - `driver_ionic.yml` runs `fetch-ionic-sources.sh` with `--patches-dir
      /tmp/ionic/patches` plus `ernic_guest_ionic_extra_patches`, which
      defaults to `[]`: the base series, one patch. The tree is
      `/var/tmp/ionic-src`, the **base** tree the bake's first play left and
      `image_slim` keeps, and its sentinel names the repository and that same
      series, so the script says `nothing to do`. The SRQ + P4 tree was never
      in the guest under that path: `ernic_srq_dkms` fetches it on the
      controller and stages it under `/var/tmp/srq-p4`, which the play
      removes.
    - It then runs `setup-ionic-dkms.sh`, whose package is `ionic-ernic` at a
      version taken from the kernel ref, `7.2.6`. That is the package the SRQ
      + P4 play installed. The script removes the registration (`dkms remove
      ionic-ernic/7.2.6 --all`), deletes `/usr/src/ionic-ernic-7.2.6`, stages
      the base tree there, and builds and installs it.
    - It unloads `ionic_rdma` and `ionic`, and `driver_common.yml` loads them
      again: the base build.

    Two CI runs of 2026-11-17 show it, the playbook-test run 44188004072 (at
    `6ad9599`) and the publish run 44191151977 (at `25bb72b`). In the
    configure stage of each: "Fetch and patch the upstream ionic sources" is
    `ok`, "Install the ionic-ernic DKMS package" is `changed` and takes 19 s
    and 48 s, "Unload any running in-tree ionic modules" is `changed` for
    `ionic_rdma`, and "Load the guest kernel modules" is `changed` for
    `ionic`. `changed` alone would prove little, since the install task is
    `changed_when: true`; the seconds are a build. **Neither log contains
    `max_srq`.** The role prints `ibv_devinfo | head -20` with no `-v`, and
    the lanes' own step looked for `PORT_ACTIVE`.

    **What the guest was left with, measured 2026-11-17.** `max_srq` had
    never been read after a configure stage before this change. It was read
    then, on one guest booted from the pulled `20261117.g481c892` (kernel
    7.2.6-070206-generic), a fresh overlay for every run, with the old
    playbook taken from a `git archive` of `aff9955` and the lanes' own
    invocation (`--tags configure,firmware`). At boot, every time: `max_srq`
    256, `PORT_ACTIVE`, all three modules under `updates/dkms`, file and
    loaded `srcversion` equal (`ionic` `44164456D34C01FB438D570`,
    `ionic_rdma` `24E57766CB83B79E273C7B0`, `ernic_p4`
    `66D2E2195859E80AE848D07`). **The answer depends on whether the stack
    offers the guest P4**, which the sources did not suggest:

    | Stack | Old stage | `max_srq` after | Driver after |
    |---|---|---|---|
    | no P4 (`<p4-enable>false`: both lanes) | green, rc 0, 92 s | **0**, port still `ACTIVE` | `ionic` `87C1704273B10896BAA6924` and `ionic_rdma` `58788FA0F3A54BD064AC464`, on disk and loaded; no `ionic_create_srq`; no P4 text in `ionic.ko`; `modprobe ernic_p4` fails, `Invalid argument` (dmesg: `disagrees about version of symbol ionic_adminq_post_wait`) |
    | P4 offered (`rocm-golden.xml` as shipped) | **fails**, rc 2, 63 s, at the role's "Load the guest kernel modules": `could not insert 'ionic_rdma': Invalid argument` | `No IB devices found` | the base build on disk; the baked `ionic` still loaded, `ionic_rdma` not loaded at all; `/dev/ernic-p4` and `ionic.p4.1` still there; rocm-ernic's naming rules left in place |

    The second row is the mechanism the first hides. With P4 offered,
    `ernic_p4` is loaded and holds `ionic`, so the role's unload removes
    `ionic_rdma` alone, and the rebuilt base `ionic_rdma` then fails symbol
    versions against the baked `ionic` that is still loaded. With no P4
    nothing holds `ionic`, both modules are swapped, and the stage is green
    on a guest with no shared receive queue.

    So in the lanes the inference held exactly: every with-device step of
    both lanes after the configure stage ran on a driver the image does not
    carry, with `max_srq` 0, and said nothing. Someone following the README's
    `--tags configure` against the default stack got a failed play and a
    guest with no RDMA device instead, which is at least loud. `dkms status`
    shows none of it: its three lines (`amdgpu`, `ernic-p4/0.2.0`,
    `ionic-ernic/7.2.6`) were identical before and after every run. The
    published qcow2 was not touched: what is published is the bake, and the
    stage runs on an overlay.

    The new stage, same guest, same invocation: rc 0 in 82 s with P4 and
    52 s without, no "Install the ionic-ernic DKMS package" task, `max_srq`
    256 and `PORT_ACTIVE` after, file and loaded `srcversion` unchanged,
    `rocm-ernic0` holding both `192.168.100.11/24` from the boot unit and
    `192.168.200.10/24` from the role. Both lanes' assertion steps, run as
    written after it in each shape, passed; the same driver step after the
    old stage in the lane shape failed with four `FAIL` lines. With
    `-e ernic_install_driver=true` the new stage fails as it should: at the
    last check, with the blame, on a stack with no P4 (123 s, `max_srq` 0),
    and inside the role at `modprobe ionic_rdma` on a stack with P4 (70 s).
    That last run is why the put-back and the last check are in an `always`
    section; see below.

    What changed:

    - The configure play includes `ernic_guest_setup` with
      `ernic_install_driver: false`. That leaves the role's preflight, the
      memlock limit and the NIC address. Nothing in the stage fetches the
      ionic sources any more, so its git rewrite, the removal and the commit
      check are gone from that play; the bake keeps all three.
    - `rocm_ernic_setup`'s `driver_load` tasks are the half of the driver
      path a live guest still wants: `modprobe` of what is not loaded, every
      emulated NIC up, every RDMA port active. No rocm-ernic udev rules, no
      `update-pciids`, nothing unloaded.
    - `driver_check` tasks run `files/ernic-driver-check.sh` in the guest.
      The stage runs them first with `installed` and last with `live`; a
      failure of the last one whose verdict is "has been replaced" says the
      stage replaced the baked SRQ + P4 driver, and a device that offers no
      SRQ is not blamed on the stage. A guest this repo did not bake fails
      the first: there is no driver for the stage to configure, and it
      builds none.
    - The role runs in a block, and the naming put-back and the last check
      are that block's `always` section, not `post_tasks`. They were
      `post_tasks` first, and the run with the driver build turned back on
      against a stack that offers P4 showed why that is wrong: the role
      fails at its own `modprobe`, tasks after a failed one do not run, and
      the guest was left with rocm-ernic's naming rules and no word about
      the driver. The move to `always` is pinned by tests and has not been
      run against a guest.
    - The bake records `ionic_srcversion`, `ionic_rdma_srcversion` and
      `ernic_p4_srcversion` in `/etc/ionic-srq-p4.json`, and runs the
      `installed` stage against what it has just written.
    - Both lanes run the step "Verify the configure stage left the baked SRQ
      + P4 ionic driver loaded" straight after their configure step, the
      publish workflow well ahead of its push. It sends the same script over
      ssh.

    What the script asks, and why each is asked that way. Nothing here needs
    a debug tool the image lacks; `nm` and `zstd` are what the guest check
    already uses.

    | Question | How | Why not something simpler |
    |---|---|---|
    | is the installed module the DKMS one | `modinfo -n` is under `updates/dkms` | |
    | is it the build the bake made | `modinfo -F srcversion` equals the record | `CONFIG_MODULE_SRCVERSION_ALL=y`, so every module carries one, DKMS builds included. A rebuild from the same sources has the same value, which is the right answer: it is the same driver |
    | is that what is loaded | `/sys/module/<m>/srcversion` equals the file's | a stale module, from an initramfs or from before a rebuild, passes every check of the file |
    | SRQ | `nm` shows `ionic_create_srq`, the whole name | `strings \| grep -i srq` hits BTF names on a module with no SRQ; see `qemu/checks/rocm-golden.sh` |
    | P4 in ionic | the module holds the text `Failed to add p4 aux device` | ionic exports nothing new for P4. `ionic_adminq_post_wait`, which the bake's "P4 symbol" task counts, is in the base driver too: that task says `ernic_p4` can link, not that the series was applied |
    | `ernic_p4` | `modprobe ernic_p4` succeeds | symbol versions are compared at load and nowhere else. It binds nothing unless ionic made a P4 device, and the script unloads it again when it loaded it and there is none: left in, it holds `ionic` (measured: `ionic 233472 2 ernic_p4,ionic_rdma` after the check, `1 ionic_rdma` before), on exactly the stacks where nothing else loads it |
    | the device | every emulated NIC, by the four-field tuple, reports `max_srq` above 0 in `ibv_devinfo -v` | |

    `max_srq` 0 with the driver intact is reported as the device's doing,
    with `<ernic-ref>` named, not as a rebuild. `/dev/ernic-p4` is asked for
    only when the device offers the guest the P4 engine, which rocm-ernic
    does exactly when given `--p4-guest-scope` (`ionic_device_attach_p4`
    registers the handler the capability bit is keyed on). The lanes read
    that off the stack they rendered; a by-hand run takes the script's
    `auto`, which asks for the node when ionic created the auxiliary device.
    **Both lanes render `<p4-enable>false`, so no lane asserts
    `/dev/ernic-p4` after a configure stage.** The step would start to, with
    no other edit, the day an overlay stops turning P4 off.

    Not chosen: handing the role the SRQ and P4 series through
    `ernic_guest_ionic_extra_patches` so that a rebuild comes out the same.
    The base series is always applied first and is the same patch as
    `srq/0009`, so `git am` would meet it twice; the sentinel would differ,
    so every configure run would fetch; and a rebuild is a second definition
    of the driver that can drift from the bake's, at 19 to 48 s a run.

    Left as it was: the bake's first play still builds the base `ionic-ernic`
    that the SRQ + P4 play replaces a few plays later. That is one wasted
    build per bake and harms nothing. Turning it off there is a larger
    change than here: that play's mirror rewrite, its source check of
    `/var/tmp/ionic-src` and its rescue are all written around the role's
    fetch, build and failed device check.

    A guest an earlier configure stage already ran on still carries the base
    driver in its overlay. The check fails it and says so; a new overlay on
    the baked image carries the SRQ + P4 driver again.

    **`vm-quickstart.yml` never had the fault.** What `qemu-tool quick-start
    test` runs on pulled guests includes no role of the collection, builds
    nothing with DKMS and unloads no module: of the ernic it installs the
    naming rules, an address and the exporter, and it says of the modules
    "No module loading here". `tests/test_ionic_source_mirror.py` fails any
    playbook but the golden one that names `ernic_guest_setup`,
    `ernic_srq_dkms` or `ernic_p4_dkms`. It does not ask the driver check
    either, so a pulled guest's `max_srq` is still read by nothing in CI.

    `tests/test_configure_keeps_baked_driver.py` holds the collection facts
    above against the installed collection, the shape of the play, the
    script against a fabricated guest (a baked one, the rebuilt one, a stale
    module, a device with no SRQ), and the lane step, which it also runs
    with a stand-in `ssh`. Restoring the old play, turning the switch back
    on, dropping or softening either lane's step each turn it red; that was
    tried on a scratch copy.

    **What has run against a guest, and what has not.** The by-hand runs
    above settled what the first version of this note listed as unproven:
    the baked modules carry a `srcversion` and sysfs shows the same one;
    `ionic.ko` holds the P4 warning text; `nm` lists `ionic_create_srq`;
    `modprobe ernic_p4` succeeds with no P4 device; the role with its driver
    path off leaves `rocm-ernic0` addressed and the port active; and
    **`max_srq` is 256 after the configure stage**. The script passed on the
    fresh guest with `ERNIC_DRIVER_EXPECT_P4=device`, printing three
    `records no srcversion` notes, since that image predates the record. The
    recorded-srcversion path was exercised with a record carrying the baked
    values on a rebuilt guest: `the installed DKMS build has srcversion
    58788...; the bake recorded 24E57.... It was built again after the bake`.

    Still unproven:

    - **The bake side.** No bake has run from this change, so nothing has
      written the three `*_srcversion` fields or run the `installed` check
      straight after. If the P4 text is not found there the bake fails at
      that check, which is the cheap place to find out.
    - **What changed after the guest runs**, all of it covered by unit tests
      against a fabricated guest only: the `always` section; the blame made
      conditional; the unload of `ernic_p4`; the record read off one line
      (a controller on ansible-core 2.16 writes `/etc/ionic-srq-p4.json`
      as a single line, where 2.21 writes it as the playbook has it, both
      measured with the task's own `copy` shape; the first `recorded()`
      read it by line, so every srcversion came back empty and the
      comparison with the bake fell away to a note; CI's controller is
      newer); `nm` missing reported as that and not as a rebuilt driver; and
      `driver_load` saying what it looked for when a guest has no emulated
      NIC, which the collection's preflight used to say and now comes too
      late to.
    - No CI lane has run it, one guest only, so no mesh worker and no
      inter-guest RDMA, and `rc_verbs` was not read: `ucx_info` is not in
      the guest.

    The role with its driver path off still stages `/tmp/ionic` in the guest
    (patches and two scripts) though nothing is built from it. It no longer
    sets the hostname to `rocm-ernic-vm-<index>`: the play turns that off
    (`ernic_set_hostname`), see "The guest's hostname". And `/var/tmp/ionic-src` has no
    reader left once the bake is over; `image_slim` still keeps it, so the
    published image does not change shape with this fix. Dropping it
    (`image_slim_drop_ionic_src`) is a decision about the image.

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
    guests were up, SSH answered, and the fleet script of the time
    (`scripts/fleet.sh`, since removed) called them ready, because it polled
    sshd. Nothing reported a wrong id; the only symptom was
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
    - Readiness is the tuple *and* an address, not sshd: `quickstart-boot`
      asserts the full tuple and `PORT_ACTIVE`, and `scripts/check-vm-devices.sh`
      checks what `list` and `inspect` report against the live guests.
    - `tests/test_ernic_identity.py` pins all of it.

    **Where a looser match is still right:** driver binding. `ionic`'s table
    is `pci:v00001DD8d00001002sv*sd*bc*sc*i*` — it wildcards the subsystem
    because the same driver drives real Pensando cards, and it cannot tell
    them apart. That is upstream's and should not be tightened. udev is the
    only layer that can discriminate, so the tuple discipline belongs there
    and in anything scanning sysfs or `lspci`, not in the driver.

## SDXI metrics

Each golden guest exports its SDXI device's counters to Prometheus through
node_exporter's textfile collector, the way the GPU presence metrics get
there. `roles/sdxi_setup/tasks/metrics.yml` installs
`/usr/local/bin/sdxi-textfile-metrics.py`, a oneshot service and
`sdxi-textfile-metrics.timer`; the script writes
`/var/lib/prometheus/node-exporter/sdxi.prom`, the `fleet-node` job scrapes
it, and the **SDXI** row of the home dashboard reads it.

| Series | Meaning |
|---|---|
| `sdxi_up` | 1 when every configured context was read, else 0. Always written |
| `sdxi_<stat>_total{context="N"}` | the 14 counters of `sdxi_cli.ioctl.STAT_NAMES`, per context; absent when `sdxi_up` is 0 |

29 series with the default contexts. Two role variables:
`sdxi_setup_metrics_contexts` (`[0, 1]`) and `sdxi_setup_metrics_interval`
(30, seconds; the amdgpu timer's). `sdxi_setup_metrics: false` leaves it all
out, and a guest with no node_exporter is skipped with a message.

What the numbers mean, measured on a live golden guest by the owner on
2026-11-18, not read off the SDXI repo:

- **Context 0 is the driver's admin ring; every client operation — copy,
  compress, decompress, elementwise — is counted in context 1.** The
  dashboard's throughput panels select `context="1"` for that reason.
  `num_cxt_start` is counted on context 0, which issues it.
- **`bytes_compressed` counts the INPUT of compress and `bytes_decompressed`
  the OUTPUT of decompress**, so both are uncompressed bytes. The counter
  table in the SDXI repo's own AGENTS.md says input for both and is wrong
  for decompress: its `src/sdxi_regs.h` says output and
  `src/sdxi_engine.c` adds `out`.
- A failed operation increments `num_errors`.
- **The counters live in the SDXI server and restart from zero with it**,
  so across a stack restart a series goes backwards. `rate()` and
  `increase()` treat that as a counter reset; a graph of the raw value
  steps down.

**The counters are not read through `sdxi_perf`.** That module serves the
same two registers through debugfs, but it is a second PCI driver for the
same device and cannot bind while `sdxi_char` holds it, it knows 12 of the
14 counters, and it reads the select/value register pair under no lock
`sdxi_char` shares. It stays installed and blacklisted.

**The unit runs as root**, as `amdgpu-textfile-metrics.service` does. The
collector directory is root's, mode 0755, as the package and
`rocjitsu_setup` leave it, and an atomic replace needs write on the
directory, not on the file. A user in the `sdxi` group could open the
device and could not write the result without loosening a directory this
role does not own.

**Sampling takes the driver's one lock.** Read from
`drivers/sdxi_char/sdxi_char.c` at `sdxi_source_pin`:

- `open()` and `close()` do nothing to the device. `sdxi_char_fops`
  (line 1118) has no `.open` and no `.release`; both contexts are set up
  once, in `sdxi_hw_init()` at probe (line 1217 submits the one
  `CXT_START`). A sample starts and stops no context.
- The STATS ioctl takes `sdev->lock` (line 600), the mutex every ioctl
  takes and a data-path ioctl holds for its whole operation, `poll_csb()`
  included (line 159, up to `CSB_TIMEOUT_MS`, 5 s). So a sample waits for a
  running copy and the next copy waits for the sample: `handle_stats()`
  (line 483) is 14 register writes and 14 reads per context, each trapped
  and forwarded to the server. Nothing is corrupted; one operation per
  sample is delayed by that long. How long was not measured.
- The io_uring path submits under `mutex_trylock()` (line 753) and returns
  `-EAGAIN` when a sample holds the lock. What io_uring then does was not
  verified here; from memory it retries once from a worker thread and a
  second collision completes the request with `-EAGAIN`. `sdxi-cli`'s
  ioctl commands and `generate-vm-report.sh`'s SDXI step use the ioctl
  path, which blocks instead.
- The server is one thread (`vfu_run_ctx` then `sdxi_engine_poll`), so a
  counter read is whole; the 14 values of a context are read one after
  another, so `bytes_moved` and `num_copy` of one sample can differ by an
  operation that completed between them.

So a sample can add its own duration to one operation of a benchmark. The
two-second SDXI benchmark has about a 1 in 15 chance of meeting one. To
rule it out, in the guest:
`sudo systemctl stop sdxi-textfile-metrics.timer` (back at the next boot,
or `start`). node_exporter then keeps serving the last file, so the panels
go flat rather than empty; `node_textfile_mtime_seconds` says how old it is.

**The SDXI rate panels use a fixed `[2m]` window, not `$__rate_interval`,
and the window is tied to the interval.** `$__rate_interval` is four
*scrapes*, 60 s at the stacks' 15 s, but the counter only moves when the
guest rewrites the file: node_exporter re-serves the last file on every
scrape, so a 60 s window holds two real updates. Worked through
Prometheus's `rate()` extrapolation in a simulation (true rate 1.0, 15 s
scrape, one hour), not measured on a guest:

| Guest writes every | Rate window | A steady workload reads |
|---|---|---|
| 30 s | 60 s (`$__rate_interval`) | 0.67 to 1.34, alternating: a sawtooth |
| 30 s | 2 m | 0.86 to 1.15 |
| 30 s | 5 m | 0.95 to 1.06 |
| 15 s | 60 s | 1.00 while the timer stays in phase with the scrape; 0.68 to 1.01 once it drifts |
| 15 s | 2 m (what the role and the panels use) | 0.87 to 1.02 |
| 5 s | 60 s | 0.92 to 1.04 |

The mean is right in every row, and no window is ever empty. The role
samples every 15 s, the owner's choice on 2026-11-18 (it was 30), and the
panels keep the two-minute window: a steady workload then reads up to 13%
low and never high, and a short burst shows as its bytes spread over two
minutes. **Sampling every 15 s is not a cure on its own**, over the
one-minute `$__rate_interval`: `OnUnitActiveSec` counts from the previous activation, so the real
period is 15 s plus the run time, it beats against the 15 s scrape, and
every time a scrape sees one value twice the window reads a third low. The
steady options are a longer window or a period well under the scrape
interval, and the second is that many more samples against the lock.
`tests/test_sdxi_metrics.py` fails if the window is less than four times
the default `sdxi_setup_metrics_interval`, so raising the interval means
widening the window in the three dashboards, the README query and that
test. A fixed window does not grow when the dashboard is zoomed out: past a
range where Grafana's step exceeds two minutes (a day or more, depending
on the panel's width), work between the steps is not drawn. The timer sets
`AccuracySec=1s`, because a timer's default accuracy is a minute.

**The home dashboard is three committed copies, not a generated file.**
`qemu/compose/<stack>/grafana/dashboards/fleet/quickstart.json` is the same
file in each stack; `gen-compose` only mirrors a stack's `grafana/` into an
`--output-dir` render. `tests/test_guest_exporters.py` holds the three
equal, so an edit goes to all three. On a fleet with `<sdxi-per-vm>0`
(`mi355x-server.xml` sets none) the row is still there: "SDXI Device Up"
reads "no counters" per guest, in blue, and the other panels read "No
data". The tile is blue and not red because `sdxi_up 0` is the right
answer there, and it does not say "no device" because the exporter writes
the same 0 for a device it could not read (no permission, no `sdxi_cli`,
an ioctl error); `journalctl -u sdxi-textfile-metrics.service` in the
guest has the one line saying which.

**A guest of today's `<backing-ref>` has no exporter until
`qemu-tool quick-start test` runs**: `vm-quickstart.yml` includes the same
task file (`tasks_from: metrics`) for images baked before it, as it does
for the other two exporters.

The guest check is the "sdxi metrics" block of `qemu/checks/rocm-golden.sh`
and, the same lines, `roles/sdxi_setup/files/sdxi-metrics-check.sh`, which
the role runs last, as root, since that is who the unit is. It needs no
device: with none it asks for `sdxi_up 0`, exit 0 and silence, and with
one for all 14 counters of contexts 0 and 1.
`tests/test_sdxi_metrics.py` runs the script against a stand-in `sdxi_cli`
and the block against a scratch tree.

**That `sdxi.prom` exists proves nothing on a booted image, so the check
asks systemd.** The bake's own run of the unit writes the file and
`image_slim` sweeps only `/tmp` and `/var/tmp`, so every guest boots with
the bake's `sdxi_up 0` already in place; and a timer is `active` whether
or not its service ever succeeds. The check therefore waits (60 s) for
`systemctl show -P ExecMainExitTimestampMonotonic
sdxi-textfile-metrics.service` to be non-zero — it is 0 until a run has
ended since boot — and then requires `-P Result` to be `success`. The
script exits 0 only after replacing the file, so those two together mean
the file is this boot's. Read off systemd 259 on the dev host, for timer-run
oneshot units: one that has run reports a non-zero time and `success`; one
that has not run since boot reports `0` and `success` too, which is why
`Result` alone would not do.

**Not run on a guest as of this writing.** The script's read path is the
owner's, proven by hand on a live guest; the role, the units, the timer
firing, the check block (its `systemctl show` answers included, which the
tests stub) and the panels against live series have run only against
stand-ins.

## The bake's compiler cache

The plays that build kernel modules — amdgpu, ionic-ernic twice, `ernic_p4`,
SDXI — are about fourteen minutes of a 26-minute `vm-rocm-golden` bake, and
the sources they compile change only when a pin does. **Both workflows that
bake the guest keep a ccache directory across runs, and it is one cache**:
the playbook-test lane, and the publish workflow. The publish workflow used
to bake without it, on the rule that a published image is always compiled
from source; the owner reversed that, to make the publish bake faster and
so that main always holds an entry pull requests can read. One flag turns
the cache on: `qemu-tool gen-vm --bake --cache-dir DIR` names a directory on
the controller, which `gen-vm` creates and hands to the playbook as the
extra var `bake_cache_dir`. Without it the restore and the save are skipped
and the bake compiles everything, which is still what a bake by hand does.
`tests/test_bake_ccache.py` pins that exactly those two workflows pass it.

**What a published image is built from now.** A kernel module in a published
image may be linked from objects compiled in an earlier run on main, not in
the run that published it. `20261117.g481c892` is the first such image:
1788 of the 2053 cacheable compiler calls of its bake were answered from an
entry a dispatched run of the lane on main had saved earlier that day. That
is the trade, and these are its terms:

- ccache returns a stored object only for the same compiler, the same
  command line and the same source. The compiler is identified by its
  content (`compiler_check = content`); the source is the file and every
  header it includes, by content, or else the preprocessed output. What it
  is told not to look at is in the settings table below: the working
  directory, header timestamps, and `__DATE__`/`__TIME__`.
- Those settings are the role's because the archive's are discarded.
  ccache reads `<cache_dir>/ccache.conf` as well as `/etc/ccache.conf`,
  and the one in the cache wins: measured on ccache 4.12.3 on the dev host,
  a `compiler_check` and a `max_size` there replaced the system file's. The
  cache directory is the restored archive, so a `ccache.conf` at the top of
  an entry would be the configuration of a published bake, and a
  `prefix_command` or a command-valued `compiler_check` in it would run as
  root at every compile. `restore.yml` deletes that file after the unpack
  and before the wrappers go in, whether or not an archive was unpacked,
  and `save.yml` leaves it out of the tar. Only the precedence was
  measured; the deletion has run against no guest. Nothing else in the
  archive is filtered: it is unpacked as root with the owners and modes it
  carries, and removed with the rest before the image is finished.
- Nothing compares a cached object with a fresh compile. A wrong object in
  the cache would be published. No such object has been seen, and no test
  here could see one.
- ccache itself must not reach the image. The playbook removes it, and
  `qemu/checks/rocm-golden.sh` fails an image that still carries the
  symlinks, `/etc/ccache.conf`, the cache or the tar. The publish workflow
  runs that check on the device-less boot, before the push, and
  `tests/test_bake_ccache.py` fails if the check is moved after the push,
  given a condition or made `continue-on-error`.
- Who can write what a publish reads is decided by GitHub's cache scopes,
  as its documentation states them; none of this was tested here. A run
  reads entries saved on its own ref and on the default branch. A publish on
  a push to main therefore reads only entries saved by runs on main. Today
  those are earlier publishes and either workflow dispatched there, and no
  other workflow names a `bake-ccache` key. But the restore falls back to
  the bare `bake-ccache-v1-` prefix and an entry is matched on key and path
  alone, so **any workflow that runs on main can save what a publish
  restores**, not only these two. A workflow on main that executed
  untrusted input (`pull_request_target`, or `workflow_run` on content it
  fetched) would be able to. **A run on main
  cannot read an entry saved by a pull request**, whose runs save under the
  pull request's own merge ref, so a pull request cannot put an object into
  a published image through the cache. **A publish dispatched by hand from
  another branch can read that branch's entries** as well as main's: those
  are what earlier dispatched runs on that branch saved.
- To publish an image compiled entirely in its own run, delete main's
  entries first (`gh cache list --key bake-ccache-v1-`, `gh cache delete`).
  There is no input on the workflow for it.

**The cache is switched by a flag or a fleet element, never by the
environment.** The owner does not want settings driven by environment
variables, and an exported variable would reach every `ansible-playbook` in a
job, where the flag reaches one bake; the playbook has no `lookup('env')`.
`<bake-cache-dir>` is the element (a path, `~` and
variables expanded, read by `gen-vm --bake` only, the flag wins);
**`qemu/stacks/rocm-golden.xml` must not set it**: a directory is a fact
about one host, that file is read by every bake, and a bake by hand would
start caching into whatever path was committed. The two workflows pass the
flag, in the bake step's own environment and nowhere else in the job; a
local overlay sets the element. `gen-vm`
refuses `--cache-dir` without `--bake`, because the element is a bake
element and the flag follows it, and refuses a cache when the bake has no
playbook to hand it to. The extra var is passed as JSON, not `key=value`,
which splits a path at its first space. A playbook that does not read
`bake_cache_dir` ignores it.

`--cache-replace` (extra var `bake_cache_replace`) is the second switch and
has no element: it is a fact about one run — the cache handed over was saved
for other pins — not a setting. The tool cannot derive it: what knows is
whoever fetched the cache, which in the two workflows is the key comparison
the restore action makes.

**What has been measured.** Fourteen minutes is those plays' wall time, not
what the cache saves. All of this is from GitHub's runners, 2026-11-16 and
17. A bake there makes 2053 cacheable ccache calls and some 330 uncacheable
ones, warm or cold.

| Run | Time | ccache, of 2053 |
|---|---|---|
| the lane on a pull request, cold | 27m27s for the run; the amdgpu play 7m49s | |
| that pull request, a later run, on an entry saved under another key prefix | 17m46s for the run; the amdgpu play 2m27s | 1812 hit, 241 missed |
| the lane, cold, on a slower runner | 35m01s for the run; the amdgpu play 15m17s | |
| the lane, cold, dispatched on main at `80c17f8` | 30m42s for the bake | 220 hit, 1833 missed |
| the lane on a pull request, on the entry main held (#238, its rerun) | 13m26s for the bake | 1818 hit, 235 missed |
| the publish workflow with no cache, 2026-11-16 and the morning of 11-17 | 24m33s and 25m08s for the bake step | |
| the publish workflow on the entry main held, at `481c892` | 18m25s for the bake step | 1788 hit, 265 missed |

A cold bake misses 1833 and hits 220. Both are read from one run's
statistics, the 30m42s one in the table: the lane dispatched on main at
`80c17f8` (run 44106616336), which found no entry to restore ("Cache not
found") and printed `Hits: 220 / 2053`, 219 of them direct, and `Misses:
1833 / 2053`, with 334 uncacheable calls. The 220 can only be compiles the
bake itself repeats, a cold cache having nothing else to answer from. The
other cold runs' statistics were not read.

So the cache took the lane's bake from 30m42s to 13m26s and the publish
bake step from 25m08s to 18m25s. The publish bake also compacts the image,
which the lane's does not, and the cache does nothing for that. Runner
speed moved the cold lane runs by seven and a half minutes between
themselves, so read those two ratios loosely. The publish run restored
main's entry and saved a new one there; the pull request's run was the
first to start from an entry a run on main had saved, which is the path
every pull request now takes.

The three warm runs each started from an entry a cold bake had saved, and
missed 235 to 265 calls. A warm bake that starts from an entry another warm
bake saved has not been seen; the next bake will be one, because main's
newest entry is the one the publish at `481c892` saved. See the threshold
below for why that matters.

ccache removes the compiler and nothing else. What stays: amdgpu's
`configure`, some 290 separate kbuild invocations whose start-up cost is
paid hit or miss; objtool on every object (`CONFIG_OBJTOOL`); the link,
`modpost`, signing and zstd compression of each module; and the package
installs, clones and the reboot the same plays do.

**The hook is a symlink per compiler name in `/usr/local/bin`, plus
`/etc/ccache.conf`.** Nothing narrower reaches every build, and this was read
off the code rather than assumed:

| Candidate | Why not |
|---|---|
| `PATH` / `CCACHE_*` in the task environment | the collection's scripts run `sudo dkms`, and sudo resets the environment to its `secure_path` |
| `/etc/dkms/framework.conf` | dkms 3.2.2 reads it in a subshell and copies out a fixed list of its own variables; an `export PATH=` there is dropped |
| `CC=` for make | dkms unsets `CC` and exports the first word of `CONFIG_CC_VERSION_TEXT`; kbuild's own `CC = $(CROSS_COMPILE)gcc` then outranks the environment, and amdgpu's top Makefile says `CC := gcc` |

What all of them do is resolve a bare compiler name through `PATH`, and
`/usr/local/bin` precedes `/usr/bin` in sudo's `secure_path` and in a login
`PATH`. The role links every name the ccache package wraps in
`/usr/lib/ccache`, so `gcc`, `cc` and `x86_64-linux-gnu-gcc-15` are all
covered. SDXI's plain `make` under `become` resolves the same way.

The settings. `hash_dir` and the path behaviour were measured on the dev
host; the rest follow from ccache's documentation and have not been:

| Setting | Why |
|---|---|
| `compiler_check = content` | every bake installs gcc afresh, so the default `mtime` never matches |
| `hash_dir = false` | ccache hashes the working directory of a `-g` compile. The modules build at fixed paths, but amdgpu's `configure` compiles ~300 feature tests each in a `mktemp -d` directory |
| `sloppiness = time_macros,include_file_mtime,include_file_ctime` | headers unpacked or generated seconds before the build are "too new" for ccache by default |
| `umask = 000` | root and the login user both build into the one cache |
| `max_size = 1G` | ages out a moved pin's objects, and bounds the GitHub cache entry |
| no `base_dir` | not needed, and the one setting that can store a wrong object |

Two facts about amdgpu-dkms 7.1.9 that decide the hit rate, read from the
package. It builds with `M=$(mktemp -t amd.XXXXXXXX)`, a **symlink** to the
DKMS build directory under a random name — harmless, because kbuild
`realpath`s `M`, and measured so on the dev host: a second build through a
differently named symlink hit 25 of 25. And its `configure` runs the feature
tests in parallel, building each test's source from whichever results are in
so far, so some of those sources differ run to run and **a warm bake still
misses a little**. A GitHub cache entry cannot be overwritten, and re-saving
a few hundred MiB per run for throwaway test objects would crowd out the
repository's other caches, so the role replaces the saved archive only when
one of three things holds:

| Condition | What it catches |
|---|---|
| no archive was handed over | the first bake |
| `bake_ccache_replace`, from `gen-vm --cache-replace` | a moved pin: the restore action sets it when the entry it restored was not saved under this run's key prefix, or nothing was restored |
| at least `bake_ccache_save_misses` (500) invocations missed | drift no pin records, such as a new gcc in the archive |

**The threshold is 500, set from the runs above, and the second row is not
redundant.** It was 100, chosen before any bake had been counted, from an
estimate that a bake makes some 6,400 compiler probes which all hit when
warm. The runs do not bear that out. A bake makes 2053 cacheable calls, and
a warm one misses 235, 241 or 265 of them, so every warm bake was over 100
and saved again. One of the three would have saved anyway, its key prefix
having changed; the other two, with unchanged pins, are what the rule
exists to stop. Each wrote an entry of about 390 MiB, 14 MiB more than the
376 MiB cold entry it had restored, and six entries were stored within two
days.

What 500 separates, and what it does not:

| Bake | Misses | At 500 |
|---|---|---|
| warm, pins unchanged | 235, 241, 265 measured | not saved |
| a new gcc in the archive | every entry stops matching, `compiler_check` being `content`, so a cold bake's 1833 | saved |
| amdgpu-dkms repackaged under the same driver release | the largest of the five builds recompiled; how many of the 1833 are amdgpu's has not been counted, and the amdgpu play is the one that goes from 7m49s to 2m27s | expected saved, not measured |
| a bumped ernic or SDXI pin, or a kernel or amdgpu release | a module of tens of files, or all of them | saved whatever it missed: the key prefix changes and the restore action passes `--cache-replace` |
| one small module recompiled with no pin moved | tens over the warm figure | not saved, and recompiled in every bake until something else saves |

The last row is the cost, and no count avoids it: the spread between warm
bakes, 30, is already the size of such a module, so a threshold low enough
to see one sits inside the noise, which is where 100 was. Nothing known
produces that row; every source a bake compiles is named by a pin in the
key. 500 leaves the warm figure room to nearly double, three runs being no
distribution, and is under a third of the nearest thing it has to catch. If
a warm bake is ever seen above it, that run saves, the next one says
whether it was drift or noise, and the figure moves.

It is a count and not a share because the question is how much this bake
compiled that the archive did not hold. The statistics every bake prints,
and both workflows copy into their step summary, are what to tune it
against; their last line says "ccache invocations", not compiles, since
ccache counts calls it could have cached and a call is not always a source
file.

**What a warm bake's misses are has been read, not measured.** amdgpu's
`configure` builds each feature test's source from whichever results are in
so far, which is the reason given above, and nothing has compared two warm
bakes' missed objects. There are two readings and the runs so far cannot
tell them apart, because all three started from a cold bake's entry. If the
sources differ at random, every warm bake misses its 240 whatever it
restored, and saving them is waste. If they differ with how fast results
come in, a warm bake's differ from a cold bake's in much the same way each
time, an entry a warm bake saved already holds most of them, and a bake
starting from one misses few. 500 is right under both: under the first
nothing saves, under the second nothing needs to. The next bake starts from
a warm bake's entry, the one main holds, and its miss count says which.

Transport is one uncompressed tar file, `ccache.tar`, in that directory:
unpacked into `/var/tmp/bake-ccache` after the reboot into the pinned kernel,
fetched back by a play that sits after SDXI and the perftest build and before
the stamp and the slim pass. The fetch runs with `become: false` on purpose — with become, `fetch`
slurps the file as base64 in memory. That play's `always` section then runs
the role's `remove` tasks, which take out the symlinks, `/etc/ccache.conf`,
the cache and the tar. **The removal is the one part that is not gated on a
cache directory**: a cached bake that fails between the restore and the save
leaves the hook in the guest, and an `--ansible-only` rerun without
`--cache-dir` would otherwise finish an image whose `gcc` is ccache. Every task in it is a
no-op on a guest that never had the hook, so a bake given no cache runs them
and changes nothing. `qemu/checks/rocm-golden.sh` asserts all four are
absent, so both the lane and the publish workflow fail on an image that
carries any of it — the publish workflow before it pushes.

**The two workflows reach `actions/cache` through two composite actions and
name nothing themselves.** `.github/actions/bake-ccache-restore` names the
cache, restores it and marks the start of the bake;
`.github/actions/bake-ccache-save` reports the statistics and saves. Each
workflow has three steps in a row: the restore action, its own bake step,
the save action. An entry is found by its key and by the path it was saved
from, so two workflows that each spelled a key or a directory would stop
sharing the cache the first time one was edited, and nothing would fail.
Hence one copy: the restore action has no inputs, its `dir` output is the
only place the directory is written, and `tests/test_bake_ccache.py` fails
a workflow that passes it a `with:`, calls `actions/cache/restore` or
`save` itself, or spells a key. Both jobs run in the same container image,
which matters for the same reason: the compression tool `actions/cache`
finds is part of what it matches on.

The key ends in the run id and attempt, since an entry is never
overwritten; its prefix hashes the kernel, ernic, SDXI and perftest pins, the
amdgpu driver release, `requirements.yml` and `amdgpu-emu-patches.py`, with
`restore-keys` falling back to any earlier cache so a pin bump still starts
warm — and, because a restore that fell back that far passes
`--cache-replace`, saves once under the new prefix so the next run is warm
for the bumped module too. The save action runs under `always()`, on the
calling step and on its own two steps, and saves only when the bake wrote a
new archive.

**Main holds an entry because the publish workflow runs there.** It runs on
every push to main that touches `ansible/**`, and every input of the key
prefix is under `ansible/`, so a merged pin bump is followed by a publish
that restores main's last entry through the fallback, bakes with
`--cache-replace` and saves under the new prefix. It also runs on a push
that changes either composite action, which is where the `v1` of the key
and the directory are: a merge that moves one of those is followed by the
publish that saves the first entry under the new naming, at the cost of one
publish per such merge. GitHub lets a pull request
read entries saved by itself or by its base branch, so a pull request's
first lane run starts from what main's last publish saved. Nobody has to
dispatch the lane on main any more, after a merge or after a pin bump. What
that does not cover:

- A pull request that itself moves a pin compiles the moved module on its
  first run, then saves under its own prefix.
- Main has no entry until a publish there has got as far as its bake. An
  entry unread for seven days is evicted, and the next publish is then cold
  and saves again.
- A publish with unchanged pins saves nothing unless five hundred
  invocations missed, which no warm bake has, so main's entry can be several
  publishes old. That is the same rule the lane has. An entry nothing has
  read for seven days is evicted whatever its age, and a publish reads
  main's, so it is publishes on main that keep it alive.
- GitHub looks for every key in the pull request's own scope before it
  tries the base branch. A pull request that holds an entry of an old
  prefix restores that one, not main's newer one; it then saves under the
  new prefix. From GitHub's documentation, not seen here.
- A change to the `v1` in the key, or to the directory, starts every reader
  cold, the publish that follows the merge included. An entry is matched on
  the path it was saved from, so `tests/test_bake_ccache.py` pins
  `/var/cache/bake-ccache` as it pins the prefix.

Each entry is 376 to 391 MiB of the repository's 10 GiB. Six were stored
by the evening of 2026-11-17: three from cold bakes, one from a warm bake
whose prefix had changed, and two from warm bakes with unchanged pins,
saved because the threshold was then 100. Later runs restore main's
newest; the rest go seven days after their last read.

The save action dates what the bake wrote against a marker in
`$RUNNER_TEMP`, which the restore action touches as its last step — the
reason the bake has to be the step straight after it. Not in the cached
directory: there it is restored with the cache, older than the files it is
meant to date.

The lane also bakes with `--no-compact`: nothing publishes its image, and the
recompress was most of the three minutes after the playbook. The backing
image's "disk size" in its `Report VM image size` step is therefore the
uncompacted allocation, 2.3–2.5x the published figure and not comparable with
runs before the flag. The publish workflow still compacts, and still bakes
with the key `gen-vm` generates.

**What CI has shown, and what it has not.** Both workflows have baked with
the cache, with the figures above. The hook gets in front of dkms and
kbuild in the guest; the tar and `actions/cache` round trips work; the two
composite actions hand the directory and the key to the bake and to the
save, in both workflows; the publish workflow restored an entry the lane
had saved on main, which is one workflow reading the other's; a pull
request's run restored an entry saved on main; and runs that restored an
entry of their own prefix missed 235 and 265, which is the case the miss
rule is about. An output that resolved to nothing would not have shown
before: `gen-vm` takes `--cache-dir ""` for the current directory and the
save action would have looked in `/` and reported nothing to save. So both
bake steps stop on an empty `BAKE_CCACHE_DIR` before anything is created,
and the save action fails on an empty directory or key once the marker says
the bake ran. Not yet seen in any run:

- a warm bake that does not save. Every warm bake so far ran with the
  threshold at 100 and saved. At 500 the role should report the archive
  "left as it is" and the save action "nothing to save"; that path has been
  run by the tests and by no workflow;
- a pull request restoring an entry a *publish* saved on main. The one seen
  restored an entry a dispatched lane run had saved there. The publish at
  `481c892` has since saved one, so the next pull request's first run is
  it, and is also the first warm bake to start from a warm bake's entry;
- the restore deleting a `ccache.conf` from an unpacked cache, and the save
  leaving one out of the tar;
- a bake with a moved pin, and so `--cache-replace` passed by the restore
  action because the prefix changed, in the publish workflow.

**Every counter zero means the hook did not intercept** — the bake is
correct and no faster — and the role's last line and a workflow warning
both say so rather than leaving it to be noticed.

## Git / GitHub

- Default GitHub account: `sbates130272` (verify with `gh auth status`)
- GPG signing required on all commits (`-S`), signoff required (`-s`)
- Main branch: `main`
- Never use `--no-verify` or `--no-gpg-sign`

## Cutting a release

`scripts/release.sh <version>` from a clean `main`. It stamps the version in
`qemu/pyproject.toml` and in line 1 of the man page (the `.TH` line, with the
month), generates the `qemu/debian/changelog` stanza from the
`CHANGELOG.md` `[Unreleased]` section, retitles that section as the new
version, commits signed-off, and makes a signed tag. Run it with `--dry-run`
first to see the generated stanza.

It never pushes; it prints the `git push origin main v<version>` that does.
Pushing the tag is what triggers `qemu-minimal-release.yml`, whose `Verify Version` job
refuses to build anything unless the tag, `pyproject.toml`,
`debian/changelog` and the man page's `.TH` line agree and `CHANGELOG.md` has
a heading for the tag. That gate exists because v1.3.0 shipped with no tag at
all and nothing noticed; the man page joined it because its version is a
literal nothing else writes, and `tests/test_man_page.py` holds it equal to
`pyproject.toml` between releases.

That workflow then builds the wheel, the sdist and the `.deb`, attaches all
three to one GitHub Release, and publishes to PyPI last — a PyPI version can
never be reused, so it goes after everything repeatable has succeeded. PyPI
uses Trusted Publishing against the `pypi` environment, so there is no API
token anywhere in the repo.

## The controller's rocm-ernic clone

`rocm.ernic.ernic_source` keeps a clone on the **controller** at
`ernic_source_clone_dir` (default `/var/tmp/rocm-ernic-src`). Until the
collection moved to `bd90234` it updated that clone with
`ansible.builtin.git`, which runs `git fetch --tags origin`. Upstream
moves tags — its `nightly` did — and git then refuses:

```
 ! [rejected]        nightly    -> nightly  (would clobber existing tag)
```

That killed a bake after 118 tasks, over a tag the bake never reads: it
checks out `ernic_source_pin`.

**The role runs git itself now, and the guard is kept for its fallback.**
Read from the role at `bd90234`, not run: an experiment against scratch
repositories was not possible in the session that made the bump.

| Step | What the role does | A moved tag |
|---|---|---|
| main path | `git fetch --no-tags --filter=blob:none origin -- <version>`, then a detached checkout of what it fetched | cannot fail it: no tag is fetched. A bake takes this path, since `ernic_source_pin` is a full commit id and GitHub serves a reachable commit by id |
| fallback | when that fetch fails for any reason, with its error discarded: `git fetch --tags --filter=blob:none origin '+refs/heads/*:refs/remotes/origin/*'`, then resolve the version locally | fails it, as before: the tags are fetched unforced |

So the guard is redundant on the path a bake takes and still needed on the
other, which is reached on an abbreviated version, on a remote that will not
serve a commit by id, and on a failure of the first fetch that the second
survives. `Role` in `tests/test_source_clone.py` reads the installed
collection and fails when the fallback stops being an unforced fetch of
every tag, which is the day the guard, its script and that file can go.

Three more things about the new clone:

- **It is blob-less, and an existing clone is converted.** The first run
  against the full clone an earlier bake left records a `blob:none` filter
  for `origin` and makes it a partial clone; the checkout stays where it
  was. The guard's own `git fetch --tags origin` names no filter and takes
  the recorded one. `/var/tmp/rocm-ernic-src` on this host had not been
  converted when the bump was made.
- **It checks out `ernic_source_pin`, not the collection's commit**, so the
  shared clone does not move under another worktree's bake.
- **A clone with local changes to tracked files fails the role**, unless
  `ernic_source_force_clone` is set, which discards them. That is not new:
  `ansible.builtin.git` failed the same clone with "Local modifications
  exist". Nothing in this repo sets the variable, and the same test fails a
  playbook or role that does. The answer to that failure is to look at the
  changes, not to force.

So `rocm_ernic_setup`'s `source_clone` tasks
run immediately before every include of `ernic_source` in
`vm-rocm-golden.yml` — the first play, the SRQ + P4 play, and as a `pre_task`
of the configure play, whose `ernic_guest_setup` includes the role itself
whatever else is switched off in it —
and take a moved tag with `git fetch --tags --force origin`, after which the
role's own fetch succeeds. `tests/test_source_clone.py` fails an include with
no guard in front of it.

**Nothing is removed, by the script or by the tasks.** An earlier version
deleted the clone and let the role clone afresh. The directory is shared by
every bake and configure run on the controller, and `vm-rocm-golden.yml`
reads patches, scripts and udev rules out of it for the length of the SRQ +
P4 play, so a second run starting after a tag moved would have deleted the
tree under the first — and taken any uncommitted or unpushed work in it. A
forced fetch rewrites tags and remote-tracking refs only: the work tree,
`HEAD`, local branches and uncommitted changes stay as they were, which the
test checks. The one thing it overwrites is a local tag that has the same
name as an upstream one.

| State | When | What happens |
|---|---|---|
| `healthy` | the fetch succeeded | nothing |
| `absent` | nothing there, a directory that is not a clone, or a repository with no `origin` | nothing; the role clones or reports |
| `repaired` | git said `would clobber existing tag` and the forced fetch succeeded | the tags now match upstream; the task reports `changed` |
| `offline` | the fetch timed out; or it failed and `git ls-remote` of the same URL, run from outside the clone, does not answer either | nothing |
| `broken` | any other failure: an unreadable clone, a ref lock held by another git, a full disk, permissions, or a forced fetch that failed | nothing; the role's own fetch reports it, or succeeds if the fault was passing |

**A timeout is `offline` whatever the probe says.** A remote slow enough to
run out the 300 s fetch can still answer a 60 s `ls-remote`, and that is a
slow network, not a fault in the clone. `offline` and `broken` differ only in
what the line tells the reader; neither changes anything on disk. The role
then makes its own fetch and fails if that fails too, a clone already at the
pinned commit included: it fetches before it looks at what the clone holds,
as `ansible.builtin.git` did. Corruption that
`git fetch` does not trip over is not detected at all; neither does the
role's own fetch.

The guard is skipped when `ernic_source_dir` names a checkout of the
caller's own, which the role never updates, and once `ernic_source_resolved`
is set — so the second include in a bake costs nothing, and does the work
when a rerun starts at that play.

## Where the ionic kernel sources come from

rocm-ernic's `fetch-ionic-sources.sh` fetches three directories of the stable
kernel tree at `ionic_kernel_pin` and applies a patch series. Its default
repository is `git.kernel.org`. **A golden bake fetches from a GitHub mirror
instead, `ionic_kernel_mirror` (`https://github.com/gregkh/linux.git`), and
checks each fetched tree against `ionic_kernel_pin_commit`.** The owner chose
the mirror over a tarball.

Why: twice in two days a GitHub runner could not open a TCP connection to
`git.kernel.org:443` (75 s connect timeouts, four attempts, then the script's
fallback, twelve minutes gone) and the playbook-test lane failed, while
GitHub answered in the same job. Measured **from the dev host, not a
runner**, 2026-11-17, for the script's own sparse depth-1 fetch of `v7.2.6`:

| Source | Time | Size |
|---|---|---|
| `git.kernel.org` | 57.6–57.8 s | 292 MB `.git` |
| `github.com/gregkh/linux` | 11.7–14.3 s | 15 MB `.git` |
| `cdn.kernel.org` tarball | 77.9 s | 160 MB |

`git.kernel.org` ignores `--filter=blob:none` and sends every blob of the
commit; GitHub honours it.

**The commit is what makes a mirror safe to use.** `git ls-remote <repo>
refs/tags/v7.2.6` prints `c148318444bab91e4768a04aad6a18f8d974ecf7` from
`git.kernel.org`, `github.com/gregkh/linux` and `kernel.googlesource.com`
alike, and that is the **tag object**. The commit is the `refs/tags/v7.2.6^{}`
line, `500df175a7f9e6bc1a9c328590ca5150f84f9ff0`, the same from all three
that day. A tree patched on anything else fails the bake, naming both commits
and the repository.

| Fetch | Runs | Script from | Reaches the mirror by | Tree checked |
|---|---|---|---|---|
| `ernic_guest_setup`, first play | guest, root | the collection (`bd90234`) | `url.*.insteadOf` rule | `/var/tmp/ionic-src` |
| `ernic_srq_dkms` | **controller** | the checkout at `ernic_source_pin` | `--repo`, via `ernic_srq_dkms_sources` | `/tmp/ionic-srq-src-controller/<ref>` |
| `ernic_p4_dkms` | guest, root | the checkout at `ernic_source_pin` | `--repo`, via `ernic_p4_dkms_kernel_repo` | `/usr/src/ionic-ernic-p4` |

The configure stage was a fourth row, the same fetch as the first. It builds
no driver now (known issue 16), so it runs the role with the driver path off
and fetches nothing.

`ernic_p4_dkms` fetches in the guest and has no switch for anything else.
The playbook used to pass it `ernic_p4_dkms_fetch_on_controller: true`,
with `ernic_p4_dkms_install` and `ernic_p4_dkms_reload`; no role read any of
the three at `90434a2` or at `bd90234`, and they are gone.
`ernic_p4_dkms_kernel_repo` is the control that is real.

**The collection's script has a second host of its own now, and only the
first play's fetch runs that script.** Since `bd90234` it asks whether the
repository answers before each fetch (`git ls-remote`, 30 s), and a
`git.kernel.org` repository that has not answered in four tries (up to
about three minutes, where an unreachable host used to cost thirteen) is
followed by the
same path on `kernel.googlesource.com`, with `origin` repointed there. That
list is built from the URL the script was *given*, so:

- under the rewrite, the first play's fetch probes and fetches the mirror
  as before (the probe's URL is rewritten like any other);
- **with the mirror down, that fetch succeeds from Google**, which the
  rewrite does not cover and nobody chose. The sentinel is the one a fetch
  from the mirror leaves, so a later run says `nothing to do`.
  `ionic_source_check` fails it on the origin, and the bake with it. That is
  deliberate: a loud failure is what choosing one mirror means. The check
  removes the tree before it fails (see "The check" below), or a rerun on
  that guest would find the sentinel, fetch nothing and fail the same way
  with the mirror long back. `test_a_mirror_that_never_answers_...` in
  `tests/test_ionic_source_mirror.py` runs the real script through exactly
  this, with local repositories standing in for both hosts;
- a fetch given `--repo`, which is the other two, has no second host, and
  those two run the older script anyway.

The role also has `ernic_guest_ionic_fetch_on_controller` now, off by
default: the controller fetches and ships the tree to the guest without its
`.git`. It takes no repository either, and nothing rewrites the controller's
git, so turned on it would fetch from kernel.org, and the commit check would
then refuse a tree with no `.git`. No playbook sets it and a test fails one
that does. Adopting it in place of the rewrite is an open question, not
something to do in passing: it has not run in a bake.

**The rewrite.** `ernic_guest_setup` runs the script with no `--repo` and no
variable for one, so `rocm_ernic_setup`'s `ionic_mirror` tasks write
`url.<mirror>.insteadOf <kernel.org URL>` into `/etc/gitconfig` before the
role, and `ionic_mirror_remove` takes it out in the `always` section of the
same block, with the file if that leaves it empty. System scope, because the
fetch runs as root under become; nothing is written on the controller.
`qemu/checks/rocm-golden.sh` fails a baked guest with any URL rewrite at
system scope. The first play includes the role from a block in `tasks:` for
this: only a block has an `always`.

- **The script does not know.** Its sentinel and its messages carry the URL
  it was *given*, so under the rule it still prints `already at v7.2.6 from
  https://git.kernel.org/...`. The check's line says where the tree is from.
- **So the rule causes no refetch and is not needed to skip one**: fetch with
  it, rerun with it, remove it, rerun, and both reruns say `nothing to do`
  (run against the pinned script). That is what the configure stage used to
  meet on a guest baked from the same checkout: `nothing to do`, on the base
  tree, and then a build of it.
- **The match side is the script's own default**,
  `rocm_ernic_setup_ionic_upstream`, the only non-comment line in a bake path
  that names kernel.org. A rule matching another URL redirects nothing and
  says nothing; `tests/test_ionic_source_mirror.py` holds both, the second
  against the installed collection's script, so only where one is installed.
  `Gen Compose Check` installs it and those tests fail there rather than
  skip.
- **`--tags configure` writes no rule**, since it no longer fetches. The
  full guest check runs before that stage in both lanes, and the
  playbook-test lane still sends the check's lines between `# begin: git url
  rewrites` and `# end: git url rewrites` again after it: a driver build
  that came back would bring the rule with it. The publish workflow does
  not: what it publishes is the bake, which the full check has already
  passed, not the overlay the configure stage ran on.

**The check**, `ionic_source_check` (`files/ionic-source-base.sh`), follows
each of the three, also when the script fetched nothing. HEAD is no use: the
script applies the series with `git am`, so HEAD is the last patch. After the
depth-1 fetch the base is the one line of `.git/shallow`, since such a fetch
brings exactly one commit; after the script's fallback fetch of every tag it
is the first commit on the first-parent chain not committed as
`rocm-ernic@localhost`, the identity the script pins. The origin is compared
as well, as git resolves it, so a role that stopped passing the repository
on fails instead of fetching from kernel.org; the guest check therefore sits
inside the block, while the rule is installed. It runs after the role, so a
wrong tree has been built by then; the bake fails and nothing is published.

**A guest's tree that fails the check is removed; a controller's is not.**
The script names its sentinel after the ref, the series and the repository
it was *given*, and neither guest fetch is run with `--force`
(`ernic_guest_setup` has no switch for it; `ernic_p4_dkms_force_fetch` is
false). So a failed tree left in the guest is the tree
`gen-vm --ansible-only` finds "already there": it fetches nothing and fails
the check again, whatever was put right in between. The removal runs only
on the script's own `FAIL:` line, never when the script printed no verdict,
and deletes the directory the check was told to read, which is the script's
`--source-dir` and which the script itself deletes whenever it fetches. The
rerun then fetches again, and `setup-ionic-dkms.sh` removes and rebuilds the
DKMS package from what it fetched, so the driver built from the failed tree
does not survive it either. The controller's tree under
`/tmp/ionic-srq-src-controller` stays, because it is on the host running
the bake, where another run may be reading it; the failure names it and
says to remove it. Run by Ansible against scratch
trees in `tests/test_ionic_source_mirror.py` (`RoleTasks`), not on a guest.

**A pin bump moves `ionic_kernel_pin` and, on the next line,
`ionic_kernel_pin_commit` and the `# <tag>^{}` comment that ends it.** The
commit is what `git ls-remote <the stable tree> 'refs/tags/<tag>^{}'`
prints. `tests/test_playbook_pins.py` fails when the comment names another
tag, when the commit is not 40 hex digits, or when a play holds a literal
instead of the alias. It cannot ask a server: a commit left at the old value
passes the tests and fails the bake at its first fetch.

Side effects, read off scratch trees on the dev host and not off a guest:
the two guest trees are real partial clones now, about 18 MB each where
kernel.org's were about 296 MB; and a git command in one that needs a file
outside the three directories would fetch it from the tree's origin, which
in the shipped image is the kernel.org URL with no rule. Nothing here or in
the collection's roles runs one.

`kernel.ubuntu.com`, where the mainline kernel `.deb` files come from, is a
different host and not part of this. The other mentions of kernel.org a bake
can see are the collection's defaults, overridden or rewritten as above, and
`sbates130272.batesste`'s `kernel_setup`, which no playbook here uses.

**Not yet seen on a GitHub runner or in a guest.** The pinned script was run
on the dev host against the mirror with each series a bake applies, with
`--repo` and under the rule. The role's three task files are run by the unit
suite through `ansible-playbook` on the local connection, in the block and
`always` shape the playbook uses, with a scratch file as git's system
configuration and become switched off (`RoleTasks` in
`tests/test_ionic_source_mirror.py`); once by hand they were run that way
around a real fetch by the pinned script with no `--repo`, which sent five
requests, all to `github.com`, and passed the check. That the guest's git
reads `/etc/gitconfig` under become, that a runner reaches GitHub where it
did not reach kernel.org, and the block, rescue and always structure of the
first play around the collection's own role are read from code.

## One kernel in the baked image

The golden image carried two kernels: the pinned mainline one it runs, and
the Ubuntu release kernel the cloud image came with — about 370 MB, and the
entry grub offers next. No module in the image is built for that kernel, so
a guest that fell back to it would boot with no ionic, no amdgpu and no SDXI
and say nothing about why. The golden slim play now sets
`image_slim_purge_old_kernels: true`; the role default stays `false`, and
`vm-basic.yml` does not set it.

**"Old" means every kernel but the RUNNING one**, so the play first asserts
the guest is running `ionic_kernel_pin`. Without that, a bake that somehow
reached the slim play on the release kernel would purge the pinned one.

What the role does with the flag on, in order, each step there because the
one before it is not enough:

| Step | Why |
|---|---|
| assert an image and a modules package are installed (`ii`, not merely known to dpkg) for the running kernel | the purge leaves one kernel; it must be a real one |
| `apt-mark manual` the running kernel's packages | a kernel installed from `.deb` files is manual already; this makes it so however it got there, so no later autoremove can take the only kernel |
| `files/kernel-purge-guard.sh` | `apt-get --simulate purge` first. apt removes **reverse dependencies** silently, so a package depending on a kernel metapackage would leave with it. Fails unless everything removed is a stale package or is named for a version being purged; fails on any `Inst`, and on anything named for the running kernel |
| purge, `autoremove: false` | the stale packages, the six metapackages included — a metapackage left behind pulls a release kernel back on the next upgrade, and it would sort above a mainline build |
| `files/kernel-leftovers.sh`, then remove what it lists | dpkg removes only files a package owns. Left behind: `/lib/modules/<old>` (depmod and DKMS output), `/boot/initrd.img-<old>*`, `/var/lib/dkms/<module>/<version>/<old>` and its `kernel-<old>-<arch>` link. The script lists and deletes nothing; a kernel whose module tree a package still owns is left whole |
| `update-grub`, then the check | headers present, one `linux-image` installed, one `/lib/modules` tree, the running kernel's image and initramfs in `/boot` and no other, and every `linux` line in `grub.cfg` loading it |

The package hooks do the rest: `/etc/kernel/prerm.d/dkms` runs `dkms
kernel_prerm` for the kernel being removed, `postrm.d/initramfs-tools` deletes
its initramfs, and `zz-update-grub` regenerates the menu. The running
kernel's initramfs is not regenerated — nothing about it changes.

**The boot entry is pinned by name, in `rocm_ernic_setup`'s `kernel.yml`, not
in the slim play.** That is where the kernel is installed and first booted,
and every later reboot of the bake relies on it. It writes
`/etc/default/grub.d/99-rocm-ernic-kernel.cfg`:

```
GRUB_TOP_LEVEL="/boot/vmlinuz-7.2.6-070206-generic"
GRUB_DEFAULT=0
```

`GRUB_TOP_LEVEL` (GRUB 2.12 and later; read from resolute's
`/etc/grub.d/10_linux`, `grub2-common 2.14-2ubuntu2.1`) moves the named
kernel to the front of the version-sorted list, so it is the top-level entry
whatever else is installed, and `GRUB_DEFAULT=0` boots the top-level entry. A
kernel that is no longer there is ignored and the order falls back to
version, so the pin cannot produce an unbootable menu. A drop-in sorting
last, because the cloud image's own `50-cloudimg-settings.cfg` is read after
`/etc/default/grub` and an edit to that file could be outranked. Not
`GRUB_DEFAULT=saved`: that puts the answer in `grubenv`, state that outlives
the bake. The pin sits outside the install block, so a guest already on the
pinned kernel is pinned too, and ahead of the reboot, so the reboot into the
new kernel is the pin's first test — `kernel.yml` then asserts what booted.

`qemu/checks/rocm-golden.sh` asserts all of it on the booted guest, in both
the playbook-test and the publish lane, before anything is pushed: one
installed kernel image and it is `uname -r`; no kernel metapackage; one
`/lib/modules` tree; no other `vmlinuz` in `/boot`; no DKMS build or `dkms
status` line for another kernel; the two lines of the drop-in; and that
**grub's default resolves to the running kernel**. The last reads the
generated `grub.cfg` (root-only on resolute, hence `sudo -n`), not
`/etc/default/grub`: it takes the `set default` grub would execute, goes
through `grub-editenv` when that is `${saved_entry}`, walks the default down
the menu by index, id or title, and requires every `linux` line of the entry
it lands on to load `vmlinuz-$(uname -r)`. A default that names no entry is
a failure, not a fall-back to entry 0, and a one-shot `next_entry` is a
failure too — it would outrank the default for exactly the first boot a
consumer makes.

**It has run against both bakes that were published.** The publish
run on main at `80c17f8` had `image_slim_purge_old_kernels: true` in its
slim play and ran `qemu/checks/rocm-golden.sh`, these assertions included,
on the booted guest before it pushed `20261117.g80c17f8`; the push
happened, so the purge, the guard, the leftovers removal, the `GRUB_TOP_LEVEL`
pin and the resolver all passed on a real guest and real `grub-mkconfig`
output. Two guests booted from that published artifact on the dev host then
reported `7.2.6-070206-generic` and one installed kernel image. The publish
run at `481c892` did the same before it pushed `20261117.g481c892`, and its
check printed `kernel: 7.2.6-070206-generic is the only kernel installed and
the one grub boots`. What that
does not cover: the purge, the guard and the leftovers script are otherwise
tested against fixtures (`tests/test_kernel_purge.py`), and the resolver
against `grub.cfg` text written out the way resolute's templates write it,
so a cloud image whose kernel packages differ from that day's, or a
`grub.cfg` laid out differently, is still first met in a bake — where the
guard fails loudly if apt wants anything the purge list does not expect.

## Two rdma-cores, one to build against

**The golden image has two rdma-cores. One runs, and only that one can be
built against.**

| | Where | What is there | Why it is there |
|---|---|---|---|
| the archive's 61 (`61.0-2ubuntu3`) | `/usr` | the runtime packages: `rdma-core`, `ibverbs-utils`, `ibverbs-providers`, `libibverbs1`, `librdmacm1t64`, `libibumad3`. **No headers, no `lib*.so` link, no `.a`, no `.pc`** | see "what stays" below |
| rocm-ernic's 66 | `/usr/local` | a whole install from source, by the collection's `ernic_srq_provider`: 30 headers in `include/infiniband` and four in `include/rdma`, `libibverbs`, `librdmacm`, `libibumad` with their unversioned links, the pkg-config files, the providers with the SRQ-capable ionic one, `ibv_devinfo` and the rest | it is the provider the emulated NIC needs for SRQ |

**66 is what runs.** `/etc/ld.so.conf.d/libc.conf` has `/usr/local/lib`
ahead of the multiarch directory, so the loader gives every program the
`/usr/local` libraries, and `/usr/local/bin` is ahead of `/usr/bin` on every
PATH the image has.

**The archive's development packages are gone:** `libibverbs-dev`,
`librdmacm-dev` and `libibumad-dev`. With them the image had two rdma-cores
to *build* against and the toolchain did not pick one: gcc reads
`/usr/local/include` ahead of `/usr/include` (66), ld searches
`/usr/lib/x86_64-linux-gnu` ahead of `/usr/local/lib` (61), and the loader
gives 66. A program built with no flags compiled against 66, linked 61 and
ran against 66. That is what broke the perftest link (`undefined reference
to ibv_alloc_buf`, which only 66 has; see the next section), and where it
does not fail it silently compiles out what the newer library has. Without
them all three answers are `/usr/local`, with no flag.

**What stays, and why.** The runtime packages: `libibverbs1` is depended on
by `perftest`, `fio`, `librados2`, `libpcap0.8t64`, `ibverbs-utils`,
`librdmacm1t64` and `ibverbs-providers`; the `rdma-core` package carries the
udev rules and `rdma-hw.target`, `rdma-load-modules@.service` and
`rdma-ndd.service`; `vm-quickstart.yml`'s RDMA test runs
`/usr/bin/ib_send_bw` by path; and `provider.stamp` and
`rocm-golden.sh`'s provider checks read the archive's `libionic` and the
package version. Also `libnl-3-dev` and `libnl-route-3-dev`, which
`libibverbs-dev` used to pull in: the rdma-core 66 build needs them, fio's
configure links them, and the check's `pkg-config --exists` reads them, so
both are named in the manifest and in `rocm_ernic_setup_packages`. And the
29 kernel uapi headers in `/usr/include/rdma` (`ib_user_verbs.h` and so on),
which are `linux-libc-dev`'s and which 66's `verbs.h` includes.

**How the image gets there.** Nothing installs the three, and a purge makes
it true for a guest that has them anyway:

| Piece | Where |
|---|---|
| not installed | gone from `qemu/packages.d/rocm-golden.txt`, from `rocm_ernic_setup_packages`, and from `perftest_setup_packages` |
| the purge | the play "Take the archive's rdma-core development packages out of the guest", after everything that compiles and after the compiler cache is saved, before the stamp. `rocm_ernic_setup`'s `rdma_dev_purge` tasks |
| the guard | `files/rdma-dev-purge-guard.sh` asks `apt-get --simulate purge` first and fails unless what goes is exactly the packages asked for. No companion is allowed: there is none. Before that, the purge is refused unless `/usr/local` has a header and a link library for each of libibverbs, librdmacm and libibumad (`include/infiniband/verbs.h`, `include/rdma/rdma_cma.h`, `include/infiniband/umad.h`, `lib/<library>.so`) |
| never `autoremove` | on the published image it would take eleven unrelated packages that were already marked automatic (`hwdata`, `pnp.ids`, the `linux-tools-7.0.0-43` set and others) |
| the check | the `# begin: rdma-core` block of `qemu/checks/rocm-golden.sh`, and the same lines as `rocm_ernic_setup/files/rdma-core-check.sh`, which is the last task of the stamp play |

On this bake's own guest the purge finds nothing and says so. It is there
for a bake given another manifest, an `--ansible-only` rerun against a guest
baked before this, or a role that puts one back; all end in the same state.

**The check needs no device.** The three packages are not installed and the
runtime ones are; no `/usr/include/infiniband`, no `/usr/include/rdma/rdma_cma.h`,
and for each of the three libraries no `.so`, `.a` or `.pc` under
`/usr/lib/<multiarch>`, `/usr/lib`, `/usr/lib64` or `/usr/share/pkgconfig`;
pkg-config's `libdir` and `includedir` for each are `/usr/local/lib` and
`/usr/local/include`; a three-line program per library, compiled and linked
with no flag but `-l` and never run, reads its header from
`/usr/local/include`, links and loads one file, and that file is under
`/usr/local/lib`; and the stamp names both rdma-cores as they are. It tests
files, not `ldconfig -p`, which went on listing the removed links after the
purge until `ldconfig` was run (the purge tasks run it).
`tests/test_rdma_dev_packages.py` holds the block and the script byte for
byte equal and runs them against stand-ins and against the host's real
toolchain.

**The stamp records both.** `/etc/rocm-golden-guest.json` had one field,
`rdma_core_version`, which is the archive package's version, although 66 is
what runs. It now has `rdma_core_archive_version` (`61.0-2ubuntu3`),
`rdma_core_local_version` (`66.0`) and `rdma_core_local_libibverbs`
(`/usr/local/lib/libibverbs.so.1.18.66.0`). The local two are read off
`ibv_devinfo`: the file its `libibverbs.so.1` resolves to, whose name ends
in its rdma-core's version. `rdma_core_version` is kept, still the
archive's, because the record is published in `vm-info.json` under `guest`
and its names are added to and not renamed; nothing in this repository
reads it, and a code search of the owner's other repositories found no
reader.
`publish_qcow.py` carries the stamp whole and names none of these, so it
did not change.

**What a consumer building against verbs in the guest must know.**

- Use no flags: `gcc prog.c -libverbs`, or `pkg-config --cflags --libs
  libibverbs` (`-I/usr/local/include -L/usr/local/lib -libverbs`). Both
  give 66 all the way.
- **Do not `apt install libibverbs-dev`** (or the other two). It installs
  cleanly and restores the mix. rocm-ernic's own
  `ansible/playbooks/s3-tests.yml` installs it in a guest, and so do
  `sbates130272.batesste`'s `rdma_setup` and `nfs_rdma_setup`; run against a
  golden guest, each puts it back. `qemu/checks/rocm-golden.sh` then fails.
- `perftest_setup` has `perftest_setup_rdma_core`. `local`, which the
  golden playbook sets, installs no rdma-core package and sends the linker
  to `perftest_setup_rdma_lib_dir`. `archive`, the role's default, installs
  the three, for a guest whose only rdma-core is the archive's; it is
  refused, before anything is installed, on a guest that has
  `<lib dir>/libibverbs.so`.
- Two things went with the packages: the verbs API man pages (the 66 build
  is `-DNO_MAN_PAGES=1`, so `man ibv_reg_mr` finds nothing) and static
  linking against rdma-core (no `.a` in `/usr/local`). The saving is 3.1 MB.
- The P4 client and benchmark were never on the 61 link path: they are
  built through pkg-config, which resolved to `/usr/local` with the packages
  present too. fio, which `scripts/vm-report/fio-hipfile-bench.sh` builds in
  the guest, probes verbs by itself with bare `-l` flags and so was the mix
  on the old image; on this one it is 66.

**A `vm-basic.yml` image is not touched.** It has no rdma-core in
`/usr/local`, and its manifests (`packages-default`, `packages-minimal`)
name no rdma package, so there is nothing to purge. A basic guest that
wants to build against verbs installs the archive's `-dev` packages, and
there they are the only rdma-core and correct.

**Measured on a live guest, not in a bake.** On one guest from the
published image `20261117.g481c892` (2026-11-17, kernel
`7.2.6-070206-generic`, gcc 15.2.0, binutils 2.46, the emulated NIC
attached): `apt-get purge` of the three removed those three and nothing
else, 482 paths, and the autoremove list was the same eleven packages
before and after. Then, with them gone: perftest built through the role
(`ok=23 changed=3 failed=0`, the probe printing `ok: one rdma-core: headers
/usr/local/include/infiniband/verbs.h, library
/usr/local/lib/libibverbs.so.1.18.66.0` with and without the `LDFLAGS`) and
moved 1000 iterations of `ib_send_bw` over the NIC from both
`/usr/local/bin` and `/usr/bin`; the P4 client and benchmark rebuilt
byte-identical to the image's and ran; rocm-ernic's rdma-core 66 built in a
mount namespace with no rdma-core header, link or `.pc` anywhere (360 of
360 ninja steps), so nothing before it in a bake needs the packages; the
`ionic-ernic` DKMS package built; fio configured with `libverbs yes`,
`rdmacm yes`; and an earlier form of the check block passed there.

**What has not run:** a bake with any of this. The purge tasks and the
guard have run against stand-ins and, for the no-op and the two refusals,
through `ansible-playbook` on the dev host; the check as it is now has run
against stand-ins and the dev host's toolchain, not on a guest (the form
that ran on the guest had the same compile, link and load steps and no
error messages, no stamp comparison and fewer directories). The stamp's new
fields have been evaluated by Ansible with made-up facts. The first
playbook-test bake is the proof: read the purge play's report, which should
say there was nothing to purge, and the last task of the stamp play.

**The guest check now refuses the published image**, which has the three
packages and the one-field stamp, as it already refuses it for want of
`/etc/perftest-rocm.json`. Only the lanes that bake run the check here.

## perftest with ROCm memory support

Ubuntu's `perftest` is built without ROCm, so its `ib_send_bw` has no
`--use_rocm`. The golden bake builds the tools from source with the
`perftest_setup` role and installs them in `/usr/local/bin`. What it builds
by default is the owner's fork, <https://github.com/sbates130272/perftest>,
branch `feat/rocm-data-validation-legacy` at
`e68c74e910451682ba1dc91b96602fbc7afaa372` — the head of the open upstream
pull request linux-rdma/perftest#419, which adds `--data_validation` for
`--use_rocm`. `--enable-rocm` itself is upstream (#418).

**Both builds are in the image, and which one runs is by path.** The
archive's package stays, in `/usr/bin`; the ROCm build is in
`/usr/local/bin`. A bare `ib_send_bw` is the ROCm build in every shell that
has a PATH of its own, and `qemu/checks/rocm-golden.sh` asserts each: the
non-login SSH session (`/etc/environment` through pam_env, which
`rocjitsu_setup` writes as `<rocm>/bin:` plus the cloud image's line), a
login shell, and sudo's `secure_path`. All three have `/usr/local/bin` ahead
of `/usr/bin` (seen on a guest of the published image: `command -v
ib_send_bw` gave `/usr/local/bin/ib_send_bw` in each); it is the same fact
the bake's ccache hook rests on. The
package was kept on purpose: `vm-quickstart.yml`'s RDMA test names
`/usr/bin/ib_send_bw`, so `quick-start test` and the daily lane run the
stock build exactly as before, and it is the baseline to compare a
work-in-progress branch against. The check fails an image that has lost
either copy. The stock build prints its help in 5 ms and the ROCm one in
32 ms (measured on a guest): it loads libamdhip64 and, through it, LLVM.

`make install` puts more under the prefix than the eight `ib_*` tools the
role checks: `raw_ethernet_bw`, `raw_ethernet_lat`, `raw_ethernet_burst_lat`
and `raw_ethernet_fs_rate`, the scripts `run_perftest_loopback` and
`run_perftest_multi_devices`, and 13 man pages in
`/usr/local/share/man/man1`. They shadow the archive's copies the same way,
and nothing asserts them. No library and no header is installed.

**The guest check now refuses an image baked before this.** Its first
assertion is that `/etc/perftest-rocm.json` exists, and the image
`<backing-ref>` pulls today has none. Inside this repository the check runs
only against a fresh bake (the playbook-test and publish lanes), so nothing
here turns red; batesste-ci-images runs the same file against the
*published* image, and has to move to an image baked from a commit that has
this before it takes this version of the check.

**The recipe**, from the repository's own ROCm workflow
(`.github/workflows/ubuntu24_04_rocm7_2.yaml`, green on #419 for gfx950 and
gfx1250) and `configure.ac`:

```
./autogen.sh
./configure --prefix=/usr/local --enable-rocm --enable-rocm-dmabuf \
    --with-rocm=/opt/rocm/core-10.1 --with-rocm-arch=gfx1250 \
    HIPCC=/opt/rocm/core-10.1/bin/hipcc LDFLAGS=-L/usr/local/lib \
    cross_compiling=yes SIMD_CFLAGS=-msse4.2 \
    'CPPFLAGS=-DHAVE_SSE42=1 -include xmmintrin.h'
make -j"$(nproc)" && make install
```

The last two lines are not in the repository's workflow. They name the SIMD
level of the host-memory validator, which `configure` otherwise takes from
the CPU of the machine that builds; see "The CPU the tools are built for"
below.

**`LDFLAGS=-L/usr/local/lib` was not optional on the golden guest as it
was published, and the first version of the role, which lacked it, failed
there.** What follows is that image, which had the archive's rdma-core
development packages; "Two rdma-cores, one to build against" above is what
replaced it, and on an image without them the linker finds `/usr/local/lib`
by itself. The flag stays on the configure line for the guest that still
has them. That image had two rdma-cores to build against: the archive's 61
under `/usr` (`libibverbs-dev 61.0-2ubuntu3`), and the whole of rocm-ernic's
patched rdma-core **66** under `/usr/local` — headers in
`/usr/local/include/infiniband`, `libibverbs.so.1.18.66.0`, `librdmacm`,
`libibumad` and the providers in `/usr/local/lib` — which
`ernic_srq_provider` installs and every tool on the image loads. "rdma-core
61 plus a patched provider" undersells it. Three search orders then
disagreed:

| | searches first | so it uses |
|---|---|---|
| gcc, for `<infiniband/verbs.h>` | `/usr/local/include` | 66 |
| ld, for `-libverbs` (perftest's `configure` uses `AC_CHECK_LIB`, no `-L`, no pkg-config) | `/usr/lib/x86_64-linux-gnu` | 61 |
| the loader (`ld.so.conf`) | `/usr/local/lib` | 66 |

Measured on both guests of a stack from the published image
`20261117.g481c892` on 2026-11-17, with the role's defaults as they then
were: `configure` succeeded, defined `HAVE_CC_UNPROTECTED_ALLOC` from the
66 headers, and the link of all eight tools failed after 18.94 s with

```
perftest_resources.c:2370: undefined reference to `ibv_alloc_buf'
perftest_resources.c:2402: undefined reference to `ibv_free_buf'
collect2: error: ld returned 1 exit status
```

because 61 has neither function. With `LDFLAGS=-L/usr/local/lib` and
nothing else changed the same guests built it: `ok=19 changed=3 failed=0`
on both, 18.76 s for the build task on 8 vCPUs with no compiler cache,
5.10 s for the fetch, `libibverbs.so.1 => /usr/local/lib/libibverbs.so.1`,
and a rerun compiled nothing (`ok=12 changed=0`). The loud failure is the
lucky case. Where the newer headers declare nothing the older library
lacks, the mix links, and a feature the loaded library has is compiled out
without a word: under the same mix `configure` printed `checking for
ibv_dm_export_dmabuf_fd in -libverbs... no`, and with the `-L` it printed
`yes` and the tools gained `--use_ib_dm_dmabuf`.

So the role has `perftest_setup_rdma_core`, which the golden playbook
sets to `local`, and `perftest_setup_rdma_lib_dir` (`/usr/local/lib`), which
then becomes that `LDFLAGS`, and `files/perftest-rdma-probe.sh`, which runs
before the build with the arguments `configure` is about to get. It
compiles, links and resolves one two-line program and prints the
`infiniband/verbs.h` the compiler read, the `libibverbs.so` the linker took
and the `libibverbs.so.1` the loader gives; the role stops unless link and
load are one file and the headers are from the same prefix, in seconds and
with a sentence instead of sixty undefined references. With `local` the
library linked also has to be in that directory, so a guest that has only
the archive's rdma-core is refused and told to say `archive`, which installs
the archive's development packages and adds no `LDFLAGS`. After the build
the role compares
what `ib_write_bw` loads with what the probe linked, records both the
headers and the library in the stamp, and `qemu/checks/rocm-golden.sh`
compares the stamp's library with the one the booted image loads.

**That fix ran on a live guest only as the configure line**
(`-e '{"perftest_setup_configure_extra":["LDFLAGS=-L/usr/local/lib"]}'`,
which gives the byte-identical line the role now builds itself, and
`tests/test_perftest_setup.py` holds it to that line). The variable, the
probe, the post-build comparison and the new stamp fields have not run on a
guest or in a bake. What they ran on is the dev host, the image's Ubuntu
release with the same `libibverbs-dev 61.0-2ubuntu3` (gcc 15.2, binutils
2.46), with an rdma-core 66 tree put ahead of the archive's by
`C_INCLUDE_PATH` and `LD_LIBRARY_PATH`: without the directory
the pinned commit failed at the same two source lines as on the guest and
the role stopped at the probe; with it the role built, recorded
`libibverbs.so.1.18.66.0`, and a rerun compiled nothing. The first
playbook-test bake is still the proof; read the probe's `ok:` line in it.

Since then the role as it stood at `5f2dd6b` has run end to end on a guest,
with the archive's development packages purged and its package list given
without them (see the section above): the probe, the post-build comparison
and the stamp all ran there. What has run nowhere but against stand-ins and
`ansible-playbook` on the dev host is what was added for that change:
`perftest_setup_rdma_core`, the refusal of `archive` beside another
rdma-core, and the probe's check that the library is in the directory named.

`--with-rocm=DIR` adds `DIR/include` and `DIR/include/hsa` to the include
path and `DIR/lib64` and `DIR/lib` to the link line and to the tools'
RUNPATH. With `--enable-rocm`, `configure` looks for `hipcc`
(`AC_CHECK_TOOL`), and what it finds decides which of two builds results:

| | with hipcc | `HIPCC=no`, or none found |
|---|---|---|
| `src/rocm_kernels.hip` | compiled for one GPU and linked into every tool (`HAVE_ROCM_KERNELS`) | not built |
| `--data_validation` with `--use_rocm` | a kernel resident on the GPU checks each cycle as it lands; falls back to the other column when the kernel will not launch | the buffer is copied to the host once, after the run, and compared there |
| the stamp's `device_code` | `true` | `false` |

`PERFTEST_ROCM_VALIDATION=staged` selects the right-hand behaviour in a
build that has both. Which path ran is printed: `[DATA_VALIDATION] rocm:
live GPU validation` or `... host-staged validation`.

**The role's variables** (`roles/perftest_setup/defaults/main.yml`):

| Variable | Default | |
|---|---|---|
| `perftest_setup_repo` | `https://github.com/sbates130272/perftest.git` | the repository |
| `perftest_setup_ref` | `feat/rocm-data-validation-legacy` | what is fetched, at depth 1: a branch, a tag or a full commit id |
| `perftest_setup_commit` | `e68c74e9…a372` | the commit that ref has to be. Compared before anything is compiled. Empty turns the check off, and nothing else does |
| `perftest_setup_rocm_path` | `/opt/rocm` | the ROCm tree, or a path whose `bin/` links into it; the bake passes `/opt/rocm/core-<line>` |
| `perftest_setup_prefix` | `/usr/local` | install prefix |
| `perftest_setup_rocm_kernels` | `true` | build the HIP device code |
| `perftest_setup_rocm_arch` | `gfx1250` | the GPU it is built for; required with the line above |
| `perftest_setup_rocm_dmabuf` | `true` | `--enable-rocm-dmabuf`, which adds `--use_rocm_dmabuf` |
| `perftest_setup_rdma_lib_dir` | `/usr/local/lib` | the directory of the libibverbs the tools link, as `LDFLAGS=-L<dir>`; empty adds nothing |
| `perftest_setup_configure_extra` | `[]` | arguments added to the configure line; a `CFLAGS=` or `LDFLAGS=` here replaces the one before it. A `CPPFLAGS=` is refused: the role writes that one |
| `perftest_setup_cppflags` | `[]` | preprocessor flags added to the role's `CPPFLAGS=`, `-I<dir>` for one; the rdma-core probe reads them |
| `perftest_setup_simd` | `sse4.2` | the SIMD level of the host-memory validator: `sse4.2`, `avx2`, `none`, or empty to leave it to `configure` and the build machine's CPU |
| `perftest_setup_mm_pause_workaround` | `true` | add `-include xmmintrin.h`, for a bug in the pinned tree; cannot be turned off while the tree has it |
| `perftest_setup_keep_source` | `false` | keep `/usr/local/src/perftest` (`perftest_setup_source_dir`); the role removes the tree only in a run that fetched it |
| `perftest_setup_force` | `false` | build even when the stamp says there is nothing to do |

It records the build in `/etc/perftest-rocm.json` — repository, ref, the
commit built and the commit required, the configure line, the ROCm tree,
the rdma-core headers and the libibverbs it was built against
(`rdma_headers`, `libibverbs`, real paths, so the library's version is in
the name), whether the binaries carry device code, read off the binary
with `nm` and not copied from the request, the SIMD level the validator was
configured for and whether the role named it (`simd`, `simd_forced`), and
whether the `_mm_pause` workaround was applied (`mm_pause_workaround`). A
rerun that finds the stamp describing
the build it was asked for, and the tools in place, compiles nothing; the
tools are checked either way.

**The pin is `perftest_source_pin` in `vm-rocm-golden.yml`**, an anchor on
the perftest play beside `perftest_source_repo` and `perftest_source_ref`,
and the stamp play reads it by alias. A branch is a name for whatever was
pushed last, so `files/perftest-fetch.sh` compares what the fetch brought
with the pin and a moved branch **fails the bake**, naming both commits,
before a line is compiled. That will happen: this branch is the head of an
open pull request. Three ways on, in order of preference:

- move the pin to the new head: `git ls-remote <repo> <ref>` prints it. The
  comment that ends the pin's line names the ref and
  `tests/test_perftest_setup.py` fails when the two disagree. The role's
  defaults and the README's "Golden perftest" badge are held equal to the
  playbook's values by the same test, so a move is the playbook's lines,
  the two defaults and the badge, and the test names whichever was missed;
- keep building the old commit: set `perftest_source_ref` to the commit id
  as well. GitHub serves a commit by id for as long as it holds it, which
  for one a force-push orphaned is not for ever. A tag in the fork would
  make a pin durable;
- another branch, `feat/rocm-data-validation` at
  `9ed580b03bfd4a9c6855b0f2d604a4515635d906` for one: the same three lines,
  or for one bake `-e perftest_source_ref=... -e perftest_source_pin=...`
  (`ANSIBLE_EXTRA_ARGS` with `gen-vm`), which reaches both plays.

`qemu/checks/rocm-golden.sh` spells no pin. It compares
`/etc/perftest-rocm.json` with the `perftest_source_pin` the bake wrote to
`/etc/rocm-golden-guest.json`, and refuses a build made with the commit
check off.

**No device is needed, and each part of that was established**, not
assumed. The ROCm probes in `configure` compile and link and run nothing;
the two programs it would run ask the CPU about AVX2 and SSE4.2, and the
role stops it running them (next section). hipcc
compiles for the GPU it is told. The role runs each tool with `--help` and
nothing else; perftest prints its usage while parsing arguments, and a
binary carrying a HIP kernel registers it at load without touching the GPU.
Run end to end in an `ubuntu:26.04` container with the ROCm 10.1 tree
mounted and no `/dev/kfd`, `/dev/dri` or `/dev/infiniband`: the pinned build
took 23 s, a rerun compiled nothing, a pin the branch was not at failed
before the build and left no tree, and the check's block passed. That
container had no rdma-core in `/usr/local`, which is why it did not see the
link failure above.

### The CPU the tools are built for

**The first golden bake with this role failed in the compiler**, in the
playbook-test lane of pull request #240 (run 44329944669), eleven minutes
in:

```
checking for AVX2 runtime CPU support... no
checking for SSE4.2 runtime CPU support... no
src/host_validation.c:33:23: error: implicit declaration of function '_mm_pause'
```

Two things, and neither is the build machine being short of anything:

- **A bake's guest has QEMU's default CPU model.** `gen-vm` boots the image
  it bakes with `-machine q35,accel=kvm` and no `-cpu`
  (`gen_vm._arch_args_for_gen`), which under KVM is still `qemu64`: the
  guest's kernel prints `AMD QEMU Virtual CPU version 2.5+ (family: 0xf,
  model: 0x6b)` and `x87 FPU will use FXSAVE`, and of `ssse3 sse4_1 sse4_2
  popcnt avx avx2 xsave` its `/proc/cpuinfo` has none, only `pni`.
  `run-vm`, and so every stack, passes `-cpu EPYC`, which has `sse4_2` and
  `avx2`. So anything in a bake that asks the CPU what it has is told
  "x86-64 baseline", whatever the host is.
- **perftest's `configure` asks.** `configure.ac` decides `HAVE_AVX2` and
  `HAVE_SSE42` with `AC_RUN_IFELSE`: it compiles a program that returns
  `__builtin_cpu_supports("avx2")` and runs it, with no switch and no
  cache variable. Makefile.am then gives `-mavx2` or `-msse4.2` to
  `host_validation.o` alone. On the bake's CPU both programs exit 1
  (`config.log`: the link succeeds, `./conftest`, `$? = 1`), neither macro
  is defined, and the tree then does not compile at all: `cpu_relax()` is
  `_mm_pause()` for every x86 build, and the header that declares it is
  included only under `HAVE_AVX512`, `HAVE_AVX2` or `HAVE_SSE42`. gcc 14 and
  later make an implicit declaration an error; gcc 13 only warns and the
  build then stops in the linker, on an undefined reference to `_mm_pause`.

Measured on 2026-11-18 on one scratch guest of the published image, no
device attached, booted both ways. The same test program, built by hand
with and without the ROCm libraries on its link line, exits 1 on the
default model and 0 on EPYC, so it is the CPU and nothing about ROCm or a
missing `/dev/kfd`. The role as it was at `40eb051` fails there exactly as
the lane did, and on the EPYC boot it builds and installs **AVX2** code:
`#define HAVE_AVX2 1`, and 29 instructions on `ymm` registers in
`ib_write_bw`. That is why it had built on two live guests and in a
container the day before, and it is the other half of the problem: an image
carried whatever its bake machine's CPU was.

**An AVX2 build on a CPU without AVX2 does not crash; it refuses.**
`host_validation_runtime_supported()` in `src/perftest_parameters.c` asks
the CPU it is started on for the level it was compiled for, and
`--data_validation` on host memory then exits 1 with `--data_validation
requires host CPU SIMD support (AVX2/SSE4.2/NEON)` before a device is
looked for. Seen with the AVX2 build on the default model. `--use_rocm`
validation does not go through it. So the cost of a level the CPU lacks is
a missing feature, with a message.

**The level is `perftest_setup_simd`, and the image's is `sse4.2`.**

| Level | Needs | Where host-memory `--data_validation` is refused |
|---|---|---|
| `sse4.2` | x86-64-v2 | a CPU whose CPUID has no `sse4_2`: QEMU's default model, and hardware older than 2008 (Intel) or 2011 (AMD) |
| `avx2` | x86-64-v3 | any CPU or CPU model without AVX2, Intel before Haswell and QEMU models older than that included |
| `none` | nothing | everywhere: there is no validator |
| empty | the build machine's | wherever the CPU lacks what the build machine had |

`sse4.2` because it is the lowest level the tree has a validator for, every
CPU a fleet can run on has it, QEMU's EPYC model has it, and a host without
AVX2 runs the same code as one with it. What it costs is the rate of
host-memory validation and nothing else: 16 bytes a compare where AVX2 does
32. `none` would give the feature up to save nothing. The validator's
instructions at this level are `movdqu`, `pcmpeqb`, `pmovmskb` and one
`pshufb`, read off the binary.

**How the level is named, without touching the tree.** No patch: the stamp
says the tools are the pinned commit, and they are.

| On the configure line | What it does |
|---|---|
| `cross_compiling=yes` | autoconf's word for "the machine that builds is not the one that runs". `AC_RUN_IFELSE` then runs nothing and takes its cross-compiling answer, which `configure.ac` gives as "no" for both. That is why both `runtime CPU support` lines read `no` in the output at any level |
| `SIMD_CFLAGS=-msse4.2` | `configure.ac` sets the variable only when a probe says yes, so the value given survives to `AC_SUBST` and to the one object |
| `CPPFLAGS=-DHAVE_SSE42=1 ...` | the macro `config.h` would have defined. One `CPPFLAGS=`, written by the role, which is why `perftest_setup_configure_extra` may not carry another |

None of the three is an interface the tree promises, so
`files/perftest-simd-check.sh` reads the result back between `configure`
and `make`: the `HAVE_` macros a file that includes `config.h` sees with
the flags make gives `host_validation.o`, taken in the order
`host_validation.c` takes them, and the Makefile's `SIMD_CFLAGS`. The flags
are the Makefile's `DEFS`, `CPPFLAGS`, `CFLAGS` and `SIMD_CFLAGS`, in
automake's order, and not `CPPFLAGS` alone: the role refuses a `CPPFLAGS=`
in `perftest_setup_configure_extra` but still takes a `CFLAGS=`, and with
only `CPPFLAGS` read a `CFLAGS=-DHAVE_AVX2=1 -mavx2` there was reported as
`sse4.2` while the compile line built AVX2: `host_validation.o` compiled
with those flags from the pinned source has 29 instructions on `ymm`
registers, and none with the role's own line (the object alone, with gcc
15.2 on the dev host; no build was run that way, so that the register
count below then fails it is by construction and not something seen). A
level other than the one asked for stops the build before it compiles,
with the reason. What the script cannot see is a `CFLAGS=-mavx2` or
`-march=` with no macro, which leaves the validator at its level and lets
the compiler use AVX in the other objects (`ymm` instructions in four of
the seven other sources that compile on the dev host); that one is the
register count's. After the build,
`verify.yml` counts instructions on `ymm` and `zmm` registers in
`ib_write_bw`, which are there exactly when the level is AVX2 or AVX-512,
and the role fails when that disagrees with the level. The guest check
then requires `simd` to be `sse4.2` and `simd_forced` to be true, and
counts the same instructions itself. That one rule is spelled in the check
on purpose: a level passed in from the playbook would pass whatever it was.

`cross_compiling=yes` also makes `configure` warn `using cross tools not
prefixed with host triplet`; the compiler is still `gcc`. A function-like
`-D__builtin_cpu_supports(x)=...` to answer the probes was not usable: the
tools call the builtin at run time for the refusal above.

**The `_mm_pause` workaround is `perftest_setup_mm_pause_workaround`**, on
by default: `-include xmmintrin.h` in `CPPFLAGS`, the header that declares
it. At `sse4.2` and `avx2` the tree compiles without it, because the SIMD
header brings the declaration in, so it only decides whether `none` and an
empty level on a CPU with neither can build. It is on at every level so
that changing the level does not trip over it. **Turn it off when the pin
moves to a commit with the fix.** The check script compiles
`src/host_validation.c` with no SIMD macro and no forced include, and a
tree that still has the bug with the variable off fails the bake there, in
words; a tree that no longer has it says so in the play output while the
variable is still on. That compile carries
`-Werror=implicit-function-declaration`, so the answer is the tree's and
not the compiler's default: without it gcc 13 compiled the pinned file
with a warning, the script reported no bug and said the workaround could
go, and a build that believed it would have failed at link (run with
gcc 13, 14 and 15 against the pinned source and against the source with
the fix; all three now agree both ways). The fix in the fork is one line, `#include
<immintrin.h>` where `cpu_relax()` is defined, and a `--with-simd=LEVEL`
switch in place of the run-time probe would turn the three arguments above
into one. Both were built on the scratch guest as patches against
`e68c74e`, at every level, and written up for the owner; nothing was pushed
to the fork.

What ran, on 2026-11-18, on that scratch guest with the fix: on the default
model, the role built `sse4.2` (`validate_sse42` in the symbol table, no
`ymm`), a rerun compiled nothing, the workaround turned off was refused
before the compile, and `avx2`, `none` and empty each built what they name;
on the EPYC model, where the unfixed role had built AVX2, it built
`sse4.2` again, and `ib_write_bw --data_validation` got past the SIMD
refusal to `Unable to find the Infiniband/RoCE device`. The `ib_write_bw`
of the two builds is the same file, byte for byte. The perftest block of
the guest check passed against that build, refused an `avx2` build by its
stamp, and refused the same binary under a stamp edited to say `sse4.2`
(`29 instructions on ymm or zmm registers`). The rest of the guest check
was not run there: the guest was the published image, which it refuses for
other reasons. **No bake has run with it**,
and no `--data_validation` transfer has: the runs recorded for 2026-11-17
were on builds made with the probes saying yes, which is AVX2. The ROCm
ones do not use the host validator. The host-memory ones do, and at
`sse4.2` they compare half as many bytes at a time.

**`gen-vm` was left alone.** A bake on QEMU's default model is the
portable direction: whatever compiles for "this CPU" there compiles for
baseline x86-64. Giving the bake boots `-cpu EPYC` would have made this
role build, and published AVX2 tools.

**Why `perftest_setup_rocm_arch` is required.** Without `--with-rocm-arch`,
`configure` asks `rocm_agent_enumerator`, which prints nothing with no
device, and leaves the target to hipcc; hipcc 10.1 with no target and no
device compiles for **gfx906** (measured). A kernel for the wrong GPU builds
cleanly and fails at launch. Every fleet here serves gfx1250.
`--with-rocm-arch` takes one GPU: `configure` tests the value unquoted, so
a list is not expressible through it.

**The compiler cache covers the C sources and not the HIP one.** `gcc` is
resolved through PATH, so it is ccache when the bake put one there; hipcc
runs its own clang by path (`<rocm>/lib/llvm/bin`), which no symlink in
`/usr/local/bin` shadows. Measured with the hook in place: 164 ccache calls
a build, 70 of them cacheable; a cold build missed 63 and took 39 s, a warm
one hit 69 of 70 and took 22 s. So it adds 70 calls to a bake's 2053 and
about one miss to a warm bake. `perftest_source_pin` is in the cache key
(`bake-ccache-restore`) for the reason the ernic and SDXI pins are: 63
misses is below what the save's miss count notices, and without the key a
moved pin would recompile in every bake.

**The test on a live stack is optional and off, because on the stacks this
repository ships it fails.** The last play of `vm-quickstart.yml` runs
`ib_write_bw --use_rocm=0 --use_rocm_dmabuf --data_validation` between the
first and last guest when `quickstart_rocm_rdma` is true. It is
`ib_write_bw` and not `ib_send_bw`: validation is for the WRITE and READ
verbs, bandwidth tests, RC only, with `-t` and `-n` of at least 32;
`ib_send_bw` lists the flag in its help and exits 1 with `--data_validation
is only supported with WRITE or READ verb` (seen on a guest). For a WRITE
the **server** validates and prints the verdict, and does not fail its exit
status on a failed one, so the play reads the server's output from a file
and asserts `VALIDATION: PASSED` from either side and no other verdict from
both. It waits for the server for as long as the client was allowed, since
the verdict is written when the server exits. The server runs as root in a
transient unit and the client as the login user. `qemu-tool quick-start` has
no flag for it and no workflow sets it.

```bash
cd ansible && ansible-playbook -i <stack dir>/fleet-inventory.yml \
    playbooks/vm-quickstart.yml --private-key <key> \
    -e quickstart_rocm_rdma=true
```

**What a live default stack measured**, 2026-11-17: `rocm-golden.xml`
unmodified (stock QEMU), two guests of 8 vCPUs from the published image
`20261117.g481c892`, the role applied to both by a scratch playbook with
the `LDFLAGS` above, perftest 6.31 against the archive's 6.24. Guest 2 was
the server and guest 1 the client, the device `rocm-rdma-ernic0` as
`ibv_devinfo -l` gave it, every process left to exit on its own. The RDMA
port stayed `PORT_ACTIVE` and amdgpu `loaded` throughout.

| Run | Result |
|---|---|
| `/usr/bin/ib_send_bw --iters 1000 --size 65536` | 484.27 MiB/s, exits 0/0 |
| `ib_send_bw`, the ROCm build, host memory | 525.30 MiB/s, `Use ROCm memory : OFF`, exits 0/0 |
| `ib_write_bw --data_validation -t 128 -n 1000 -s 65536`, host memory | 466.12 MiB/s, `VALIDATION: PASSED - 896 chunks, 58720256 bytes`, exits 0/0 |
| `ib_send_bw --use_rocm=0`, between guests and in loopback | **fails at registration**, both sides, exit 1 |
| `ib_write_bw --use_rocm=0 --data_validation`, live and `PERFTEST_ROCM_VALIDATION=staged` | **fails at registration**, the same way |
| `ib_send_bw --use_rocm=0 --use_rocm_dmabuf` | registers; **first work request fails**, client exit 17; the server exits 17 after perftest's own 120 s wait |
| `ib_write_bw --use_rocm=0 --use_rocm_dmabuf --data_validation` (staged) | registers; **first work request fails**, both exit 1 within seconds |
| `PERFTEST_ROCM_HOST_ALLOC=1 ib_send_bw --use_rocm=0` | 552.44 MiB/s, `Use ROCm memory : ON`, exits 0/0 |
| `PERFTEST_ROCM_HOST_ALLOC=1 PERFTEST_ROCM_VALIDATION=staged ib_write_bw --use_rocm=0 --data_validation ...` | 642.09 MiB/s, `[DATA_VALIDATION] rocm: host-staged validation`, `VALIDATION: PASSED - 7 chunks, 58720256 bytes` |
| the same without `staged` | 440.58 MiB/s, `[DATA_VALIDATION] rocm: live GPU validation`, `VALIDATION: PASSED - 7 chunks, 58720256 bytes` |

So, on the default stack:

- **Does `--use_rocm` move data from VRAM? No, and it fails in two
  different places.** Plain `--use_rocm` gets as far as HIP coming up and
  `hipMalloc` succeeding (`Using ROCm Device with ID: 0, Name: AMD Radeon
  Graphics, PCI Bus ID: 0x2, GCN Arch: gfx1250`, `allocated 131072 bytes of
  GPU buffer at 0x...`), and then `ibv_reg_mr` of that address is refused:

  ```
  Couldn't allocate MR with error=14
  failed to create mr
  Failed to create MR
   Couldn't create IB resources
  ```

  Errno 14 is EFAULT, it comes back inside the guest, and the NIC's server
  logs nothing, so the device is never asked. That is the guest kernel
  declining to pin a GPU mapping for an ordinary memory registration, which
  makes this form **independent of which QEMU is pinned**: the fork image
  cannot change it. (The cause is read from where the error comes from, not
  from a trace of the kernel.)
- **`--use_rocm_dmabuf` is the form that reaches the NIC**, and the one
  issue 15 is about. `ionic_rdma` imports the dma-buf (`Calling
  ibv_reg_mr_ex with dmabuf(offset=0, size=131072, ...)`), the queue pairs
  connect, and the first work request fails:

  ```
   Completion with error at client
   Failed status-remote access error (10): wr_id 0 syndrom 0x9
   scnt=128, ccnt=0
  ```

  while the sender's `ernic-N` logs `WARN: ionic_datapath: QP 2 could not
  gather 65536 bytes`, 128 times, and `CQ 5 completing QP 2 in error: status
  9`. In loopback on one guest it is `local access error (8) ... syndrom
  0x7` and `RDMA write rkey=0x10e va=... moved 0 of 65536 bytes`. The NIC's
  server cannot read the GPU's memory: one vfio-user server reaching into
  another's BAR, on a QEMU that does not hand it the region. **Whether the
  fork QEMU makes this pass has not been tried**: `mi355x-server.xml` pins
  the fork as two refs (`<qemu-ref>` and `<ernic-ref>`), neither image was
  on the host, and there is no one-element overlay that swaps them into
  `rocm-golden.xml`. That is the experiment worth running next, and it is
  this form, not the plain one.
- **Does `--data_validation` pass with `--use_rocm`? Not reached for VRAM**:
  the transfer fails first. It passes on plain host memory, which also
  shows the emulated NIC does the atomics validation signals with, and it
  passes with `PERFTEST_ROCM_HOST_ALLOC=1`, both host-staged and with the
  live kernel on the GPU, so rocjitsu launches and retires the gfx1250
  validation kernel.
- **`PERFTEST_ROCM_HOST_ALLOC=1` is the control.** It allocates with
  `hipHostMalloc`, host memory the GPU can address, and with it everything
  works: HIP, the registration, the transfer, both validation paths. A stack
  that passes only with it set has a NIC that cannot reach VRAM and is
  otherwise sound, which is this one. It was run with `--use_rocm` alone;
  exporting a host-mapped buffer as a dma-buf has not been tried, so pair it
  with `quickstart_rocm_rdma_dmabuf=false`.
- **Further GPU processes in one boot are fine.** After `quick-start`'s
  `vector_add`, about a dozen more HIP processes ran on each guest, two at
  once in loopback, with no hang, and `amdgpu-probe --status` stayed
  `loaded`. The only new kernel lines were `amdgpu 0000:02:00.0: query gpu
  clock counter is not supported`.
- The guest's GPU has a 256 GiB BAR0 at `0x4000000000`, the kernel is
  7.2.6 with `CONFIG_PCI_P2PDMA=y`.

The play as it was before these results (plain `--use_rocm`) failed as the
table says, rc 2 with both sides shown; with `PERFTEST_ROCM_HOST_ALLOC=1`
added to its environment it passed. It now passes `--use_rocm_dmabuf` by
default (`quickstart_rocm_rdma_dmabuf`), so that turning it on tests the
layer that can change, and its verdict says when the buffers were
host-mapped memory and not VRAM. **In that form the play has not run**: its
command lines, retry count and messages were rendered locally, no more.

**perftest can exit 1 after a transfer that completed.** The first
`quick-start test` of that run failed at the *existing* stock RDMA step,
before any of this was applied: `Correlation coefficient r^2: 0.457788 <
0.9` / `Can't produce a report`, rc 1; the same command two minutes later
gave 484 MiB/s and exit 0. That is perftest's CPU-frequency calibration on a
busy host, not the transfer. It is not fixed here; the default RDMA test
and the optional play both assert the client's exit status and can both go
red on it.

**Still unproven:**

- **The role in a bake.** It ran on live guests of the published image
  (device present, amdgpu loaded, no compiler cache) and not through
  `gen-vm --bake`; and in the form that is committed, with the probe and
  the variable, only on the dev host. The first playbook-test run is the
  proof.
- **The guest check on a baked image.** Its block passed on a live guest
  against a scratch root holding the stamp and a copy of
  `/etc/rocm-golden-guest.json` with the pin added (the role alone does not
  write that key), before the block compared the stamp's `libibverbs`.
- **The fork QEMU**, as above.
- **The dma-buf export of a host-mapped buffer**, and READ
  (`ib_read_bw`), which nothing ran.

To repeat the measurements on two guests of a running stack, each line on
the server first and then on the client with the server's ernic address
appended (`DEV` from `ibv_devinfo -l`):

```bash
/usr/bin/ib_write_bw -d DEV -n 1000                                  # the stock build, as a baseline
ib_write_bw -d DEV -n 1000                                           # the ROCm build, host memory
ib_write_bw -d DEV -n 1000 -t 128 --data_validation                  # the NIC's atomics
ib_write_bw -d DEV -n 1000 --use_rocm=0                              # error=14 at registration, any QEMU
ib_write_bw -d DEV -n 1000 --use_rocm=0 --use_rocm_dmabuf            # the NIC reaching VRAM: issue 15
PERFTEST_ROCM_HOST_ALLOC=1 ib_write_bw -d DEV -n 1000 -t 128 --use_rocm=0 --data_validation   # the control
```

Let each run finish. A perftest killed mid-run leaves the guest's RDMA
device broken until the stack is recreated (issue 3). Every failure above
ended on its own; the slowest was the `ib_send_bw` server, which waits 120 s
for a message before it gives up.

## AppArmor and the snapd leftover

**`apparmor.service` is failed at every boot of `20261117.g80c17f8`, and of
any image baked before this fix; `20261117.g481c892`, which the fleet files
pull, was baked after it.** On the older image `systemctl
is-system-running` says `degraded`, and the journal has one line:

```
AppArmor parser error for /etc/apparmor.d in profile
/etc/apparmor.d/usr.lib.snapd.snap-confine.real at line 13: Could not open
'/var/lib/snapd/apparmor/snap-confine'
```

The cause is in `sbates130272.batesste.user_setup`, which `vm-basic.yml`
runs for every bake. With `user_setup_disable_snap` at its default it
removes the `snapd` package with `state: absent` and no purge
(`tasks/main.yml:186`), masks the units, and deletes `/snap`, `/var/snap`
and `/var/lib/snapd` (line 208). dpkg keeps a removed package's conffiles,
so snapd stays in state `rc` with `usr.lib.snapd.snap-confine.real` still in
`/etc/apparmor.d`, and line 13 of that profile is
`#include "/var/lib/snapd/apparmor/snap-confine"`: quoted, absolute, and not
`include if exists`. The unit's start is `apparmor.systemd reload`, which
parses every profile and fails on the first that does not load.

Reproduced without a guest. The dev host is the same release with the same
leftover (resolute, apparmor 5.0.2, snapd `rc`): `apparmor_parser
--skip-kernel-load --skip-read-cache --skip-cache /etc/apparmor.d` prints
that line word for word and exits 1, and a copy of the directory without
that one file parses clean, 161 entries, exit 0.

**The fix is in `vm-basic.yml`, as `post_tasks` of the play that runs the
role**: purge snapd, under the role's own two conditions, then restart
`apparmor.service`, which is what a boot does and fails the bake on a
profile that does not load. The purge removes the conffile and, through the
package's `postrm`, the profile's cache under `/var/cache/apparmor`. That
restart runs on the kernel the guest is on at that point, which in the
golden bake is still the cloud image's release kernel: the pinned one is
installed by the next play. So in the bake it proves the leftover is gone
and the profiles parse, and what proves they load on the pinned kernel is
the guest check on the two qualifying boots. Three choices in that:

- **Not in the collection.** `purge: true` on the role's task is the right
  fix and is the collection's to make; this is the purge it does not do.
  Ansible's `apt` module purges a package in state `rc` (it asks whether the
  package still owns files), so the task does something on a guest the role
  has already been through.
- **Not in `image_slim`.** `vm-rocm-golden.yml` imports `vm-basic.yml` with
  `image_slim_enable: false` and slims once at the end; a fix inside the
  slim pass would depend on a switch whose job is to turn that pass off.
  `post_tasks` of the base play reach both bakes, because the golden image
  is baked on that play.
- **After the roles, not before.** A purge ahead of the role's remove would
  leave the role to strand the conffiles again.

`vm-basic.yml` images have the same fault by construction: it is the same
play. None was booted to see it.

**`qemu/checks/rocm-golden.sh` asserts it on the booted guest**, between its
`apparmor` markers: `apparmor.service` is not failed, and the snap-confine
profile is not present with its include target gone, which is what the next
boot would trip over. It then prints `systemctl --failed`. It does **not**
assert that no unit failed. That check also runs on a boot with no device,
where `rocm-ernic-netcfg.service` waits 30 s for a NIC that is not there
and exits 1 (the script's last lines before it addresses anything), so a
blanket assertion fails a correct image; which other units fail without a
device has not been established. The assertion is an `if`, not
`! systemctl is-failed ...`: bash never applies `set -e` to a command
inverted with `!`.

`tests/test_bake_faults.py` pins where the purge sits, runs the check's own
lines against a stub `systemctl` and scratch files, and, where
`apparmor_parser` is installed, shows the parser stopping on an include of
a path that is not there.

**Seen in one bake, on that bake's boot, and on pulled guests.**
In the publish run of `20261117.g481c892` the purge reported changed, the
restart after it passed, and the guest check on the boot with no device
printed `apparmor: apparmor.service is not failed`, on the pinned kernel,
before the push. That the purge leaves nothing behind had been read from a
`snapd.postrm` on the dev host, which is snapd `2.76.3+ubuntu24.04` and not
the cloud image's package; the check's second assertion, that the leftover
profile is gone, passing in that run is what bears it out. `<backing-ref>`
now names that image in all three fleet files, so a fleet's guests no longer
boot degraded for this reason. `20261117.g80c17f8` is unchanged and its
guests still do. On two guests pulled from the registry with their devices
attached, by hand on the dev host, `systemctl --failed` was empty and
`systemctl is-system-running` said `running`, before `quick-start test` and
after it; `apparmor.service` was active, snapd was `un` to dpkg, and neither
the snap-confine profile nor `/var/lib/snapd` was there.

## The published guest image

**This repo bakes, qualifies and publishes the rocm-golden qcow2**, to
`docker.io/sbates130272/qemu-tool-rocm-golden` with the
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repository secrets;
batesste-ci-images pulls it and no longer builds guests. The workflow is
`qemu-minimal-publish-qcow-rocm-golden.yml`, the pusher is `qemu-tool
publish-qcow`, and the artifact format is in `qemu/qemu-tool/vm_artifact.py`,
which `gen_vm.py` (the puller) reads too — keep it the only place those
media types are spelled.

**The image's settings are in `qemu/stacks/rocm-golden.xml`, not in the workflow.**
Its `<bake-*>` elements (name, release, size, vCPUs, package manifest,
playbook, account) and `<publish-ref>`/`<publish-flavour>` are read by exactly
two commands:

```bash
qemu-tool gen-vm --bake --xml qemu/stacks/rocm-golden.xml
qemu-tool publish-qcow --xml qemu/stacks/rocm-golden.xml --tag <stamp>
```

**Three tags per publish, one manifest, the dated one first.** The workflow
passes `--tag <YYYYMMDD>.g<sha7> --tag latest --tag qm.<sha7>`. Consumers pin
the dated tag; `latest` rolls; `qm.<sha7>` is the qemu-minimal commit alone
and rolls only when one commit is published again. The order matters: the
keypair referrer attaches to the first tag and `--digest-file` reports it.

**Retention is `qemu-tool publish-qcow-prune`, run by the workflow after a
push and never on a dry run.** The policy the owner approved: keep the 10
newest dated tags and any dated tag pushed within 30 days; always keep
`latest` and every `qm.*`; delete the other dated tags. Those numbers are
the command's defaults (`--keep`, `--days`), stated nowhere else.

- **Only a tag matching `[0-9]{8}\.g[0-9a-f]{7}` exactly is ever deleted.**
  `latest`, `qm.*`, a `-dirty` build and anything pushed by hand are not of
  that form.
- **It is a dry run unless given `--yes`**, and `--dry-run` beats `--yes`.
  The subparser sets `allow_abbrev=False`, so `--y` is an error rather than
  a deletion.
- **Docker Hub only.** The tag delete is the Hub's own API, so the ref must
  read `docker.io/<namespace>/<repository>`; anything else is refused before
  a request is made. The registry is never guessed from a bare `me/repo`.
- **Credentials are `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` from the
  environment**, the same secrets the push uses, never a flag and never
  printed. This is not a setting driven by the environment: a credential is
  not a setting. **The token now needs delete access**, not only read and
  write.
- **A listing it cannot complete deletes nothing.** Every page must answer
  200, the pages must add up to the `count` the API reports, the count must
  not move between pages, and no tag may appear twice. Page URLs are built
  by the command; the `next` link in a response is never followed, and the
  transport refuses every redirect — `urllib` would otherwise follow a 30x
  on a GET and send the `Authorization` header to the host it names — so
  the token goes nowhere a response names.
- **A push time before 2013 is no push time.** Go services write
  `0001-01-01T00:00:00Z` for a time never set; read as a date it would make
  the tag the oldest and the first deleted. It is treated like a missing
  one: the tag is kept and takes none of the `--keep` slots.
- **A prune failure warns; it does not fail the publish run.** The image is
  out by then, and "re-run failed jobs" on a red publish bakes and pushes it
  again. The step prints a `::warning::` and writes the step summary.

**It removes tag names, and nothing else.** `vm-info.json` and the keypair
are a referrer of the image manifest. Docker Hub serves the OCI referrers
API — checked anonymously: `GET /v2/<repo>/referrers/<digest>` answers 200
with an index, and there is no `sha256-<hex>` tag in the listing — so oras
never creates the fallback tag there, and an earlier version's handling of
that tag matched nothing on the only registry this command supports. It is
removed. A deleted dated tag's manifest, its referrer and its blobs all
stay, and `qm.<sha7>`, on every published manifest and never deleted, keeps
the image reachable as well. So **this does not reclaim storage, and a
retired image's published keypair stays fetchable.** Changing either needs
a manifest delete by digest and a policy that lets old `qm.*` tags go, which
are decisions for the owner and not something this command does on its own.

The API, checked against Docker's published OpenAPI description and the live
listing of a public repository: `POST /v2/auth/token` with
`{identifier, secret}` gives a ten-minute bearer (`/v2/users/login` is
deprecated); `GET /v2/namespaces/<ns>/repositories/<repo>/tags?page_size=100`
returns `{count, next, previous, results[]}` with `name`, `tag_last_pushed`
and `digest` per result, and timestamps with anywhere from zero to nine
fractional digits. **`DELETE /v2/repositories/<ns>/<repo>/tags/<tag>/` is
not in that description** — it documents only GET and HEAD for a tag — and
is the request the Hub's own web interface sends. It has not been sent from
here: no test and no run of this branch has deleted a tag or logged in. So
three things are unproven until the first real run: that `/v2/auth/token`
takes the repository's PAT, that the DELETE works, and that
`DOCKERHUB_TOKEN` has delete scope. With fewer than 11 dated tags no DELETE
is sent at all. Before relying on it, run the command once by hand with the
two variables set and **no** `--yes`: that exercises the login and the
authenticated listing and deletes nothing.

- **`gen-vm --bake`** builds from the cloud image instead of pulling
  `<backing-ref>`. It takes the fleet's per-VM defaults minus `<backing-ref>`,
  `<vm-username>`, `<vm-password>` and `<vm-extra-hostfwd>`, then the
  `<bake-*>` elements; flags still win. The forwards go because a bake runs
  QEMU on the host, not in a per-VM netns, and QEMU will not start on a
  forward whose port is taken (node_exporter on 9100). It refuses `--backing-image`, `--backing-file` and
  `--restore-image`. Without `--bake`, nothing reads a `<bake-*>` element —
  a plain `gen-vm --xml rocm-golden.xml` pulls the published image.
- **`publish-qcow --xml`** takes its defaults from the same file: the ref from
  `<publish-ref>`, the name from `<bake-name>`, the image from
  `<vm-images-dir>/<bake-name>-backing.qcow2`. It reads only layers named
  with `--xml`, never the search path. It refuses an image with an
  `.image-ref` marker beside it — one `gen-vm` pulled, which a plain
  `gen-vm --xml rocm-golden.xml --vm-name rocm-golden` writes to exactly that
  name — and a bake removes a stale marker.
- **The baked account is `<bake-username>`/`<bake-password>`, never
  `<vm-username>`/`<vm-password>`**, and there is no fallback between them.
  The baked account is written to `vm-info.json` and pushed to a registry; a
  fleet's own login must not be able to reach one by default.
- **`rocm-golden.xml` names no key, on purpose, and no command defaults to
  a personal one.** `gen-vm --bake` generates a keypair for the image and
  records `ssh_key_generated: true` in `<images>/<vm-name>.json`;
  `publish-qcow` with no `--key` publishes that pair, prints which, and does
  so **only** when the record says `gen-vm` generated it. An image baked
  with a named key, or one with no record or an older record, stops and asks
  for `--key`. The record is trusted for that claim and for nothing else.
  It is found from the image file (`gen_vm.image_sidecars`), never from
  `--vm-name`, which only names the artifact; the key is looked for at
  `gen_vm.generated_key_path()` and the recorded path has to be that one;
  neither half, nor `keys/`, may be a symlink; and the public half has to
  carry the recorded `ssh_key_fingerprint`. Each of those was a way to get a
  key nobody named published: a hand-edited or copied sidecar, a pair
  swapped after the bake, or another image's name. The key is never taken
  from `<vm-ssh-key-file>` — a fleet's own login key, usually somebody's
  `~/.ssh` one — and never from `~/.ssh`.
  There is no fleet element naming a key to publish, on purpose: a key
  named in a file would be published on the strength of file contents
  nobody typed.
- **An explicit `--key` still has to be the key the image authorises.**
  `publish-qcow` compares `<key>.pub` with the fingerprint recorded in the
  sidecar (with what is at the recorded path, for a record that has none)
  and refuses a mismatch. On either path the private key must be the public
  key's other half, checked with `ssh-keygen -y`; a passphrase fails that
  check, which is right, since a published key nobody can use is no key.
  The publish workflow passes no `--key` and no `--ssh-key-file`.
- **`rocm-golden.xml` bakes on `<bake-ssh-port>auto</bake-ssh-port>`**, the
  bake's `--ssh-port`. Only the bake's second boot forwards a host port (the
  cloud-init boot has no hostfwd), and on the WSL2 dev host Windows held
  2222-2225, so QEMU died on `Could not set up host forwarding rule`. `auto`
  binds to find a free port from 2222 up (`ports.port_bindable`, which
  `quick_start._port_free` also calls: wildcard and loopback, SO_REUSEADDR,
  bind and listen, matching what QEMU's hostfwd does) immediately before the
  start, and `gen-vm` restarts on the next port if QEMU still loses it. The
  watcher matches the refused rule, `'tcp::<port>-:22'`, not just QEMU's
  message: a held extra hostfwd prints the same words and no SSH port cures
  it. `<bake-ssh-port>` is the bake's alone, so a `run-vm` of the baked
  image still asks for 2222 unless given `--ssh-port auto`.
  `run-vm --ssh-port auto` probes too but cannot retry: it execs QEMU. The
  management MAC is derived from 2222 in auto mode, not from the chosen port,
  so it is the same on every boot; the QGA and QMP socket paths, the data tap
  name and the data and mcast NIC MACs follow the chosen port, since each
  must be unique per running QEMU. `list`, `inspect` and `ssh` read the port
  off the QEMU command line, so they need no telling. The publish workflow's
  later steps boot with `run-vm` on a plain 2222 and never read the bake's
  port. **Do not make `<ssh-port-base>` accept `auto`**: those ports are in
  the generated compose YAML that `gen-compose --check` compares byte for
  byte, and `quick-start` already remaps them at run time.
- `<bake-packages>` and `<bake-playbook>` are resolved against the current
  directory, so a bake runs from the checkout root; a missing one says so and
  names the cwd, and a missing manifest stops the bake before the cloud image
  download. Only the playbook does not ship in the wheel — the manifest does,
  as `packages_d/rocm-golden.txt`, but is named by its checkout path — so
  baking is a from-a-checkout operation.
- The two bake lanes call `qemu-tool gen-vm --bake` in a run step, not through
  `qemu-tool-action`: the action always passes `--release`, which outranks
  `<bake-release>`. `setup-vm-job`'s `release` input only keys the cloud-image
  cache and has to be kept equal to `<bake-release>` by hand.
- **The publish bake goes through the compiler cache**, as the lane's does:
  both wrap their bake step in `.github/actions/bake-ccache-restore` and
  `bake-ccache-save` and pass `--cache-dir`. A published module may
  therefore be linked from objects an earlier run on main compiled; "The
  bake's compiler cache" has the terms. Nothing else about the publish bake
  changed: it still compacts, and still names no key. A dry run bakes, so it
  restores and saves like any other run. It first did on main at `481c892`:
  an 18m25s bake step against 25m08s without the cache that morning.
- `<backing-ref>` (what the fleet's guests pull) is a dated tag of
  `<publish-ref>` (where this repo pushes), since 2026-11-17. It does not
  follow a publish by itself: a publish pushes a new tag and moves `latest`,
  and the fleet files go on naming the tag they name until someone moves
  them. See "The stacks" for the rule a backing ref is held to and for the
  test that compares that guest's pins with the servers.

Three things about it are deliberate. **It never runs from a pull request**:
it writes to a registry. **What is published is the bake, not the disk that
was tested**: both qualifying boots run on overlays of it — the device-less
boot on the one `gen-vm` creates, the with-device boot on a fresh one as the
single guest of a rendered default stack (`rocm-golden.xml` plus an inline
`<vm-count>1` overlay) — and the workflow fails if the base underneath
changed. And **the keypair in the
referrer is generated for the image** — by the job in CI, which names it to
both commands, and by `gen-vm` itself in a bake by hand — because it is
published; never hand `publish-qcow --key` a personal key.

The release order between the repos is fixed, because a guest must never be
ahead of the servers it talks to: batesste-ci-images releases the container
images, this repo moves its image refs and guest pins to that release and
publishes, then consumers move their qcow2 pin. `tests/test_playbook_pins.py`
is the first step of the publish job for that reason. This repo is one of
those consumers: its own fleet files move `<backing-ref>` last, in a change
after the publish, and between the pin change and that one they carry
`<image-allow-guest-lag>`.

The published name is outside `batesste-ci-images`, so it drops out of the
Image Tag Consistency stamp counts. Read from the code, not assumed: the
workflow's `REF_RE` and the test's `TAG_RE` both begin `batesste-ci-images`,
so a `qemu-tool-rocm-golden` ref matches neither. That is correct — the
qcow2 no longer shares a build stamp with the containers — but it left the
guest ref checked by nothing, so both now have a guest section of their own:
one ref across the three fleet files, in `<publish-ref>`'s repository, by a
dated tag, and no batesste-ci-images qcow2 named anywhere they search.

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

**The P4 number is the rate of the fp8 reduction, on one guest, with no
peer.** The reduction is compute inside the NIC: an RDMA WRITE on an RC queue
pair whose gather list names two equal-length operand regions is turned, by a
table entry of `fp8_policy.json`, into an elementwise fp8 add written to the
region the write named as its destination. The device checks all three keys
against its *own* memory-region table (`ionic_datapath_fp8_map`), so the
destination is local and nothing crosses the mesh — there is no "guest 1 to
guest 2" in it, and `ib_write_bw` between two guests measures something else
entirely: a perftest write has one gather entry, which the engine passes
through as an ordinary transfer.

Two programs do it, both installed by the golden bake and both run as the
ordinary guest user. `p4_fp8_client` is rocm-ernic's own
`tests/p4_fp8_client.c`, built from `ernic_source_pin`, so the ioctl and RPC
layout it speaks is the pinned device's and is copied nowhere in this repo;
it loads the program (LOAD_BEGIN, chunked LOAD_CHUNK, LOAD_COMMIT), installs
the `Ing.policy` → `Ing.reduce_with` entry and checks one 4096-element
reduction. `p4_fp8_bench`
([source](ansible/playbooks/roles/rocm_ernic_setup/files/p4_fp8_bench.c)) is
this repo's: plain verbs, no P4 ABI, 2000 writes of 262144 elements
(`IONIC_FP8_MAP_MAX`, the most one operation moves), alternating operands and
checking every result so a device with no entry fails with a remote access
error instead of timing a plain write. The badge is **result bytes per
second in MB/s** — one byte an element; the device reads twice that in
operands. Measured on this host against the 2026-11-16 local bake: about
210 MB/s at 262144 elements, 134 MB/s at 65536 and 19 MB/s at 4096, so the
figure is dominated by per-operation cost below the cap and is not comparable
across element counts.

**`/dev/ernic-p4` is group-accessible in the golden image, and upstream keeps
it root-only.** `ernic_p4` registers the node mode 0600 with the comment that
it "is not something to leave open to a group by default", because whoever
opens it can replace the P4 program. This image is for testing and the
benchmark should not need sudo, so `rocm_ernic_setup`'s `p4_access` tasks
install `99-ernic-p4.rules` (`SUBSYSTEM=="misc", KERNEL=="ernic-p4*",
GROUP="ernic-p4", MODE="0660"` — the driver names its nodes `ernic-p4` and
`ernic-p4.N`), create the `ernic-p4` group and add the baked account. Never
widen it to world access. What a member can do is capped by the server's
`--p4-guest-scope`, which is `<p4-guest-scope>` in the fleet file: `load`
there is the widest and covers table writes (`write`) as well, which is what
the benchmark needs — LOAD_* requires `load`, ENTRY_UPDATE requires `write`,
and the scopes are ordered. Group membership applies to login sessions opened
after the user was added; an SSH session to a booted image has it.

A guest without the client, the program, the node or group access fails the
step with a message naming which, and so does an SDXI run with no `/dev/sdxi0`
or no figure above zero: an `n/a` badge is a green run that measured nothing.
The SDXI node is tested inside the guest, so a guest ssh cannot reach reports
ssh's own error rather than a missing device, and its copies are timed for two
seconds: twenty of them is about 40 ms and swung 30% between runs.
The program and entry the P4 step loads stay in the device until its server
restarts. Ordinary transfers are untouched by them; a two-equal-operand RDMA
WRITE on that NIC is a reduction from then on.

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
drives `qemu/stacks/rocm-golden.xml` through `qemu-tool quick-start up` and
`quick-start test`, then runs the four benchmark steps with
`scripts/generate-vm-report.sh`. The markdown report that script writes is
left on the runner. No settings live in the workflow's `env:` and it writes
no overlay: the fleet file runs unmodified, Loki and Alloy included, under
the layers `quick-start` writes for itself.

The same lane is the daily run of the documented quickstart command. It
checks that Prometheus, Grafana, Loki and Alloy are running, that Prometheus
answers a query, that every guest exporter the fleet file forwards a port
for is up (`scripts/check-guest-exporters.py`; see "Guest exporters") and
that Loki is healthy, and it runs `scripts/check-vm-devices.sh` against both live guests,
because every field `qemu-tool list` and `inspect` report degrades to a
plausible placeholder rather than to an error. It replaced a separate
`report-for-quickstart` lane. The daily run is the documented quickstart file
again, now that `rocm-golden.xml` is it.

`report-for-vm-basic` still exists as a post-merge test, and still uploads a
report artifact that nothing fetches.

**`quickstart-boot` is the PR-time half.** The daily lane finds a broken
quickstart the morning after the merge; `Gen Compose Check` renders
`qemu/stacks/rocm-golden.xml` on every PR but starts nothing. This one boots
**one** guest from `rocm-golden.xml` and asserts the emulated NIC is there by
its full `1dd8:1002` + `1dd8:5400` tuple and that `ibv_devinfo` reports
`PORT_ACTIVE`, then stops. It publishes nothing.

Three choices in it are load-bearing. **One guest, not two**: the 4 vCPU floor
is real and two guests on a 4-core runner is 2x oversubscribed against the
mesh manager's untunable 20 s heartbeat — tolerable once a day, not on a check
that gates merges. At `<vm-count>1` the mesh is just the manager and
`gen_compose` omits the worker branch entirely. **Its settings are an
overlay** the workflow writes inline (`<vm-count>1`, `<ernic-tap>false`), so
`qemu/stacks/rocm-golden.xml` stays the file a reader diffs and the file the
daily lane runs. The SDXI server stays in: the guest waits on it being
healthy, so a server that cannot start fails the lane. `<ernic-tap>false` is safe there and nowhere
else: one guest has no peer to reach. **The IB device name is resolved from
`ibv_devinfo -l`**, never hardcoded: the image's bake installs udev rules
and `rocm-rdma-ernic0` is expected, but a guest whose rename did not fire is
still a working device, and the lane warns about the name instead of failing
on it.

A badge for a workflow that does NOT run on every push to main wants no
`?branch=main`: shields pins that query to main's latest run, and a weekly or
dispatch-only workflow can have none for days, which renders as "no status"
rather than as its real result.
