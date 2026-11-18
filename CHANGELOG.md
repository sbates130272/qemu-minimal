# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Security

- **A stack's guests and Grafana now answer on the host it runs on, and
  nowhere else, unless you say otherwise.** Every generated stack published
  each guest's SSH port on all of the host's interfaces: the mapping was
  written with no address (`"2222:2222"`), and `<publish-bind-addr>`, the
  setting described as deciding who can reach a stack, was applied to
  Grafana's port alone and defaulted to `0.0.0.0`. A guest made from the
  published image can be logged in to by anyone: its SSH key is published
  beside the image, the password of its `ubuntu` account is in the image's
  `vm-info.json` and the guest's sshd accepts it, and that account has sudo.
  So on a host other machines can reach, 2.0.0 gave them a root shell in
  every guest, beside a Grafana with anonymous admin.
  - `gen-compose` now binds every port it publishes, the guests' SSH ports
    and Grafana's, to `<publish-bind-addr>`, and the default is `127.0.0.1`.
    The three shipped fleet files state it. The element takes an IPv4 or
    IPv6 address and nothing else; a host name or an IPv6 zone
    (`fe80::1%eth0`, which docker refuses) is an error naming the element,
    and an empty element is the default, not every interface.
  - **Loopback means "this host only" on Docker Engine 28.0 and later.**
    Earlier engines let a machine on the host's network segment reach a
    port published on `127.0.0.1`; the 28.0.0 release notes list the fix.
    `qemu-tool check` reports the engine version and warns below 28.0.
  - Measured on a running stack (one guest, Docker 29.1.3, from inside
    WSL): docker published guest SSH and Grafana on `127.0.0.1` alone, both
    answered there, and both were refused on the host's other addresses.
    Prometheus and Loki had no host port. Not tried from another machine.
  - *Who is affected:* anyone who reached a guest over SSH, or Grafana, from
    a machine other than the one running the stack. After this change those
    connections are refused.
  - *The way back:* `<publish-bind-addr>0.0.0.0</publish-bind-addr>` in a
    fleet XML overlay, then render the stack again (`quick-start` does so on
    every `up`). Read the README's Quick Start first for what that exposes;
    an SSH tunnel, or one address of the host in place of `0.0.0.0`,
    exposes less.
  - **A stack rendered by an earlier version keeps publishing on every
    interface until it is rendered again**, whatever the fleet file says:
    the ports are written into its `docker-compose.yml`. `qemu-tool compose
    up` now warns when the stack it starts publishes a port past loopback
    and names the services.
  - `qemu-tool check` reports the address in force and warns when it is not
    loopback. `quick-start` prints who can reach the stack in its plan and
    its summary. `qemu-tool list`, `inspect` and `ssh`, and the generated
    Ansible inventory, reach a guest on the address its port is bound to,
    so a stack published on one particular address still works.
  - Not changed: a guest run on the host by `run-vm`, and so the boot
    `gen-vm --bake` hands to Ansible, still forwards its SSH port on every
    interface (`hostfwd=tcp::<port>-:22`), and a baked image's account has
    the same public password.
  - Not changed either: **the published image still accepts that
    password.** Confirmed on a booted guest of `20261117.g481c892` without
    submitting it: sshd offers `publickey,password`, `sshd -T` reports
    `passwordauthentication yes`, and the `ubuntu` account has a usable
    password and passwordless sudo. This release keeps other machines away
    from the port; closing the login needs a bake change and a new image.

### Added

- **`quick-start` can test the SDXI device and the NIC's P4 engine:
  `--sdxi-test` and `--p4-test`.** Both are off by default, both run with
  the `test` and `all` verbs, and both run in the guest as its login user
  with no sudo. They are two new plays of `vm-quickstart.yml`, each ended
  at its first task unless its extra var is true (`quickstart_sdxi_test`,
  `quickstart_p4_test`), so a run without the switches executes nothing new
  in a guest and `ansible-playbook` is handed the arguments it always was.
  - `--sdxi-test`: on every guest, 4 MiB of random data is copied through
    `/dev/sdxi0` with `sdxi-cli` and compared with its source. A pass
    prints `<guest>: SDXI copy of 4194304 bytes verified`. It is a
    functional check and reports no rate; the daily lane's SDXI figure is a
    different measurement.
  - `--p4-test`: on the first guest only, `p4_fp8_client` loads the fp8 P4
    program into that guest's NIC and checks one reduction, and
    `p4_fp8_bench` times 2000 reductions of 262144 elements. A pass prints
    `<guest>: P4 FP8 reduction <rate> MB/s (262144 elements x 2000
    iterations)`. `--p4-iters N` times another count. One guest because
    the reduction is compute inside one NIC: no peer, nothing across the
    mesh. The RDMA device name is asked of the guest.
  - *A fleet that cannot run one is refused before anything is started*,
    naming the element and the one settings file that set it:
    `<sdxi-per-vm>` of 0 for the first; `<p4-enable>` not true, or a
    `<p4-guest-scope>` other than `load`, for the second. The check reads
    the settings the command names, so a stack brought up from other
    settings is caught in the guest instead, which says whether it is the
    device, the driver or the group that is missing.
  - A verb that runs no test (`up`, `preflight`, `report`, `logs`, `down`)
    ignores the three switches and says so on stderr.
  - The closing summary of a full run names the switches the fleet can
    run.
  - *The P4 program and its table entry stay in that NIC until its ernic
    server restarts.* A two-equal-operand RDMA WRITE on it is a reduction
    from then on; ordinary transfers are untouched.
  - Both results are in the per-guest summary under the stack's
    `artifacts/` and in the report, and a test that was asked for and left
    no result fails the run.
  - *Run against real guests on 2026-11-18*: two pulled guests of
    `20261117.g481c892` on the development host, one boot, each guest
    sized by `quick-start` to 8 vCPU and 8192 MiB. Five `quick-start test`
    runs, all exit 0:
    - plain, before either switch: both new plays ran their skip task and
      nothing else; `ib_send_bw` averaged 595.15 MiB/s.
    - `--sdxi-test --p4-test`: the SDXI copy verified on both guests; the
      P4 rate on the first was 194.1 MB/s (1.351 ms a reduction).
    - the same again in that boot: both copies verified, and the program
      loaded over the one already in the NIC, 195.0 MB/s.
    - `--p4-test --p4-iters 200`: 194.5 MB/s, reported as 200 iterations;
      the SDXI play ran its skip task only.
    - plain again, the program left in the first guest's NIC: `ib_send_bw`
      between the guests passed at 572.93 MiB/s, and neither guest's
      summary has an SDXI or a P4 section.
  - *Not run:* the `all` verb with a switch, so the closing summary line
    naming the switches has not been seen live; the in-guest refusals,
    through the play, for a missing device, a driver that is not loaded or
    a user outside the group; and either test on 4 vCPU guests. The
    refusals before anything starts were measured through the CLI (exit 1
    in under 0.2 s, before the preflight). The suite,
    `tests/test_quickstart_optional_tests.py`, runs both plays against
    stand-ins for the guest's tools.

