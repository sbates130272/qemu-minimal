#!/usr/bin/env bash
# Generate a markdown VM inspection report from a running QEMU VM.
# Usage: generate-vm-report.sh <output-dir> [ssh-port] [ssh-user] [vm-label]
#
# vm-label is an optional display name for the VM (e.g. "VM 2"). When the
# output dir is a subdirectory of an existing site, _config.yml is written
# only at the site root (detected by the absence of _config.yml in OUTDIR's
# parent). Pass vm-label to distinguish reports when calling this script for
# multiple VMs.
set -euo pipefail

OUTDIR=${1:-site}
PORT=${2:-2222}
USER=${3:-ubuntu}
VM_LABEL=${4:-}
# Defaults to localhost for bare-runner callers. Container jobs reach the VM
# by its compose service name instead, since the published port lands on the
# host rather than in the job container.
HOST=${5:-localhost}
SSH="ssh -o NoHostAuthenticationForLocalhost=yes -o StrictHostKeyChecking=no -p ${PORT} ${USER}@${HOST}"
SCP=(scp -o NoHostAuthenticationForLocalhost=yes -o StrictHostKeyChecking=no -P "${PORT}")
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VM_REPORT_BENCH_DIR="${SCRIPT_DIR}/vm-report"

TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M UTC')
REPO="sbates130272/qemu-minimal"
# Every report lane runs the script inside a container, where git rev-parse
# fails and the old fallback published a literal "unknown" linked to
# /commit/unknown -- a dead link on all four pages. GITHUB_SHA is always set by
# Actions; git is only the local-invocation path. Link with the full SHA and
# display the short form, and emit plain text rather than a broken link when
# there is genuinely no commit to point at.
COMMIT_SHA=${GITHUB_SHA:-$(git rev-parse HEAD 2>/dev/null || true)}
if [ -n "${COMMIT_SHA}" ]; then
  COMMIT_MD="[\`${COMMIT_SHA:0:7}\`](https://github.com/${REPO}/commit/${COMMIT_SHA})"
else
  COMMIT_MD="unknown"
fi
TITLE_SUFFIX=${VM_LABEL:+" — ${VM_LABEL}"}
# Stamped into the report so the Pages publish can tell a report that passed
# from one that is merely still on the branch. It is earned rather than
# decorative: the script runs under set -e and every benchmark path ends in
# bench_failed, which exits 1, so reaching the heredoc at all means nothing
# failed. A half that did fail writes no report and uploads no artifact, and
# render-site-perf.py then sees that lane's subtree go stale rather than green.
# Override only if a caller has a verdict this script cannot see.
REPORT_STATUS=${REPORT_STATUS:-pass}

collect() { $SSH "$1" 2>/dev/null || echo "(not available)"; }

copy_to_guest() {
  "${SCP[@]}" "$1" "${USER}@${HOST}:$2" >/dev/null 2>&1
}

json_string() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

# The benchmarks report in G-units, but the same code spans five orders of
# magnitude between bare metal and a vfio-user guest, so a fixed unit renders
# one of the two unreadably (0.00187734275 GFLOP/s on a badge). Pick the
# prefix from the value and round to three significant figures.
scale_metric() {
  python3 - "$1" "$2" <<'PY'
import sys

value = float(sys.argv[1]) * 1e9
unit = sys.argv[2]
for prefix, factor in (("T", 1e12), ("G", 1e9), ("M", 1e6), ("k", 1e3)):
    if abs(value) >= factor:
        print(f"{value / factor:.3g} {prefix}{unit}")
        break
else:
    print(f"{value:.3g} {unit}")
PY
}

write_badge_json() {
  local path=$1 label=$2 message=$3 color=$4
  local badge_color
  if [ -n "${message}" ]; then
    badge_color="${color}"
  else
    message="n/a"
    badge_color="lightgrey"
  fi
  local label_json message_json color_json
  label_json=$(json_string "${label}")
  message_json=$(json_string "${message}")
  color_json=$(json_string "${badge_color}")
  cat > "${path}" <<JSON
{
  "schemaVersion": 1,
  "label": ${label_json},
  "message": ${message_json},
  "color": ${color_json}
}
JSON
}

