#!/bin/sh
# Assert that a running guest is an AMD machine, whatever the host is.
#
#   scripts/check-guest-cpu.sh prboot-1 ssh -i KEY -p 2222 ubuntu@172.31.3.1
#
# Everything after the guest's name is a command that runs one shell command
# line in the guest: this script appends that line as the last argument.
#
# WHY THIS EXISTS. Under KVM QEMU gives every CPU model the HOST's CPUID
# vendor, so on an Intel host a bare `-cpu EPYC` guest is GenuineIntel family
# 23. The guest kernel allows P2PDMA between devices that share no upstream
# bridge only on an AMD CPU of family 0x17 or later (drivers/pci/p2pdma.c,
# cpu_supports_p2pdma()); q35's host bridge is not on its whitelist. So
# run-vm states the vendor (AGENTS.md, "The guest is an AMD machine"), and
# this is the check that it reached the guest. A run of this on an Intel
# host is the only verification of that path there is; the notice it prints
# says which vendor the host of a run was.
#
# Fails on: a guest vendor that is not AuthenticAMD, a family that is not 23,
# the kernel's host-bridge P2PDMA refusal in dmesg, and a dmesg that cannot
# be read.
#
# THE ASSERTION THAT COUNTS IS THE VENDOR AND THE FAMILY. The kernel prints
# its refusal only for a caller that asks verbosely, and nothing the lanes
# run is one: amdgpu's AIS and dma-buf paths ask quietly, so on a guest
# that is not AMD hipFile logs amdgpu's own "AIS: P2P DMA not accessible",
# falls back and still passes, about twelve times slower. Seen on a
# GenuineIntel-vendor guest on an AMD host, 2026-11-20. So:
#
# Warns, and does not fail, on amdgpu's "AIS: P2P DMA not accessible": what
# else makes amdgpu log it has not been established, and the vendor check
# above already fails the case this script is for.
# Warns, and does not fail, on unhandled-MSR lines: an AMD guest on an Intel
# host reads AMD MSRs the host may not emulate, and what the kernel then logs
# is what a reader of the first Intel run will want to see. A line counts
# only when it names an MSR access ("unchecked MSR", "rdmsr", "wrmsr"): the
# word "unhandled" alone matched lines that are nothing to do with one.
# Warns, and does not fail, on a kernel oops or page fault in dmesg. An
# AMD-vendor guest makes KVM hypercalls with VMMCALL, which an Intel host's
# KVM answers by writing VMCALL over it in the guest's read-only text; read
# from source, that is a page fault at the call site (AGENTS.md has when a
# guest makes one). An oops has other causes, so it is printed, not blamed.
#
# WHAT A PASS SAYS DEPENDS ON THE HOST, and the script says which. On an AMD
# host a guest reads AuthenticAMD with or without the stated vendor, so a
# pass there shows nothing about this change. It is printed as a notice, as
# the last line and as a row of the step summary ("what this run shows"),
# so that a green run on an AMD runner is not read as a pass of the Intel
# case. It is never a failure: a runner's vendor is not the lane's to pick.
#
# /bin/sh, not bash: the CI lanes here run steps under dash.
set -eu

HOST_CPUINFO=/proc/cpuinfo
if [ "${1:-}" = "--host-cpuinfo" ]; then
  HOST_CPUINFO=$2
  shift 2
fi
[ $# -ge 2 ] || {
  echo "Usage: check-guest-cpu.sh [--host-cpuinfo FILE] NAME COMMAND..." >&2
  exit 2
}
NAME=$1
shift

WANT_VENDOR=AuthenticAMD
WANT_FAMILY=23
# The host-bridge refusal, by its tail. "cannot be used for peer-to-peer DMA"
# also begins "... as it is not a PCI device", which is nothing to do with
# the CPU.
REFUSAL='do not share an upstream bridge or whitelisted host bridge'
AIS='AIS: P2P DMA not accessible'

# The value of the first "key : value" line of a cpuinfo text on stdin.
field() {
  awk -F: -v key="$1" '
    { name = $1; gsub(/[ \t]+$/, "", name) }
    name == key { value = $2; sub(/^[ \t]+/, "", value); print value; exit }'
}

guest() {
  "$@" "$GUEST_LINE"
}

# (a) The host, as this job sees it. A job container has the host's cpuinfo.
host_vendor=$(field vendor_id < "$HOST_CPUINFO")
host_model=$(field "model name" < "$HOST_CPUINFO")
echo "::notice::host CPU for ${NAME}: ${host_vendor:-unknown}, ${host_model:-unknown}"

# (b) The guest's CPU.
GUEST_LINE='cat /proc/cpuinfo'
cpuinfo=$(guest "$@") || {
  echo "::error::${NAME}: could not read the guest's /proc/cpuinfo"
  exit 1
}
guest_vendor=$(printf '%s\n' "$cpuinfo" | field vendor_id)
guest_family=$(printf '%s\n' "$cpuinfo" | field "cpu family")
guest_model=$(printf '%s\n' "$cpuinfo" | field "model name")
echo "${NAME}: vendor_id=${guest_vendor} cpu family=${guest_family} model name=${guest_model}"