- **The golden guest carries a perftest built with ROCm memory support.**
  Ubuntu's `perftest` is built without ROCm, so its `ib_send_bw` has no
  `--use_rocm`. A new role, `perftest_setup`, builds the tools from source
  against the guest's ROCm and installs them in `/usr/local/bin`, where a
  bare `ib_send_bw` finds them first; the archive's package stays in
  `/usr/bin`, and `vm-quickstart.yml`'s RDMA test, which names
  `/usr/bin/ib_send_bw`, runs what it always ran. The source is
  `sbates130272/perftest`, branch `feat/rocm-data-validation-legacy`, at
  `e68c74e`: the head of linux-rdma/perftest#419, which adds
  `--data_validation` for GPU memory to `ib_write_bw` and `ib_read_bw`.
  - *The repository, the ref and the commit are variables*
    (`perftest_setup_repo`, `_ref`, `_commit`), set by `vm-rocm-golden.yml`
    from `perftest_source_repo`, `perftest_source_ref` and
    `perftest_source_pin`. The fetch is compared with the commit before
    anything is compiled, so **a bake fails when the branch has moved**,
    naming both commits. An empty commit turns the check off, and the
    guest check then refuses the image.
  - *It needs no device*, so it runs in the bake: hipcc is told its GPU
    (`perftest_setup_rocm_arch`, `gfx1250`) because a bake cannot ask one,
    and left to itself hipcc 10.1 compiles for gfx906.
  - *It does not take its CPU from the bake machine either.* perftest's
    `configure` runs a program to ask the CPU for AVX2 and SSE4.2 and
    compiles the host-memory validator for what it finds, with no switch.
    **The first bake with this role failed on that**: a bake's guest has
    QEMU's default CPU model, which has neither, and with no level the
    pinned tree does not compile (`implicit declaration of function
    '_mm_pause'`: `src/host_validation.c` includes the header that declares
    it only with a SIMD level). The same build on a guest with the EPYC
    model, which is what every stack runs, had come out AVX2. So the role
    names the level, `perftest_setup_simd`, default `sse4.2`, which every
    CPU a fleet runs on has: `cross_compiling=yes` on the configure line
    stops the probes, and `SIMD_CFLAGS=` and `-DHAVE_SSE42=1` in `CPPFLAGS`
    give the level. Nothing in the tree is patched.
    `perftest-simd-check.sh` reads the level back from `config.h` and the
    flags the Makefile gives that object (`DEFS`, `CPPFLAGS`, `CFLAGS` and
    `SIMD_CFLAGS`, so a level written into a `CFLAGS=` is seen too) before
    anything is compiled and stops a build at another one,
    and the role then counts AVX instructions in the tool it built. The
    tools check the CPU they start on and refuse `--data_validation` on
    host memory where it lacks the level; an `sse4.2` build is refused only
    where the CPUID has no `sse4_2`, QEMU's default model included.
    `--use_rocm` validation is unaffected. What `sse4.2` costs against
    AVX2 is the rate of host-memory validation: 16 bytes a compare, not 32.
  - *`perftest_setup_mm_pause_workaround`*, on by default, adds `-include
    xmmintrin.h` so the tree compiles at any level, `none` included. It is
    a workaround for the pinned commit and is to be turned off when the
    pin moves to one with the fix; turned off while the tree still has the
    bug, the bake stops before the compile and says so, on a compiler that
    only warns about an implicit declaration as well as on one that
    refuses it.
    `perftest_setup_configure_extra` no longer takes a `CPPFLAGS=`, which
    the role now writes; flags of the caller's go in
    `perftest_setup_cppflags`.
  - *It is linked against the rdma-core the guest loads*, which has to be
    said: the image has the archive's rdma-core 61 under `/usr` and
    rocm-ernic's 66 under `/usr/local`, the compiler reads the second one's
    headers and the linker's default is the first one's library. On two
    guests of the published image the build without it failed to link
    (``undefined reference to `ibv_alloc_buf'``); with
    `LDFLAGS=-L/usr/local/lib` it built in 18.76 s on 8 vCPUs. The role
    adds that from `perftest_setup_rdma_lib_dir`, and
    `perftest-rdma-probe.sh` stops the build beforehand unless the headers,
    the library linked and the library loaded are one rdma-core. The
    configure line is the one that built on the guests; the variable and
    the probe around it have run on the dev host against an rdma-core 66
    tree, not yet on a guest.
  - `/etc/perftest-rocm.json` records the build, `~/WELCOME.md` describes
    it, and `qemu/checks/rocm-golden.sh` asserts which copy a bare name
    runs in a non-login shell, a login shell and under sudo, that the help
    offers `--use_rocm` and `--data_validation`, that the tools link the
    image's ROCm, and that the libibverbs they load is the one they were
    linked against. The stamp also records the SIMD level, whether the role
    named it and whether the workaround was applied (`simd`, `simd_forced`,
    `mm_pause_workaround`), and the check refuses an image whose level is
    not `sse4.2`, or was left to the bake machine, or whose `ib_write_bw`
    has an instruction on a `ymm` or `zmm` register.
    `perftest_source_pin` joins the bake's compiler-cache key.
  - **The guest check now fails an image baked before this**, for want of
    `/etc/perftest-rocm.json`. The lanes here run it against a fresh bake;
    batesste-ci-images runs it against the published image and has to take
    this version of the check together with an image that has the build.
  - *No bake has completed with it.* The one that ran stopped in this
    role, as above. With the level named, the role built on a guest of the
    published image booted the way a bake boots it, with QEMU's default
    CPU model and no device, and built the same level on the EPYC model.
    It is not in the image the fleet files pull until one is published
    from a commit that has it and `<backing-ref>` moves.
- **An optional test of RDMA with GPU memory**, the last play of
  `vm-quickstart.yml`: `ib_write_bw --use_rocm=0 --use_rocm_dmabuf
  --data_validation` between the first and last guest. Off unless
  `-e quickstart_rocm_rdma=true`; `qemu-tool quick-start` has no flag for
  it and no workflow sets it. `ib_write_bw` because `--data_validation` is
  for the WRITE and READ verbs; `ib_send_bw` lists the flag and refuses it.
  It is off because **on the default stack the transfer does not work**,
  measured on two guests on 2026-11-17:
  - `--use_rocm` alone fails at registration, in the guest and on both
    sides: `Couldn't allocate MR with error=14`. The NIC's server is never
    asked, so no QEMU image changes it.
  - `--use_rocm --use_rocm_dmabuf` registers, and the first work request
    fails: `Failed status-remote access error (10): wr_id 0 syndrom 0x9`,
    with `could not gather 65536 bytes` in the NIC server's log. The NIC
    cannot read the GPU's memory, which is one vfio-user device reaching
    another's BAR on stock QEMU. Not tried on the fork QEMU image.
  - `--data_validation` with GPU memory is therefore not reached. It
    passes on host memory, and with `PERFTEST_ROCM_HOST_ALLOC=1` (a buffer
    in host memory the GPU can address) both host-staged and with the live
    kernel on the GPU: `VALIDATION: PASSED - 7 chunks, 58720256 bytes`.
    Those runs were on a build whose host-memory validator was AVX2, the
    level `configure` found on those guests; the validator a bake now
    builds is SSE4.2, and no transfer has been validated with it.

  The play passes `--use_rocm_dmabuf` because that is the form that reaches
  the NIC; in that form it has not been run.

### Changed

- **The golden guest no longer carries Ubuntu's rdma-core development
  packages**: `libibverbs-dev`, `librdmacm-dev` and `libibumad-dev`. The
  image has two rdma-cores, the archive's 61 under `/usr` and rocm-ernic's
  66, built from source under `/usr/local`, which every tool loads. Both had
  headers and link libraries, and the toolchain took the headers from one
  and the library from the other (the perftest entry above is that image).
  The archive's is now a runtime only, and a build in the guest finds one
  rdma-core with no flags: `gcc prog.c -libverbs` compiles against, links
  and loads the one in `/usr/local`.
  - *Not installed, and purged if present.* The three are out of
    `qemu/packages.d/rocm-golden.txt`, `rocm_ernic_setup_packages` and the
    perftest role's build dependencies. A new play, after everything that
    compiles and before the stamp, purges them from a guest that has them
    anyway (another manifest, an `--ansible-only` rerun on an older guest);
    it first asks apt what the purge would remove and stops unless it is
    those packages and nothing else, refuses when `/usr/local` has no
    rdma-core to take their place, and never autoremoves.
  - *The runtime stays*: `rdma-core`, `ibverbs-utils`, `ibverbs-providers`,
    `libibverbs1`, `librdmacm1t64`, `libibumad3`. Other packages are linked
    against them, `rdma-core` has the udev rules and units, and the
    quickstart RDMA test runs `/usr/bin/ib_send_bw` by path. `libnl-3-dev`
    and `libnl-route-3-dev` stay too.
  - *`perftest_setup` has `perftest_setup_rdma_core`.* `local`, which
    `vm-rocm-golden.yml` sets, installs no rdma-core package and sends the
    linker to `perftest_setup_rdma_lib_dir`; `archive`, the default,
    installs the three for a guest whose only rdma-core is the archive's,
    and is refused before it installs anything on a guest that has one in
    that directory. The probe stays, for the guest that still has both.
  - *`/etc/rocm-golden-guest.json` names both rdma-cores.*
    `rdma_core_version` was the archive package's version, though 66 is
    what runs. It is kept, and beside it are `rdma_core_archive_version`,
    `rdma_core_local_version` (`66.0`) and `rdma_core_local_libibverbs`, the
    last two read off the libibverbs `ibv_devinfo` loads. `~/WELCOME.md`
    names both and says how to build against RDMA.
  - *`qemu/checks/rocm-golden.sh` has a block for it*, which the bake also
    runs as its last check: none of the three is installed, no rdma-core
    header set, link library or pkg-config file is outside `/usr/local`,
    pkg-config resolves there, and a program built with no flags reads,
    links and loads the `/usr/local` copy. **It fails the image the fleet
    files pull**, which has the packages; only the lanes that bake run it
    here.
  - *What a guest loses*: the verbs API man pages and static linking against
    rdma-core, for 3.1 MB. *What to avoid*: `apt install libibverbs-dev` in
    a golden guest restores the mix. A `vm-basic.yml` guest is unchanged.
  - *Measured on a guest, not in a bake.* On a guest of
    `20261117.g481c892` the purge removed the three and nothing else, and
    with them gone perftest, the P4 client and benchmark, rocm-ernic's
    rdma-core itself, the `ionic-ernic` DKMS package and fio all built;
    perftest and the P4 pair ran against the NIC. No bake has run with the
    change, and the check block in its final form has run against stand-ins
    and the dev host's toolchain only.

- **`sbates130272.batesste` is pinned at 3.4.0** (was 3.3.1). It is a
  provisioning-speed release: `rocm_setup` looks up every version with one
  `rocm-latest` call, `user_setup` writes its SSH client settings as one
  block, and `fave_packages` skips the `uv` install instead of failing the
  play when GitHub rate-limits the version lookup. `user_setup` still
  removes snapd without purging it, so `vm-basic.yml` keeps its purge.
  Read from the two release tarballs; the first bake on it is the
  playbook-test run of the bump.
- **The rocm-ernic Ansible collection is `rocm.ernic` now, and its pin no
  longer has to be the commit the guest's driver is built from.** Upstream
  renamed `sbates130272.rocm_ernic` to `rocm.ernic` and stopped publishing it
  to Galaxy. `ansible/requirements.yml` installs it from upstream commit
  `bd90234` (was `90434a2`); it lands under
  `ansible_collections/rocm/ernic`, at 0.3.0, and every role reference,
  test and the `Gen Compose Check` install step name it that way. A test
  fails any tracked file but the two changelogs that still carries the old
  name: on a host with the old collection installed, an un-renamed role
  resolves to the old roles and says nothing.
  - *Two pins where there was one.* The collection pin is the **roles**.
    `ernic_source_pin` in `vm-rocm-golden.yml` is the **sources**: the
    checkout the SRQ and P4 series, the scripts that fetch and build them,
    the rdma-core provider series, the `ernic_p4` module and the P4 client
    are read out of. The two were required to be one commit. Now
    `ernic_source_pin` alone has to match the `ernic.<sha>` of every server
    image, and `tests/test_playbook_pins.py` holds the playbook to handing
    the SRQ, provider and P4 roles paths under that checkout, where it used
    to compare the two commits.
  - *Not moved:* `ernic_source_pin` stays at `90434a2`, and so does every
    `<ernic-ref>` and `ERNIC_IMAGE` (`ernic.90434a2`). The image a bake
    produces is built from the same sources against the same servers as
    before, and records the same `ernic_source_pin`. No fleet file or
    generated stack changes.
  - *Why the collection could move alone:* between the two commits no
    `.patch` file under the collection's `ansible/files/patches` differs,
    `setup-ionic-dkms.sh` is identical, and the SRQ, provider and P4 roles
    differ only in how they spell a fact (`ansible_facts['kernel']`).
  - *What does change in a bake:* `ernic_source` clones the sources with git
    commands of its own, blob-less and with no tags on the path a pinned
    commit takes, and converts the clone an earlier bake left on the
    controller; the first play's fetch of the ionic sources runs the newer
    `fetch-ionic-sources.sh`, which asks whether the repository answers
    before each fetch and, when a `git.kernel.org` repository never does,
    goes on to `kernel.googlesource.com`. A bake reaches its GitHub mirror
    through a git rewrite of the first URL only, so with that mirror down
    the fetch now succeeds from Google; the bake's commit check fails it on
    the origin, as a test now shows with the real script. The check also
    removes a guest's tree it fails. The script's sentinel records the URL
    it was given, not the host that answered, so that tree left in place
    was one every rerun on the guest (`gen-vm --ansible-only`) would find
    "already there", fetch nothing over and fail on again. A controller's
    tree is not removed; the failure says to.
  - Three variables the playbook passed to `ernic_p4_dkms`
    (`ernic_p4_dkms_install`, `ernic_p4_dkms_reload`,
    `ernic_p4_dkms_fetch_on_controller`) are gone: no role read them at
    either commit. Every local workaround stays: the mirror rewrite, the
    commit check, the guard on the controller's clone and the naming rules.
  - The bake's compiler cache starts under a new key: its prefix hashes
    `requirements.yml`. The first bake after this restores an earlier
    entry through the fallback and saves once under the new prefix.
  - Read from the two commits in a clone of upstream, and tested against
    the collection installed from this pin. **No bake has run on the new
    roles**; the first is the playbook-test run of this change. A host that
    bakes by hand installs the collection on its next `gen-vm`; a bare
    `ansible-playbook` run needs `ansible-galaxy collection install
    --no-deps -r ansible/requirements.yml` first. The old collection's
    directory is not removed by either.

### Fixed

- **`vm-rocm-golden.yml --tags configure` no longer replaces the image's
  SRQ + P4 ionic driver.** The golden bake builds `ionic-ernic` from the
  kernel's ionic sources plus rocm-ernic's SRQ and P4 series. The configure
  stage, which the README gives for a live guest and which the playbook-test
  lane and the publish workflow both run, ran the collection's
  `ernic_guest_setup` with its driver build on. That build applies the base
  series only, and `setup-ionic-dkms.sh` removes and rebuilds
  `ionic-ernic/<kernel ref>` under the same name and version, after which
  the role reloads the modules. Two CI runs show the rebuild in that stage
  (19 s and 48 s), and nothing read `max_srq` after it. Measured since, on a
  guest booted from `20261117.g481c892`, which has `max_srq` 256 at boot:
  - *On a stack that offers the guest no P4*, which is what both lanes
    render, the old stage was green and left `max_srq` 0 with the port still
    active, so no shared receive queue and no `rc_verbs` through UCX, and an
    `ernic_p4` that no longer loads. Every with-device step after it ran on
    that driver.
  - *On a stack that offers P4*, which `rocm-golden.xml` does, the old stage
    failed: `ernic_p4` holds `ionic`, so only `ionic_rdma` was unloaded, and
    the rebuilt one does not load against the baked `ionic`. The guest was
    left with no RDMA device at all.
  - The stage as it is now, on the same guest in both shapes: green, no
    DKMS build, `max_srq` 256 and the port active afterwards, the loaded
    modules still the baked ones.

  The published qcow2 was not affected: the stage runs on an overlay of it.
  - The stage now runs the role with `ernic_install_driver: false`, so it
    builds nothing, fetches no kernel sources and reloads nothing. It loads
    the baked modules if they are not loaded, brings the emulated NICs up
    and assigns the address as before.
  - It begins and ends by asking the guest whether its ionic driver is the
    baked one (`rocm_ernic_setup`'s `driver_check` tasks, which run
    `files/ernic-driver-check.sh`): the loaded `ionic` and `ionic_rdma` are
    the DKMS build the bake recorded, `ionic_rdma` defines the SRQ verbs,
    `ionic` registers a P4 auxiliary device, `ernic_p4` loads against it,
    and every emulated NIC reports `max_srq` above zero. A failure names
    the cause. The last check runs when the role fails too, and loads
    nothing it does not unload again.
  - Both lanes send the same script to the guest straight after the stage,
    the publish workflow before it pushes. `/dev/ernic-p4` is asked for when
    the stack offers the guest the P4 engine; both lanes' stacks turn P4
    off, so neither asserts it.
  - The bake records the `srcversion` of `ionic`, `ionic_rdma` and
    `ernic_p4` in `/etc/ionic-srq-p4.json` and checks the record against the
    modules it installed, the P4 series included: the bake's earlier "P4
    symbol" check counted a symbol the base driver also exports.
  - *Behaviour change:* `--tags configure` now fails, at its first task, on
    a guest `vm-rocm-golden.yml` did not bake. It used to build a base
    driver into one that had the pinned kernel and a toolchain.
  - *If you ran `--tags configure` before this:* that guest's overlay holds
    the base driver. Make a new overlay on the baked image, which was not
    changed.
  - Run by hand against one live guest, as above, with both lanes'
    assertion steps passing after the new stage and the driver step failing
    after the old one. Not yet run: a bake from this change, so the
    recorded `srcversion` and the bake's own check of it have met no real
    module; any CI lane; and the changes made after those runs (the check
    running when the role fails, its unload of `ernic_p4`, a record written
    on one line), which the unit suite covers against a fabricated guest.

## [v2.0.0] - 2026-11-17

Fleet XML is now the only settings format, and the stacks are renamed. Read
**Breaking** first if you have settings in an env-format file, a stack
rendered by an earlier version, a script that names a `vfio-user-*` stack or
passes `--env-file` or `--fleet-file`, a fleet file with `<lmcache-*>`
elements, or a guest you log in to with your own `~/.ssh/id_rsa`.

### Breaking

- **`gen-vm` no longer authorises `~/.ssh/id_rsa.pub` by default.** An image
  it bakes from the cloud image now gets a keypair generated for it, under
  `<images>/keys/<vm-name>_id_rsa`, and `gen-vm` prints where. Nothing is
  read from `~/.ssh` unless you name it, by the playbook either: `gen-vm`
  passes `user_setup_ssh_key_src_dir` and `user_setup_ssh_key_name` to
  `ansible-playbook`, so the `user_setup` role authorises the image's key
  where it used to add the controller's `~/.ssh/id_rsa.pub` whatever key was
  named. With `vm-basic.yml` run on its own, the private key that role
  copies into the guest is now the image's as well. The reason is `publish-qcow`: an
  image's key is published with it, and the default must not be a personal
  one.
  - *To log in:* `qemu-tool ssh <vm>`, which finds the key, or
    `ssh -i <images>/keys/<vm-name>_id_rsa -p <port> <user>@localhost`. A
    plain `ssh -p 2222 ubuntu@localhost` no longer offers a key the guest
    accepts.
  - *To keep the old behaviour:* `gen-vm --ssh-key-file ~/.ssh/id_rsa.pub`,
    or `<vm-ssh-key-file>~/.ssh/id_rsa.pub</vm-ssh-key-file>` in the fleet
    file. A key named either way is authorised as before and is never
    published unless `publish-qcow --key` names it too.
  - Baking again under the same name generates a new keypair and deletes
    the old one, whose image has just been overwritten. Naming a key on a
    re-bake deletes the old generated pair too, and every bake deletes
    `<images>/<vm-name>.json` when it starts, so a bake that fails leaves
    no record describing the image it replaced.
  - Guests that bake nothing are unchanged: an overlay made with
    `--backing-file` uses the key recorded for its backing image, and a
    guest on a pulled `--backing-image` uses the keypair pulled with it.
  - The sidecar `<images>/<vm-name>.json` gains `ssh_key_generated` and
    `ssh_key_fingerprint`, and its `ssh_key_file` is `null` when no key is known rather than a path under
    `~/.ssh`.
  - `publish-qcow --key` is optional: see `gen-vm --bake` under Added.

- **Env-format settings files are no longer read.** There is no `KEY=value`
  parser left: `$QEMU_TOOL_ENV`, `./.env`, `qemu/.env`,
  `~/.config/qemu-tool/env` and `/etc/qemu-tool/env` are ignored, and an
  env-format file given to `--xml` is an XML parse error. Settings are fleet
  XML elements — `<vm-images-dir>`, not `VM_IMAGES_DIR` — and layers are
  repeated `--xml`, later files winning element by element. With no `--xml`
  the search path is `$QEMU_TOOL_FLEET`, `./fleet.xml`,
  `qemu/stacks/fleet.xml` in a checkout, `$XDG_CONFIG_HOME/qemu-tool/fleet.xml`,
  `/etc/qemu-tool/fleet.xml`.
  - *If your settings are in an env file:* rewrite it as fleet XML.
    `qemu/stacks/rocm-golden.xml` is the annotated template and `qemu-tool man`
    lists every element; the name is the old key lower-cased with hyphens for
    most (`VM_VCPUS` is `<vm-vcpus>`), and the image keys became `<qemu-ref>`,
    `<ernic-ref>`, `<rocjitsu-ref>`, `<backing-ref>` and `<sdxi-image>`.
  - Keys with no element are flags only now: `VM_COMPACT`, `VM_CA_CERT_FILE`,
    `VM_PCI_HOSTDEV`, `VM_VFIO_USERDEV`, and the per-VM override lists
    `VM_NAMES`, `VM_SSH_PORTS`, `ERNIC_MACS` and `ERNIC_GUEST_IPS`, which are
    always derived.
  - `CONTAINER_USER` and `CONTAINER_KVM_GID` cannot be pinned in a settings
    file. Export them, or leave them unset and the caller's ids are used.
  - The `.deb` no longer installs `/etc/qemu-tool/env`, and seeds nothing in
    its place: `/etc/qemu-tool/fleet.xml` is a file an admin creates. A file
    there is the settings of every command given no `--xml`, so a copy of
    `rocm-golden.xml` would make a plain `gen-vm` pull that fleet's image and a
    plain `run-vm` take its NVMe and fixed port forwards. An old
    `/etc/qemu-tool/env` is left in place, unread.
  - `<vm-cpu-sockets>` and `<vm-cpu-phys-bits>` must be integers, checked
    when the file is parsed like every other integer element, and a `~` in a
    path element is expanded then too, so `docker compose` and `check` see
    the path `gen-vm` and `run-vm` do.

- **Fleet XML is parsed strictly.** An element the parser does not know, an
  element repeated within one file, an attribute on `<fleet>` or on a
  setting, a nested element and text directly under `<fleet>` are errors
  naming the file and the element; an unknown element also names the closest
  known one (`unknown element <vm-vcpu>; did you mean <vm-vcpus>?`). All of
  these used to be ignored, so a typo was a setting that silently kept its
  default, and of a repeated element the first quietly won. Every problem in
  a file is reported in one go. XML comments are still allowed.
  - *If a file that used to load is now refused:* the message names what to
    fix. Nothing that was being applied has changed meaning.

- **`qemu-tool fleet-to-env` is removed**, and qemu-tool no longer writes a
  temporary env file or passes `--env-file` to `docker compose`. The four
  variables a generated stack leaves for compose to interpolate —
  `CONTAINER_USER`, `CONTAINER_KVM_GID`, `VM_IMAGES_DIR`, `QEMU_TOOL_SRC` —
  are set in the environment of the `docker compose` process, and only where
  the caller's environment does not already set them. Plain `docker compose`
  against a stack needs the first two exported.

- **`qemu/xml/` is `qemu/stacks/`, with three files.** `helios-tray.xml` keeps
  its name, `mi355x-node.xml` is `mi355x-server.xml`, and `quickstart.xml` and
  `scale-out.xml` are replaced by the new `rocm-golden.xml`, which is what
  `quick-start` runs and the template to copy. `overlay-ci.xml`,
  `overlay-mi100.xml` and `fleet-example.xml` are deleted, and no overlay is
  committed any more.

- **Three stacks, each named after its fleet file.**
  - `vfio-user-ernic-rocjitsu-scale-out` is `rocm-golden`, now two guests
    rendered from `rocm-golden.xml`. It is the default stack.
  - `vfio-user-mi355x-node` is `mi355x-server`.
  - `vfio-user-helios-tray` is `helios-tray`.
  - `vfio-user-ernic-vm`, `vfio-user-rocjitsu-vm`,
    `vfio-user-ernic-rocjitsu-vm` and `vfio-user-ernic-2vm`, the hand-written
    stacks, are removed.

  `compose --stack` and `gen-compose --stack` accept only the three new
  names, and `compose` with no `--stack` runs `rocm-golden` — it used to run the
  hand-written single-VM stack. `compose --vm-name` and `--vm2-name` are
  gone with the stacks that read them. One guest is `rocm-golden.xml` plus an
  overlay setting `<vm-count>1`; a NIC-only or GPU-only guest is not
  expressible.
  - `gen-compose` infers the stack from the first `--xml` file, so
    `gen-compose --xml qemu/stacks/mi355x-server.xml` needs no `--stack`, and
    with no `--xml` and no `fleet.xml` on the search path it renders the
    stack's own file instead of exiting.
  - *If you have a stack rendered by an earlier version* (from `scale-out.xml`
    with or without an overlay, or by an older `quick-start`): re-render it.
    Its header says `# env-sha256:`, which this version does not recognise, so
    `qemu-tool down` does not find it and `qemu-tool compose` run from inside
    its directory falls through to the committed default stack. Put the
    overlay's values in a fleet XML of your own, run `gen-compose --xml
    qemu/stacks/rocm-golden.xml --xml yours.xml --output-dir DIR`, and stop
    containers the old version started with `docker compose -p <project>
    down`. The default fleet has an SDXI device, P4 and log collection that
    `scale-out.xml` did not; set `<sdxi-per-vm>0`, `<p4-enable>false` and
    `<logs-enable>false` in the overlay to keep the old shape.
  - `quick-start` guests are now `rocm-golden-1` and `-2`, not `quickstart-1`
    and `-2`, and each also has an SDXI device and P4. `--flavor default` is
    still the name of that flavor, and the state directories are still
    `quickstart` and `quickstart-key`. Overlays written by an
    older `quick-start` are orphaned, and `quick-start up` must be re-run
    before `quick-start test` against a stack an older version brought up.

- **Generated files carry `# xml-sha256:`**, not `# env-sha256:`, and
  `qemu-tool down` identifies a stack by that header alone, no longer by its
  directory name.

- **`--env-file` and `--fleet-file` are `--xml` on every subcommand.** Neither
  old spelling is accepted; see "Every subcommand takes its settings as
  `--xml`" under Changed.

- **`qemu-tool lmcache-stats` and the `<lmcache-*>` fleet XML elements are
  removed** with the rest of LMCache; see Removed. Under strict parsing a
  leftover `<lmcache-*>` element is an error, not ignored.

- **`helios-tray.xml` no longer carries one host's images directory.** It
  set `<vm-images-dir>` to a developer's checkout and now uses
  `/var/lib/qemu-tool/images` like the other two. Keep a local path in a
  `fleet.xml` of your own. The stack's checkout-only
  `docker-compose.override.yml` and the small-BAR rocjitsu config it mounted
  are deleted: a checkout now runs the large BAR the fleet file sizes for,
  as every other install already did.

### Added

- **The daily benchmark lane checks that every guest exporter the fleet
  forwards is up.** `scripts/check-guest-exporters.py` reads the fleet file
  and Prometheus's answer to `up`, and for each guest and each TCP port in
  `<vm-extra-hostfwd>` requires a target at the guest's container address
  and that port with a value of 1. It names every target that is down and
  every forwarded port no job scrapes, and the lane fails with those names.
  The lane used to ask only whether Prometheus answered a query, which it
  did with half its guest targets down.

- **`qemu-tool publish-qcow-prune`: retention for the published image's
  repository.** Keeps the 10 newest dated tags (`<YYYYMMDD>.g<sha7>`) and
  any pushed within 30 days, always keeps `latest`, every `qm.<sha7>` and
  every tag of another shape, and deletes the other dated tags; `--keep`
  and `--days` change the numbers. It prints its plan and deletes nothing
  unless given `--yes`. Docker Hub only — a repository on another registry
  is refused — with the repository from `--ref` or an `--xml` layer's
  `<publish-ref>`, and `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` read from
  the environment. A listing it cannot complete deletes nothing. The publish
  workflow runs it after a push, never on a dry run, so **the Docker Hub
  token now needs delete access**. A prune that fails there is a warning on
  the run, not a failure: the image is already published.
  - It removes tag names and nothing else. The image a deleted tag named,
    and the `vm-info.json` and keypair attached to it as a referrer, stay
    in the registry; every publish also carries a `qm.<sha7>` tag that is
    never deleted, so the image stays reachable too. This tidies the tag
    list and does not reclaim storage.
  - `--yes` is taken only spelled in full; the subcommand accepts no
    abbreviated option.

- **`qm.<sha7>`, a third tag on every published image**: the qemu-minimal
  commit alone, beside the dated tag and `latest`.

- **The golden image carries one kernel.** The Ubuntu release kernel the
  cloud image ships is purged at the end of the bake — about 370 MB, and the
  entry GRUB would fall back to, for which the image has no ionic, amdgpu or
  SDXI module. The slim play asserts the guest is running the pinned kernel
  first, `image_slim` asks apt what the purge would remove and stops if it
  is anything but kernels, and what the packages leave in `/lib/modules`,
  `/boot` and `/var/lib/dkms` is removed too. `image_slim_purge_old_kernels`
  is still off by default.
  - GRUB boots the pinned kernel by name: `rocm_ernic_setup` writes
    `GRUB_TOP_LEVEL` to `/etc/default/grub.d/99-rocm-ernic-kernel.cfg` when
    it installs the kernel, where the boot used to go to whichever kernel
    sorted first.
  - `qemu/checks/rocm-golden.sh` fails an image with a second kernel image,
    a kernel metapackage, another kernel's module tree or DKMS build, or a
    GRUB default that does not resolve to the running kernel.
  - Not yet run against a guest; both lanes boot the baked image and run
    that check before anything is published.

- **`--ssh-port auto` on `gen-vm` and `run-vm`, and `<bake-ssh-port>` in the
  fleet XML.** The tool no longer has to be told a free host port for the
  guest's SSH forward: `auto` takes the first port from 2222 up (to 2321)
  that can be bound on every address and on loopback, immediately before
  QEMU starts, and prints it. A bake on a WSL2 host died with `Could not set
  up host forwarding rule 'tcp::2222-:22'` because something on the Windows
  side held 2222; nothing in WSL listened on it, but a bind fails, which is
  what the probe does.
  - `gen-vm` starts QEMU again on the next free port, up to five times, if
    the probed one is taken before QEMU binds it. `run-vm` becomes QEMU and
    cannot retry. Only a refusal of the SSH forward itself is retried: a
    held `--extra-hostfwd` port stops the bake at once with QEMU's message,
    since no other SSH port would free it. An SSH answer on the port does
    not count as the guest if QEMU has exited by the time it arrives.
  - The management NIC MAC is derived from the default port in auto mode,
    not the chosen one, so every boot of an image presents the same MAC;
    `--mac` still wins. The guest agent and QMP socket paths follow the
    chosen port.
  - `qemu-tool ssh`, `list` and `inspect` already read the port from the
    running QEMU, so they find an auto port unaided.
  - `<bake-ssh-port>` (a number or `auto`) is read by `gen-vm --bake` only
    and loses to an `--ssh-port` flag. `rocm-golden.xml` sets it to `auto`,
    so `qemu-tool gen-vm --bake --xml qemu/stacks/rocm-golden.xml` no longer
    depends on 2222 being free.
  - A number behaves exactly as before, failure message included, except
    that `--ssh-port` now takes plain ASCII digits in 1-65535 only: `0`, a
    negative, anything above 65535 and spellings like `+22` are usage
    errors where they used to reach QEMU. A fleet's
    `<ssh-port-base>` does not take `auto`: its ports are written into the
    generated compose file, and `quick-start` already remaps them.
- `qemu-tool check` reports whether each settings file in force is valid
  fleet XML: one `FAIL` line per problem, naming the file and the element,
  where an unparsable layer used to leave the report with no fleet and no
  word about why.
- `tests/test_fleet_files.py` parses every `qemu/stacks/*.xml` and every
  fleet XML overlay a workflow writes inline, so a typo in either fails
  `Gen Compose Check` instead of surfacing in a guest boot.

- **`gen-vm --bake` and `publish-qcow --xml`.** `rocm-golden.xml` carries the
  rocm-golden image's bake and publish settings in new `<bake-name>`,
  `<bake-release>`, `<bake-size>`, `<bake-vcpus>`, `<bake-vmem>`,
  `<bake-packages>`, `<bake-playbook>`, `<bake-username>`,
  `<bake-password>`, `<publish-ref>` and
  `<publish-flavour>` elements. `gen-vm --bake --xml qemu/stacks/rocm-golden.xml`
  builds the image from the cloud image instead of pulling `<backing-ref>`,
  and `publish-qcow --xml` takes its ref, name and image path from the same
  file. Nothing else reads them. The baked account is separate from
  `<vm-username>`/`<vm-password>` because it is published. A missing
  `publish-qcow` argument now exits 1 naming the flag and the element, rather
  than as a usage error.
  - `publish-qcow` needs no `--key` for an image `gen-vm` baked with a
    key it generated: it publishes that keypair and prints which one. An
    image baked with a key you named, or one with no record, needs `--key`,
    which names the private key that gets published. The key is never taken
    from `<vm-ssh-key-file>` or from `~/.ssh`. The record is the one beside
    the image file, not one chosen by `--vm-name`, and it is believed only
    about `<images>/keys/<name>_id_rsa`: a record naming any other path, a
    symlink there, a pair that no longer has the fingerprint the bake
    recorded, or a record with no fingerprint is refused. It refuses a
    `--key` that is not the one `gen-vm` recorded baking into the image, a
    private key that is not the other half of its `.pub`, and an image
    `gen-vm` pulled rather than baked.
  - A bake leaves out the fleet's `<vm-extra-hostfwd>`: it runs QEMU on the
    host, where a forward whose port is taken stops it starting.
  - `<bake-packages>` and `<bake-playbook>` are resolved against the current
    directory. A missing one names the directory, and a missing manifest
    stops the bake before the cloud image is downloaded.


- **`gen-vm --bake` refuses a bake short of memory.** The bake's module
  builds run one compiler per vCPU, so under 1024 MiB per vCPU the guest's
  OOM killer ends the amdgpu build a quarter of an hour in, with an error
  that names dkms. The check names both numbers and what to change.
  `<bake-vmem>` sets a bake's memory beside `<bake-vcpus>`; a bake used to
  borrow the fleet guests' `<vm-vmem>`.
- **`qemu-tool compose --stack NAME` reads the stack's own
  `qemu/stacks/NAME.xml`** when no `--xml` is given and no `fleet.xml` is on
  the search path, as `gen-compose` does, so the images directory and the
  stale check have a source.

- **`qemu/stacks/rocm-golden.xml`, the default fleet.** Two guests from the
  published rocm-golden qcow2, each with a GPU, an NVMe, an ernic with P4 in
  guest scope and an SDXI device. It is the default stack, what `quick-start`
  runs, the template for a fleet of your own, and what every tracked
  benchmark runs against.

- **The SDXI server image is pulled, not built.** `<sdxi-image>` in
  `rocm-golden.xml` and `helios-tray.xml` names the published
  `batesste-ci-images-ubuntu-rocm-sdxi` tag. The
  generated `sdxi-N` services name `rocm-sdxi` as their entrypoint, because
  that image has none. `qemu/docker/sdxi/` and the `qemu-tool/sdxi:latest`
  tag are gone, and `quick-start` no longer asks for a build.

- **The device-side images share one build stamp except SDXI, which follows
  the guest's `sdxi_source_pin`.** `<sdxi-image>` is now
  `20261113.g379fbf6-sdxi.dfd76f2`, the server built from the commit the
  guest's driver is built from, while the rest of the fleet stays on
  `20261112.gdca6903`. `gen-compose` no longer compares the SDXI image's
  stamp with the fleet's; `tests/test_playbook_pins.py` requires the
  `sdxi.<sha7>` in every fleet file's `<sdxi-image>` to equal the first seven
  characters of `sdxi_source_pin`, as a live assertion rather than an
  expected failure, and the `Image Tag Consistency` workflow exempts
  `-sdxi.<sha>` refs from its stamp count and requires them to name one tag.
  A non-zero `<sdxi-per-vm>` with no `<sdxi-image>` is still refused.
  The pairing holds for a guest baked from this checkout, and for the guest
  `<backing-ref>` pulls: see "The fleet pulls the guest this repository
  publishes" under Changed.

- **The playbook-test and smoke-test lanes boot the fleet's SDXI device.**
  Their inline overlays no longer set `<sdxi-per-vm>0`, so each guest waits
  on its `sdxi-N` server being healthy. Both lanes pull the SDXI image named
  in `rocm-golden.xml` and include the SDXI servers in their health waits.

- **The `.deb` ships every stack whole** — `grafana/`, `logging/` and
  `tapsetup/` as well as the YAML, the helios stack and the fleet files — so `quick-start` works from a package install.

- **This repo publishes the rocm-golden qcow2.** batesste-ci-images stops
  building guests and pulls this one.
  - `qemu-tool publish-qcow` converts a baked qcow2 to a standalone image,
    compresses it with zstd and pushes it with `oras`, with `vm-info.json`
    and the guest keypair as a referrer. It is the artifact
    `gen-vm --backing-image` pulls, and the media types both sides use now
    live in one module, `vm_artifact.py`. An overlay is refused, and
    `--dry-run` builds everything and pushes nothing.
  - The `Publish - qcow-rocm-golden` workflow runs on pushes to main that
    touch the image's inputs, and by dispatch with a dry-run switch. It
    checks the guest pins against the server images, bakes, runs
    `qemu/checks/rocm-golden.sh` on a boot with no device, then the
    configure, firmware and test stages and an SDXI copy as the one guest
    of a rendered default stack, and publishes to
    `docker.io/sbates130272/qemu-tool-rocm-golden` as
    `<YYYYMMDD>.g<sha7>`, `latest` and `qm.<sha7>`, three names for one
    manifest with the dated one first, since the keypair referrer attaches
    to the first tag. The published disk is the bake, not the disk that was
    tested.
  - `gen-vm --packages` takes a manifest of bare names, one per line, as
    well as the YAML fragment it always took. `rocm-golden.txt` is the first
    kind, and pasted into the cloud-config unchanged it made the document
    invalid: cloud-init applied none of it and the bake waited two hours on
    a guest that would never power off. The generated cloud-config is now
    parsed before the guest boots, and a first boot under KVM is capped at
    30 minutes.
  - `sdxi_source_pin` moves to `dfd76f2`, where upstream `sdxi_char` builds
    on 7.2 unaided, and the carried io_uring patch is deleted.

- **`vm-rocm-golden.yml` bakes a publishable guest without a Docker
  daemon, and that guest passes `qemu/checks/rocm-golden.sh`.** The target is
  a bake inside a container build, with nothing but `gen-vm
  --ansible-playbook`. What that took:
  - Every bake play is tagged `install`. The SRQ + P4 and SDXI plays had no
    tag, so `--tags install` finished green without them. The live firmware
    play had none either, so it ran in every bake, needed Docker, and baked
    an `ip_discovery.bin` the caller had asked not to have. It is now
    `--tags firmware`, and replaces an existing blob instead of keeping it.
  - `-e rocjitsu_firmware_script=<path>` copies rocjitsu's
    `vfio_guest_firmware.py` to the guest and runs it there. The gap set goes
    beside the packaged blobs in `/lib/firmware/updates/amdgpu` with its
    manifest, a stub is never written over a packaged blob, and
    `/lib/firmware/amdgpu` is left empty for a consumer's `ip_discovery.bin`.
    `rocjitsu_firmware_ip_discovery` now defaults to false.
  - An `ionic-exclude` initramfs hook, and a regeneration of every initramfs
    after the SRQ + P4 build, so no image carries an ionic module older than
    the one `ionic_rdma` and `ernic_p4` were built against.
  - `sdxi_setup` builds `sdxi_perf` as well as `sdxi_char`, installs both
    under `updates/`, blacklists `sdxi_perf`, and fetches the pinned commit
    itself instead of the tip of a branch.
  - ROCm is pinned to the 10.1 line, with `ROCM_PATH` and `PATH` set for
    login and non-login shells, `/opt/rocm/bin` and `/opt/rocm/lib` symlinks,
    importable `amdsmi` bindings and `amdrocm-blas-dev`.
  - `amdgpu-probe --status` reports `absent`, `nofirmware`, `ready`,
    `loaded`, `unbound` or `stranded` instead of loading the driver, and a
    login hint reads it.
  - `/etc/rocm-golden-guest.json` and `~/WELCOME.md` record what was baked.
  - The playbook's defaults are the published image's: no dotfiles, no SSH
    private key, no GPG signing, no distro upgrade, no device metrics
    exporter. `unattended-upgrades` is purged before the first apt run.
  - `rocm_ernic_setup_kernel_debs_dir` installs the kernel from .debs already
    on the controller, and the mirror fetches retry 8 times.
  - The playbook-test lane bakes with those defaults and the
    `rocm-golden.txt` package manifest, then runs the check.
  - `tests/test_playbook_pins.py` compares the guest pins with the server
    image tags and asserts every play is a bake play or a live one.

- **The golden image bakes in the metrics exporters and the wait-online
  mask.** node_exporter with its amdgpu textfile collector, `rdma_exporter`
  (pinned to v0.7.5 by tarball checksum; enabled, not started, since a bake
  has no RDMA device) and the
  `systemd-networkd-wait-online` mask are now role tasks that
  `vm-rocm-golden.yml` runs. `vm-quickstart.yml` calls the same task files,
  so it loses about 140 inline lines and changes nothing on an image that
  already has them.

- **Three consolidated roles.** `rocjitsu_setup` (firmware, amdgpu, guest
  tools, live GPU tests), `rocm_ernic_setup` (image preparation, device naming,
  boot-time addressing) and `sdxi_setup`. Callers reach the parts with
  `tasks_from`. All four repo-local roles, `image_slim` included, are now
  linted at the `production` profile in CI; two were before.

- **Ernic P4 bandwidth on the performance page.** The metric was tracked but
  missing from the page's badges and its recent-runs table, which are now
  generated from the metric list rather than written out.

### Changed

- **The fleet pulls the guest this repository publishes.** `<backing-ref>` in
  `rocm-golden.xml`, `helios-tray.xml` and `mi355x-server.xml` became
  `docker.io/sbates130272/qemu-tool-rocm-golden:20261117.g80c17f8`, a dated
  tag of `<publish-ref>`, in place of the batesste-ci-images qcow2
  `20261112.gdca6903-...-qm.096ddfe`; it has since moved again, to
  `20261117.g481c892`, which is the next entry, and what follows here is
  about the first of the two. That older guest had no SDXI driver or
  CLI, no hipFile SDK and no P4 client, so the daily report lane failed all
  four of its benchmark steps against it. The new one was baked from
  `vm-rocm-golden.yml` at `80c17f8`, which installs all of them, one kernel,
  the ernic udev naming rules and the boot-time addressing unit, and
  publishes the image's own generated keypair beside it. Its account is
  `<bake-username>`, `ubuntu`, which is what the fleet files already log in
  as; `<vm-password>` never reaches a pulled guest.
  - *What has been seen:* the publish run baked it, ran
    `qemu/checks/rocm-golden.sh` against it and booted it behind a
    one-guest stack with an active RDMA port, the GPU tests and a copy
    through `/dev/sdxi0`, with P4 off. And one run by hand on the dev host,
    2026-11-17, from this tree: `quick-start up` and `test` on a fresh
    images directory, then the daily report lane's steps as the lane runs
    them. All exited 0. The artifact unpacked to 8,422,096,896 bytes; `up`
    took 2 m 5 s with the server images already cached and both guests
    answered SSH with the published key about 23 s later; `test` took
    1 m 38 s (`ib_send_bw` 559.27 MiB/s at 65536 B, NVMe about 118 MiB/s
    write and 288 read). Before any playbook each guest had one kernel
    (7.2.6), `/dev/sdxi0`, `/dev/ernic-p4` open to the `ernic-p4` group
    with the account in it, the P4 client and benchmark, `sdxi-cli`, the
    hipFile SDK, `rocm-ernic0` and `rocm-rdma-ernic0` by name, and the
    address `quick-start` expects. The benchmarks: GEMM 643 kFLOP/s,
    hipFile 13.3 MB/s, hipFile fio 12.3 MB/s, P4 FP8 206.7 MB/s with P4 on
    and no sudo, SDXI copy 2523.0 MB/s. Guests were 8 vCPU, not the lane's
    4, and this host installs a TLS-inspection CA into the guests, which
    the fio step's fetch from GitHub relied on here.
    *Since, in CI:* `quickstart-boot` passed on the pull request that
    pinned it, and the daily report lane passed against it on `main`, its
    first pass on an image published from here; the figures are in the next
    entry. `helios-tray` and `mi355x-server` have not been booted from it
    at all.
  - *Known wrong with that image:* `apparmor.service` is failed at boot and
    the system reports `degraded`. snapd is removed without being purged,
    and the profile it leaves in `/etc/apparmor.d` no longer parses.
    Nothing checked for failed units, so it was published. No lane step
    depends on it. The bake is fixed (see Fixed); that image is not, and
    its guests boot degraded. `20261117.g481c892`, which the fleet files
    now pull, was baked with the fix.
  - Prometheus's `amd-gpu` and `hsa-snoop` targets were down for golden
    guests: the bake disables both exporters, since neither can run
    against the emulated GPU. Their scrape jobs, forwards and panels are
    removed (see Removed), which needs no new image.
  - The image's naming rules are rocm-ernic's copy at the pinned commit
    (2928 bytes in the guest; this repository's file is 9647), whose
    Ethernet stanza names one NIC per guest. Fine for the default fleet,
    as measured above. The eight-NIC `helios-tray` and `mi355x-server`
    guests should have one renamed Ethernet device and seven under kernel
    names, and nothing replaces the rules there: their `quick-start`
    flavors run no playbook. Read from the rules file, not from a guest.
    Addresses do not depend on the names. The bake is fixed (see Fixed),
    and those guests get this repository's rules from `20261117.g481c892`.
  - *On a host that already ran `quick-start`:* the next run sees that the
    guest disks were made on another image, says which, and stops unless
    you answer yes at a terminal or pass `--clean-overlays`. See the
    `quick-start` entry under Fixed.

- **`<backing-ref>` is `20261117.g481c892` in all three fleet files**, the
  image the publish workflow baked on `main` at `481c892` and pushed after
  its guest check and its one-guest gate passed (manifest
  `sha256:72a0aa0c1ad4...`, 2.08 GB to pull, 8.38 GB unpacked by its
  annotation). It is the first published image with the bake fixes listed
  under Fixed: snapd purged so `apparmor.service` starts, this repository's
  NIC naming rules, the `amdgpu-probe` firmware guard with its `nofirmware`
  and `unbound` status words, and a record of which `rdma_exporter` is
  installed. The pins it was baked at are the ones `20261117.g80c17f8` had,
  so the server images do not move. All three stacks are regenerated.
  - *Seen, in that publish run:* the purge and the AppArmor restart in the
    bake; the bake's comparison of the installed naming rules with this
    repository's; both of the bake's probe assertions; and on the boot with
    no device, `apparmor: apparmor.service is not failed`, the naming-rules
    assertions and the probe's refusal against a scratch tree.
  - *Pulled and booted by hand, on the dev host, in one session:* the two-guest
    default fleet, `quick-start preflight`, `up`, `test` and `down` from
    this tree into a scratch directory, guests of 8 vCPU on 32 cores. The
    pull was the manifest the publish pushed and unpacked to 8,379,301,888
    bytes. On both guests before any playbook: no failed unit,
    `systemctl is-system-running` reading `running`, snapd gone and
    `apparmor.service` active; `amdgpu-probe --status` reading
    `nofirmware`, the login hint naming the host command at a real login,
    and `sudo amdgpu-probe` refusing with nothing loaded; the installed
    naming rules equal to this repository's file and the NIC and the IB
    device renamed; listeners on 9100 and 9879 only. `quick-start test`
    passed in 1m38s (ok=65 changed=17 and ok=61 changed=14, where
    `20261117.g80c17f8` gave 66/21 and 62/18): it left the naming rules
    and `rdma_exporter` alone, `vector_add` passed on both guests,
    `ib_send_bw` averaged 515.38 MiB/s, and NVMe write/read was 140/234 and
    143/215 MiB/s. Prometheus had all five targets up and no `amd-gpu` or
    `hsa-snoop` job. On guest 1, as the guest user, `p4_fp8_bench` gave
    208.0 MB/s and a timed SDXI copy 2702.0 MB/s. The recovery from a real
    failed probe was run both ways on fresh overlays, through `quick-start
    test` and through `sudo amdgpu-probe` after a blob staged by hand, and
    bound the GPU each time with no reload.
  - *Not seen:* any CI lane on the pulled image; `quickstart-boot` and the
    daily report lane will be the first. `helios-tray` and `mi355x-server`
    were not booted, so eight renamed NICs in one guest are expected and
    unobserved, and the lane's GEMM, hipFile and fio steps, `--in-network`
    and 4 vCPU guests were not run against this image.
  - *The daily report lane's first pass on a published image*, on `main`
    against `20261117.g80c17f8`, two guests of 4 vCPU on a 4-core GitHub
    runner: GEMM 484 kFLOP/s, hipFile 45.5 MB/s, hipFile fio 36.2 MB/s, P4
    FP8 80.3 MB/s, SDXI copy 737.7 MB/s, and its check that every forwarded
    exporter is up passed. Not comparable with the dev-host figures above,
    which were 8 vCPU guests on 32 cores.
  - *The compiler cache, measured:* the publish bake step took 18m25s with
    the cache against 25m08s without it the same morning, 1788 of 2053
    cacheable ccache calls hitting; a pull request's lane run starting from
    the entry `main` held baked in 13m26s against 30m42s cold, 1818 of 2053
    hitting.
  - *A warm bake no longer saves its cache again.* The role's threshold,
    `bake_ccache_save_misses`, was 100, and every warm bake seen missed
    235, 241 or 265 calls, so each saved another entry of about 390 MiB;
    six were stored in two days. It is now 500. A cold bake, or a new
    compiler in the archive, misses 1833 and still saves; a moved pin still
    saves through `--cache-replace`, whatever it missed.
  - *On a host that already ran `quick-start`:* as for the move before it,
    the next run stops and asks before it replaces the guest disks; pass
    `--clean-overlays` to say yes unattended.

- **`gen-compose` holds `<backing-ref>` to a rule of its own instead of the
  servers' build stamp.** The guest is published from here under this
  repository's stamp, the day and the commit it was baked from, which never
  equals the `<date>.<ci-sha>` of the batesste-ci-images build the three
  device-side images share; the old four-way comparison would have refused
  every fleet pulling it. Now, by the tag of the ref:
  - exactly `<YYYYMMDD>.g<sha7>`, the dated tag `publish-qcow` pushes: accepted
    and not compared;
  - no tag, `latest` or `qm.<sha7>`: **refused**. Those move, `gen-vm` and
    `quick-start` cache the image by the text of the ref, and a moved tag
    leaves each host on whatever it pulled first. `<image-allow-guest-lag>`
    does not excuse this;
  - a digest: accepted;
  - a tag that can only be a mistake for one of those: **refused**, where it
    used to render with a warning about an image with no build stamp. That
    is a near miss of the dated tag (`2026117.g80c17f8`, an eighth hex
    digit, upper case), `latest` in another case, `qm.` followed by anything
    but seven hex digits, and, when a layer sets `<publish-ref>`, any tag
    but the dated one in that repository, `stable` and `<stamp>-dirty`
    among them. `<image-allow-guest-lag>` does not excuse it;
  - `<stamp>-...`, a batesste-ci-images qcow2: the old rule, so a local fleet
    still pinning one is still checked against the servers' stamp;
  - anything else: warned about and left out, as before. With no
    `<publish-ref>` in force, which is every fleet not layered on
    `rocm-golden.xml`, a free-form tag in the publish repository is still
    here.
  `<qemu-ref>`, `<ernic-ref>` and `<rocjitsu-ref>` are compared exactly as
  they were. *If your fleet file names `latest` as its `<backing-ref>`,
  `gen-compose` and `quick-start` now stop; name the dated tag.*
  - What ties the pulled guest to the servers is checked in the test suite,
    which can see what `gen-compose` cannot: `tests/test_playbook_pins.py`
    reads `vm-rocm-golden.yml` at the commit the tag names and compares its
    `ernic_source_pin` and `sdxi_source_pin` with the `ernic.<sha>` and
    `sdxi.<sha>` each fleet file serves. A change that moves a pin has to
    set `<image-allow-guest-lag>true` in the fleet files until a guest at
    the new pin is published and `<backing-ref>` moves, and the test fails
    a fleet file that keeps the element after that. It needs git history:
    `Gen Compose Check` now checks out with `fetch-depth: 0`, and a shallow
    checkout skips it.
  - `tests/test_image_tag_consistency.py` and the `Image Tag Consistency`
    workflow gain a guest section: one `<backing-ref>` across the three
    fleet files, in `<publish-ref>`'s repository, by a dated tag, and no
    batesste-ci-images qcow2 named in any workflow, stack or fleet file.
    Their stamp counts never saw the new ref, since both match only
    `batesste-ci-images` names.
  - `quickstart-boot` warns, without failing, when the IB device it resolves
    is not `rocm-rdma-ernic*`.

- **`sbates130272.batesste` 3.2.1 to 3.3.1.** Picks up two fixes asked for
  from here: `check_platform` no longer falls back to top-level fact
  variables, which printed a deprecation warning for every role and stops
  working at ansible-core 2.24; and with dotfiles off, `user_setup` and
  `git_setup` now make `~/.bashrc` source the `~/.bashrc.d` snippets that
  3.3.0 moved their shell setup into. 3.3.0 also removed `ptop` from
  `fave_packages`. A baked guest gains `~/.bashrc.d/user_setup.sh` and a
  sourcing loop in `~/.bashrc`, and no longer has `ptop`.

- **Both workflows that bake the golden guest keep a compiler cache between
  runs and share it, the publish workflow included; the playbook-test lane
  no longer compacts the image it bakes.** The plays that build kernel
  modules are about fourteen minutes of that bake, and the inputs of those
  modules move only when a pin does. A bake given a cache
  directory — `qemu-tool gen-vm --bake --cache-dir DIR`, or
  `<bake-cache-dir>` in a fleet file — now puts
  ccache in front of every module build — amdgpu, both ionic-ernic builds,
  `ernic_p4` and SDXI — and carries the cache in and out of the guest as one
  tar file; the new `bake_ccache` role does it, and removes every trace
  before the image is finished. The lane and the publish workflow restore
  and save that directory with `actions/cache`, through two composite
  actions they share, `.github/actions/bake-ccache-restore` and
  `bake-ccache-save`, so both compute one key for one directory. The lane
  bakes with `--no-compact`, since nothing publishes its image; the publish
  workflow compacts, as before. There is no cache directory by default and
  `qemu/stacks/rocm-golden.xml` names none: a bake by hand compiles
  everything unless it is given one. The one thing every bake now runs is
  the role's removal, which is a no-op on a guest that never had the cache:
  it is what cleans up after a cached bake that failed part way, whatever
  the rerun was given. `qemu/checks/rocm-golden.sh` now fails an image that
  carries a ccache configuration, cache or compiler symlink.
  - **A kernel module in a published image may now be linked from objects
    compiled in an earlier run on `main`**, not in the run that published
    it. ccache reuses an object only for the same compiler, flags and
    source, and nothing compares a cached object with a fresh compile. The
    publish workflow runs `qemu/checks/rocm-golden.sh` before it pushes, so
    an image that still carried ccache would not be published. A run on
    `main` cannot read a cache a pull request saved; a publish dispatched by
    hand from another branch can read what dispatched runs on that branch
    saved.
  - The configuration a cached bake compiles under is the role's
    `/etc/ccache.conf` and nothing from the cache. ccache also reads a
    `ccache.conf` at the top of its cache directory, ahead of the system
    file, and that directory is the restored archive; the role now deletes
    one after unpacking and leaves it out of the archive it saves.
  - Both bake steps stop on an empty cache directory, and the save action
    fails on an empty directory or key once the bake has run. `gen-vm`
    takes `--cache-dir ""` for the current directory, so an action output
    that resolved to nothing was a green run that saved nothing.
  - The publish workflow also runs on a push to `main` that changes either
    composite action, so a change to the cache's key or directory is
    followed by the publish that saves the first entry under it.
  - Measured on one pull request: the lane took 27m27s cold and 17m46s on a
    later run, the amdgpu play going from 7m49s to 2m27s; that later run hit
    1812 of 2053 cacheable ccache calls. A later cold lane run on a slower
    runner took 35m01s. The publish workflow's bake step took 24m33s
    uncached. Its first run with the cache is in the `20261117.g481c892`
    entry above.
  - The cache is named by the flag or the element and by nothing in the
    environment. `gen-vm` creates the directory and passes it to the
    playbook as the extra var `bake_cache_dir`; it refuses `--cache-dir`
    without `--bake`, and a cache when the bake has no playbook.
  - The saved cache is replaced when the entry restored was saved for other
    pins (`gen-vm --cache-replace`), or when five hundred or more ccache
    invocations missed; otherwise it is left, since a cache entry cannot be
    overwritten.
  - The publish workflow runs on pushes to `main`, so `main` holds a cache
    that every pull request's lane run can read, and it is replaced when a
    merged pin bump is published. The lane no longer has to be run by hand
    on `main` to seed one.
  - The lane's `Report VM image size` step now reports an uncompacted
    backing image, 2.3–2.5x the published size.
  - The comment in `qemu/packages.d/rocm-golden.txt` described cache plumbing
    (`dkms_build_cached`, `probe-guest`) that this repository never had.

- **`report-for-vm-rocm-golden` is the one benchmark workflow.** It replaces
  `report-for-vm-rocjitsu` and absorbs the ernic P4 and SDXI benchmarks from
  `report-for-vm-ernic` and `report-for-helios`. It brings the stack up with
  `qemu-tool quick-start` from `rocm-golden.xml`, carries no settings in its
  `env:`, and uploads only `badge-*.json`, as `perf-rocjitsu`,
  `perf-hipfile-fio`, `perf-ernic` and `perf-helios`.

- **The published site carries documentation and performance trends only.**
  `publish-pages` fetches the four `perf-*` artifacts into a scratch directory
  and publishes nothing from them but the generated `perf/` page.
  `render-site-perf.py` takes `--input-dir` and no longer reads report pages.

- **Two image playbooks instead of seven.** `vm-rocm-golden.yml` imports
  `vm-basic.yml` once and carries the ernic, ROCm and rocjitsu plays itself;
  `--tags configure` and `--tags test` are its live-guest entry points.
  `vm-basic.yml` now runs once per bake rather than twice. The slim pass is a
  play in each playbook rather than an imported file.

- **The rocjitsu play is three role calls.** The four inline amdgpu source
  patches became one script, `amdgpu-emu-patches.py`, checked byte for byte
  against the heredocs it replaces. Firmware generation, which was written out
  in four places, is one task file; a live guest selects it with
  `rocjitsu_setup_firmware_live`. The names CI and the image bake already pass
  (`rocjitsu_firmware_ip_discovery`, `rocjitsu_generate_firmware`,
  `vm_rocjitsu_*`) still work.

- **Fleet inventory variables renamed.** `lmcache_guest_rocjitsu_image` is
  `fleet_guest_rocjitsu_image` and `lmcache_guest_ernic_mac` is
  `fleet_guest_ernic_mac`. The committed stacks are regenerated.

- **The ernic pins live in `vm-rocm-golden.yml`.** `ionic_kernel_pin` and
  `ernic_source_pin` are defined once on its first play and shared with the
  other two through YAML anchors. The SDXI pin is a variable on the SDXI play.

- **`udev/` is gone.** `99-rocm-ernic.rules` and `99-sdxi.rules` are in their
  roles' `files/`, replacing the symlinks that pointed at them. The host-side
  `install-vfio-rules` and `99-qemu-minimal-vfio.rules` are in `scripts/`.

- **`fleet-quickstart.yml` is `vm-quickstart.yml`, and does less.** Checked
  against a guest booted from the published image, three things it did are
  already there: the ROCm prefix lookup and the explicit library path (ROCm
  is on PATH and in the linker cache), and the ionic module load with its
  version-mismatch reload (the image loads the DKMS pair at boot). The udev
  rules now come from `rocm_ernic_setup`'s `naming` tasks rather than a second
  copy of the same steps.

- **Every subcommand takes its settings as `--xml`.** It replaces
  `--fleet-file` on `quick-start`, `gen-compose` and `gen-vm`, and
  `--env-file` on `run-vm`, `compose`, `inspect`, `ssh`, `qcow` and `check`.
  Neither old spelling is accepted. `inspect`, `ssh`, `qcow`, `check` and
  `run-vm` could not read fleet XML at all before; one loader,
  `settings.py`, now serves them all.

- **`--help` is reorganised.** Commands are listed by purpose, options are
  grouped, every argument has help text and the main commands have examples.
  Help wraps at 80 columns however wide the terminal is. The reorganisation
  itself changes no flag, default or parsing; the defaults that did change
  in this release are listed under Breaking. The man page is rewritten to
  match.

- **`scripts/release.sh` stamps the man page.** Its `.TH` line carried a
  version and month nothing updated; the release script now writes both, and
  the release workflow's `Verify Version` job refuses a tag the man page
  disagrees with.

- **gen-compose messages name XML elements**, e.g. `<vm-shm-size> has 2
  values but <vm-count> is 3`, and the `--check` failure hint now includes
  `--stack` when the render needs it.

- **CI boots baked guests behind a rendered default stack.** The publish and
  playbook-test lanes render `rocm-golden.xml` with an inline one-guest overlay
  instead of using the hand-written single-VM stack, so the guest sees a mesh
  manager ernic, root ports and an NVMe. `quickstart-boot` and the two-guest smoke test run `rocm-golden.xml` with
  inline overlays too; the daily report lane runs it with none, Loki and
  Alloy included.

- **The `rocm-ernic` smoke-test workflow is `smoke-test-vm-rocm-golden`**,
  file, name and job.

- **A golden bake fetches the ionic kernel sources from a GitHub mirror of
  the stable tree, and checks what it fetched.** rocm-ernic's
  `fetch-ionic-sources.sh` defaults to `git.kernel.org`, which a GitHub
  runner could not connect to twice in two days while GitHub answered in the
  same job; each time the playbook-test lane lost twelve minutes to connect
  timeouts and failed. `vm-rocm-golden.yml` now names a mirror,
  `ionic_kernel_mirror` (`https://github.com/gregkh/linux.git`), and the
  commit the pinned tag peels to, `ionic_kernel_pin_commit`, beside
  `ionic_kernel_pin`. Measured from the dev host for the script's own fetch
  of `v7.2.6`: about 58 s and 292 MB from `git.kernel.org`, which ignores
  the partial-clone filter, against 12 to 14 s and 15 MB from the mirror.
  Nothing here has run on a GitHub runner yet, so whether the lane's
  failure is gone is not known.
  - All four runs of the script are redirected: the controller-side fetch
    of `ernic_srq_dkms` and the in-guest fetch of `ernic_p4_dkms` are given
    the mirror as `--repo` (the second one used the script's default until
    now), and the in-guest fetch of `ernic_guest_setup`, which takes no
    repository, runs under a `url.<mirror>.insteadOf` rule in the guest's
    `/etc/gitconfig`, in the bake and in `--tags configure`. The rule is
    removed in an `always` section when the role has run, and
    `qemu/checks/rocm-golden.sh` fails a baked guest that still has any URL
    rewrite at system scope. Nothing is written to the controller's git
    configuration.
  - After each fetch the bake fails unless the tree is patched on top of
    `ionic_kernel_pin_commit` and its origin resolves to the mirror, with a
    message naming the commit found, the commit expected and the
    repository. A mirror whose tag points somewhere else fails the bake.
    The check runs after the role that fetched, so it does not stop that
    role building from a wrong tree; it stops the bake.
  - **Moving `ionic_kernel_pin` now means moving `ionic_kernel_pin_commit`
    and the tag named in the comment on its line.** The commit is what
    `git ls-remote <the stable tree> 'refs/tags/<tag>^{}'` prints; without
    the `^{}` it prints the tag object, which is not it.
    `tests/test_playbook_pins.py` fails on a comment naming another tag.
  - The configure play includes `ernic_guest_setup` from a block in its
    `tasks:` where it was a `roles:` entry, so that the rule can be removed
    when the role fails. The playbook-test lane runs the guest check's
    URL-rewrite lines again after that stage.
  - `Gen Compose Check` installs the rocm-ernic collection, from the entry
    in `ansible/requirements.yml`, before the unit suite. The tests that
    compare this change with the collection's fetch script and roles read
    the installed copy and skipped without it; under `CI=true` they now
    fail instead.
  - In a baked image `/var/tmp/ionic-src` and `/usr/src/ionic-ernic-p4`
    are partial clones of about 18 MB each where they were about 296 MB,
    going by the same fetches run on the dev host; no image has been baked
    with this change.

### Removed

- **The `amd-gpu` and `hsa-snoop` scrape jobs, their port forwards and
  their dashboard panels.** `gen-compose` rendered both jobs for every
  stack and `rocm-golden.xml` forwarded `:9488` and `:9490` for them, but
  nothing in a guest listens on either: after a successful `quick-start
  test` both targets were down in every guest and nothing said so. Neither
  exporter can run against the emulated GPU every generated stack has.
  amd-metrics-exporter's `gpuagent` assertion-fails on it and leaves
  `/dev/kfd` busy, and the guest image does not install it; hsa-snoop's
  collector deadlocks GPU dispatch on a vfio-user device in its default
  mode, by its own packaging's account, and holds `trace_pipe`, so the
  image disables it. `rocm-golden.xml` now forwards node-exporter (`:9100`)
  and rdma-exporter (`:9879`) only.
  - The home dashboard loses its amd-metrics-exporter row and its hsa-snoop
    panels. "GPU Up" now counts guests with a GPU bound to `amdgpu`, from
    the `amdgpu_devices_total` metric each guest already writes for
    node-exporter, and the log panel shows the stack's own containers; it
    used to read a file inside a guest, which the log collector never sees.
  - *If you render stacks of your own:* regenerate, and drop `tcp::9488-:9488`
    and `tcp::9490-:9490` from a `<vm-extra-hostfwd>` you copied. A stack
    rendered earlier keeps its two dead targets until then; nothing else
    about it changes.
  - `hsa-snoop` itself stays in the image and is still run per workload by
    `vm-rocm-golden.yml --tags test`.

- **LMCache.** The `lmcache_guest` role, `fleet-lmcache.yml`,
  `qemu/docker/lmcache/`, the `lmcache-stats` subcommand, the coordinator and
  exporter services and their scrape jobs, the `<lmcache-*>` fleet XML
  elements, the LMCache dashboards and the panels for it on the fleet
  dashboard. A fleet file that still carries the elements is refused as
  unknown elements (see strict parsing under Breaking); delete them. Guests no longer ship their journals to Loki, which was the guest
  Alloy that role installed.

- **Report pages and the "all reports green" badge.** `docs/reports.md`, the
  report-freshness table and `perf/badge-all-green.json` are gone, with the
  README badges for them.

- **Workflows:** `report-for-vm-ernic`, `report-for-helios`,
  `report-for-lmcache`, `report-for-mi355x`, and the `vm-ernic`, `vm-rocjitsu`
  and `vm-rocm` playbook-test lanes.

- **The KVM verification steps.** Seven copies of an ioctl check on
  `/dev/kvm`, one in the `setup-vm-job` action and six inline. GitHub-hosted
  runners have KVM, and `qemu-tool quick-start` checks it in its own
  preflight.

- **`report-for-quickstart`.** `report-for-vm-rocm-golden` already ran
  `qemu-tool quick-start up` and `test` daily, so the two lanes were one. Its
  checks on Prometheus and Grafana and on what `qemu-tool list` and `inspect`
  report about live guests moved across, and it checks Loki and Alloy too.

- **`scripts/mi355x-node.sh` and `qemu/xml/overlay-mi355x-ci.xml`.** No lane
  ran the script once `report-for-mi355x` was gone, and nothing booted the
  overlay. The MI355X stack itself, its fleet file and the checks on its
  switch topology stay.

- **`Site Script Tests`.** It ran the whole of `tests/` on a bare runner,
  which `Gen Compose Check` already does with `CI=true` and the tools that
  stop the udev and `lspci` tests from skipping. Its one unique step, a
  report-format check, went with the report pages. `Gen Compose Check` is now
  the only workflow that runs the unit tests.

- **Playbooks:** `vm-ernic.yml`, `vm-rocjitsu.yml`, `vm-rocm.yml`,
  `vm-slim.yml` and `vm-helios.yml`. There is no longer a playbook that bakes
  a ROCm-only, ernic-only or rocjitsu-only image, or a helios guest on in-tree
  amdgpu. The `helios` quick-start flavor is unaffected.

- **Roles:** `ionic_image_prep`, `ernic_fleet_guest`, `rocjitsu_gpu_test` and
  `sdxi_guest`, each folded into one of the three above. The task files of
  `ernic_fleet_guest` that no playbook reached — login, packages, patched
  modules, memlock, perftest, netplan — are deleted rather than moved.

- **Settings files:** `overlay-dense.xml` and `overlay-helios-ci.xml`, which
  nothing read, and `vars/github-api.yml`, `vars/sdxi-pins.yml` and
  `vars/ernic-pins.yml`.

### Fixed

- **A baked guest no longer boots with `apparmor.service` failed.** Every
  image baked here did: the base play's `user_setup` role removes snapd
  without purging it and deletes `/var/lib/snapd`, the profile snapd leaves
  in `/etc/apparmor.d` includes a path under that directory, and the parser
  stops on it, so `systemctl is-system-running` said `degraded`.
  `vm-basic.yml` now purges snapd after the role, then restarts
  `apparmor.service` so that a profile that does not load fails the bake
  rather than a guest's boot. The golden image is baked on that play and is
  fixed with it. `qemu/checks/rocm-golden.sh` asserts on the booted guest
  that the unit is not failed and that the leftover profile is gone, and
  prints the failed units; it does not assert that there are none, because
  it also runs on a boot with no NIC, where `rocm-ernic-netcfg.service`
  fails by design.
  - *The published image `20261117.g80c17f8` still has the fault.* Nothing
    in the tree changes an image already published, and a guest pulled from
    it boots degraded. `20261117.g481c892`, which the fleet files now pull,
    does not have it.
  - *Run:* in the bake of `20261117.g481c892`, where the purge changed the
    guest and the restart passed, and on that bake's boot with no device,
    where the check passed. And on two guests pulled from the registry with
    their devices attached, by hand: no failed unit, and `systemctl
    is-system-running` reading `running`.

- **`helios-tray` and `mi355x-server` forward the RDMA exporter's port.**
  `gen-compose` renders an `rdma-exporter` scrape job for every stack, on
  guest port 9879, and those two fleet files forwarded 9100 alone, so every
  `rdma-exporter` target of theirs was down and the home dashboard's RDMA
  exporter panels read 0. The guest image has had the listener all along.
  Both files now carry `tcp::9879-:9879` in `<vm-extra-hostfwd>`, as
  `rocm-golden.xml` does, and both stacks are regenerated.
  - *If you layer an overlay on either file* that sets `<vm-extra-hostfwd>`,
    it replaces the whole value: add the rule there too.
  - *Not run:* against a guest. `rdma_exporter` was seen up on the one-NIC
    default fleet, not on a guest with eight NICs.

- **A golden image is baked with this repository's NIC naming rules, and
  keeps them.** `vm-rocm-golden.yml` handed the naming tasks rocm-ernic's
  copy of `99-rocm-ernic.rules` instead of the role's own, so the published
  image names its Ethernet device `rocm-ernic%n`: one NIC per guest, however
  many it has, and an InfiniBand index two udev workers race for. That is
  invisible on the default fleet, one NIC a guest, and wrong on the
  eight-NIC `helios-tray` and `mi355x-server` guests, which no playbook
  touches after boot. The override is gone. The bake now ends by comparing
  the installed file with the role's and fails on a difference, and the
  `--tags configure` play, whose collection role copies rocm-ernic's rules
  over the image's every time it runs, puts this repository's back
  afterwards. `qemu/checks/rocm-golden.sh` reads the installed file on the
  booted guest, and the playbook-test lane reads it again after its
  configure stage.
  - *The published image `20261117.g80c17f8` still carries rocm-ernic's
    rules*, and with that image the acceptance check now fails on them and
    on `apparmor.service`, here and where batesste-ci-images runs it. On
    the default fleet `quick-start test` replaces the file, as before.
    `20261117.g481c892`, which the fleet files now pull, carries this
    repository's.
  - *A configure run that fails inside the collection's role* does not
    reach the step that puts the rules back. Run it again.
  - *Run:* in the publish run of `20261117.g481c892`: the bake's
    comparison, the configure stage putting the rules back after the
    collection's copy, and the check on the booted guest. On two pulled
    guests the installed file was this repository's byte for byte and
    `quick-start test` left it alone. Not on a guest with more than one
    NIC.

- **The golden guest's login hint no longer tells you to probe a GPU that
  has no firmware yet, and `amdgpu-probe` refuses to.** The image carries no
  `ip_discovery.bin`, since that belongs to the rocjitsu build serving the
  guest, but the hint saw a GPU with no driver and said `sudo amdgpu-probe`.
  The probe failed in the driver and left `amdgpu` loaded with nothing
  bound; a second run reported only "no render nodes found", and unloading
  that module is not safe. Now:
  - `amdgpu-probe` exits 1 and loads nothing while a GPU is on the bus and
    the blob is not staged, and prints the three ways to stage it: `qemu-tool
    quick-start test` on the host, `ansible-playbook vm-rocm-golden.yml
    --tags firmware`, or a blob written by `rj-ip-discovery gfx1250 FILE`
    in the rocjitsu container serving the guest.
  - On a guest already in the failed state it binds the GPU through sysfs
    once the blob is there, without a reload, and says whether it bound; with
    the blob still missing it says so and changes nothing.
  - `--status` has two new words, `nofirmware` and `unbound`. `unbound` is
    amdgpu loaded with the right parameters and a GPU with no driver, which
    used to read `loaded`. The hint has a message for each and names the host
    command first. While a load is still running the word is `loaded`, and a
    plain run says to try again instead of binding under it.
  - `qemu-tool quick-start test` binds a GPU an earlier probe left without a
    driver. It skipped the probe whenever `/dev/kfd` existed, which is the
    case after a probe that bound nothing, and then waited for that same
    node. It now waits for a GPU in the KFD topology and names
    `amdgpu-probe --status` when none appears.
  - The probe's message for amdgpu loaded with other parameters says to
    reboot. It used to offer `sudo rmmod amdgpu` first, which the hint for
    the same state says not to do, and the playbook-test lane's retry no
    longer unloads the module either.
  - A directory named `ip_discovery.bin` no longer counts as the blob.
  - The bake and `qemu/checks/rocm-golden.sh` run the installed scripts
    against a scratch tree and fail an image whose probe does not refuse. A
    with-device run of that check now passes
    `ROCM_GOLDEN_EXPECT_PROBE=nofirmware` where it passed `ready`.

  This is in `20261117.g481c892`, which the fleet files now pull, and in
  guests baked since. The image published as `20261117.g80c17f8` has the
  old hint; on a guest from it run `qemu-tool quick-start test` on the host
  before, or instead of, `sudo amdgpu-probe`. On pulled guests of the new
  image the refusal, the hint at a real login and both recoveries from a
  real failed probe were run by hand and behaved as described.

- **The by-hand way to stage `ip_discovery.bin` names a command that
  works.** The probe's refusal, the guest's `WELCOME.md` and
  `/lib/firmware/amdgpu/README` said to copy "the output of
  `rj-ip-discovery gfx1250`". That command takes an output path and prints
  nothing to copy; without one it exits 1 on `Required arguments:
  <generation> <output-path>`. They now say `rj-ip-discovery gfx1250 FILE`,
  run in the rocjitsu container serving the guest, then copy `FILE`.
  - *Run:* by hand against the rocjitsu image the fleet files pin; a blob
    made that way let `sudo amdgpu-probe` bind the GPU on a pulled guest.
  - *`20261117.g481c892` and every earlier image keep the old sentence*:
    all three are files the bake installs. The next published image has
    the new one.

- **`quick-start` no longer says Prometheus is published on a host port.**
  The generated stacks have given Prometheus no host port since 2026-11-03;
  only Grafana and guest SSH reach the host. `quick-start` still printed
  `Prometheus :9090` in its preflight, asked where to move 9090 when it was
  busy and said "Prometheus will be published on" the answer, wrote
  `<prom-port>` to its port overlay, and gave `http://localhost:9090` in
  its summary; the README listed the same URL. Nothing listened there. It
  now handles Grafana's port and the guest SSH ports only, and the summary
  and the README give what works: Grafana's Explore, or `docker exec
  <stack>-prometheus-1 wget -q -O- 'http://127.0.0.1:9090/...'`, which is
  how the daily lane asks. `<prom-port>` is still accepted in a fleet file
  and still does nothing.

- **No root-owned directory is left in the Grafana data directory.** The
  dashboards are mounted at `/var/lib/grafana/dashboards`, inside the
  `<grafana-data-dir>` mount, and the container runtime created that mount
  point itself: `<grafana-data-dir>/dashboards`, root-owned, in a directory
  `quick-start` and `qemu-tool compose up` otherwise create as the caller.
  Both now create it first.
  - *Not run:* the unit tests create the directory; no stack has been
    started since to show Docker leaving it alone.

- **`quick-start` no longer boots old guest disks with a new image's key when
  `<backing-ref>` moves.** It left an existing overlay alone whatever image
  it was made on, while re-fetching the keypair for the new ref, so on a
  host that had run it before every guest came up on the old image, refused
  the new key, and was reported as never answering SSH. It now compares the
  ref `gen-vm` recorded beside the backing file (`<vm>-backing.qcow2.image-ref`)
  with the one the settings name, before it touches the keypair, and on a
  difference asks to replace the guest disks. The question defaults to no,
  the only `quick-start` prompt that does, so `--yes` and a run with no
  terminal stop there with nothing removed; `--clean-overlays` is how to
  say yes unattended. A stop leaves the old guests still reachable. It
  refuses outright, removing nothing, while a guest of the stack is running
  or Docker cannot be asked: deleting an overlay under a running QEMU
  leaves the guest on a file with no name. `--clean-overlays` by itself is
  refused under running guests for the same reason, which it was not
  before. A backing file with no record is left alone.
  The copy of the keypair it publishes to `<images>/keys/quickstart_id_rsa`
  is now replaced on a re-fetch too; it used to keep the first image's key
  forever. Every earlier move of the tag left the old disks in place the
  same way; whether the guests then accepted the new key was down to the
  two images.

- **`vm-quickstart.yml` no longer downloads and reinstalls `rdma_exporter`
  on a guest that has it.** The role's install had no guard, so every
  `quick-start test` fetched the tarball from GitHub and replaced the binary
  on every guest, baked in or not. It now records the checksum of the
  tarball it installed in `/usr/local/share/rdma_exporter.sha256` and skips
  both steps when that names the pinned release. The image published as
  `20261117.g80c17f8` has the binary and no record, so a guest made from it
  is still installed to once. The bake of `20261117.g481c892` wrote the
  record, and `quick-start test` against two pulled guests of it skipped
  the download, the install and the record on both.

- **`quick-start test` keeps its per-guest summaries in the stack
  directory,** `<stack-dir>/artifacts`, instead of
  `/var/tmp/quickstart-artifacts-<flavor>`. No flag moved the old path and
  `test` empties it first, so two stacks of one flavor deleted each other's
  summaries.

- **The guest image's size in `quick-start`'s disk warning, `check` and the
  README** is the published image's: about 2 GB to pull and 8.5 GB
  unpacked. `check` said 16 GiB unpacked and the others 2.5 and 9, figures
  for images the fleet no longer pulls.

- **The "Ernic P4 FP8 BW" number now measures the P4 FP8 path.** The step
  had never produced a number: it opened `/dev/ernic-p4`, a root-only node,
  as the guest user, issued an ioctl the driver does not have, and would
  then have timed plain `ib_write_bw` against a program whose table was
  empty. It now runs two programs the golden bake installs.
  `p4_fp8_client` is rocm-ernic's own guest test, built from the pinned
  sources, and loads `fp8_policy.json` and installs the entry that turns a
  two-operand RDMA WRITE into an elementwise fp8 add. `p4_fp8_bench`
  repeats that work request 2000 times at 262144 elements, the most the
  device moves in one operation, and checks every result. The badge is fp8
  result bytes per second in MB/s, one byte an element; the device reads
  twice that in operands. It is measured on guest 1 alone: the reduction is
  compute inside one NIC and nothing crosses the mesh, so the old
  "guest 1 to guest 2" description was wrong as well.
  - `/dev/ernic-p4` stays mode 0600 and root-owned upstream. The golden
    image opens it to a new `ernic-p4` group, mode 0660, through
    `99-ernic-p4.rules`, and adds the baked account, so the benchmark runs
    with no sudo. A member can do what the fleet's `<p4-guest-scope>`
    allows, which for `load` is replace the program and write its tables.
  - The step fails, naming what is missing, on a guest without the module,
    the node, the client, the program or group access to the node. All of
    them come from the golden bake, and `<backing-ref>` now names an image
    baked from it.
  - `ansible/files/p4/fp8_policy.json` is removed. It was a hand copy of
    the program; the guest now carries the one from the pinned sources.

- **The SDXI benchmark no longer dies in `awk`.** Its parser used gawk's
  three-argument `match()` and the job container's awk is mawk, so the step
  ended in `awk: line 1: syntax error at or near ,`. It is POSIX awk now.
  With the benchmark requested, an absent `/dev/sdxi0` or output with no
  MB/s figure above zero fails the step; both used to write an `n/a` badge
  and exit 0. The P4 step follows the same rule. The copies are now timed
  for two seconds rather than twenty iterations, which was about 40 ms and
  swung 30% from run to run.
  - `qemu/stacks/rocm-golden.xml` changed in a comment only, which still
    moves its digest: a `rocm-golden` stack rendered before this warns as
    stale until it is rendered again.
  - `qemu/checks/rocm-golden.sh` asked for several tools with one
    `command -v`, which succeeds when any one of them is found. Each is now
    asserted by itself.

- **A tag moved upstream no longer kills a bake.** The rocm-ernic sources
  come from a clone the collection keeps on the controller and updates with
  `git fetch --tags`; once upstream moved its `nightly` tag, git refused it
  ("would clobber existing tag") and the bake died after 118 tasks over a
  tag it never reads. Before each use the clone is now fetched into, and
  a moved tag is taken with a forced fetch. Nothing is removed: the clone
  is shared by every bake and configure run on the controller, and the
  forced fetch leaves its work tree, branches and uncommitted changes as
  they were. A fetch that fails any other way is reported and the clone
  left alone.

- **`gen-compose` refuses a fleet that names no image for a service it
  renders.** An empty `<qemu-ref>`, `<ernic-ref>` or `<rocjitsu-ref>`, or a
  non-zero `<sdxi-per-vm>` with no `<sdxi-image>`, used to render
  `image: ""` and exit 0, failing only when compose read the stack.

- **The SDXI gate and benchmark run a command the guest has.** The guest
  carries the Python `sdxi-cli`, which has no `bench copy`; the publish gate
  now copies a file through the engine and compares it, and
  `generate-vm-report.sh` times the ioctl copy itself.

- **`qemu-tool down` no longer merges a compose project that merely shares a
  stack's project name.** A stack's project is its directory name, which an
  unrelated project can share. Containers are now grouped by project and
  compose file, so a same-named foreign project is not counted into the fleet, cannot make
  it read as yours or hide it, and is not removed as the fleet's orphans:
  a stack whose project name is shared is torn down without
  `--remove-orphans`.
- **`qemu-tool compose up` pre-creates the data directories the stack really
  mounts.** They are read from the rendered `docker-compose.yml`, so the
  defaults the render supplies for an unset `<prom-data-dir>` or
  `<grafana-data-dir>` — which is what `rocm-golden.xml` has — are created as the
  caller, and so are a rendered stack's with no `--xml`. Docker used to
  create them root-owned and Prometheus and Grafana crash-looped.
- **A per-VM `<vm-filesystem>` list bind-mounts each share.** It rendered one
  mount of the unsplit string, `"/a,/b:/a,/b:ro"`, so no guest's share existed
  in its container.
- **The stale-stack warning no longer suggests rendering another fleet over a
  shipped stack.** `compose --stack NAME` given layers that are not the
  stack's own file points at `gen-compose --output-dir` and `compose
  --stack-dir` instead, and says nothing about a `fleet.xml` the search path
  found.
- **`qemu-tool qcow --list` survives a malformed `fleet.xml` on the search
  path**, with a warning naming it; the guard there caught `Exception`, and
  the strict parser exits.
- `quick-start --flavor` help said mi355x is one guest; it is four.

- **The P4 bandwidth benchmark used one hostname for two networks.**
  `generate-vm-report.sh` SSHed to guest 2 to start the server and then told
  guest 1 to dial that same name over RDMA. It now takes the RDMA address
  separately (`ERNIC_P4_VM2_RDMA_ADDR`) and resolves the IB device name in the
  guest instead of assuming `rocm-rdma-ernic0`.

- **A GitHub token was offered to hosts that are not GitHub.** The playbooks
  set an `Authorization` header as a default on every `uri` call. The
  collection roles already authenticate their own GitHub requests, so the only
  calls the default reached were a localhost metrics check and the kernel
  index fetch, which carried an override to keep the token away from
  kernel.ubuntu.com. The default is removed.

- **`update-initramfs` could be deleted with nothing behind it.** Undoing the
  deferred amdgpu-dkms build removed the no-op stub unconditionally and then
  undiverted only if a diversion existed. The removal is now gated on the
  diversion.

- **`gen-vm` read a fleet XML file and then ignored it.** The fleet XML was
  converted to settings keys and merged as though they were field names, so
  none matched and every value was dropped without a word. A fleet file now
  sets the guest's vCPUs, memory, images directory and backing image.

- **`qemu-tool qcow --xml` raised `AttributeError`**, and `qcow --list` never
  scanned the settings file's images directory because it looked the value up
  under the wrong name. Both work.

- **`tests/test_image_tag_consistency.py` matched nothing.** Its pattern could
  not match the `g` in a stamp such as `20261112.gdca6903`, so every check in
  it passed vacuously.

- **`tests/test_quick_start.py` named two workflows that no longer exist.**

## [v1.7.0] - 2026-11-12

### Added

- **`vfio-user-ernic-2vm`: P4 enabled on both ernic services.** `ERNIC_P4=true`
  and `ERNIC_P4_GUEST_SCOPE=load` are now set on the hub and worker; the
  entrypoints pass `--p4-guest-scope` to `rocm-ernic` when `ERNIC_P4=true`,
  creating `/dev/ernic-p4` in both VMs as the runtime prerequisite for the P4
  benchmark. The DKMS half (`ernic_p4.ko`) comes from the batesste-ci-images
  qcow2 built with `vm-ernic-srq-p4.yml` as a post-playbook.

- **Image tags bumped to `20261110.g05827f9`; new `ubuntu-rocm-sdxi` image
  added.** All container and qcow2 references advance together. The SDXI image
  is now a first-class fleet image referenced in `scale-out.xml` and the
  generated stacks.

### Changed

- **`qemu/env/` renamed to `qemu/xml/`.** Fleet descriptor files now live in
  `qemu/xml/`; generated compose stacks inline all XML-sourced values at
  `gen-compose` time so `docker compose down` requires no `--env-file`. Only
  `CONTAINER_USER` and `CONTAINER_KVM_GID` remain as runtime references.

- **SDXI: source repo changed to `ROCm/rocm-systems`; pin advanced to `3feb585`
  (dma-buf).** `sdxi_guest_source_repo` defaults to the upstream repo;
  `sdxi_guest_source_branch` (default `develop`) lets callers point at
  non-default branches without hardcoding.

### Fixed

- **SDXI: bump pin to `af06f717` (`MODULE_IMPORT_NS` fix).** The prior pin
  failed to build on 7.2.x kernels due to a missing `MODULE_IMPORT_NS`
  declaration.

- **`qemu-tool quick-start` without `--fleet-file` failed with "quickstart.xml
  is missing".** `_packaged_fleet()` still referenced the old `qemu/env/`
  source-checkout path after the env→xml rename; it now correctly looks in
  `qemu/xml/`.

## [v1.6.0] - 2026-11-04

### Added

- **XML fleet config format.** Fleet settings files are now XML (`fleet-example.xml`,
  `scale-out.xml`, `mi355x-node.xml`, `quickstart.xml`, and all overlays). The
  old flat env-format files are removed. `--fleet-file <file.xml>` replaces
  `--env-file` on `gen-vm`, `run-vm`, `gen-compose`, `compose`, and `inspect`.
  A new `fleet_xml.py` module handles parsing; existing tests are updated.

- **`qemu-tool quick-start` WSL2 port-conflict retry.** On WSL2 `socket.bind()`
  succeeds for ports the Windows proxy holds (separate network namespaces), so
  `_port_free()` falsely reports a port as free. `quick-start` now retries
  `docker compose up` with `force_port_remap=True` after any failure, remapping
  to a fresh SSH-port base regardless of what `_port_free()` says. Port numbers
  are baked into the generated YAML at `gen-compose` time, so the stack is
  regenerated inside the remap block after `state.values` is updated.

### Fixed

- **`fix(quick-start)`: wrong XML tag name for `VM_SSH_PORT_BASE`.** `_ENV_TO_XML`
  mapped `VM_SSH_PORT_BASE` to `"vm-ssh-port-base"` but `fleet_xml.py` reads
  `"ssh-port-base"`. The unrecognised tag was silently ignored and the default
  port 2222 was baked in regardless of any remap.

- **MkDocs Material site.** The GitHub Pages site (`docs/`) is now built with
  MkDocs Material (teal primary, deep-orange accent, light/dark mode toggle,
  sidebar nav, search). The old Jekyll/minima setup under `site/` is removed.
  The publish-pages workflow fetches report artifacts into `docs/<dest>/`,
  runs `mkdocs build --strict`, and pushes the built `site/` HTML to
  `gh-pages`. `render-site-perf.py` now emits MkDocs frontmatter.

- **`qemu-tool inspect` reports net/block I/O counters and detects the Docker
  daemon.** Each running container row now shows cumulative Rx/Tx bytes and
  block read/write bytes (from `docker stats --no-stream`). A one-line "Docker
  daemon" row appears at the top of the output when the daemon is reachable.

### Changed

- **`refactor(ansible)`: `amdgpu_firmware` and `netplan_ernic` tasks moved out
  of `lmcache_guest`.** Both tasks now live in their own roles so they can be
  applied outside the LMCache playbook without pulling in the full LMCache
  stack.

### Fixed

- **`fix(lmcache)`: re-enabled `report-for-lmcache` workflow.** The lane was
  accidentally disabled; it now runs again and publishes the LMCache report
  artifact.

### Chores

- **`chore(deps)`: bump `sbates130272.batesste` to 3.2.1.**

- **`chore(grafana)`: remove LMCache panels from MI355X dashboards.** The MI355X
  stack does not run LMCache; the stale panels caused misleading empty charts.

## [v1.5.0] - 2026-11-03

### Added

- **Backing image tag tracking in `gen-vm` and `quick-start`.** A sidecar
  file (`<vm>-backing.qcow2.image-ref`) records the tag last used to pull the
  backing image. On the next run, if the tag in the settings file differs,
  the backing is re-pulled and stale overlays are deleted automatically. The
  same pattern applies to the guest keypair. Previously a tag bump in an env
  file had no effect until the backing file was deleted by hand.

- **`--clean-overlays` on `qemu-tool quick-start`.** Deletes the guest overlay
  and NVMe qcow2 files before `make_overlays` runs, so the guests boot from a
  fresh overlay of the backing image. The backing file is kept.

- **`--rdma-duration SECONDS` and `--nvme-duration SECONDS` on `quick-start`.**
  Run `ib_send_bw` for the given number of seconds (instead of the default 1000
  iterations) and each `fio` NVMe pass for the given duration. Useful for
  longer soak tests.

- **NVMe I/O test added to `fleet-quickstart.yml`.** Sequential fio write then
  read on `/dev/nvme0n1` (direct I/O, 128K blocks, `--nvme-duration` seconds,
  default 10 each). Bandwidth reported per guest and included in the artifact
  markdown table. `qemu-tool quick-start` prints a summary line per VM after
  the test.

- **Ethernet connectivity added to `vfio-user-ernic-2vm`.** The 2-VM stack was
  missing the `tapsetup` sidecars and shared `l2` bridge network that the
  scale-out stack has had since d2d9d62 (2 Oct 2026). Without them the ernic
  TCP mesh carries only RDMA payload, ARP goes unanswered, and `ib_send_bw`
  cannot complete its out-of-band exchange. Added: `tapsetup/` directory,
  `tapsetup-hub`/`tapsetup-worker` sidecar services, `l2` bridge network,
  `/dev/net/tun` on both ernic services, `-T tap0` entrypoints with a wait
  loop, `ERNIC_TCP_GUEST_GIDS` per ernic, and `/proc/net/tcp` healthchecks.

- **`vm-ernic-srq-p4.yml` installs `udev/99-rocm-ernic.rules` and
  `rocm-ernic-netcfg.sh`.** The udev rule gives the emulated NIC a stable
  `rocm-ernic<n>` name. The netcfg script (rendered from the
  `ernic_fleet_guest` role template with role defaults) assigns the NIC its IP
  at boot, deriving the index from the MAC so one image serves every guest. A
  guard was added to the template for last-octet overflow (`10 + n > 254`).

- **Image tag consistency CI check.** `qemu-minimal-image-tag-consistency.yml`
  runs on every PR and fails if any `batesste-ci-images` tag in
  `.github/workflows/` carries a different stamp than `qemu/xml/quickstart`,
  or if the env files themselves disagree.

- **Container logs wired into Alloy → Loki → Grafana.** A "Logs (Alloy →
  Loki)" section in the quickstart dashboard shows stdout/stderr from every
  compose-managed container, shipped by Alloy via the Docker socket.

- **`quick-start logs -f` / `--follow`.** Passes `-f` to `docker compose logs`
  and excludes the `alloy` service (which generates continuous access-log noise
  from its Docker socket polling).

- **`qemu-tool qcow` subcommand for overlay management.** Lists all qcow2
  images found across the system path, `$XDG_DATA_HOME`, and `$VM_IMAGES_DIR`,
  with columns for size, backing file, delta, age, and NVMe scratch images.
  `--clean` removes overlay-only images (backing files are exempt). The state
  check (`--state`) reports whether each overlay is consistent with its backing
  file. A man page section covering all options is included.

- **`vm-ernic-p4.yml` playbook for baking the P4 ionic stack into a guest.**
  Follows the order required by `docs/p4.rst` (added in rocm-ernic `c828f99`):
  `ernic_guest_setup` with `patches/p4` first (registers the aux device), then
  `ernic_p4_dkms` to build `ernic_p4.ko`. Verifies `ionic_adminq_post_wait` is
  exported. Writes `/etc/ionic-p4.json` stamp. Cleans build trees. No module
  reload (image-build time, no device attached).

- **`--cpu-phys-bits N` on run-vm, `VM_CPU_PHYS_BITS` in env files, and
  `cpu_phys_bits` VMConfig field.** When set, `run_vm.py` emits
  `-cpu EPYC,phys-bits=N` and gen-compose passes `--cpu-phys-bits` in the
  generated `run-vm` command. Required alongside `VM_PCI_HOLE64_SIZE` when
  the 64-bit PCI hole needs to exceed the EPYC default of 40 bits (1 TiB).
  `qemu/xml/mi355x-node` sets it to 41, raising the ceiling to 2 TiB.

- **`/etc/motd.d/qemu-minimal` installed on rocjitsu guest images.** Shown on
  every SSH login via PAM. Templated with hostname, kernel version and ROCm
  prefix; explains why `hsa-snoop` and `gpuagent` are disabled so a user who
  re-enables either understands the risk (emulated GPU lockup).

### Changed

- **Prometheus, Loki and LMCache coordinator ports removed from host network.**
  Generated stacks (`vfio-user-ernic-rocjitsu-scale-out`, `vfio-user-mi355x-node`)
  no longer publish `9090`, `3100` or the LMCache coordinator port to the host.
  Only VM SSH ports and Grafana (`3000` by default, overridable via `GRAFANA_PORT`)
  remain mapped. Internal-to-stack traffic goes over the compose network without
  any host port exposure. This eliminates VS Code port-forwarding conflicts on
  the Prometheus and Loki ports.

- **`dockerapi` socket proxy removed; Alloy mounts `/var/run/docker.sock`
  directly.** The haproxy proxy's access log produced ~10 lines per container
  every 5 s, flooding `quick-start logs` output. Alloy runs with `cap_drop:
  ALL` and `no-new-privileges`; the socket is the accepted tradeoff in a lab
  stack. The Alloy container discovery keep rule was also fixed: it previously
  used `sys.env("COMPOSE_PROJECT_NAME")`, which is not set inside the Alloy
  container, so the empty-string regex matched nothing and no logs were
  shipped. Changed to `.+` (keep all compose-managed containers, drop bare
  Docker containers like buildx whose old log entries Loki would reject).

- **GPU panels in the quickstart Grafana dashboard replaced with node_exporter
  textfile metrics.** `amdgpu-exporter` is disabled on emulated gfx1250.
  `fleet-quickstart.yml` now installs a small metrics script and systemd timer
  that write `amdgpu_kfd_present` and `amdgpu_devices_total` to the node
  exporter textfile directory. The `rocm-ernic0` panel was changed to match
  by MAC prefix (`72:6f:63:6d:.*`) rather than interface name, so it shows
  green even when the udev rename has not fired.

- **`fleet-quickstart.yml` quickstart play simplified for current images.**
  Removed: ionic-exclude initramfs hook + `update-initramfs` + guest reboot
  (the `20261030.gadc94af` image has a clean initramfs); `gpuagent` /
  `amd-metrics-exporter` stop/disable tasks (neither package is installed).
  Added: udev rules install (`udev/99-rocm-ernic.rules`); ernic Ethernet
  address assignment via MAC prefix; `prometheus-node-exporter` install;
  ZScaler CA cert install from `AMD_CERT` host env var (no-op on non-AMD
  hosts). The ionic modprobe task now loads `ionic` and `ionic_rdma`
  sequentially, checks srcversion to detect a stale initramfs-loaded ionic,
  and reloads the DKMS version if they differ.

- **rocm-ernic collection and source pins advanced to `61b934a`.** The
  collection `version:` in `ansible/requirements.yml` and `ernic_source_pin`
  in `ansible/playbooks/vars/ernic-pins.yml` were already mismatched
  (`31d3114` vs `3fb74d1`) before this bump; both are now equal and at the
  new HEAD. `ionic_kernel_pin` advanced from `v7.2.4` to `v7.2.6` to match
  `IONIC_KERNEL_REF` in the collection's `cmake/ErnicKernelModule.cmake`;
  `driver_ionic.yml`'s major.minor check had allowed the gap to go unnoticed.

- **hsa-snoop bumped from 1.1.1 to 1.3.0** in `vm-rocjitsu.yml`.

- **`gpuagent` disabled at image-bake time** on rocjitsu guests.
  `amdsmi` returns `AMDSMI_STATUS_NOT_SUPPORTED` for temperature/clock/
  utilisation on the emulated gfx1250, causing `gpuagent` to assertion-fail
  and wedge `/dev/kfd` for subsequent openers (symptom: `-EBUSY` with no
  mention of gpuagent). Previously only disabled in `fleet-quickstart.yml`
  before the GPU test; now baked off so a fresh boot is clean.

- **`quickstart-boot` CI lane renamed from `pr-quickstart-boot`.** The `pr-`
  prefix was misleading — the lane is identified by what it does, not when
  it triggers. Workflow file, badge, AGENTS.md and comments in three other
  workflows updated.

- **Every container in every stack now runs as the caller, with no
  capabilities.** Before this, all six stacks ran every service as uid 0 with
  Docker's full default capability set. `CONTAINER_USER` existed to fix that
  and was opt-in, which in practice meant root: the value cannot be committed
  (it differs per host, and `gen-compose --check` compares the rendered stack
  byte for byte), so "put it in an overlay" was the only on-ramp and two
  committed overlays took it, both pinning one operator's ids.

  What the render carries is a *reference*, not a value, and that is exactly
  what makes it safe to emit unconditionally — the stack stays
  machine-independent either way. `qemu-tool compose` and `qemu-tool
  quick-start` answer it from your own `uid:gid`, and `CONTAINER_KVM_GID` from
  the gid that owns `/dev/kvm`, following the same precedence compose itself
  uses: the environment first, then the `--env-file` layers, and only an unset
  key filled in. Set `CONTAINER_USER` now only to pin a *different* identity,
  including `0:0` to go back to root deliberately.

  A plain `docker compose up` has no caller to ask and refuses with `required
  variable CONTAINER_USER is missing a value`. That is deliberate: the cheap
  alternative `${CONTAINER_USER:-}` interpolates to empty, `user: ":"` is not
  an error to Docker, and the fleet runs as root with one warning line as the
  evidence.

  Three services are exempt and say so in the render. `tapsetup-N` cannot take
  a uid at all — Docker sets no ambient capabilities, so a non-root uid holds
  no effective `NET_ADMIN` — but it dropped from Docker's thirteen defaults
  plus `NET_ADMIN` to `NET_ADMIN` alone on a read-only rootfs. `dockerapi`
  holds the host socket. The two stats exporters keep `65534`.

- **`cap_drop: [ALL]` and `no-new-privileges` on everything**, independently of
  who it runs as. A uid is not a capability set: a container can be uid 1000
  and still hold `NET_RAW`.

- **Alloy no longer holds the Docker socket.** `:ro` on a socket mount
  restricts the inode, not the API behind it — anything that can connect to
  `/var/run/docker.sock` can create a privileged container and own the host —
  so the stack's log collector, which parses untrusted container log content,
  held a root credential. A new `dockerapi` service (haproxy, path allowlist,
  `POST=0`) sits in front of it and Alloy reaches the daemon through
  `DOCKER_HOST` over the fleet network. The proxy publishes no port. This
  shrinks the root-equivalent container from a log pipeline to a proxy whose
  whole job is saying no; it does not remove it, because anything speaking to
  the daemon needs the socket.

- **The in-guest MP server unit dropped `seccomp=unconfined`.** It was the one
  relaxation in the fleet with no comment justifying it. Docker's default
  profile allows every syscall the ROCm runtime and the ionic verbs path use;
  it blocks the long tail. Now `lmcache_guest_seccomp_unconfined`, default
  false, beside `--cap-drop ALL` and `--security-opt no-new-privileges`.
  `--network host` and `--ipc host` stay — the first is required for P2P.

- **`qemu-tool compose up` creates the metrics data directories as you** and
  warns about artifacts an earlier root-era run left behind. Docker creates a
  missing bind-mount source root-owned, and the service that then cannot write
  it is what reports the problem, several minutes and one stack trace later.
  The one case it cannot fix is a `<vm>-nvmeN.qcow2` from a root run: 0644
  root-owned, and QEMU opens a drive read-write, so the guest does not boot at
  all rather than merely owning the wrong file.

### Fixed

- **`ionic_rdma` symbol-mismatch logging added before the IB device check** in
  `fleet-quickstart.yml`. The modprobe task now logs kernel version, whether
  the DKMS `ionic_rdma` module file exists for the running kernel, and the
  `modprobe` output. Root cause (initramfs carrying stale in-tree `ionic`) is
  fixed in `batesste-ci-images`, not here.

- **"Find the IB device" retry window reverted to 30 × 2s.** A prior commit
  doubled it to 60 × 2s to cover `ionic_rdma` initialisation delays, but the
  real failure is an initramfs symbol mismatch that retries cannot resolve.

- **`compose down`/`stop` enable every profile and report survivors.** A fleet
  was found with three profile-gated containers still running 41 hours after a
  `down --remove-orphans` that completed successfully. The obvious explanation
  — that profiles are filtered out of `down` — was tested and is false on
  compose v5.3.1, so the cause is not established; `--profile "*"` is
  belt-and-braces and the survivor warning is the part that catches this
  whatever causes it. A teardown that exits 0 while its own project still has
  running containers now names them. It never changes the exit status.

- **An uncreatable metrics data directory says what to run.** The default
  `/var/lib/qemu-tool` is root-owned on every host that ever ran this as root,
  so `Warning: could not create LOKI_DATA_DIR ...: Permission denied` fires for
  an operator doing nothing wrong and left them to work out the remedy. It now
  prints the `sudo mkdir -p ... && sudo chown` that fixes it, and still
  degrades rather than failing — the service may not be in the profiles being
  started.

- **`run-vm` puts `net.ipv4.ip_forward` back.** The TAP path set it
  unconditionally with no matching restore, so running one VM once left the
  host forwarding packets forever. It is now set only if it was off, and
  restored on teardown only when no other `qemu-br*` bridge is left — a second
  `run-vm` that found it already on will not have recorded it, and switching it
  off under them would break a running guest's networking.

- **`tests/test_lmcache_unit_render.py` rendered a unit nobody runs.** It built
  its own Jinja environment without `trim_blocks`, which Ansible's `template`
  module defaults to on. Without it a `{% for %}` over an empty list leaves a
  blank line, and a blank line inside `ExecStart`'s backslash continuation
  terminates it — systemd reports `Missing '=', ignoring line` and the
  container starts with some of its arguments. The real render was fine; the
  test's was broken, which is the forgiving direction and so went unnoticed.
  There is now a test that the continuation is unbroken.

### Added

- **`qemu-tool down`**, which gracefully shuts down guests via the QEMU guest
  agent and, optionally, tears down the containing stack or all owned stacks.
  `sudo qemu-tool down --all` takes every stack on the system regardless of
  owner.

  Both of its questions are answered from what compose records on each
  container rather than guessed. A stack is identified by its compose file —
  the `gen-compose` provenance header, or one of the directories this tool
  ships — and never by service name, because `qemu-1` is a name anyone might
  use and this removes containers. It is *yours* if any of its containers runs
  as your uid, which is what `CONTAINER_USER` put there.

  A stack started before that change has every container as root and so reads
  as root's to a non-root caller; those are listed as skipped, with the reason,
  rather than passed over in silence. The teardown pins the project with an
  explicit `-p`, which the stack-directory discovery order does not guarantee.

- **`run-vm` refuses, by name, when a disk or its directory is not writable
  by the user it runs as.** This is where the root-era migration lands, and
  both ways it landed were unhelpful. QEMU opens a drive read-write even for
  a guest that only reads it, so a 0644 root-owned qcow2 did not degrade --
  the guest did not boot, and QEMU's own `Could not open '<path>': Permission
  denied` named neither the owner nor why it changed. An unwritable images
  directory failed later and elsewhere, as a `CalledProcessError` traceback
  from `qemu-img`.

  The message names the file, its owner (uid *and* name, since one of them
  is always the one you can search for), and a fix that works: `rm` for an
  NVMe drive, which is scratch and recreated; `chown` for a boot disk;
  `chmod` when the file is already yours and only the mode denies write.
  That last distinction matters -- the obvious wording produced "owned by
  uid 1000, not read-write for uid 1000", which reads like a contradiction
  and sends you to the wrong command.

- **`qemu-tool inspect` reports each GPU's PCIe BARs**, indented under its row:

  ```
  GPU:       gfx1250 (emulated, rocjitsu-1.sock)
               BAR0 256 MiB (64-bit prefetchable)
  GPU:       gfx908 (passthrough, 63:00.0)
               BAR0 32 GiB (64-bit prefetchable)
  ```

  BAR0 is the VRAM aperture, which is the figure that decides whether a device
  can be the target of a peer-to-peer DMA. Two sources, because there is no
  single one: a passthrough GPU's BARs are read from the host's own sysfs and
  need no agent, while an emulated one's come from the guest — reached through
  the KFD node's `location_id`, since the agent has no readdir and rocjitsu's
  config lives inside a container `inspect` cannot reach.

  The three silent ways to misparse a sysfs `resource` file are each pinned by
  a test against a verbatim MI100 fixture: inclusive bounds (size is
  `end - start + 1`), 64-bit BARs occupying two slots with the upper reading as
  zeros, and only the first six lines being BARs at all.

- **`qemu-tool check`**, one command that asks every host question the
  stacks depend on and prints a line per answer: tools on PATH, the Docker
  daemon and your access to it, KVM (the ioctl, not just the node),
  `/dev/net/tun`, the uid the containers will run as, which settings file is
  in force, the vCPU budget against `nproc`, and whether `VM_IMAGES_DIR` has
  room and is owned by you. Read-only, no `sudo`, writes nothing — safe on a
  host you have not decided to use yet.

  Every failure it reports has been seen in the wild, and most present
  somewhere other than where they are caused: a missing kvm group looks like
  slow guests, an absent `/dev/net/tun` looks like an RDMA failure, a
  `CONTAINER_USER` typo looks like a stack that will not start for reasons the
  error does not name. `FAIL` exits 1; `warn` does not, because a checker that
  failed on a merely tight host would get disabled in whatever runs it.

  It is the same code `quick-start` runs in its preflight, not a copy —
  `quick_start` imports the checks from it, and the test asserts identity
  rather than equality so a second copy that agrees today still fails.


- **`qemu-tool quick-start`**, one command that brings up two guests sized for
  whatever machine it was run on. Each gets an emulated gfx1250, an emulated
  NVMe and an emulated rocm-ernic RDMA NIC; Prometheus and Grafana come up
  beside them; then a GPU test runs on each guest and an RDMA test across
  them, and the NIC's own byte counter is checked to have moved.

  The sizing is the point. `VM_VCPUS`, `VM_VMEM` and `VM_SHM_SIZE` are derived
  from `nproc` and `MemTotal` and written to an overlay beside the stack, so a
  run on a small host does not look like a settings change in the diff. The
  floors are not preferences — below 4 vCPU `ionic_rdma` never probes, and the
  only symptom is a line in a guest log nobody is reading. There is a ceiling
  too, at 8: vCPU the guest cannot use is vCPU that makes the mesh manager
  slip its untunable 20 s heartbeat and evict live nodes.

  It asks rather than assuming, and tells you what it is about to do before it
  does it — the plan, the images directory and the disk it needs. An
  unwritable `/var/lib/qemu-tool/images` is a question up front instead of a
  failure three minutes into a 17 GB pull; a busy 9090 or 3000 is moved rather
  than discovered at `up`; a missing `docker`, `oras` or `ansible-playbook` is
  named with the command that installs it. Every prompt takes its default
  answer when stdin is not a terminal, so nothing hangs in a script, and the
  answer it took is printed either way.

  Prometheus and Grafana write to directories beside the generated stack
  rather than to their defaults under `/var/lib/qemu-tool`. The defaults need
  root to create and then a chown to uid 65534 and 472 respectively; this
  needs neither, and puts the TSDB where somebody would think to look for it.

  When it finishes it prints how to get in — `qemu-tool ssh quickstart-1`,
  which resolves the port, the account and the key, and the plain `ssh` line
  spelled out beside it — and where to watch it.

- **A PR-time boot check for the quickstart**, `quickstart-boot`. The
  daily report lane runs on push to main and on a cron, so a broken quickstart
  was found the morning after the merge; `Gen Compose Check` renders the
  settings file on every PR but starts nothing. This boots one guest from the
  `-slim` image — 0.75 GB against the fat image's 2.52 GB at the same stamp —
  and asserts the emulated NIC is present by its full `1dd8:1002` +
  `1dd8:5400` tuple and that `ibv_devinfo` reports `PORT_ACTIVE`. One guest,
  not two, because the 4 vCPU floor is real and two of them on a 4-core runner
  is 2x oversubscribed against a 20 s heartbeat that is not tunable; a daily
  report can absorb that flake and a merge gate cannot. The slim image is
  pinned through an overlay, leaving `qemu/xml/quickstart` alone: slim has no
  compiler, so the GPU test cannot build there and stays in the daily lane.

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

- **`qemu/xml/overlay-dense`**, a 32-VM fleet with two GPUs, two ernics and
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
  the reason report-for-quickstart runs `qemu-tool quick-start`: a documented
  path that nothing executes rots.

  **One VM where the lab stack is four**, via the committed
  `qemu/xml/overlay-mi355x-ci` — a runner has 4 cores against the stack's 32.
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
  of `up` could write it into the tracked `qemu/xml/scale-out` and break
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

  Nothing is built. The guest is the published `rocm-golden` qcow2, which
  already carries the mainline kernel, the ionic-ernic DKMS modules, the udev
  naming rules, rdma-core, perftest and ROCm 10.1, and the device servers are
  the tagged CI images. What the image does *not* carry is the per-rocjitsu
  `ip_discovery.bin`, an address on the ernic and a loaded `amdgpu` — so
  `ansible/playbooks/fleet-quickstart.yml` stages those three and nothing else,
  reusing `lmcache_guest`'s `amdgpu_firmware` and `netplan_ernic` task files
  and `rocjitsu_gpu_test`'s `vector_add.hip` rather than copying them.

  The stack comes from `qemu/xml/quickstart`, a two-VM cut of the scale-out
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
      --env-file qemu/xml/scale-out --env-file qemu/xml/overlay-mi100
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

- **The quickstart's disk estimate was wrong by about 7x, and is now
  measured.** It claimed a "~17 GB pull" needing ~60 GB free — a figure that
  predates anyone checking. The registry reports 2.52 GB compressed for the
  pinned tag and the image's own annotation says 9.17 GB on disk, so the
  warning threshold is 30 GB and the prose says what the numbers are. The old
  figure made the check unsatisfiable on a laptop, which is the same as not
  having one.

- **`qemu-tool ssh` now works against a quickstart guest.** `inspect` derives
  the private key path from the public one by stripping `.pub` and offers an
  identity only if that file exists, and quick-start published the public half
  alone — so the command the README tells you to run found the key, matched it
  against the guest, and connected with no `-i`, landing on a password prompt.
  Both halves are published now. The key is not a secret: it ships in a public
  OCI referrer beside a public image.

- **The Quick Start is a subcommand, and `scripts/quickstart-2vm.sh` is
  gone.** It was 558 lines of `/bin/sh` doing what the tool could do with the
  generator and the config loader it already has, and the README, the daily
  lane and the badge now all name `qemu-tool quick-start`. The arrangement
  that script existed for is unchanged and is the reason it was replaced
  rather than kept beside the subcommand: the path a human is told to run and
  the path CI runs have to be one thing, and two implementations of it is the
  drift that arrangement prevents. `tests/test_quick_start.py` fails if
  anything current points at the old script.

  Everything the script had learned the hard way moved with it — the guest
  account coming from the image rather than the settings file, the keypair
  referrer selected by `artifactType` rather than by position, the public half
  published where `inspect` looks for it, `--in-network`, and the stack
  directory falling back off an unwritable `/var/lib/qemu-tool`. What changed
  is that sizing from the host is the default rather than a CI-only flag, the
  monitoring sidecars come up with it, and it asks.

- **`qemu/xml/quickstart` moved to the `20261022.gc533d98` build**, with
  `ernic.151379b` and `rocjitsu.98f6a72`, and switched Grafana on. It now runs
  deliberately ahead of `qemu/xml/scale-out`: the build-stamp coherence check
  is per settings file, not across them, and the quickstart is the shop window
  while the fleet file is what a long soak is pinned to. The README carries a
  separate badge for it for that reason — do not fold the two together.

- **Image pins moved to the `20261021.g742e8e2` build** in
  `qemu/xml/scale-out` and `qemu/xml/overlay-mi100`. Component SHAs are
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

  `qemu/xml/overlay-mi100` no longer forces it off. That override was
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

- **The settings files moved into `qemu/xml/`, dropping the `env.` prefix.**
  Five files that were `qemu/env.<name>` are now `qemu/xml/<name>`:

  | Was | Is |
  |-----|----|
  | `qemu/env.example` | `qemu/xml/example` |
  | `qemu/env.scale-out` | `qemu/xml/scale-out` |
  | `qemu/env.quickstart` | `qemu/xml/quickstart` |
  | `qemu/env.overlay-ci` | `qemu/xml/overlay-ci` |
  | `qemu/env.overlay-mi100` | `qemu/xml/overlay-mi100` |

  **Every documented command that names a settings file has to change.** The
  flag and the file contents are untouched; only the path moves:

  ```bash
  qemu-tool gen-compose \
      --env-file qemu/xml/scale-out --env-file qemu/xml/overlay-mi100
  ```

  A gitignored local file follows the same rule — `qemu/env.local-*` is now
  `qemu/xml/local-*`. `qemu/.env` and the rest of the search path
  (`$QEMU_TOOL_ENV`, `./.env`, `/etc/qemu-tool/env`) are unaffected, so a
  machine that never passes `--env-file` needs no change.

  The .deb follows the layout: the packaged template is
  `/usr/share/qemu-tool/env/example`, not `/usr/share/qemu-tool/env.example`.
  `postinst` still seeds `/etc/qemu-tool/env` from it on first install, so an
  upgrade keeps its existing settings.

  `qemu/xml/` is a third sibling of `qemu/compose/` and `qemu/packages.d/`,
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

  `qemu/xml/scale-out` is now authoritative. The fleets that differ from it
  state only their differing keys in `qemu/xml/overlay-ci` and
  `qemu/xml/overlay-mi100`, layered over it with a repeated `--env-file`.
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
  `vm-rocm-golden.yml` is untouched.

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
  rocjitsu and rocm-golden alike — baked them in by accident. Nothing in
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
  `rocm-golden` qcow2. It had been booting resolute GA, 7.0.0-34, while
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

- `ansible-playbook-test-rocjitsu` and `ansible-playbook-test-rocm-golden`
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
  times on `vm-rocm-golden.yml` — five of them mid-chain, with real work
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
  | `vm-rocm-golden` | 11.2 GiB | 9.15 GiB | **3.90 GiB** | −65% |

  The ROCm guests compress at 2.45x against ernic's 1.67x, so the lanes that
  were worst off benefit most. Four images that totalled 38.9 GiB now total
  13.8 GiB.

- **The `vm-rocm` and `vm-rocm-golden` lanes now trigger on
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
  `-rocjitsu`, `-ernic`, `-rocm-golden`) sharing a `setup-vm-job` action,
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