# A guest-side script that dies under set -e can leave nothing to print, and a
# bare exit 1 then reports a 30-minute run as a single blank line. Always say
# which benchmark failed, even when the guest said nothing at all.
bench_failed() {
  printf '%s failed\n' "$1" >&2
  if [ -n "$2" ]; then
    printf '%s\n' "$2" >&2
  else
    printf '(the guest produced no output before exiting)\n' >&2
  fi
  exit 1
}

# ---------------------------------------------------------------------------
# Ernic SRQ + P4 helpers
# ---------------------------------------------------------------------------

# Check the installed ionic_rdma: DKMS path, max_srq from ibv_devinfo.
# Returns two variables: ERNIC_SRQ_PATH and ERNIC_SRQ_MAX (the max_srq value
# from ibv_devinfo -v, or "0" if not present).
collect_ernic_srq() {
  ERNIC_SRQ_PATH=$(collect "modinfo -n ionic_rdma 2>/dev/null || echo '(not installed)'")
  local devinfo
  devinfo=$(collect "ibv_devinfo -v 2>/dev/null | grep max_srq || echo 'max_srq: 0'")
  ERNIC_SRQ_MAX=$(printf '%s\n' "${devinfo}" | sed -n 's/.*max_srq:\s*\([0-9]*\).*/\1/p' | head -1)
  ERNIC_SRQ_MAX=${ERNIC_SRQ_MAX:-0}
}

# Check if ernic_p4.ko is installed at updates/dkms in the guest.
collect_ernic_p4() {
  ERNIC_P4_PATH=$(collect "modinfo -n ernic_p4 2>/dev/null || echo '(not installed)'")
}

# P4 FP8 reduction bandwidth, measured on one guest.
#
# The reduction is in-NIC compute: an RDMA WRITE whose gather list names two
# equal-length operand regions is turned, by a table entry of the P4 program,
# into an elementwise fp8 add whose result lands in the region the write named
# as its destination. All three regions belong to the same device, so there is
# no peer guest in this and nothing crosses the mesh.
#
# Two programs the golden bake installs do the work, both as the ordinary guest
# user. p4_fp8_client is rocm-ernic's own guest test, built from the pinned
# sources: it loads fp8_policy.json through /dev/ernic-p4, installs the reduce
# entry and checks one reduction. p4_fp8_bench then repeats that work request
# and checks every result, so a device that did not reduce fails rather than
# timing a plain RDMA WRITE.
#
# The number is fp8 result bytes per second, one byte an element: each
# iteration reads 2 x ELEMENTS operand bytes and writes ELEMENTS. 262144 is
# the most the device moves in one operation (IONIC_FP8_MAP_MAX).
#
# A missing module, node, client or program is a failure, and so is a node the
# guest user cannot open: every one of them is the golden bake's to provide.
# The program and its entry stay loaded until the ernic server restarts; an
# ordinary transfer is not a two-equal-operand write and is left alone.
ERNIC_P4_FP8_ELEMENTS=${ERNIC_P4_FP8_ELEMENTS:-262144}
ERNIC_P4_FP8_ITERS=${ERNIC_P4_FP8_ITERS:-2000}
run_ernic_p4_fp8_bw() {
  $SSH "ELEMENTS='${ERNIC_P4_FP8_ELEMENTS}' ITERS='${ERNIC_P4_FP8_ITERS}' bash -s" 2>&1 <<'BENCH'
set -euo pipefail
node=/dev/ernic-p4
program=/usr/local/share/rocm-ernic/fp8_policy.json
golden="the guest image must be the golden bake (ansible/playbooks/vm-rocm-golden.yml)"
missing() { echo "$1: ${golden}"; exit 1; }

modinfo -n ernic_p4 >/dev/null 2>&1 || missing "the ernic_p4 module is not installed"
for tool in p4_fp8_client p4_fp8_bench; do
  command -v "${tool}" >/dev/null || missing "${tool} is not installed"
done
[ -r "${program}" ] || missing "${program} is not installed"
if [ ! -e "${node}" ]; then
  echo "${node} is absent: ernic_p4 is not loaded, or the ernic server was" \
       "started without a P4 guest scope (<p4-enable> and <p4-guest-scope>" \
       "in the fleet file)"
  exit 1
fi
if [ ! -r "${node}" ] || [ ! -w "${node}" ]; then
  echo "${node} is $(stat -c '%U:%G mode %a' "${node}") and $(id -un) is in" \
       "groups: $(id -Gn)"
  echo "the guest user is not in the ernic-p4 group or the udev rule is" \
       "missing: ${golden}"
  exit 1
fi

# Resolved, not assumed: the IB device keeps its kernel name on a guest whose
# rename did not fire.
ib=$(ibv_devinfo -l | sed -n '2p' | tr -d ' \t\r')
[ -n "${ib}" ] || { echo "no RDMA device in the guest"; exit 1; }

# RDMA_DEVICE makes an absent device exit 1; 77 is then only "no P4 node".
rc=0
RDMA_DEVICE="${ib}" timeout 60 p4_fp8_client "${program}" || rc=$?
if [ "${rc}" -ne 0 ]; then
  echo "p4_fp8_client exited ${rc}: the program was not loaded or the" \
       "reduction was wrong"
  exit 1
fi
timeout 300 p4_fp8_bench "${ib}" "${ELEMENTS}" "${ITERS}"
BENCH
}

