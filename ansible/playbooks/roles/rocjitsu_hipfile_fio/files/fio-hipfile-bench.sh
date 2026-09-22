#!/usr/bin/env bash
# Build fio with the libhipfile ioengine and measure NVMe->VRAM read bandwidth.
#
# Runs in the guest. Prints one metric line in the same shape as the .hip
# benches so generate-vm-report.sh can scrape it the same way:
#
#   fio-hipfile-bench: read_bytes=<n> seconds=<f> read_gbs=<f>
#
# Missing prerequisites print a reason and exit 0. The caller turns "no
# read_gbs" into a hard failure, so a silent skip still fails the lane loudly
# rather than publishing a stale badge.
set -euo pipefail

# libhipfile landed on fio master (67256d4e, 2026-05-08) and is in no release
# tag, so this has to be a master commit. Kept in step with FIO_COMMIT in
# batesste-ci-images/ubuntu-cuda-rocm-fio/Dockerfile: the point of this lane is
# to measure the same engine that image ships.
FIO_COMMIT=${FIO_COMMIT:-6bc57a931f04fa3f50348d8c8f087187f050c6e1}
FIO_REPO=${FIO_REPO:-https://github.com/axboe/fio.git}
PREFIX=${PREFIX:-/opt/fio-hipfile}
MNT=${MNT:-/mnt/nvme}
SIZE_MB=${SIZE_MB:-64}
BS=${BS:-1M}

gpu_nodes=$(cat /sys/class/kfd/kfd/topology/nodes/*/name 2>/dev/null | grep -c . || true)
[ "${gpu_nodes}" -ge 1 ] || { echo "No bound GPU KFD node"; exit 0; }

# || true for the same reason as the hip benches: with no matching tree the
# loop's last test sets the exit status and pipefail carries it out.
# TheRock ships the header flat at include/hipfile.h; other ROCm layouts nest
# it under include/hipfile/. Accept either.
rocm=$(for d in /opt/rocm /opt/rocm-* /opt/rocm/*; do
  { [ -e "${d}/include/hipfile/hipfile.h" ] || [ -e "${d}/include/hipfile.h" ]; } \
    && [ -e "${d}/lib/libhipfile.so" ] && echo "${d}"
done | head -1 || true)
[ -n "${rocm}" ] || { echo "hipFile headers or library unavailable"; exit 0; }
export ROCM_PATH="${rocm}"

dev=$(lsblk -dpno NAME,TYPE | awk '$2=="disk" && $1 ~ /^\/dev\/nvme[0-9]+n[0-9]+$/ { print $1; exit }')
[ -n "${dev}" ] || { echo "No NVMe namespace present"; exit 0; }
sudo -n mkdir -p "${MNT}"
if ! mountpoint -q "${MNT}"; then
  fstype=$(sudo -n blkid -o value -s TYPE "${dev}" 2>/dev/null || true)
  if [ -z "${fstype}" ]; then
    sudo -n mkfs.ext4 -q -F "${dev}"
  fi
  sudo -n mount -o noatime "${dev}" "${MNT}"
fi
sudo -n chmod 1777 "${MNT}"

echo "rocm: ${rocm}"

# Rebuilt only when absent or built from a different commit, so a baked image
# or a second report pass in the same guest skips straight to the run.
stamp="${PREFIX}/share/fio-commit.txt"
if [ ! -x "${PREFIX}/bin/fio" ] || [ "$(cat "${stamp}" 2>/dev/null || true)" != "${FIO_COMMIT}" ]; then
  echo "building fio ${FIO_COMMIT}"
  rm -rf /tmp/fio-build
  git init -q /tmp/fio-build
  git -C /tmp/fio-build remote add origin "${FIO_REPO}"
  git -C /tmp/fio-build fetch -q --depth 1 origin "${FIO_COMMIT}"
  git -C /tmp/fio-build checkout -q FETCH_HEAD
  # --enable-libhipfile so the probe hard-fails rather than silently dropping
  # the engine and leaving us benchmarking nothing. --disable-native because
  # fio's configure turns on -march=native by itself, and this binary may be
  # reused on a guest that CI sized differently. No CUDA here, unlike the
  # published image: without --enable-cuda the binary carries no libcuda.so.1
  # or libcufile.so.0 DT_NEEDED, so there are no NVIDIA stubs to stage.
  (
    cd /tmp/fio-build
    ./configure --disable-native --enable-libhipfile >/dev/null
    make -j"$(nproc)" >/dev/null
    sudo -n make install "prefix=${PREFIX}" >/dev/null
  )
  sudo -n mkdir -p "${PREFIX}/share"
  echo "${FIO_COMMIT}" | sudo -n tee "${stamp}" >/dev/null
fi

fio="${PREFIX}/bin/fio"
"${fio}" --enghelp 2>/dev/null | grep -qE '^[[:space:]]*libhipfile$' \
  || { echo "fio built without the libhipfile engine"; exit 0; }
echo "fio: $("${fio}" --version) commit=${FIO_COMMIT}"

# Written with real data rather than fallocate'd. A fallocate'd file is all
# unwritten extents, so the reads are served as holes without ever reaching the
# device and the run reports memory bandwidth -- which looks like a spectacular
# result and measures nothing.
target="${MNT}/fio-hipfile-bench.dat"
dd if=/dev/urandom "of=${target}" bs=1M "count=${SIZE_MB}" oflag=direct status=none
sync
echo 3 | sudo -n tee /proc/sys/vm/drop_caches >/dev/null

sudo -n "${fio}" \
  --name=hipfile-read \
  --ioengine=libhipfile \
  --rocm_io=hipfile \
  --filename="${target}" \
  --rw=read \
  --bs="${BS}" \
  --size="${SIZE_MB}M" \
  --iodepth=1 \
  --direct=1 \
  --group_reporting \
  --output-format=json \
  --output=/tmp/fio-hipfile.json >/dev/null

python3 - /tmp/fio-hipfile.json <<'PY'
import json, sys

with open(sys.argv[1]) as fh:
    job = json.load(fh)["jobs"][0]
read = job["read"]
# bw_bytes is already B/s over the job's own runtime; deriving it from
# io_bytes/runtime instead would fold in ramp and teardown.
gbs = read["bw_bytes"] / 1e9
seconds = read["runtime"] / 1000.0
print("fio-hipfile-bench: read_bytes=%d seconds=%.9g read_gbs=%.9g"
      % (read["io_bytes"], seconds, gbs))
PY
