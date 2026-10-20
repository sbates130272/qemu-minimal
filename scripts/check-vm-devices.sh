#!/bin/sh
# Assert that `qemu-tool list` and `qemu-tool inspect` report the devices a
# running guest actually has.
#
#   scripts/check-vm-devices.sh --gpu gfx1250 --login ubuntu quickstart-1
#
# WHY THIS EXISTS. Every field these two commands report degrades to a
# plausible-looking placeholder when its source stops answering: the NVME
# column becomes "-", the Disk and GPU lines vanish, the Key line falls back
# to the configured default labelled "unverified", and Login reads "none" or
# "unknown". None of that is an error and none of it changes the exit status,
# so a CI lane that only runs the commands stays green through a total
# regression. Every check below therefore demands the POSITIVE value.
#
# It reads --json rather than the rendered lines. The human output is prose
# and gets reworded -- the Login line gained an elapsed time mid-review while
# this was being written, with the JSON byte-identical across the change -- so
# grepping it would train people to ignore this script. The one exception is
# the NVME column itself, which only exists in the table: that is read by
# field index off the header, never by character offset, because the columns
# are width-padded to their contents.
#
# /bin/sh, not bash: the CI lanes here run steps under dash.
set -eu

GPU=""
NVME_MODE=emul
STRICT_KEY=0
LOGIN_USER=""
ENV_ARGS=""
VM_NAME=""

usage() {
  cat <<'EOF'
Usage: check-vm-devices.sh [options] VM_NAME

  --env-file FILE  settings file passed through to qemu-tool (repeatable)
  --gpu GFX        require a GPU reporting this gfx version
  --nvme-bdf       require a guest BDF for the NVMe, not "emul". Only
                   possible when the VM was started with --qmp-socket:
                   without QMP the guest address is unknowable and "emul"
                   is the correct answer
  --strict-key     also require that the key the guest accepts was found
                   locally, so the Key line names a path
  --login USER     require a logged-in session for USER. The caller must
                   hold one open: these guests have no utmp, so the value
                   comes from logind session files and is only non-empty
                   while a session exists
  -h, --help       this
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_ARGS="${ENV_ARGS} --env-file $2"; shift 2 ;;
    --gpu) GPU=$2; shift 2 ;;
    --nvme-bdf) NVME_MODE=bdf; shift ;;
    --strict-key) STRICT_KEY=1; shift ;;
    --login) LOGIN_USER=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) VM_NAME=$1; shift ;;
  esac
done

