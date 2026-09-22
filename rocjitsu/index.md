---
title: VM Report — rocjitsu VM
---

# VM Report — qemu-minimal — rocjitsu VM

Generated: **2026-09-22 21:33 UTC** &middot; Commit: [`13c504e`](https://github.com/sbates130272/qemu-minimal/commit/13c504e99774e4bb63653c769898ad79b5325e2a)

## Hardware

| Resource | Value |
|---|---|
| CPU | AMD EPYC Processor |
| vCPUs | 4 |
| Threads/core | 1 |
| RAM | 3.8Gi total, 3.1Gi free |
| Swap | 0B |

## Kernel

```
7.0.0-34-generic
```

<details><summary>Full version string</summary>

```
Linux version 7.0.0-34-generic (buildd@lcy02-amd64-082) (x86_64-linux-gnu-gcc (Ubuntu 15.2.0-16ubuntu1) 15.2.0, GNU ld (GNU Binutils for Ubuntu) 2.46) #34-Ubuntu SMP PREEMPT_DYNAMIC Wed Sep  2 14:29:37 UTC 2026
```

</details>

## Storage

```
NAME     SIZE TYPE MOUNTPOINT
sr0     1024M rom  
vda       64G disk 
├─vda1  62.9G part /
├─vda13 1023M part /boot
├─vda14    4M part 
└─vda15  106M part /boot/efi
nvme0n1    1T disk 
```

### Disk usage

```
Filesystem      Size  Used Avail Use% Mounted on
/dev/vda1        61G   11G   51G  18% /
/dev/vda13      989M  387M  536M  42% /boot
```

### NVMe

```
nvme-cli unavailable
```

## ROCm

### Installed packages (sample)

```
amdrocm-base10.0 10.0.0-4
amdrocm-blas-dev 10.0.0-4
amdrocm-blas-dev10.0 10.0.0-4
amdrocm-hipfile-dev 10.0.0-4
amdrocm-hipfile-dev10.0 10.0.0-4
amdrocm-hipfile10.0 10.0.0-4
amdrocm-llvm-dev10.0 10.0.0-4
amdrocm-llvm10.0 10.0.0-4
```

### /opt/rocm/bin

```

```

### hipFile

```
ii  amdrocm-hipfile-dev                              10.0.0-4                                   amd64        AMD ROCm hipFile library development files
ii  amdrocm-hipfile-dev10.0                          10.0.0-4                                   amd64        AMD ROCm hipFile library development files
ii  amdrocm-hipfile10.0                              10.0.0-4                                   amd64        AMD ROCm hipFile library
```

## AMDGPU Kernel Driver

```
ii  amdgpu-dkms                                      1:7.1.9.31600000-2403767.26.04             all          amdgpu driver in DKMS format.
ii  amdgpu-dkms-firmware                             1:31.60.0.0.31600000-2403767.26.04         all          firmware blobs used by amdgpu driver in DKMS format
ii  amdgpu-exporter                                  1.5.2-10~24.04                             amd64        AMD GPU Metrics Exporter for Ubuntu
ii  libdrm-amdgpu1:amd64                             2.4.131-1                                  amd64        Userspace interface to amdgpu-specific kernel DRM services -- runtime
```

## APT Sources

```
device-metrics-exporter.sources
hashicorp.list
rocm.sources
ubuntu.sources
```

## User Groups

```
ubuntu : ubuntu video users render admin
```

## Running Services

```
  amd-metrics-exporter.service     loaded active running AMD GPU Prometheus Exporter Service
  chrony.service                   loaded active running chrony, an NTP client/server
  cron.service                     loaded active running Regular background program processing daemon
  dbus.service                     loaded active running D-Bus System Message Bus
  getty@tty1.service               loaded active running Getty on tty1
  ModemManager.service             loaded active running Modem Manager
  multipathd.service               loaded active running Device-Mapper Multipath Device Controller
  networkd-dispatcher.service      loaded active running Dispatcher daemon for systemd-networkd
  polkit.service                   loaded active running Authorization Manager
  prometheus-node-exporter.service loaded active running Prometheus exporter for machine metrics
  qemu-guest-agent.service         loaded active running QEMU Guest Agent
  rsyslog.service                  loaded active running System Logging Service
  serial-getty@ttyS0.service       loaded active running Serial Getty on ttyS0
  ssh.service                      loaded active running OpenBSD Secure Shell server
  systemd-journald.service         loaded active running Journal Service
  systemd-logind.service           loaded active running User Login Management
  systemd-networkd.service         loaded active running Network Management
  systemd-resolved.service         loaded active running Network Name Resolution
  systemd-timedated.service        loaded active running Time & Date Service
  systemd-udevd.service            loaded active running Rule-based Manager for Device Events and Files
  udisks2.service                  loaded active running Disk Manager
  unattended-upgrades.service      loaded active running Unattended Upgrades Shutdown
  user@1000.service                loaded active running User Manager for UID 1000
```

## System Health

### Journal errors (this boot)

```
-- No entries --
```

## rocjitsu Benchmarks

| Metric | Value |
|---|---|
| GEMM | 500 kFLOP/s |
| hipFile read throughput | 195 MB/s |

These run inside a QEMU guest with the GPU attached over vfio-user, so the
absolute numbers sit far below what the same benchmark reports on bare metal —
that is expected, not a regression. What is worth watching is how they move
between runs. The unit is chosen from the value, so compare the unit too.

### GEMM output

```
hipcc: /opt/rocm/core-10.0/bin/hipcc
sgemm-bench: device=AMD Radeon Graphics arch=gfx1250
sgemm-bench: private_segment_bytes=0
sgemm-bench: m=128 n=128 k=128
sgemm-bench: max_absolute_error=0 failures=0
sgemm-bench: seconds_per_iter=8.3824522 gflops=0.000500367183
sgemm-bench: PASS
```

### hipFile + NVMe output

```
hipcc: /opt/rocm/core-10.0/bin/hipcc
rocm: /opt/rocm/core-10.0
gemm-hipfile-bench: device=AMD Radeon Graphics arch=gfx1250
gemm-hipfile-bench: path=/mnt/nvme/gemm-hipfile-bench.bin block_bytes=1048576 blocks=32 iters=4
gemm-hipfile-bench: private_segment_bytes=0
gemm-hipfile-bench: compat_requested=self buf_registered=yes
gemm-hipfile-bench: read_bytes=33554432 seconds_per_pass=0.171716212 read_gbs=0.195406314
gemm-hipfile-bench: m=128 n=128 k=128
gemm-hipfile-bench: max_absolute_error=0 failures=0
gemm-hipfile-bench: gemm_seconds_per_iter=8.42847813 gflops=0.000497634797
gemm-hipfile-bench: PASS
```
