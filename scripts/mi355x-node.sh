#!/usr/bin/env bash
# Bring up an MI355X-shaped node, make it test-ready, and write a report on
# the shape it actually enumerates.
#
#   scripts/mi355x-node.sh up
#   scripts/mi355x-node.sh test --report-dir site/mi355x
#   scripts/mi355x-node.sh down
#
# WHAT THIS IS FOR. vfio-user-mi355x-node exists to give a guest a PCIe tree
# that resembles a real node -- 8 GPUs, 8 NICs and 8 switches, one NIC and
# one GPU below each switch -- because every other stack here hangs one of
# each flat off the root complex. The failure that matters is not a crash: a
# flat tree boots exactly as happily as a switched one, and nothing in the
# guest complains. So the report's job is to show the TREE, from inside the
# guest, not merely that the guest came up.
#
# It is also what .github/workflows/qemu-minimal-report-for-mi355x.yml runs,
# for the reason report-for-quickstart runs `qemu-tool quick-start`: a documented
# path that nothing executes rots, and a lane that executes something else
# proves nothing about the documentation.
#
# Deliberately NOT a copy of `qemu-tool quick-start`. That one owns fetching a
# keypair, adopting the image's account and sizing from the host, and all
# three are reused here rather than reimplemented -- this file is the
# MI355X-specific parts: the overlay to layer, and the report.
set -euo pipefail

REPO_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="${REPO_ROOT}/qemu/env/mi355x-node"
OVERLAY="${REPO_ROOT}/qemu/env/overlay-mi355x-ci"
STACK_DIR=/var/lib/qemu-tool/stacks/mi355x
KEY_DIR=/var/lib/qemu-tool/mi355x-key
REPORT_DIR=""
STACK=vfio-user-mi355x-node
IN_NETWORK=0

usage() {
  cat <<'EOF'
Usage: mi355x-node.sh [options] <up|test|down>

  --env-file FILE     golden settings file (default qemu/env/mi355x-node)
  --overlay FILE      delta layered on it (default qemu/env/overlay-mi355x-ci)
  --stack-dir DIR     where the stack is rendered
  --key-dir DIR       where the guest keypair is pulled to
  --report-dir DIR    write a markdown report here (test only)
  --in-network        reach the guest on the compose network rather than on
                      a published host port, for a caller that is itself a
                      container
EOF
}

while [ $# -gt 0 ]; do
  case $1 in
    --env-file) ENV_FILE=$2; shift 2 ;;
    --overlay) OVERLAY=$2; shift 2 ;;
    --stack-dir) STACK_DIR=$2; shift 2 ;;
    --key-dir) KEY_DIR=$2; shift 2 ;;
    --report-dir) REPORT_DIR=$2; shift 2 ;;
    --in-network) IN_NETWORK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    up|test|down) VERB=$1; shift ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[ -n "${VERB:-}" ] || { usage; exit 2; }

say() { printf '\n== %s ==\n' "$1"; }
die() { echo "error: $1" >&2; exit 1; }

env_get() {
  # Last assignment wins, overlay over golden, matching envfile layering.
  val=$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -1)
  [ -f "$OVERLAY" ] && o=$(sed -n "s/^$1=//p" "$OVERLAY" | tail -1) || o=""
  [ -n "$o" ] && val=$o
  printf '%s' "$val"
}

VM_COUNT=$(env_get VM_COUNT)
VM_IMAGES_DIR=$(env_get VM_IMAGES_DIR)
VM_BACKING_IMAGE=$(env_get VM_BACKING_IMAGE)
VM_NAME_PREFIX=$(env_get VM_NAME_PREFIX)
VM_USERNAME=$(env_get VM_USERNAME)
ERNIC_PER_VM=$(env_get ERNIC_PER_VM)
VM_SSH_PORT_BASE=$(env_get VM_SSH_PORT_BASE)
FLEET_NET_PREFIX=$(env_get FLEET_NET_PREFIX)
KEY="${KEY_DIR}/id_rsa"

