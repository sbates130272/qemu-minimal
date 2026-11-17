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
- `ansible/playbooks/` — three playbooks and five roles, and nothing else:
  - `vm-basic.yml` — a complete bake on its own, and the base of the golden
  - `vm-rocm-golden.yml` — the one image beyond it: imports `vm-basic.yml`, then the mainline kernel and ernic prerequisites, ROCm, the rocjitsu roles, SRQ + P4, SDXI and one slim pass. Every bake play is tagged `install`; `tests/test_playbook_pins.py` fails a play with no tag, because an untagged play runs in every bake. Three plays need a device and are tagged `never`: `--tags configure` sets up the NIC, `--tags firmware` installs `ip_discovery.bin` from the rocjitsu serving the guest, and `--tags test` runs the GPU workloads. The pins (`ionic_kernel_pin`, `ernic_source_pin`, `rocm_line_pin`, `sdxi_source_pin`) are each defined once and shared between plays through YAML anchors. Its defaults are a published image's, and a bake needs no Docker daemon when given `-e rocjitsu_firmware_script=<path to vfio_guest_firmware.py>`. Given a cache directory (`gen-vm --bake --cache-dir DIR`, or `<bake-cache-dir>` in a fleet file, which reaches the playbook as the extra var `bake_cache_dir`) the module builds go through a compiler cache kept in that directory; there is none by default and only the playbook-test lane passes one — see "The bake's compiler cache". The slim play also purges every kernel but the running one, after asserting that is the pinned one — see "One kernel in the baked image". The acceptance test is `qemu/checks/rocm-golden.sh`, which the playbook-test lane runs against the baked guest and batesste-ci-images runs against the published one
  - `vm-quickstart.yml` — configures and tests a running fleet; what `qemu-tool quick-start test` runs
  - roles `rocm_ernic_setup` (image prep, device naming, boot-time addressing, the P4 client and who may open `/dev/ernic-p4`), `rocjitsu_setup` (firmware, amdgpu, tools, live tests), `sdxi_setup`, `image_slim` and `bake_ccache` (the compiler cache: `restore`, `save` and `remove`, no `main`). Each has task files callers reach with `tasks_from`; all five are linted at the `production` profile in CI
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
   resizes them for the host), Grafana and logs on, the four guest-exporter
   `<vm-extra-hostfwd>` rules the home dashboard needs, TAP on, and
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

**Every image a fleet names is pulled.** `<sdxi-image>` is a published
`batesste-ci-images-ubuntu-rocm-sdxi` tag.

**All images share one build stamp except SDXI, which follows the guest's
`sdxi_source_pin`.** `<qemu-ref>`, `<ernic-ref>`, `<rocjitsu-ref>` and
`<backing-ref>` must carry one `<date>.<ci-sha>`; `_check_image_coherence`
refuses a mix at render time. `<sdxi-image>` is exempt from that comparison
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

**That pairing covers a guest baked from this checkout, not the one
`<backing-ref>` pulls.** The published qcow2 at
`20261112.gdca6903-...-qm.096ddfe` predates the SDXI play and has no SDXI
side at all. Read off the image rather than inferred: no
`/etc/sdxi-guest-ref`, no `sdxi.conf` in `modules-load.d` or `modprobe.d`, no
`sdxi-cli`, and nothing but `dkms/` under `updates/`. So `quick-start`,
`quickstart-boot` and the daily report lane boot guests with an SDXI device
that no driver binds. Nothing wedges, because nothing talks to the server;
`generate-vm-report.sh` finds no `/dev/sdxi0` and fails the SDXI step saying
so, where it used to exit 0 with an `n/a` badge. The lanes that boot a baked guest — playbook-test and publish — are
the only ones that exercise the pair, and both assert `/dev/sdxi0`. This
ends when `<backing-ref>` moves to a `<publish-ref>` tag baked at
`sdxi_source_pin`.

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
load the package straight from the checkout, so they need no install.

The same lane is where the cheap guards live that no guest-booting lane can
replace:

- It runs the unit suite with `CI=true` after installing udevadm, pciutils,
  PyYAML and Jinja2, because `tests/test_ernic_identity.py` is the only thing
  that asks udev to *parse* `99-rocm-ernic.rules` and the only thing that
  reads `rocm_ernic_setup`'s wiring, and under `CI=true` those checks fail
  instead of skipping.
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
guest; `<ernic-per-vm>` made it reachable and the MI355X server shape, at eight
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
rather than hardcoding it — which is what `vm-quickstart.yml` does, and
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
    `<ernic-ref>` move together.

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

## The bake's compiler cache

