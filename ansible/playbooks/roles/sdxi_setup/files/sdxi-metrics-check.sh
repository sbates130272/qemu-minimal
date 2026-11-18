#!/bin/bash
# shellcheck shell=bash
#
# Is this guest exporting the SDXI counters? Run in the guest; changes
# nothing outside a scratch directory, and needs no device.
#
# tasks/metrics.yml installs sdxi-textfile-metrics.py and a timer that runs
# it, and node_exporter's textfile collector serves the sdxi.prom it writes.
# This asks that the script is there and compiles, that the timer is enabled
# and running, that the service has finished a run since this boot and that
# run succeeded, that the file it writes is there, and then runs the script
# once itself into a scratch file: with no /dev/sdxi0 that has to be
# "sdxi_up 0", exit 0 and nothing on stderr, and with one "sdxi_up 1" and
# all 14 counters for contexts 0 and 1.
#
# The lines between the two markers are, byte for byte, the block of the
# same name in qemu/checks/rocm-golden.sh, which the lanes run on the booted
# image. tests/test_sdxi_metrics.py holds the two equal and runs them
# against a scratch tree.
#
# Prints one line and exits 0, or "ERROR: sdxi metrics: ..." on stderr and
# exits 1. SDXI_METRICS_CHECK_ROOT, for the tests, is prefixed to every
# path; SDXI_METRICS_CHECK_WAIT is how long to wait for the service's
# first run of this boot.
set -eu

# begin: sdxi metrics
_sm_root=${SDXI_METRICS_CHECK_ROOT:-}
_sm_script="${_sm_root}/usr/local/bin/sdxi-textfile-metrics.py"
_sm_written="${_sm_root}/var/lib/prometheus/node-exporter/sdxi.prom"
_sm_dev="${_sm_root}/dev/sdxi0"
_sm_timer=sdxi-textfile-metrics.timer
_sm_service=sdxi-textfile-metrics.service
_sm_tmp=''
_sm_fail() {
    echo "ERROR: sdxi metrics: $*" >&2
    [ -z "${_sm_tmp}" ] || rm -rf "${_sm_tmp}"
    exit 1
}

[ -x "${_sm_script}" ] || _sm_fail "${_sm_script} is not installed"
_sm_tmp=$(mktemp -d)
# Compiled into the scratch directory: nothing is written beside the script.
python3 -c 'import py_compile, sys
py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)' \
    "${_sm_script}" "${_sm_tmp}/compiled.pyc" 2> "${_sm_tmp}/err" ||
    _sm_fail "${_sm_script} does not compile: $(tail -n 3 "${_sm_tmp}/err")"

systemctl is-enabled --quiet "${_sm_timer}" ||
    _sm_fail "${_sm_timer} is not enabled, so nothing refreshes" \
             "${_sm_written} after a reboot"
systemctl is-active --quiet "${_sm_timer}" ||
    _sm_fail "${_sm_timer} is enabled and not running"

# The file is in the image: the bake's own run of the unit wrote it and
# nothing sweeps it, so that it is there says nothing about this boot.
# systemd does. The monotonic time the service's last run ended is 0 until a
# run has ended since boot, and the script exits 0 only once it has replaced
# the file.
_sm_ended() {
    _sm_at=$(systemctl show -P ExecMainExitTimestampMonotonic \
                 "${_sm_service}" 2> /dev/null) || _sm_at=0
    case "${_sm_at}" in '' | *[!0-9]*) _sm_at=0 ;; esac
    [ "${_sm_at}" -gt 0 ]
}
_sm_waited=0
until _sm_ended; do
    [ "${_sm_waited}" -lt "${SDXI_METRICS_CHECK_WAIT:-60}" ] ||
        _sm_fail "${_sm_timer} is running and ${_sm_service} has not" \
                 "finished a run since boot after ${_sm_waited}s:" \
                 "${_sm_written} is not from this boot"
    sleep 1
    _sm_waited=$((_sm_waited + 1))
done
_sm_result=$(systemctl show -P Result "${_sm_service}") || _sm_result=unknown
[ "${_sm_result}" = success ] ||
    _sm_fail "${_sm_service} last ended with result '${_sm_result}':" \
             "journalctl -b -u ${_sm_service} says why"
# Only that it is there and whole: its sdxi_up is from a run up to one
# interval old, which on a guest still booting may predate the device.
[ -s "${_sm_written}" ] ||
    _sm_fail "${_sm_service} ran and ${_sm_written} is not there"
grep -qxE 'sdxi_up [01]' "${_sm_written}" ||
    _sm_fail "${_sm_written} has no sdxi_up line"

# One run of its own, into the scratch directory, by the script's own
# interpreter line as the unit runs it.
_sm_out="${_sm_tmp}/sdxi.prom"
"${_sm_script}" --device "${_sm_dev}" --output "${_sm_out}" \
    2> "${_sm_tmp}/err" ||
    _sm_fail "${_sm_script} exited $?: $(tail -n 3 "${_sm_tmp}/err")"
if [ -e "${_sm_dev}" ]; then
    grep -qx 'sdxi_up 1' "${_sm_out}" ||
        _sm_fail "${_sm_dev} is there and the script could not read it:" \
                 "$(tail -n 3 "${_sm_tmp}/err")"
    for _sm_stat in bytes_moved num_copy num_errors num_intr num_cxt_start \
                    num_cxt_stop num_nop num_sync bytes_compressed \
                    bytes_decompressed num_compress num_decompress \
                    num_elemwise bytes_elemwise; do
        for _sm_cid in 0 1; do
            grep -qxE "sdxi_${_sm_stat}_total\{context=\"${_sm_cid}\"\} [0-9]+" \
                "${_sm_out}" ||
                _sm_fail "no sdxi_${_sm_stat}_total for context ${_sm_cid}:" \
                         "the dashboard's SDXI row reads these names"
        done
    done
    echo "sdxi metrics: ${_sm_dev} read, 14 counters each for contexts 0" \
         "and 1; ${_sm_timer} enabled"
else
    grep -qx 'sdxi_up 0' "${_sm_out}" ||
        _sm_fail "no ${_sm_dev} and the script did not write sdxi_up 0"
    if grep -q '_total' "${_sm_out}"; then
        _sm_fail "no ${_sm_dev} and the script wrote counter series"
    fi
    [ ! -s "${_sm_tmp}/err" ] ||
        _sm_fail "no ${_sm_dev}, which is a normal guest, and the script" \
                 "complained: $(tail -n 3 "${_sm_tmp}/err")"
    echo "sdxi metrics: no ${_sm_dev}; sdxi_up 0, exit 0 and silent;" \
         "${_sm_timer} enabled"
fi
rm -rf "${_sm_tmp}"
_sm_tmp=''
# end: sdxi metrics