# Time 4 MiB copies through the SDXI engine and report the throughput in MB/s.
# An absent device is a failure: the fleet this runs against has one. The node
# is tested in the guest, so an ssh failure is not reported as a missing device.
# Timed for two seconds: twenty copies is about 40 ms and swung 30% run to run.
run_sdxi_bw() {
  # Times the copy ioctl directly, through the module sdxi-cli is built on.
  # Reports "bench copy: N bytes x M iters, avg X ms, Y MB/s".
  $SSH "timeout 120 python3 -" 2>&1 <<'BENCH'
import os, time

if not os.path.exists("/dev/sdxi0"):
    raise SystemExit("/dev/sdxi0 is absent: sdxi_char is not loaded, or the "
                     "fleet has no SDXI device (<sdxi-per-vm>)")

from sdxi_cli.ioctl import sdxi_copy

size, seconds = 4194304, 2.0
data = os.urandom(size)
fd = os.open("/dev/sdxi0", os.O_RDWR)
iters = 0
start = time.perf_counter()
while True:
    out = sdxi_copy(fd, data)
    iters += 1
    elapsed = time.perf_counter() - start
    if elapsed >= seconds:
        break
if out != data:
    raise SystemExit("bench copy: destination differs from source")
print(f"bench copy: {size} bytes x {iters} iters, "
      f"avg {elapsed / iters * 1000:.2f} ms, "
      f"{size * iters / elapsed / 1e6:.1f} MB/s")
BENCH
}

# The number in front of "MB/s" on the last line carrying $1, if it is above
# zero. POSIX awk: the job container's awk is mawk, which has no three-argument
# match().
parse_mbs() {
  awk -v tag="$1" '
    index($0, tag) {
      for (i = 2; i <= NF; i++)
        if ($i == "MB/s" && $(i - 1) ~ /^[0-9]+(\.[0-9]+)?$/) v = $(i - 1)
    }
    END { if (v + 0 > 0) print v }'
}