The plays that build kernel modules — amdgpu, ionic-ernic twice, `ernic_p4`,
SDXI — are about fourteen minutes of a 26-minute `vm-rocm-golden` bake, and
the sources they compile change only when a pin does. The playbook-test lane
keeps a ccache directory across runs; **the publish lane does not, and a
published image is always compiled from source.** One flag separates them:
`qemu-tool gen-vm --bake --cache-dir DIR` names a directory on the
controller, which `gen-vm` creates and hands to the playbook as the extra var
`bake_cache_dir`. Without it the restore and the save are skipped and the
bake compiles what it always did. `tests/test_bake_ccache.py` pins that only
the one workflow passes it.

**The cache is switched by a flag or a fleet element, never by the
environment.** The owner does not want settings driven by environment
variables, and an exported variable would reach every `ansible-playbook` in a
job, where the flag reaches one bake; the playbook has no `lookup('env')`.
`<bake-cache-dir>` is the element (a path, `~` and
variables expanded, read by `gen-vm --bake` only, the flag wins);
**`qemu/stacks/rocm-golden.xml` must not set it**, since the image published
from that file is compiled from source — a local overlay sets it. `gen-vm`
refuses `--cache-dir` without `--bake`, because the element is a bake
element and the flag follows it, and refuses a cache when the bake has no
playbook to hand it to. The extra var is passed as JSON, not `key=value`,
which splits a path at its first space. A playbook that does not read
`bake_cache_dir` ignores it.

`--cache-replace` (extra var `bake_cache_replace`) is the second switch and
has no element: it is a fact about one run — the cache handed over was saved
for other pins — not a setting. The tool cannot derive it: what knows is whoever fetched the cache, which in the
lane is the `actions/cache` key comparison.

**Fourteen minutes is those plays' wall time, not what the cache saves, and
the saving has not been measured.** ccache removes the compiler and nothing
else. What stays: amdgpu's `configure`, some 290 separate kbuild invocations
whose start-up cost is paid hit or miss; objtool on every object
(`CONFIG_OBJTOOL`); the link, `modpost`, signing and zstd compression of each
module; and the package installs, clones and the reboot the same plays do.
The one earlier measurement put amdgpu's straight compile at about 210 s of
its 7m49s play. Four to six minutes is a defensible expectation; take the
real figure from the play timings and the statistics of the first two lane
runs, and replace this paragraph with it.

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
| `bake_ccache_replace`, from `gen-vm --cache-replace` | a moved pin: the lane sets it when the entry it restored was not saved under this run's key prefix |
| at least `bake_ccache_save_misses` (100) invocations missed | drift no pin records, such as a new gcc in the archive |

**The count is absolute, not a share, and the second row is not redundant.**
ccache counts invocations, and most of them are not compiles: each kbuild
`make` probes the compiler about 22 times (a module of 3 sources reports 25
misses cold and 25 hits warm), and amdgpu's `configure` is some 290 makes.
Those ~6,400 probes all hit on a warm cache, so against that denominator a
recompile of the whole of amdgpu is under a fifth, and a bumped ernic or SDXI
pin a few percent (arithmetic from those counts, not a measured bake) — an
earlier 25% threshold would have left the archive
stale after every pin bump, recompiling the changed module in every run. A
count fixes amdgpu; it cannot fix a module of tens of files, which is what
the key comparison is for. The count is still a guess. The statistics every
bake prints, and the lane copies into its step summary, are what to tune it
against, and their last line says "ccache invocations", not compiles, for
the same reason.

Transport is one uncompressed tar file, `ccache.tar`, in that directory:
unpacked into `/var/tmp/bake-ccache` after the reboot into the pinned kernel,
fetched back by a play that sits after SDXI and before the stamp and the slim
pass. The fetch runs with `become: false` on purpose — with become, `fetch`
slurps the file as base64 in memory. That play's `always` section then runs
the role's `remove` tasks, which take out the symlinks, `/etc/ccache.conf`,
the cache and the tar. **The removal is the one part that is not gated on a
cache directory**: a cached bake that fails between the restore and the save
leaves the hook in the guest, and an `--ansible-only` rerun without
`--cache-dir` would otherwise finish an image whose `gcc` is ccache. Every task in it is a
no-op on a guest that never had the hook, so a published bake runs them and
changes nothing. `qemu/checks/rocm-golden.sh` asserts all four are absent, so
both the lane and the publish workflow would fail on an image that carried
any of it.