# Published host ports by default. Under --in-network the guest is addressed
# on the fleet network instead, by the fixed address gen-compose assigned to
# its qemu service, on the in-container port -- because a caller that is
# itself a container sits on the runner's own bridge and cannot see a port
# published on the host.
guest_host() {
  if [ "$IN_NETWORK" = 1 ]; then echo "${FLEET_NET_PREFIX}.3.${1}"; else echo localhost; fi
}
guest_port() {
  if [ "$IN_NETWORK" = 1 ]; then echo 2222; else echo $((VM_SSH_PORT_BASE + $1 - 1)); fi
}

qt() { qemu-tool "$@"; }
compose() {
  docker compose --project-directory "$STACK_DIR" \
    --env-file "$ENV_FILE" --env-file "$OVERLAY" "$@"
}

# ---------------------------------------------------------------------------

# The guest keypair ships as an ORAS referrer next to the qcow2, not inside
# it, and gen-vm pulls only the disk. Selected by artifactType rather than by
# position, because [0] breaks the day a signature referrer is attached
# ahead of this one. Same logic as quick-start's fetch_key().
fetch_key() {
  say "Fetching the guest keypair"
  if [ -f "$KEY" ]; then echo "already at $KEY"; publish_pubkey; return 0; fi
  mkdir -p "$KEY_DIR"
  oras discover "$VM_BACKING_IMAGE" --format json > "${KEY_DIR}/referrers.json"
  ref=$(VM_INFO_TYPE=application/vnd.batesste.vm-info.v1 python3 -I -c 'import json,os,sys; want=os.environ["VM_INFO_TYPE"]; refs=json.load(sys.stdin).get("referrers",[]); hits=[r["reference"] for r in refs if r.get("artifactType")==want]; print(hits[0]) if hits else sys.exit("no %s referrer on the qcow2" % want)' \
        < "${KEY_DIR}/referrers.json")
  oras pull "$ref" -o "$KEY_DIR"
  [ -f "$KEY" ] || die "the referrer carried no id_rsa"
  chmod 600 "$KEY"
  publish_pubkey
}

# inspect resolves a guest key by looking beside the image, in keys/, never
# in the directory this script pulls to -- so without this it reports the
# key as "accepted by guest, not found locally" and prints an ssh command
# with no -i. The report quotes that line, so it has to be the true one.
publish_pubkey() {
  mkdir -p "${VM_IMAGES_DIR}/keys"
  ssh-keygen -y -f "$KEY" > "${VM_IMAGES_DIR}/keys/mi355x_id_rsa.pub"
}

make_overlays() {
  say "Creating guest overlays"
  first="${VM_NAME_PREFIX}-1"
  if [ ! -f "${VM_IMAGES_DIR}/${first}-backing.qcow2" ]; then
    qt gen-vm --env-file "$ENV_FILE" --env-file "$OVERLAY" \
      --vm-name "$first" --backing-image "$VM_BACKING_IMAGE"
  fi
  n=2
  while [ "$n" -le "$VM_COUNT" ]; do
    qt gen-vm --env-file "$ENV_FILE" --env-file "$OVERLAY" \
      --vm-name "${VM_NAME_PREFIX}-${n}" \
      --backing-file "${VM_IMAGES_DIR}/${first}-backing.qcow2"
    n=$((n + 1))
  done
}

generate_stack() {
  say "Rendering ${STACK} to ${STACK_DIR}"
  mkdir -p "$STACK_DIR"
  qt gen-compose --stack "$STACK" \
    --env-file "$ENV_FILE" --env-file "$OVERLAY" --output-dir "$STACK_DIR"

  # Copy the repo's qemu package into the stack directory so the QEMU
  # containers can install qemu-tool FROM SOURCE. Two reasons it has to be a
  # copy rather than $REPO_ROOT:
  #
  #   1. The generated compose mounts "${QEMU_TOOL_SRC:-../../..}", and that
  #      default only resolves to the repo while the stack directory sits at
  #      qemu/compose/<stack>/. Rendered anywhere else it points at some
  #      unrelated parent, the entrypoint's `if [ -f
  #      /qemu-tool-src/qemu/pyproject.toml ]` falls through, and the
  #      container installs the RELEASED qemu-tool from PyPI.
  #   2. $REPO_ROOT is a container-internal path when this runs in a CI job
  #      container, and the host docker daemon cannot resolve it as a
  #      bind-mount source.
  #
  # The failure is total and late: the released qemu-tool has no
  # --vfio-user-switch-width, which the rendered run-vm command passes, so
  # every guest dies instantly with "unrecognized arguments" and the run
  # only notices ~15 minutes later in wait_for_guest, reporting that the
  # guest did not answer SSH. gen-compose will not copy this for us --
  # _owned_assets deliberately ignores `..` references.
  rm -rf "${STACK_DIR}/qemu"
  cp -r "${REPO_ROOT}/qemu" "${STACK_DIR}/qemu"
}

