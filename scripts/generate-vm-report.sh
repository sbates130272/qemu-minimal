#!/usr/bin/env bash
# Generate a markdown VM inspection report from a running QEMU VM.
# Usage: generate-vm-report.sh <output-dir> [ssh-port] [ssh-user]
set -euxo pipefail

OUTDIR=${1:-site}
PORT=${2:-2222}
USER=${3:-ubuntu}
SSH="ssh -o NoHostAuthenticationForLocalhost=yes -o StrictHostKeyChecking=no -p ${PORT} ${USER}@localhost"

TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M UTC')
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
REPO="sbates130272/qemu-minimal"

collect() { $SSH "$1" 2>/dev/null || echo "(not available)"; }

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
GROUPS=$(collect "groups ${USER}")
SERVICES=$(collect "systemctl list-units --type=service --state=running --no-pager --no-legend")
SOURCES=$(collect "ls /etc/apt/sources.list.d/")
JOURNAL=$(collect "journalctl -p err -b --no-pager 2>/dev/null | tail -5")

mkdir -p "${OUTDIR}"

cat > "${OUTDIR}/_config.yml" <<'CFG'
theme: minima
title: qemu-minimal VM Report
description: Live VM inspection report for the qemu-minimal project
CFG

cat > "${OUTDIR}/index.md" <<MD
---
title: VM Report
---

# VM Report — qemu-minimal

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
${GROUPS}
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
MD

echo "Report written to ${OUTDIR}/index.md"