In the lane, `actions/cache/restore` and `actions/cache/save` are separate
steps so the save sits straight after the bake. The key ends in the run id,
since an entry is never overwritten; its prefix hashes the kernel, ernic and
SDXI pins, the amdgpu driver release, `requirements.yml` and
`amdgpu-emu-patches.py`, with `restore-keys` falling back to any earlier
cache so a pin bump still starts warm — and, because a restore that fell
back that far passes `--cache-replace`, saves once under the new
prefix so the next run is warm for the bumped module too. **The lane has no
push trigger, so nothing saves a cache on main**, and GitHub only lets a pull
request read caches saved by itself or by its base branch: each pull
request's first run is cold, and only its later pushes are fast, until
someone runs the workflow by hand on main (`gh workflow run ... --ref main`),
which seeds every later pull request. That wants doing once after this
merges and again after each pin bump; a push trigger would do it unasked at
the cost of a bake per qualifying merge, and was left out for that reason.
An entry unused for seven days is evicted, and each pull request's own entry
is a few hundred MiB of the repository's 10 GiB.

The report step dates what the bake wrote against a marker the bake step
touches in `$RUNNER_TEMP`. Not in the cached directory: there it is restored
with the cache, older than the files it is meant to date.

The lane also bakes with `--no-compact`: nothing publishes its image, and the
recompress was most of the three minutes after the playbook. The backing
image's "disk size" in its `Report VM image size` step is therefore the
uncompacted allocation, 2.3–2.5x the published figure and not comparable with
runs before the flag. The publish lane still compacts.

Not yet seen in CI as of this writing: the role has run against no guest.
What was exercised is a kbuild module build on the dev host through the same
symlinks and settings (25 misses, then 25 hits), not dkms itself and not the
tar round trip. That dkms hands kbuild a `PATH` with `/usr/local/bin` first,
that the guest's sudo `secure_path` has it first, and that `/etc/ccache.conf`
is honoured there are all read from code. So on the first lane run read the
statistics in the step summary: a non-zero miss count means the hook is live,
and a second run on the same pull request should show direct hits in the
thousands. **Every counter zero means the hook did not intercept** — the bake
is correct and no faster — and the role's last line and a workflow warning
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

`sbates130272.rocm_ernic.ernic_source` keeps a clone on the **controller** at
`ernic_source_clone_dir` (default `/var/tmp/rocm-ernic-src`) and updates it
with `ansible.builtin.git`, which runs `git fetch --tags origin`. Upstream
moves tags — its `nightly` did — and git then refuses:

```
 ! [rejected]        nightly    -> nightly  (would clobber existing tag)
```

That killed a bake after 118 tasks, over a tag the bake never reads: it
checks out `ernic_source_pin`. So `rocm_ernic_setup`'s `source_clone` tasks
run immediately before every include of `ernic_source` in
`vm-rocm-golden.yml` — the first play, the SRQ + P4 play, and as a `pre_task`
of the configure play, whose `ernic_guest_setup` includes the role itself —
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
what the line tells the reader; neither changes anything on disk, and in
both the clone may already be at the pinned commit, which
`ansible.builtin.git` checks out without fetching. Corruption that
`git fetch` does not trip over is not detected at all; neither does the
role's own fetch.

The guard is skipped when `ernic_source_dir` names a checkout of the
caller's own, which the role never updates, and once `ernic_source_resolved`
is set — so the second include in a bake costs nothing, and does the work
when a rerun starts at that play.

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

**None of this has run against a guest.** The purge, the guard and the
leftovers script are tested against fixtures (`tests/test_kernel_purge.py`),
and the grub resolver against `grub.cfg` text written out the way resolute's
templates write it, not against `grub-mkconfig` output. What a bake has to
show: that the cloud image's packages are the ones the purge list expects
(the guard fails loudly if apt wants anything else), that `GRUB_TOP_LEVEL`
is honoured, and that the guest comes back up — which both lanes prove by
booting the image twice and running the check, and the publish lane does
before it pushes.

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
- `<backing-ref>` (what the fleet's guests pull) and `<publish-ref>` (where
  this repo pushes) are different repositories today.

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
is the first step of the publish job for that reason.

The published name is outside `batesste-ci-images`, so it drops out of the
Image Tag Consistency check. That is correct: the qcow2 no longer shares a
build stamp with the containers.

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
answers a query and that Loki is healthy, and it runs `scripts/check-vm-devices.sh` against both live guests,
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
`ibv_devinfo -l`**, never hardcoded: the published image ships no udev rules,
so the device keeps its kernel name.

A badge for a workflow that does NOT run on every push to main wants no
`?branch=main`: shields pins that query to main's latest run, and a weekly or
dispatch-only workflow can have none for days, which renders as "no status"
rather than as its real result.
