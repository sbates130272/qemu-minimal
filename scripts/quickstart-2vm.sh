#!/bin/sh
# Two VMs, each with an emulated gfx1250, an emulated NVMe and an emulated
# RDMA NIC, from a published guest image -- then one GPU test and one RDMA
# test across them.
#
#   scripts/quickstart-2vm.sh            # fetch, boot, configure, test, report
#   scripts/quickstart-2vm.sh up         # stop after the stack is up
#   scripts/quickstart-2vm.sh test       # configure and test an already-up stack
#   scripts/quickstart-2vm.sh down       # stop the stack, keep the overlays
#
# This script IS the Quick Start in README.md and it IS what the daily
# report-for-quickstart lane runs. That is the point: a documented path that
# nothing executes rots, and a lane that executes something else proves
# nothing about the documentation.
#
# /bin/sh, not bash: the CI lanes here run steps under dash, and `set -o
# pipefail` is not available there -- see the note in
# .github/workflows/qemu-minimal-report-for-lmcache.yml. Nothing below needs
# it.
set -eu

REPO_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

ENV_FILE="${REPO_ROOT}/qemu/env.quickstart"
STACK_DIR=/var/lib/qemu-tool/stacks/quickstart
STACK_DIR_EXPLICIT=0
KEY_DIR=/var/lib/qemu-tool/quickstart-key
REPORT_DIR=""
SIZE_FROM_HOST=0
IN_NETWORK=0
ARTIFACTS=/var/tmp/quickstart-artifacts
VERB=all

usage() {
  cat <<'EOF'
Usage: quickstart-2vm.sh [options] [all|preflight|up|test|report|down]

  --env-file FILE     settings file (default qemu/env.quickstart)
  --stack-dir DIR     where the stack is generated (default
                      /var/lib/qemu-tool/stacks/quickstart)
  --key-dir DIR       where the guest keypair is pulled to
  --report-dir DIR    write a markdown report here (implies the report step)
  --size-from-host    derive VM_VCPUS/VM_VMEM/VM_SHM_SIZE from this machine
  --in-network        reach the guests on the compose network rather than on
                      published host ports (for a caller that is itself a
                      container attached to that network -- see the CI lane)
  -h, --help          this
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE=$2; shift 2 ;;
    --stack-dir) STACK_DIR=$2; STACK_DIR_EXPLICIT=1; shift 2 ;;
    --key-dir) KEY_DIR=$2; shift 2 ;;
    --report-dir) REPORT_DIR=$2; shift 2 ;;
    --size-from-host) SIZE_FROM_HOST=1; shift ;;
    --in-network) IN_NETWORK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    all|preflight|up|test|report|down) VERB=$1; shift ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Every path is made absolute here, once. Two steps below run from somewhere