rc=0
[ "$guest_vendor" = "$WANT_VENDOR" ] || {
  echo "::error::${NAME}: the guest's vendor_id is '${guest_vendor}', not ${WANT_VENDOR} (host: ${host_vendor:-unknown}). run-vm states the vendor on -cpu; see AGENTS.md, 'The guest is an AMD machine'"
  rc=1
}
[ "$guest_family" = "$WANT_FAMILY" ] || {
  echo "::error::${NAME}: the guest's cpu family is '${guest_family}', not ${WANT_FAMILY}"
  rc=1
}

# What this run can say about the stated vendor, from the two vendors.
if [ "$rc" != 0 ]; then
  shows="the guest is NOT an AMD machine of family ${WANT_FAMILY} (host: ${host_vendor:-unknown})"
elif [ "${host_vendor:-}" = "$WANT_VENDOR" ]; then
  shows="nothing about the stated vendor: the host is ${WANT_VENDOR}, where a guest reads ${WANT_VENDOR} with or without it. NOT a pass of the Intel-host case"
elif [ -z "${host_vendor:-}" ]; then
  shows="the guest's vendor only: the host's vendor could not be read, so this is not counted as a run on a host that is not AMD"
else
  shows="the stated vendor reached the guest: the host is ${host_vendor} and the guest reads ${WANT_VENDOR} family ${WANT_FAMILY}. This is the case the check is for"
fi
echo "::notice::${NAME}: this run shows ${shows}"

# (c), (d) The guest kernel's log. Not readable is a failure: an empty log
# would pass both greps.
GUEST_LINE='sudo -n dmesg'
dmesg=$(guest "$@") || {
  echo "::error::${NAME}: could not read the guest's dmesg"
  exit 1
}
[ -n "$dmesg" ] || {
  echo "::error::${NAME}: the guest's dmesg is empty"
  exit 1
}

p2p=$(printf '%s\n' "$dmesg" | grep -i -E 'p2p|peer-to-peer' || true)
echo "--- ${NAME}: dmesg lines about P2PDMA ---"
printf '%s\n' "${p2p:-(none)}"
refused=$(printf '%s\n' "$p2p" | grep -c -F "$REFUSAL" || true)
[ "$refused" = 0 ] || {
  echo "::error::${NAME}: the guest kernel refused P2PDMA ${refused} time(s): '${REFUSAL}'. It does that unless the CPU is AMD family 0x17 or later (guest: ${guest_vendor} family ${guest_family})"
  rc=1
}
ais=$(printf '%s\n' "$p2p" | grep -c -F "$AIS" || true)
[ "$ais" = 0 ] || {
  echo "::warning::${NAME}: amdgpu logged '${AIS}' ${ais} time(s): hipFile's direct path was not available and its reads fell back to the slow path (guest: ${guest_vendor} family ${guest_family})"
}

msr=$(printf '%s\n' "$dmesg" | grep -i -E 'unchecked MSR|rdmsr|wrmsr' || true)
msr_count=0
if [ -n "$msr" ]; then
  msr_count=$(printf '%s\n' "$msr" | wc -l | tr -d ' ')
  echo "--- ${NAME}: dmesg lines about unhandled MSRs ---"
  printf '%s\n' "$msr"
  echo "::warning::${NAME}: ${msr_count} unhandled-MSR line(s) in the guest's dmesg (host: ${host_vendor:-unknown}, guest: ${guest_vendor}); see the step's log"
fi

oops=$(printf '%s\n' "$dmesg" | grep -E 'BUG: unable to handle page fault|Oops: ' || true)
oops_count=0
if [ -n "$oops" ]; then
  oops_count=$(printf '%s\n' "$oops" | wc -l | tr -d ' ')
  echo "--- ${NAME}: dmesg lines about a kernel oops ---"
  printf '%s\n' "$oops"
  echo "::warning::${NAME}: ${oops_count} kernel oops line(s) in the guest's dmesg (host: ${host_vendor:-unknown}, guest: ${guest_vendor}); on an Intel host a KVM hypercall from an AMD-vendor guest is one cause, see AGENTS.md, 'The guest is an AMD machine'"
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Guest CPU: ${NAME}"
    echo
    echo "| | |"
    echo "|---|---|"
    echo "| host vendor | ${host_vendor:-unknown} |"
    echo "| host model | ${host_model:-unknown} |"
    echo "| guest vendor | ${guest_vendor} |"
    echo "| guest cpu family | ${guest_family} |"
    echo "| P2PDMA refusals in dmesg | ${refused} |"
    echo "| amdgpu 'AIS: P2P DMA not accessible' lines in dmesg | ${ais} |"
    echo "| unhandled-MSR lines in dmesg | ${msr_count} |"
    echo "| kernel oops lines in dmesg | ${oops_count} |"
    echo "| what this run shows | ${shows} |"
    echo
  } >> "$GITHUB_STEP_SUMMARY"
fi

[ "$rc" = 0 ] && echo "${NAME}: the guest is an AMD machine on a ${host_vendor:-unknown} host; this run shows ${shows}"
exit "$rc"
