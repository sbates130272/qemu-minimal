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
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
REPO="sbates130272/qemu-minimal"
TITLE_SUFFIX=${VM_LABEL:+" — ${VM_LABEL}"}

collect() { $SSH "$1" 2>/dev/null || echo "(not available)"; }

copy_to_guest() {
  "${SCP[@]}" "$1" "${USER}@${HOST}:$2" >/dev/null 2>&1
}

write_badge_json() {
  local path=$1 label=$2 value=$3 unit=$4 color=$5
  local message badge_color
  if [ -n "${value}" ]; then
    message="${value} ${unit}"
    badge_color="${color}"
  else
    message="n/a"
    badge_color="lightgrey"
  fi
  cat > "${path}" <<JSON
{
  "schemaVersion": 1,
  "label": "${label}",
  "message": "${message}",
  "color": "${badge_color}"
}
JSON
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
hipcc=$(command -v hipcc || ls /opt/rocm*/bin/hipcc /opt/rocm/*/bin/hipcc 2>/dev/null | head -1)
[ -n "${hipcc}" ] || { echo "hipcc unavailable"; exit 0; }
echo "hipcc: ${hipcc}"
"${hipcc}" -O2 --offload-arch=gfx1250 -o /tmp/sgemm-bench /tmp/sgemm-bench.hip
sudo -n /tmp/sgemm-bench 8
EOF
  } 2>&1 || true
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
hipcc=$(command -v hipcc || ls /opt/rocm*/bin/hipcc /opt/rocm/*/bin/hipcc 2>/dev/null | head -1)
[ -n "${hipcc}" ] || { echo "hipcc unavailable"; exit 0; }
rocm=$(for d in /opt/rocm /opt/rocm-* /opt/rocm/*; do
  [ -e "${d}/include/hipfile/hipfile.h" ] && [ -e "${d}/lib/libhipfile.so" ] && echo "${d}"
done | head -1)
[ -n "${rocm}" ] || { echo "hipFile headers or library unavailable"; exit 0; }
dev=$(lsblk -dno NAME,TYPE | awk '$2=="disk" && $1 ~ /^nvme/ { print "/dev/" $1; exit }')
[ -n "${dev}" ] || { echo "No NVMe namespace present"; exit 0; }
sudo -n mkdir -p /mnt/nvme
if ! mountpoint -q /mnt/nvme; then
  sudo -n mkfs.ext4 -q -F "${dev}"
  sudo -n mount -o noatime "${dev}" /mnt/nvme
fi
sudo -n chmod 1777 /mnt/nvme
echo "hipcc: ${hipcc}"
echo "rocm: ${rocm}"
"${hipcc}" -O2 --offload-arch=gfx1250 \
  -I"${rocm}/include" -I"${rocm}/include/hipfile" \
  -o /tmp/gemm-hipfile-bench /tmp/gemm-hipfile-bench.hip \
  -L"${rocm}/lib" -lhipfile -Wl,-rpath,"${rocm}/lib"
sudo -n /tmp/gemm-hipfile-bench /mnt/nvme 4
EOF
  } 2>&1 || true
}

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
AMDGPU=$(collect "dpkg -l | grep '^ii' | grep -i amdgpu")
ROCM_PKGS=$(collect "dpkg -l | grep '^ii' | grep '^ii  amdrocm' | awk '{print \$2, \$3}' | head -8")
ROCM_BINS=$(collect "ls /opt/rocm/bin/ 2>/dev/null | sort")
HIPFILE=$(collect "dpkg -l | grep '^ii' | grep -i hipfile")
USER_GROUPS=$(collect "groups ${USER}")
SERVICES=$(collect "systemctl list-units --type=service --state=running --no-pager --no-legend")
SOURCES=$(collect "ls /etc/apt/sources.list.d/")
JOURNAL=$(collect "journalctl -p err -b --no-pager 2>/dev/null | tail -5")

ROCJITSU_GEMM_OUTPUT=""
ROCJITSU_GEMM_GFLOPS=""
ROCJITSU_HIPFILE_OUTPUT=""
ROCJITSU_HIPFILE_GBS=""
if [ "${REPORT_ROCJITSU_BENCH:-0}" = "1" ]; then
  ROCJITSU_GEMM_OUTPUT=$(run_rocjitsu_gemm)
  ROCJITSU_GEMM_GFLOPS=$(printf '%s\n' "${ROCJITSU_GEMM_OUTPUT}" \
    | sed -n 's/.*gflops=\([0-9.eE+-]*\).*/\1/p' | tail -1)
  ROCJITSU_HIPFILE_OUTPUT=$(run_rocjitsu_hipfile)
  ROCJITSU_HIPFILE_GBS=$(printf '%s\n' "${ROCJITSU_HIPFILE_OUTPUT}" \
    | sed -n 's/.*read_gbs=\([0-9.eE+-]*\).*/\1/p' | tail -1)
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

Generated: **${TIMESTAMP}** &middot; Commit: [\`${COMMIT}\`](https://github.com/${REPO}/commit/${COMMIT})

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

## ROCm

### Installed packages (sample)

\`\`\`
${ROCM_PKGS}
\`\`\`

### /opt/rocm/bin

\`\`\`
${ROCM_BINS}
\`\`\`

### hipFile

\`\`\`
${HIPFILE}
\`\`\`

## AMDGPU Kernel Driver

\`\`\`
${AMDGPU}
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
| GEMM | ${ROCJITSU_GEMM_GFLOPS:-n/a}${ROCJITSU_GEMM_GFLOPS:+ GFLOP/s} |
| hipFile read throughput | ${ROCJITSU_HIPFILE_GBS:-n/a}${ROCJITSU_HIPFILE_GBS:+ GB/s} |

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
MD

if [ "${REPORT_ROCJITSU_BENCH:-0}" = "1" ]; then
  write_badge_json "${OUTDIR}/badge-gemm.json" gemm "${ROCJITSU_GEMM_GFLOPS}" "GFLOP/s" "ED1C24"
  write_badge_json "${OUTDIR}/badge-hipfile.json" hipfile "${ROCJITSU_HIPFILE_GBS}" "GB/s" "76B900"
fi

echo "Report written to ${OUTDIR}/index.md"