up() {
  fetch_key
  make_overlays
  generate_stack
  say "Bringing the stack up"
  QEMU_TOOL_SRC="$STACK_DIR" compose up -d
}

# ---------------------------------------------------------------------------

# Everything the report quotes, gathered in one ssh round trip per guest.
# Takes a 1-based VM index, not a port: where a guest answers depends on
# --in-network, and resolving it in one place is what stops half the callers
# reaching a published port that a container job cannot see.
guest() {
  ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o NoHostAuthenticationForLocalhost=yes -o ConnectTimeout=10 \
    -p "$(guest_port "$1")" "${VM_USERNAME}@$(guest_host "$1")" "$2"
}

wait_for_guest() {
  vm=$1
  say "Waiting for SSH at $(guest_host "$vm"):$(guest_port "$vm")"
  i=0
  while [ "$i" -lt 90 ]; do
    guest "$vm" true 2>/dev/null && return 0
    i=$((i + 1)); sleep 10
  done
  die "guest ${vm} did not answer SSH at $(guest_host "$vm"):$(guest_port "$vm")"
}

# A second inventory source overriding where each guest answers. Ansible
# merges inventories and the later source wins, so the generated one stays
# authoritative for everything else -- the MACs, the ernic addresses and the
# rocjitsu image the firmware is generated from.
write_network_inventory() {
  OVERRIDE="${STACK_DIR}/inventory-in-network.yml"
  {
    echo "---"
    echo "# Written by scripts/mi355x-node.sh --in-network. Do not edit."
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

configure() {
  say "Making the guest test-ready"
  # Reused rather than reimplemented: the same playbook the quickstart lane
  # runs. It stages ip_discovery.bin (without which amdgpu probes -2 and the
  # guest looks GPU-less), addresses the ernic, and loads amdgpu, which is
  # blacklisted at boot by design. `hosts: fleet` so a one-VM inventory is
  # just a smaller fleet.
  extra_inventory=""
  [ "$IN_NETWORK" = 1 ] && extra_inventory="-i $(write_network_inventory)"
  # Unquoted on purpose: empty must expand to no argument at all.
  # shellcheck disable=SC2086
  ( cd "${REPO_ROOT}/ansible" && ansible-playbook \
      -i "${STACK_DIR}/fleet-inventory.yml" $extra_inventory \
      playbooks/fleet-quickstart.yml --private-key "$KEY" \
      -e ansible_remote_tmp=/tmp/.ansible/tmp )
}

# The assertions, separate from the report: a report is prose and a reader
# believes it, so the numbers in it have to have been checked first. Each of
# these fails to a plausible value rather than to an error -- an empty tree,
# zero GPUs, a guest that simply has fewer devices than asked for -- which is
# why every check demands the positive value rather than merely running.
check_shape() {
  vm=$1
  want_sw=$ERNIC_PER_VM
  say "Checking the shape of guest ${vm}"

  # The switch count comes first because it is the only thing here that is
  # unique to this stack. Everything else -- GPUs, RDMA devices, NVMe -- a
  # flat-tree guest would also report, so asserting only those would pass a
  # guest with the exact topology this lane exists to rule out.
  #
  # Counted as upstream ports: an x3130-upstream sits at 00.0 of the bus
  # behind each root port, so each switch contributes one "00.0-[" to
  # `lspci -t`. A flat tree has none.
  sw=$(guest "$vm" 'lspci -t | grep -c "00\.0-\[" || true')
  gpus=$(guest "$vm" 'ls /sys/bus/pci/drivers/amdgpu/ 2>/dev/null | grep -c "^0000" || true')
  rdma=$(guest "$vm" 'ibv_devinfo -l 2>/dev/null | tail -n +2 | tr -d " \t" | grep -c . || true')
  nvme=$(guest "$vm" 'ls /dev/nvme[0-9]n[0-9] 2>/dev/null | wc -l')
  named=$(guest "$vm" 'ip -o link show 2>/dev/null | grep -c "rocm-ernic0" || true')

  [ "$sw" -eq "$want_sw" ] || die "expected ${want_sw} PCIe switches, got ${sw} -- the guest did not get the MI355X tree"
  [ "$gpus" -eq "$want_sw" ] || die "expected ${want_sw} GPUs bound, got ${gpus}"
  [ "$rdma" -eq "$want_sw" ] || die "expected ${want_sw} RDMA devices, got ${rdma}"
  # Not derived from a settings key: VM_NVME accepts a count, a negative
  # count (null_blk) or a literal QEMU argument string, so only a positive
  # number means "this many drives" and anything else is not ours to predict.
  [ "$nvme" -gt 0 ] || die "no NVMe namespaces in the guest"
  # EXACTLY one, and that is not a typo for ERNIC_PER_VM. 99-rocm-ernic.rules
  # names the Ethernet interface rocm-ernic%n, and %n is 0 for every one of
  # the eight -- so one wins the name and the other seven keep kernel names
  # (enp7s0np0 and friends). Everything downstream that says rocm-ernic0 --
  # this script's rdma_test, fleet-quickstart.yml's netplan task -- depends
  # on that single winner existing. Asserting 1 catches both halves: zero
  # means the rename did not fire at all, more than one means the rule
  # started disambiguating and every hardcoded name is now a coin flip.
  [ "$named" -eq 1 ] || die "expected exactly 1 interface named rocm-ernic0, got ${named}"
  echo "switches ${sw}, GPUs ${gpus}, RDMA ${rdma}, NVMe ${nvme}, named NICs ${named}/${want_sw}"
}

# Loopback on purpose when VM_COUNT is 1, and said so in the report. With one
# guest there is no second node to send to; this proves the verbs and the
# queue pairs, never the mesh.
rdma_test() {
  vm=$1
  say "RDMA on guest ${vm}"
  dev=$(guest "$vm" 'for d in $(ibv_devinfo -l | tail -n +2); do
      [ -e /sys/class/infiniband/$d/device/net/rocm-ernic0 ] && echo $d; done | head -1')
  [ -n "$dev" ] || die "no IB device is bound to rocm-ernic0"
  # Poll for the server's listening socket rather than sleeping a fixed 3 s.
  # On a 4-core runner carrying seventeen device-server containers the server
  # can easily take longer, and the client then dies with "Couldn't connect"
  # -- which, piped, used to be invisible. fleet-quickstart.yml polls for the
  # same reason.
  out=$(guest "$vm" "ib_send_bw -d ${dev} --iters 200 -s 65536 >/tmp/srv.out 2>&1 &
    for i in \$(seq 1 30); do ss -ltn 2>/dev/null | grep -q ':18515' && break; sleep 1; done
    ib_send_bw -d ${dev} --iters 200 -s 65536 127.0.0.1 2>&1") || true
  echo "$out" | tail -6

  # ib_send_bw's exit status is not enough on its own, and piping it through
  # tail threw even that away: ssh returns the REMOTE shell's status, the
  # remote shell has no pipefail, so the status was always tail's 0. A failed
  # run then reached report(), which stamped Status: **pass** unconditionally,
  # the artifact uploaded, and publish-pages put a green MI355X report on the
  # site -- feeding the all-green badge, which this lane is now part of.
  #
  # So assert the payload line itself, the way the quickstart lane asserts a
  # non-zero bandwidth. A run that moved nothing has no 65536 row.
  echo "$out" | grep -qE '^[[:space:]]*65536[[:space:]]+' \
    || die "ib_send_bw reported no 65536-byte result; the RDMA run did not complete"
}

# ---------------------------------------------------------------------------

report() {
  [ -n "$REPORT_DIR" ] || return 0
  say "Writing the report to ${REPORT_DIR}"
  mkdir -p "$REPORT_DIR"
  vm=1
  sha=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
  # Earned, not decorative. render-site-perf.py reads this stamp and will not
  # count the lane green without it, and mi355x is in its REPORTS set -- so a
  # pass printed unconditionally would green the front-page badge over a run
  # that moved nothing. Derived from the payload line rdma_test asserted.
  if grep -qE '^[[:space:]]*65536[[:space:]]+' "${REPORT_DIR}/.rdma.txt" 2>/dev/null; then
    status=pass
  else
    status=unknown
  fi
  {
    echo "# MI355X-shaped node report"
    echo
    echo "One guest with ${ERNIC_PER_VM} GPUs, ${ERNIC_PER_VM} emulated RDMA NICs and"
    echo "${ERNIC_PER_VM} PCIe switches -- one NIC and one GPU below each switch, each"
    echo "switch on its own root port. Every other stack in this repo hangs one NIC"
    echo "and one GPU flat off the root complex, so this is the only lane that"
    echo "exercises software reading a realistic PCIe tree."
    echo
    echo "- Guest image: \`${VM_BACKING_IMAGE##*:}\`"
    echo "- Fleet size: ${VM_COUNT} VM (the lab stack is 4; a runner fits one)"
    echo "- Addressed NICs: 1 of ${ERNIC_PER_VM}. All ${ERNIC_PER_VM} RDMA devices come up, but"
    echo "  \`99-rocm-ernic.rules\` names the Ethernet interface \`rocm-ernic%n\` and \`%n\`"
    echo "  is 0 for every one of them, so a single NIC wins that name and the other"
    echo "  seven keep kernel names. Only the named one carries an address."
    echo "- Generated: **$(date -u '+%Y-%m-%d %H:%M UTC')** &middot; Commit: \`${sha}\` &middot; Status: **${status}**"
    echo
    echo "## The PCIe tree the guest enumerates"
    echo
    echo "Each switch carries exactly one Pensando NIC and one GPU, so their"
    echo "traffic has a path that does not cross the root complex."
    echo
    echo '```'
    guest "$vm" 'lspci -t -v 2>/dev/null' | head -40
    echo '```'
    echo
    echo "## Devices"
    echo
    echo '```'
    guest "$vm" '
      echo "sockets:  $(lscpu | sed -n "s/^Socket(s): *//p")"
      echo "cores:    $(nproc)"
      echo "GPUs:     $(ls /sys/bus/pci/drivers/amdgpu/ | grep -c "^0000") bound, $(ls /sys/class/kfd/kfd/topology/nodes | wc -l) KFD nodes"
      echo "RDMA:     $(ibv_devinfo -l | head -1)"
      echo "NVMe:     $(ls /dev/nvme[0-9]n[0-9] 2>/dev/null | tr "\n" " ")"
      lsblk -d -o NAME,SIZE,TYPE | grep -v loop'
    echo '```'
    echo
    echo "## RDMA"
    echo
    if [ "$VM_COUNT" -eq 1 ]; then
      echo "**Loopback, not inter-node.** With one guest there is no second node to"
      echo "send to, so this proves the verbs and the queue pairs work. It is not a"
      echo "mesh bandwidth figure and must not be read as one."
    else
      echo "Between two guests over the TCP mesh."
    fi
    echo
    echo '```'
    cat "${REPORT_DIR}/.rdma.txt" 2>/dev/null || echo "not run"
    echo '```'
    echo
    echo "## What qemu-tool reports about the live guest"
    echo
    echo '```'
    qt inspect --env-file "$ENV_FILE" --env-file "$OVERLAY" \
      --vm-name "${VM_NAME_PREFIX}-1" 2>&1 | head -40
    echo '```'
  } > "${REPORT_DIR}/index.md"
  rm -f "${REPORT_DIR}/.rdma.txt"
}

run_test() {
  wait_for_guest 1
  configure
  check_shape 1
  if [ -n "$REPORT_DIR" ]; then
    mkdir -p "$REPORT_DIR"
    rdma_test 1 > "${REPORT_DIR}/.rdma.txt"
    cat "${REPORT_DIR}/.rdma.txt"
  else
    rdma_test 1
  fi
  report
}

down() {
  say "Taking the stack down"
  compose down
  echo "overlays under ${VM_IMAGES_DIR} are left in place; delete them to start over"
}

case "$VERB" in
  up) up ;;
  test) run_test ;;
  down) down ;;
esac
