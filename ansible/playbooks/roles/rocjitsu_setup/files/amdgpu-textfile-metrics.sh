#!/bin/sh
# Write amdgpu presence metrics for node_exporter's textfile collector.
out=/var/lib/prometheus/node-exporter/amdgpu.prom
kfd=0; [ -e /dev/kfd ] && kfd=1
gpus=$(find /sys/bus/pci/drivers/amdgpu/ -maxdepth 1 \
            -name '????:??:??.?' 2>/dev/null | wc -l)
cat > "${out}.tmp" << EOF
# HELP amdgpu_kfd_present Whether /dev/kfd is present (1=yes, 0=no).
# TYPE amdgpu_kfd_present gauge
amdgpu_kfd_present ${kfd}
# HELP amdgpu_devices_total Number of amdgpu DRM render devices.
# TYPE amdgpu_devices_total gauge
amdgpu_devices_total ${gpus}
EOF
mv "${out}.tmp" "${out}"