# else -- compose from the stack directory and ansible-playbook from
# ansible/ -- so a relative --env-file would resolve against the wrong
# directory and report a missing file that is plainly there.
abspath() { case "$1" in /*) echo "$1" ;; *) echo "$(pwd)/$1" ;; esac; }
ENV_FILE=$(abspath "$ENV_FILE")
STACK_DIR=$(abspath "$STACK_DIR")
KEY_DIR=$(abspath "$KEY_DIR")
if [ -n "$REPORT_DIR" ]; then REPORT_DIR=$(abspath "$REPORT_DIR"); fi

# The settings this script needs by name. Everything else is the generator's
# business, so they are read rather than restated.
env_get() {
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -1
}

[ -f "$ENV_FILE" ] || die "no settings file at $ENV_FILE"

VM_COUNT=$(env_get VM_COUNT)
VM_NAME_PREFIX=$(env_get VM_NAME_PREFIX)
VM_IMAGES_DIR=$(env_get VM_IMAGES_DIR)
VM_BACKING_IMAGE=$(env_get VM_BACKING_IMAGE)
VM_USERNAME=$(env_get VM_USERNAME)
VM_SSH_PORT_BASE=$(env_get VM_SSH_PORT_BASE)
FLEET_NET_PREFIX=$(env_get FLEET_NET_PREFIX)
STACK_NAME=$(basename "$STACK_DIR")

# Where guest N answers SSH.
#
# Each qemu container publishes its guest on a HOST port, which is right for
# a human on that host and unreachable for a caller that is itself a
# container -- the runner puts the job container on its own bridge. With
# --in-network the guests are addressed on the fleet network instead, by the
# fixed address the generator gave their container (third octet 3 is the qemu
# family) rather than by service name: this stack resolves no service names
# at run time, on purpose.
guest_host() {
  if [ "$IN_NETWORK" = 1 ]; then echo "${FLEET_NET_PREFIX}.3.${1}"; else echo localhost; fi
}

guest_port() {
  if [ "$IN_NETWORK" = 1 ]; then echo 2222; else echo $((VM_SSH_PORT_BASE + $1 - 1)); fi
}

VM1="${VM_NAME_PREFIX}-1"
VM2="${VM_NAME_PREFIX}-2"
KEY="${KEY_DIR}/id_rsa"

# The settings file the stack is actually brought up with. --size-from-host
# appends to a copy rather than editing the committed file, so a CI run on a
# small runner does not look like a settings change in the diff.
RUN_ENV="$ENV_FILE"

# /var/lib/qemu-tool/stacks is created by whichever run got there first, and
# a root one leaves it root-owned 755 -- so the default landed on "mkdir:
# Permission denied" three minutes into a 20 GB pull, naming neither the path
# nor the fix. Resolved up front instead, and for EVERY verb rather than
# inside preflight: `down` does not run preflight, so resolving there sent it
# looking for the stack in a directory this user could never have written,
# leaving the containers up while reporting that it had stopped them.
#
# Only when the caller did not choose the directory: an explicit --stack-dir
# that cannot be created is an error, not something to silently relocate.
resolve_stack_dir() {
  [ -d "$STACK_DIR" ] && return 0
  fallback="${XDG_STATE_HOME:-${HOME}/.local/state}/qemu-tool/stacks/${STACK_NAME}"
  # An earlier run's fallback wins over an empty default: otherwise a later
  # verb would create the default, find no compose file in it, and report
  # that against a stack that is sitting in the fallback perfectly intact.
  if [ "$STACK_DIR_EXPLICIT" != 1 ] && [ -f "${fallback}/docker-compose.yml" ]; then
    STACK_DIR="$fallback"
    return 0
  fi
  if mkdir -p "$STACK_DIR" 2>/dev/null; then return 0; fi
  if [ "$STACK_DIR_EXPLICIT" = 1 ]; then die "cannot create $STACK_DIR"; fi
  mkdir -p "$fallback" || die "cannot create $STACK_DIR or $fallback"
  echo "note: ${STACK_DIR} is not writable, using ${fallback}"
  STACK_DIR="$fallback"
}

# ---------------------------------------------------------------------------

preflight() {
  say "Preflight"

  for tool in docker qemu-tool oras ansible-playbook; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is not on PATH"
  done

  # The ioctl, not the node: a device node can exist and still be
  # non-functional, and QEMU falls back to TCG without saying so. Two guests
  # under TCG will not finish inside any timeout worth setting.
  [ -c /dev/kvm ] || die "/dev/kvm is missing; the guests would run under TCG"
  python3 -I -c "import fcntl, os; fcntl.ioctl(os.open('/dev/kvm', os.O_RDWR), 0xAE00, 0)" \
    >/dev/null 2>&1 || die "/dev/kvm is present but KVM_GET_API_VERSION failed"

  if [ "$SIZE_FROM_HOST" = 1 ]; then
    size_from_host
  fi

  vcpus=$(env_get VM_VCPUS)
  vmem=$(env_get VM_VMEM)
  shm=$(env_get VM_SHM_SIZE)

  # Not fatal, because a host that is merely tight still finishes. Warned
  # about because the symptom is not "slow": the mesh manager's heartbeat
  # timeout is 20 s of wall clock and is not tunable, so an oversubscribed
  # host makes it mistake live nodes for dead ones and the guests attached to
  # them die with "vfio-user: timed out waiting for reply" (AGENTS.md issue
  # 14).
  cores=$(nproc)
  asked=$((vcpus * VM_COUNT))
  if [ "$asked" -gt $(((cores * 5) / 4)) ]; then
    echo "warning: ${VM_COUNT} x ${vcpus} vCPU on ${cores} cores is oversubscribed;" >&2
    echo "         expect heartbeat evictions (AGENTS.md issue 14)." >&2
  fi

  # Skipped under --in-network, where this process is itself a container: the
  # /dev/shm it can see is its own 64 MiB one, not the host's, and each guest
  # container gets a tmpfs of its own from shm_size regardless. Warning on
  # that would be a false alarm on every CI run.
  if [ "$IN_NETWORK" != 1 ]; then
    shm_free_mb=$(df -m --output=size /dev/shm 2>/dev/null | tail -1 | tr -d ' ')
    case "$shm" in
      *[gG]) want_mb=$(( ${shm%[gG]} * 1024 * VM_COUNT )) ;;
      *[mM]) want_mb=$(( ${shm%[mM]} * VM_COUNT )) ;;
      *) want_mb=0 ;;
    esac
    if [ -n "$shm_free_mb" ] && [ "$want_mb" -gt "$shm_free_mb" ]; then
      echo "warning: ${VM_COUNT} x ${shm} does not fit /dev/shm (${shm_free_mb} MiB)." >&2
    fi
  fi

  echo "settings: $ENV_FILE"
  echo "guests:   ${VM_COUNT} x ${vcpus} vCPU, ${vmem} MiB, ${shm} shm"
  echo "stack:    $STACK_DIR"
}

# A GitHub runner is smaller than any host a human runs this on, so the
# settings file's defaults would ask for more than it has. Appended to a copy,
# with 4 vCPU as a floor rather than a preference: below it ionic_rdma never
# probes.
size_from_host() {
  RUN_ENV="${STACK_DIR}.env"
  mkdir -p "$(dirname "$RUN_ENV")"
  cp "$ENV_FILE" "$RUN_ENV"

  host_cpus=$(nproc)
  host_mem_mb=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)

  vcpus=$(( (host_cpus - 1) / VM_COUNT ))
  [ "$vcpus" -lt 4 ] && vcpus=4

  vmem=$(( host_mem_mb * 3 / 4 / VM_COUNT ))
  [ "$vmem" -gt 8192 ] && vmem=8192
  [ "$vmem" -lt 4096 ] && vmem=4096

  {
    echo "VM_VCPUS=${vcpus}"
    echo "VM_VMEM=${vmem}"
    # VM_VMEM plus 1 GiB, not equal to it. env.scale-out's rule is
    # "VM_SHM_SIZE >= VM_VMEM, per VM", so equal is the boundary and leaves
    # the vfio-user memfd backend no slack -- which is what the lmcache lane
    # set and had to correct. shm_size caps a tmpfs rather than reserving
    # it, so the headroom costs nothing until it is touched.
    echo "VM_SHM_SIZE=$((vmem + 1024))m"
  } >> "$RUN_ENV"

  ENV_FILE="$RUN_ENV"
  echo "sized from this host (${host_cpus} vCPU, ${host_mem_mb} MiB)"
}

# The guest's keypair ships as an ORAS REFERRER next to the qcow2, not inside
# it, and gen-vm pulls only the disk -- so without this the generated
# inventory authenticates as a user that exists with a key that does not.
#
# Selected by artifactType rather than by position: oras 1.3.4 calls the list
# "referrers", and [0] is a guess waiting to break the day a signature or an
# SBOM referrer is attached ahead of this one.
fetch_key() {
  say "Fetching the guest keypair"
  if [ -f "$KEY" ]; then
    echo "already at $KEY"
    return 0
  fi
  mkdir -p "$KEY_DIR"
  oras discover "$VM_BACKING_IMAGE" --format json > "${KEY_DIR}/referrers.json"
  ref=$(VM_INFO_TYPE=application/vnd.batesste.vm-info.v1 python3 -I -c 'import json,os,sys; want=os.environ["VM_INFO_TYPE"]; refs=json.load(sys.stdin).get("referrers",[]); hits=[r["reference"] for r in refs if r.get("artifactType")==want]; print(hits[0]) if hits else sys.exit("no %s referrer on the qcow2; found %r" % (want,[r.get("artifactType") for r in refs]))' \
        < "${KEY_DIR}/referrers.json")
  oras pull "$ref" -o "$KEY_DIR"
  # Named explicitly: a silent miss here surfaces much later as a permission
  # denial against a guest that is perfectly healthy.
  [ -f "$KEY" ] || die "the referrer carried no id_rsa"
  chmod 600 "$KEY"
}

# ~17 GB pulled and decompressed once; VM 2 is an overlay of the same backing
# file, which is what VM_BACKING_SHARED is for. This is the long step.
make_overlays() {
  say "Creating the guest overlays"
  if [ -f "${VM_IMAGES_DIR}/${VM1}.qcow2" ]; then
    echo "${VM1} already exists, leaving it alone"
  else
    qemu-tool gen-vm --env-file "$ENV_FILE" \
      --vm-name "$VM1" --backing-image "$VM_BACKING_IMAGE"
  fi
  if [ -f "${VM_IMAGES_DIR}/${VM2}.qcow2" ]; then
    echo "${VM2} already exists, leaving it alone"
  else
    qemu-tool gen-vm --env-file "$ENV_FILE" \
      --vm-name "$VM2" --backing-file "${VM_IMAGES_DIR}/${VM1}-backing.qcow2"
  fi
}

# To a scratch directory, never over the committed stack: that one is
# generated from qemu/env.scale-out and `gen-compose --check` compares the
# two. gen-compose also copies the stack's non-generated assets (tapsetup/),
# without which compose fails with "unable to prepare context".
generate_stack() {
  say "Generating the stack"
  mkdir -p "$STACK_DIR"
  qemu-tool gen-compose --env-file "$ENV_FILE" --output-dir "$STACK_DIR" >/dev/null
  echo "wrote $STACK_DIR/docker-compose.yml"
  # Copy the repo's qemu package into the stack directory so the QEMU
  # containers can install from source even when $REPO_ROOT is a
  # container-internal path that the host docker daemon cannot resolve as a
  # bind-mount source.  QEMU_TOOL_SRC is then set to $STACK_DIR (host-
  # accessible), and the generated compose bind-mounts it as /qemu-tool-src.
  rm -rf "$STACK_DIR/qemu"
  cp -r "$REPO_ROOT/qemu" "$STACK_DIR/qemu"
}

compose() {
  ( cd "$STACK_DIR" && docker compose --project-name "$STACK_NAME" \
      --env-file "$ENV_FILE" "$@" )
}

up() {
  say "Bringing the stack up"
  # Use the stack directory as the source root: $REPO_ROOT is a
  # container-internal path that the host docker daemon cannot resolve as a
  # bind-mount source when this script runs inside a CI job container.
  # generate_stack() already copied $REPO_ROOT/qemu there.
  QEMU_TOOL_SRC="$STACK_DIR" compose up --detach

  say "Waiting for the NIC servers"
  n=0
  while [ "$n" -lt 60 ]; do
    ok=1
    i=1
    while [ "$i" -le "$VM_COUNT" ]; do
      state=$(docker inspect -f '{{.State.Health.Status}}' \
                "${STACK_NAME}-ernic-${i}-1" 2>/dev/null || echo missing)
      [ "$state" = healthy ] || ok=0
      i=$((i + 1))
    done
    [ "$ok" = 1 ] && break
    n=$((n + 1))
    sleep 2
  done
  [ "${ok:-0}" = 1 ] || { compose logs ernic-1 ernic-2; die "the NIC servers never became healthy"; }
  echo "NIC servers healthy"
}

# Separate from up() rather than the end of it, because a caller using
# --in-network can only reach the guests once it has joined the network that
# up() just created. The CI lane does exactly that between the two.
wait_for_guests() {
  say "Waiting for the guests"
  i=1
  while [ "$i" -le "$VM_COUNT" ]; do
    host=$(guest_host "$i")
    port=$(guest_port "$i")
    n=0
    until ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
              -o ConnectTimeout=5 -i "$KEY" -p "$port" \
              "${VM_USERNAME}@${host}" true 2>/dev/null; do
      n=$((n + 1))
      [ "$n" -gt 120 ] && die "guest ${i} did not answer SSH at ${host}:${port}"
      sleep 5
    done
    echo "guest ${i} up at ${host}:${port}"
    i=$((i + 1))
  done
}

# A second inventory source overriding where each guest answers. Ansible
# merges inventories and the later source wins, so the generated one stays
# authoritative for everything else -- the MACs, the ernic addresses and the
# rocjitsu image the firmware is generated from.
write_network_inventory() {
  OVERRIDE="${STACK_DIR}/inventory-in-network.yml"
  {
    echo "---"
    echo "# Written by scripts/quickstart-2vm.sh --in-network. Do not edit."
    echo "all:"
    echo "  hosts:"
    i=1
    while [ "$i" -le "$VM_COUNT" ]; do
      echo "    ${VM_NAME_PREFIX}-${i}:"
      echo "      ansible_host: $(guest_host "$i")"
      echo "      ansible_port: $(guest_port "$i")"
      i=$((i + 1))
    done
  } > "$OVERRIDE"
  echo "$OVERRIDE"
}

configure_and_test() {
  wait_for_guests
  say "Configuring the guests and running the tests"
  rm -rf "$ARTIFACTS"
  before=$(ernic_bytes 1)
  extra_inventory=""
  [ "$IN_NETWORK" = 1 ] && extra_inventory="-i $(write_network_inventory)"
  # Unquoted on purpose: empty must expand to no argument at all.
  # shellcheck disable=SC2086
  ( cd "${REPO_ROOT}/ansible" && ansible-playbook \
      -i "${STACK_DIR}/fleet-inventory.yml" $extra_inventory \
      playbooks/fleet-quickstart.yml \
      --private-key "$KEY" \
      -e "quickstart_artifacts_dir=${ARTIFACTS}" )
  after=$(ernic_bytes 1)

  RDMA_DELTA=$((after - before))
  say "ernic 1 moved ${RDMA_DELTA} bytes"
  # A passing ib_send_bw proves the verbs worked; only the NIC's own counter
  # proves the bytes crossed the mesh. rocm-ernic writes this dump
  # unconditionally under -S, so it needs no metrics profile.
  [ "$RDMA_DELTA" -gt 0 ] || die "the ernic byte counter did not move"
}

# The NIC's own counter, from the dump rocm-ernic writes under -S. Read from
# the ernic container itself, which is in no compose profile and is always
# up. A miss is an error rather than a 0: an unreadable counter and a
# transfer that moved nothing produce the same delta, and reporting the
# second when it was the first is a test that cannot fail.
ernic_bytes() {
  bytes=$(docker exec "${STACK_NAME}-ernic-${1}-1" \
            sh -c "awk '/total_bytes_sent/ {print \$NF}' /run/ernic-stats/ernic-${1}.stats" \
            2>/dev/null | head -1 | tr -dc '0-9')
  [ -n "$bytes" ] || die "could not read the byte counter from ernic ${1}"
  echo "$bytes"
}

report() {
  [ -n "$REPORT_DIR" ] || return 0
  say "Writing the report to ${REPORT_DIR}"
  mkdir -p "$REPORT_DIR"
  # The site treats a report with no `Status: **pass**` as unknown rather than
  # as a pass, which is what a `report` run on its own -- with no test behind
  # it -- should produce.
  if [ "${RDMA_DELTA:-0}" -gt 0 ]; then status=pass; else status=unknown; fi
  {
    echo "# Two-VM quickstart report"
    echo
    echo "Two guests from the published \`ernic-rocjitsu\` image, each with an"
    echo "emulated gfx1250, an emulated NVMe and an emulated RDMA NIC, booted"
    echo "by \`scripts/quickstart-2vm.sh\`."
    echo
    echo "- Guest image: \`${VM_BACKING_IMAGE##*:}\`"
    echo "- RDMA bytes across the mesh: ${RDMA_DELTA:-unknown}"
    # The stamp scripts/render-site-perf.py reads.
    echo "- Generated: **$(date -u '+%Y-%m-%d %H:%M UTC')** &middot; Commit: \`$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)\` &middot; Status: **${status}**"
    echo
    for f in "$ARTIFACTS"/*.md; do
      [ -f "$f" ] || continue
      cat "$f"
      echo
    done
    echo "## ernic counters"
    echo '```'
    i=1
    while [ "$i" -le "$VM_COUNT" ]; do
      docker exec "${STACK_NAME}-ernic-${i}-1" \
        sh -c "grep -E 'total_bytes' /run/ernic-stats/ernic-${i}.stats" 2>&1 \
        | sed "s/^/ernic-${i}: /" || true
      i=$((i + 1))
    done
    echo '```'
  } > "${REPORT_DIR}/index.md"
}

down() {
  say "Taking the stack down"
  compose down
  echo "the overlays under ${VM_IMAGES_DIR} are left in place; delete them to start over"
}

# ---------------------------------------------------------------------------

resolve_stack_dir

case "$VERB" in
  preflight)
    preflight ;;
  up)
    preflight; fetch_key; make_overlays; generate_stack; up ;;
  test)
    preflight; configure_and_test; report ;;
  report)
    report ;;
  down)
    down ;;
  all)
    preflight; fetch_key; make_overlays; generate_stack; up
    configure_and_test; report
    say "Done. ssh -i ${KEY} -p ${VM_SSH_PORT_BASE} ${VM_USERNAME}@localhost"
    ;;
esac