run_rocjitsu_gemm() {
  if ! copy_to_guest "${VM_REPORT_BENCH_DIR}/sgemm-bench.hip" /tmp/sgemm-bench.hip; then
    echo "sgemm-bench source upload failed"
    return 1
  fi

  {
    $SSH "bash -s" <<'EOF'
set -euo pipefail
gpu_nodes=$(cat /sys/class/kfd/kfd/topology/nodes/*/name 2>/dev/null | grep -c . || true)
[ "${gpu_nodes}" -ge 1 ] || { echo "No bound GPU KFD node"; exit 0; }
# || true: with pipefail set, no hipcc anywhere means the unmatched glob makes
# ls exit non-zero, the assignment inherits that, and set -e kills this script
# here -- silently, ls stderr being discarded -- so the guard below, which
# exists to report precisely that case, never gets to run.
hipcc=$(command -v hipcc || ls /opt/rocm*/bin/hipcc /opt/rocm/*/bin/hipcc 2>/dev/null | head -1 || true)
[ -n "${hipcc}" ] || { echo "hipcc unavailable"; exit 0; }
rocm_prefix=$(dirname "$(dirname "${hipcc}")")
echo "hipcc: ${hipcc}"
echo "rocm: ${rocm_prefix}"
"${hipcc}" -O2 --offload-arch=gfx1250 \
  -Wl,-rpath,"${rocm_prefix}/lib" \
  -o /tmp/sgemm-bench /tmp/sgemm-bench.hip
/tmp/sgemm-bench 8
EOF
  } 2>&1
}

run_rocjitsu_hipfile() {
  if ! copy_to_guest "${VM_REPORT_BENCH_DIR}/gemm-hipfile-bench.hip" /tmp/gemm-hipfile-bench.hip; then
    echo "gemm-hipfile-bench source upload failed"
    return 1
  fi

  {
    $SSH "bash -s" <<'EOF'
set -euo pipefail
gpu_nodes=$(cat /sys/class/kfd/kfd/topology/nodes/*/name 2>/dev/null | grep -c . || true)
[ "${gpu_nodes}" -ge 1 ] || { echo "No bound GPU KFD node"; exit 0; }
# || true: with pipefail set, no hipcc anywhere means the unmatched glob makes
# ls exit non-zero, the assignment inherits that, and set -e kills this script
# here -- silently, ls stderr being discarded -- so the guard below, which
# exists to report precisely that case, never gets to run.
hipcc=$(command -v hipcc || ls /opt/rocm*/bin/hipcc /opt/rocm/*/bin/hipcc 2>/dev/null | head -1 || true)
[ -n "${hipcc}" ] || { echo "hipcc unavailable"; exit 0; }
# || true for the same reason as hipcc above: with no matching tree the loop's
# last test is what sets the exit status, and pipefail carries it out.
# TheRock ships the header flat at include/hipfile.h; other ROCm layouts nest
# it under include/hipfile/. Accept either, as the compile line below and the
# benchmark's own __has_include already do -- looking only for the nested one
# reported "unavailable" on a guest that had the SDK installed.
rocm=$(for d in /opt/rocm /opt/rocm-* /opt/rocm/*; do
  { [ -e "${d}/include/hipfile/hipfile.h" ] || [ -e "${d}/include/hipfile.h" ]; } \
    && [ -e "${d}/lib/libhipfile.so" ] && echo "${d}"
done | head -1 || true)
[ -n "${rocm}" ] || { echo "hipFile headers or library unavailable"; exit 0; }
dev=$(lsblk -dpno NAME,TYPE | awk '$2=="disk" && $1 ~ /^\/dev\/nvme[0-9]+n[0-9]+$/ { print $1; exit }')
[ -n "${dev}" ] || { echo "No NVMe namespace present"; exit 0; }
sudo -n mkdir -p /mnt/nvme
if ! mountpoint -q /mnt/nvme; then
  fstype=$(sudo -n blkid -o value -s TYPE "${dev}" 2>/dev/null || true)
  if [ -z "${fstype}" ]; then
    sudo -n mkfs.ext4 -q -F "${dev}"
  fi
  sudo -n mount -o noatime "${dev}" /mnt/nvme
fi
sudo -n chmod 1777 /mnt/nvme
MNT=/mnt/nvme
# begin: hipfile target is on nvme
# A hipFile direct read of a file that is not on an NVMe namespace can hang
# the guest for good: AIS takes any PCI block device, and a virtio disk
# cannot DMA to a GPU BAR address (seen on the root disk: "virtio: bogus
# descriptor or out of resources", and the guest never answered again).
src=$(findmnt -n -o SOURCE --target "${MNT}" 2>/dev/null || true)
# A namespace or a partition of one, with btrfs's "[/subvolume]" allowed.
nvme_re='^/dev/nvme[0-9]+n[0-9]+(p[0-9]+)?(\[.*\])?$'
if ! [[ "${src}" =~ ${nvme_re} ]]; then
  echo "${MNT} is on '${src:-no filesystem}', not an NVMe namespace: refusing a hipFile direct read there"
  exit 1
fi
# end: hipfile target is on nvme
echo "hipcc: ${hipcc}"
echo "rocm: ${rocm}"
"${hipcc}" -O2 --offload-arch=gfx1250 \
  -I"${rocm}/include" -I"${rocm}/include/hipfile" \
  -o /tmp/gemm-hipfile-bench /tmp/gemm-hipfile-bench.hip \
  -L"${rocm}/lib" -lhipfile -Wl,-rpath,"${rocm}/lib"
/tmp/gemm-hipfile-bench "${MNT}" 4
EOF
  } 2>&1
}

# Unlike the two benches above this one has no source to compile here: the
# guest script builds fio itself and prints the metric, so all this does is
# stage it and run it.
run_hipfile_fio() {
  if ! copy_to_guest "${VM_REPORT_BENCH_DIR}/fio-hipfile-bench.sh" /tmp/fio-hipfile-bench.sh; then
    echo "fio-hipfile-bench upload failed"
    return 1
  fi

  { $SSH "bash /tmp/fio-hipfile-bench.sh"; } 2>&1
}

# SRQ + P4 status (always collected when the ernic stack is running)
ERNIC_SRQ_PATH=""
ERNIC_SRQ_MAX="0"
ERNIC_P4_PATH=""
if [ "${REPORT_ERNIC_SRQ_P4:-0}" = "1" ]; then
  collect_ernic_srq
  collect_ernic_p4
fi

# P4 FP8 bandwidth (only when explicitly requested). No number is a failure,
# not an n/a badge: a badge that reads n/a is a green run that measured nothing.
ERNIC_P4_BW_OUTPUT=""
ERNIC_P4_BW_MBS=""
ERNIC_P4_BW_DISPLAY=""
if [ "${REPORT_ERNIC_P4_BENCH:-0}" = "1" ]; then
  if ! ERNIC_P4_BW_OUTPUT=$(run_ernic_p4_fp8_bw); then
    bench_failed "ernic P4 FP8 bandwidth benchmark" "${ERNIC_P4_BW_OUTPUT}"
  fi
  ERNIC_P4_BW_MBS=$(printf '%s\n' "${ERNIC_P4_BW_OUTPUT}" | parse_mbs "p4 fp8 bench:")
  if [ -z "${ERNIC_P4_BW_MBS}" ]; then
    bench_failed "ernic P4 FP8 bandwidth benchmark (no MB/s figure above zero in its output)" \
      "${ERNIC_P4_BW_OUTPUT}"
  fi
  ERNIC_P4_BW_DISPLAY="${ERNIC_P4_BW_MBS} MB/s"
fi

# SDXI copy bandwidth (only when explicitly requested), held to the same rule.
SDXI_BW_OUTPUT=""
SDXI_BW_MBS=""
SDXI_BW_DISPLAY=""
if [ "${REPORT_SDXI_BENCH:-0}" = "1" ]; then
  if ! SDXI_BW_OUTPUT=$(run_sdxi_bw); then
    bench_failed "SDXI copy bandwidth benchmark" "${SDXI_BW_OUTPUT}"
  fi
  SDXI_BW_MBS=$(printf '%s\n' "${SDXI_BW_OUTPUT}" | parse_mbs "bench copy:")
  if [ -z "${SDXI_BW_MBS}" ]; then
    bench_failed "SDXI copy bandwidth benchmark (no MB/s figure above zero in its output)" \
      "${SDXI_BW_OUTPUT}"
  fi
  SDXI_BW_DISPLAY="${SDXI_BW_MBS} MB/s"
fi

KERNEL=$(collect "uname -r")
PROC_VER=$(collect "cat /proc/version")
CPU_INFO=$(collect "lscpu | grep -E '^CPU\(s\)|^Model name|^Thread|^Core|^Socket'")
CPU_MODEL=$(echo "$CPU_INFO" | grep 'Model name' | sed 's/.*: *//' || echo "(not available)")
CPU_COUNT=$(echo "$CPU_INFO" | awk '/^CPU\(s\)/{print $2}')
CPU_THREADS=$(echo "$CPU_INFO" | awk '/Thread/{print $NF}')
MEM=$(collect "free -h")
MEM_TOTAL=$(echo "$MEM" | awk '/^Mem/{print $2}')
MEM_FREE=$(echo "$MEM"  | awk '/^Mem/{print $4}')
SWAP=$(echo "$MEM" | awk '/^Swap/{print $2}')
DISK=$(collect "lsblk -o NAME,SIZE,TYPE,MOUNTPOINT")
DF=$(collect "df -h / /boot")
NVME=$(collect "nvme list 2>/dev/null || echo 'nvme-cli unavailable'")
PCI=$(collect "lspci -nn 2>/dev/null || echo 'pciutils unavailable'")
NVME_CLI_PKG=$(collect "dpkg-query -W -f='\${binary:Package}\t\${Version}\n' | grep -E '^nvme-cli[[:space:]]' || echo 'nvme-cli not installed'")
AMDGPU_PKGS=$(collect "dpkg-query -W -f='\${binary:Package}\t\${Version}\n' | grep -Ei '^[^[:space:]]*amdgpu[^[:space:]]*[[:space:]]' | sort || echo 'No amdgpu packages installed'")
ROCM_PKGS=$(collect "dpkg-query -W -f='\${binary:Package}\t\${Version}\n' | grep -E '^amdrocm' | sort || echo 'No amdrocm packages installed'")
ROCM_BINS=$(collect "ls /opt/rocm/bin/ 2>/dev/null | sort")
HIPFILE_PKGS=$(collect "dpkg-query -W -f='\${binary:Package}\t\${Version}\n' | grep -i 'hipfile' | sort || echo 'No hipfile packages installed'")
USER_GROUPS=$(collect "groups ${USER}")
SERVICES=$(collect "systemctl list-units --type=service --state=running --no-pager --no-legend")
SOURCES=$(collect "ls /etc/apt/sources.list.d/")
JOURNAL=$(collect "journalctl -p err -b --no-pager 2>/dev/null | tail -5")

ROCJITSU_GEMM_OUTPUT=""
ROCJITSU_GEMM_GFLOPS=""
ROCJITSU_GEMM_DISPLAY=""
ROCJITSU_HIPFILE_OUTPUT=""
ROCJITSU_HIPFILE_GBS=""
ROCJITSU_HIPFILE_DISPLAY=""
if [ "${REPORT_ROCJITSU_BENCH:-0}" = "1" ]; then
  if ! ROCJITSU_GEMM_OUTPUT=$(run_rocjitsu_gemm); then
    bench_failed "rocjitsu GEMM benchmark" "${ROCJITSU_GEMM_OUTPUT}"
  fi
  ROCJITSU_GEMM_GFLOPS=$(printf '%s\n' "${ROCJITSU_GEMM_OUTPUT}" \
    | sed -n 's/.*gflops=\([0-9.eE+-]*\).*/\1/p' | tail -1)
  [ -n "${ROCJITSU_GEMM_GFLOPS}" ] || {
    printf '%s\n' "${ROCJITSU_GEMM_OUTPUT}"
    echo "rocjitsu GEMM benchmark produced no gflops metric" >&2
    exit 1
  }
  ROCJITSU_GEMM_DISPLAY=$(scale_metric "${ROCJITSU_GEMM_GFLOPS}" "FLOP/s")
  if ! ROCJITSU_HIPFILE_OUTPUT=$(run_rocjitsu_hipfile); then
    bench_failed "rocjitsu hipFile benchmark" "${ROCJITSU_HIPFILE_OUTPUT}"
  fi
  ROCJITSU_HIPFILE_GBS=$(printf '%s\n' "${ROCJITSU_HIPFILE_OUTPUT}" \
    | sed -n 's/.*read_gbs=\([0-9.eE+-]*\).*/\1/p' | tail -1)
  [ -n "${ROCJITSU_HIPFILE_GBS}" ] || {
    printf '%s\n' "${ROCJITSU_HIPFILE_OUTPUT}"
    echo "rocjitsu hipFile benchmark produced no read_gbs metric" >&2
    exit 1
  }
  ROCJITSU_HIPFILE_DISPLAY=$(scale_metric "${ROCJITSU_HIPFILE_GBS}" "B/s")
fi

HIPFILE_FIO_OUTPUT=""
HIPFILE_FIO_GBS=""
HIPFILE_FIO_DISPLAY=""
if [ "${REPORT_HIPFILE_FIO_BENCH:-0}" = "1" ]; then
  if ! HIPFILE_FIO_OUTPUT=$(run_hipfile_fio); then
    bench_failed "hipFile fio benchmark" "${HIPFILE_FIO_OUTPUT}"
  fi
  HIPFILE_FIO_GBS=$(printf '%s\n' "${HIPFILE_FIO_OUTPUT}" \
    | sed -n 's/.*read_gbs=\([0-9.eE+-]*\).*/\1/p' | tail -1)
  [ -n "${HIPFILE_FIO_GBS}" ] || {
    printf '%s\n' "${HIPFILE_FIO_OUTPUT}"
    echo "hipFile fio benchmark produced no read_gbs metric" >&2
    exit 1
  }
  HIPFILE_FIO_DISPLAY=$(scale_metric "${HIPFILE_FIO_GBS}" "B/s")
fi

mkdir -p "${OUTDIR}"

# Write _config.yml only at the site root (skip for subdirectory reports).
if [ ! -f "${OUTDIR}/../_config.yml" ] && [ ! -f "${OUTDIR}/_config.yml" ]; then
  cat > "${OUTDIR}/_config.yml" <<'CFG'
theme: minima
title: qemu-minimal VM Report
description: Live VM inspection report for the qemu-minimal project
CFG
fi

cat > "${OUTDIR}/index.md" <<MD
---
title: VM Report${TITLE_SUFFIX}
---

# VM Report — qemu-minimal${TITLE_SUFFIX}

Generated: **${TIMESTAMP}** &middot; Commit: ${COMMIT_MD} &middot; Status: **${REPORT_STATUS}**

## Hardware

| Resource | Value |
|---|---|
| CPU | ${CPU_MODEL} |
| vCPUs | ${CPU_COUNT} |
| Threads/core | ${CPU_THREADS} |
| RAM | ${MEM_TOTAL} total, ${MEM_FREE} free |
| Swap | ${SWAP} |

## Kernel

\`\`\`
${KERNEL}
\`\`\`

<details><summary>Full version string</summary>

\`\`\`
${PROC_VER}
\`\`\`

</details>

## Storage

\`\`\`
${DISK}
\`\`\`

### Disk usage

\`\`\`
${DF}
\`\`\`

### NVMe

\`\`\`
${NVME}
\`\`\`

## PCI devices

\`\`\`
${PCI}
\`\`\`

## Debian Packages

### nvme-cli

\`\`\`
${NVME_CLI_PKG}
\`\`\`

## ROCm

### Installed Debian packages

\`\`\`
${ROCM_PKGS}
\`\`\`

### /opt/rocm/bin

\`\`\`
${ROCM_BINS}
\`\`\`

### hipFile Debian packages

\`\`\`
${HIPFILE_PKGS}
\`\`\`

## AMDGPU Debian Packages

\`\`\`
${AMDGPU_PKGS}
\`\`\`

## APT Sources

\`\`\`
${SOURCES}
\`\`\`

## User Groups

\`\`\`
${USER_GROUPS}
\`\`\`

## Running Services

\`\`\`
${SERVICES}
\`\`\`

## System Health

### Journal errors (this boot)

\`\`\`
${JOURNAL}
\`\`\`
$(if [ "${REPORT_ROCJITSU_BENCH:-0}" = "1" ]; then cat <<ROCJITSU

## rocjitsu Benchmarks

| Metric | Value |
|---|---|
| GEMM | ${ROCJITSU_GEMM_DISPLAY:-n/a} |
| hipFile read throughput | ${ROCJITSU_HIPFILE_DISPLAY:-n/a} |

These run inside a QEMU guest with the GPU attached over vfio-user, so the
absolute numbers sit far below what the same benchmark reports on bare metal —
that is expected, not a regression. What is worth watching is how they move
between runs. The unit is chosen from the value, so compare the unit too.

### GEMM output

\`\`\`
${ROCJITSU_GEMM_OUTPUT:-not run}
\`\`\`

### hipFile + NVMe output

\`\`\`
${ROCJITSU_HIPFILE_OUTPUT:-not run}
\`\`\`
ROCJITSU
fi)
$(if [ "${REPORT_ERNIC_SRQ_P4:-0}" = "1" ]; then cat <<SRQP4

## Ernic SRQ + P4 Status

| Component | Path | Status |
|---|---|---|
| ionic_rdma | ${ERNIC_SRQ_PATH} | max_srq: ${ERNIC_SRQ_MAX} |
| ernic_p4 | ${ERNIC_P4_PATH} | $([ "${ERNIC_P4_PATH}" = "(not installed)" ] && echo "not installed" || echo "installed") |

$([ "${ERNIC_SRQ_MAX}" -gt 0 ] 2>/dev/null && echo "SRQ is functional (max_srq=${ERNIC_SRQ_MAX}). UCX will use rc_verbs." || echo "SRQ not available (max_srq=0). UCX will fall back to ud_verbs.")
SRQP4
fi)
$(if [ "${REPORT_ERNIC_P4_BENCH:-0}" = "1" ] && [ -n "${ERNIC_P4_BW_OUTPUT}" ]; then cat <<ERNICP4BW

## Ernic P4 FP8 Bandwidth

The rate at which the emulated NIC's P4 pipeline reduces fp8 data. Each
iteration is one RDMA WRITE on an RC queue pair naming two
${ERNIC_P4_FP8_ELEMENTS}-element operand regions, which the fp8_policy
program's table entry turns into an elementwise fp8 add written to a third
region of the same device. The figure counts result bytes, one an element;
the device reads twice that in operands. Nothing crosses the mesh.

| Metric | Value |
|---|---|
| FP8 reduction BW | ${ERNIC_P4_BW_DISPLAY:-n/a} |

\`\`\`
${ERNIC_P4_BW_OUTPUT}
\`\`\`
ERNICP4BW
fi)
$(if [ "${REPORT_SDXI_BENCH:-0}" = "1" ] && [ -n "${SDXI_BW_OUTPUT}" ]; then cat <<SDXIBW

## SDXI Copy Bandwidth

| Metric | Value |
|---|---|
| Copy BW (4 MiB) | ${SDXI_BW_DISPLAY:-n/a} |

\`\`\`
${SDXI_BW_OUTPUT}
\`\`\`
SDXIBW
fi)
$(if [ "${REPORT_HIPFILE_FIO_BENCH:-0}" = "1" ]; then cat <<HIPFILEFIO

## hipFile fio Benchmark

| Metric | Value |
|---|---|
| fio libhipfile read throughput | ${HIPFILE_FIO_DISPLAY:-n/a} |

This is fio's own \`libhipfile\` ioengine reading from the guest NVMe straight
into VRAM, rather than the hand-written benchmark the rocjitsu report uses. fio
is built in the guest from a pinned master commit, because the engine is not in
any fio release tag. Same caveat as the other lanes: the GPU is emulated over
vfio-user, so watch the trend between runs rather than the absolute number.

### fio output

\`\`\`
${HIPFILE_FIO_OUTPUT:-not run}
\`\`\`
HIPFILEFIO
fi)
MD

if [ "${REPORT_ROCJITSU_BENCH:-0}" = "1" ]; then
  write_badge_json "${OUTDIR}/badge-gemm.json" GEMM "${ROCJITSU_GEMM_DISPLAY}" "ED1C24"
  write_badge_json "${OUTDIR}/badge-hipfile.json" hipfile "${ROCJITSU_HIPFILE_DISPLAY}" "76B900"
fi

if [ "${REPORT_HIPFILE_FIO_BENCH:-0}" = "1" ]; then
  write_badge_json "${OUTDIR}/badge-hipfile-fio.json" "hipfile fio" \
    "${HIPFILE_FIO_DISPLAY}" "0071C5"
fi

if [ "${REPORT_ERNIC_P4_BENCH:-0}" = "1" ]; then
  write_badge_json "${OUTDIR}/badge-ernic-p4-bw.json" "Ernic P4 FP8 BW" \
    "${ERNIC_P4_BW_DISPLAY}" "ED6B21"
fi

if [ "${REPORT_SDXI_BENCH:-0}" = "1" ]; then
  write_badge_json "${OUTDIR}/badge-sdxi-bw.json" "SDXI copy BW" \
    "${SDXI_BW_DISPLAY}" "0071C5"
fi

echo "Report written to ${OUTDIR}/index.md"