[ -n "$VM_NAME" ] || { echo "error: no VM name given" >&2; usage >&2; exit 2; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

qt_inspect() {
  # Unquoted on purpose: an empty ENV_ARGS must expand to no argument at all.
  # shellcheck disable=SC2086
  qemu-tool inspect $ENV_ARGS --vm-name "$VM_NAME" "$@"
}

qemu-tool list --json > "${WORK}/list.json"
qemu-tool list        > "${WORK}/list.txt"
qt_inspect --json     > "${WORK}/inspect.json"
qt_inspect            > "${WORK}/inspect.txt"

# Written out rather than passed to -c so the failure messages can be written
# as prose. Run under python3 -I: the checker imports stdlib only and must not
# pick anything up from the directory it was written into.
cat > "${WORK}/check.py" <<'PYEOF'
import json
import re
import sys

name, list_json, list_txt, inspect_json = sys.argv[1:5]
want_gpu, nvme_mode, strict_key, want_login = sys.argv[5:9]

with open(list_json) as f:
    listing = json.load(f)
with open(inspect_json) as f:
    rec = json.load(f)
with open(list_txt) as f:
    table = f.read().splitlines()

fail = []


def check(ok, message):
    if not ok:
        fail.append(message)


row = next((v for v in listing if v.get("name") == name), None)
if row is None:
    names = [v.get("name") for v in listing]
    # The usual cause is a job container that does not share the host PID
    # namespace: list enumerates /proc, so sibling containers' QEMU processes
    # are invisible and the table comes back empty with exit status 0.
    sys.exit(f"{name} is not in `qemu-tool list` (saw {names}). "
             "If this ran from a container, it needs --pid=host.")

# The NVME column, read by field index off the header: _print_table pads every
# column to its widest cell, so character offsets move with the data. No cell
# contains a space, so the split is unambiguous.
header = next((l for l in table if l.split()[:2] == ["PID", "NAME"]), None)
check(header is not None, "`qemu-tool list` printed no header row")
if header is not None:
    cols = header.split()
    check("NVME" in cols, f"no NVME column in the table header: {cols}")
    if "NVME" in cols:
        idx = cols.index("NVME")
        line = next((l for l in table if l.split()[1:2] == [name]), None)
        check(line is not None, f"no table row for {name}")
        if line is not None:
            cell = line.split()[idx]
            check(cell != "-",
                  f"the NVME column reads '-' for {name}, which has VM_NVME set")
            if nvme_mode == "bdf":
                check(re.fullmatch(r"[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]", cell) is not None,
                      f"the NVME column reads {cell!r}, wanted a guest BDF "
                      "(the VM was started with --qmp-socket, so the address "
                      "is knowable)")

nvme = row.get("nvme") or []
check(len(nvme) >= 1, f"list reports no NVMe devices for {name}")
for d in nvme:
    check(d.get("kind") in ("emulated", "passthrough"),
          f"NVMe of unresolved kind {d.get('kind')!r}")
if nvme_mode == "bdf":
    check(row.get("qmp_available") is True,
          "qmp_available is false, so no guest BDF can be read")
    for d in nvme:
        check(d.get("source") == "qmp",
              f"NVMe address came from {d.get('source')!r}, not from QMP")
        check((d.get("bdf") or "").startswith("0000:"),
              f"NVMe bdf is {d.get('bdf')!r}, wanted a qualified guest address")

# None, not [], is the degraded value: the guest agent could not be asked, and
# inspect prints no Disk line at all rather than a wrong one.
disks = rec.get("block_devices")
check(disks is not None,
      "block_devices is null: the guest agent did not answer, so no Disk "
      "line was printed")
if disks is not None:
    nvme_disks = [d for d in disks if d.get("bus") == "nvme"]
    check(len(nvme_disks) >= 1,
          f"no block device on the nvme bus; saw {[d.get('dev') for d in disks]}")
    for d in nvme_disks:
        check((d.get("bytes") or 0) > 0,
              f"{d.get('dev')} reports a capacity of {d.get('bytes')!r}")
        check(d.get("origin") in ("emulated", "passthrough"),
              f"{d.get('dev')} has origin {d.get('origin')!r}")

if want_gpu:
    gpus = rec.get("gpus") or []
    check(len(gpus) >= 1, "inspect reports no GPU")
    for g in gpus:
        check(g.get("gfx") == want_gpu,
              f"GPU reports gfx {g.get('gfx')!r}, wanted {want_gpu!r}")
        check(g.get("origin") in ("emulated", "passthrough"),
              f"GPU has origin {g.get('origin')!r}")
        check(bool(g.get("source")), "GPU names no source device")

# The strongest check available, and the one the shipped bug was in: a guest
# that cannot be asked still yields a Key line, built from the configured
# default. Only ssh_key_source == "guest" means the fingerprint came from the
# guest's own authorized_keys.
check(rec.get("ssh_key_source") == "guest",
      f"ssh_key_source is {rec.get('ssh_key_source')!r}, not 'guest': the key "
      "was not verified against the guest")
keys = rec.get("ssh_keys") or []
check(len(keys) >= 1, "inspect reports no SSH key")
accepted = [k for k in keys if k.get("accepted") is True]
check(len(accepted) >= 1,
      f"no key marked accepted by the guest: {keys}")
if strict_key == "1" and accepted:
    # accepted alone still admits "(accepted by guest, not found locally)",
    # where path is null and the printed line names no file.
    check(any(k.get("path") for k in accepted),
          "the accepted key was not found locally, so the Key line names no "
          f"path: {accepted}")

sessions = rec.get("ssh_sessions")
if want_login:
    check(sessions is not None,
          "ssh_sessions is null: neither utmp nor logind could be read")
    check(bool(sessions), "ssh_sessions is empty, so Login reads 'none' -- "
          "the caller was supposed to be holding a session open")
    for s in sessions or []:
        check(isinstance(s.get("login_time"), (int, float)),
              f"session login_time is {s.get('login_time')!r}")
    check(any(s.get("user") == want_login for s in sessions or []),
          f"no session for {want_login!r}; saw "
          f"{[s.get('user') for s in sessions or []]}")
else:
    # Weaker on purpose: with no session held open the honest answer is an
    # empty list, which is a pass. All this separates is "logind answered"
    # (a list, Login reads a user or "none") from "nothing answered" (null,
    # Login reads "unknown"). Pass --login to make it mean something.
    check(sessions is not None,
          "ssh_sessions is null: Login would read 'unknown'")

if fail:
    for f in fail:
        print(f"FAIL: {f}")
    sys.exit(1)
print(f"{name}: device reporting checks passed")
PYEOF

if python3 -I "${WORK}/check.py" "$VM_NAME" \
     "${WORK}/list.json" "${WORK}/list.txt" "${WORK}/inspect.json" \
     "$GPU" "$NVME_MODE" "$STRICT_KEY" "$LOGIN_USER"; then
  exit 0
fi

# The whole point of printing these: a CI failure nobody can diagnose from the
# log gets rerun, not fixed.
echo
echo "--- qemu-tool list ---"
cat "${WORK}/list.txt"
echo
echo "--- qemu-tool inspect --vm-name ${VM_NAME} ---"
cat "${WORK}/inspect.txt"
echo
echo "--- qemu-tool inspect --vm-name ${VM_NAME} --json ---"
cat "${WORK}/inspect.json"
exit 1
