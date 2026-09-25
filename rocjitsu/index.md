---
title: VM Report — rocjitsu VM
---

# VM Report — qemu-minimal — rocjitsu VM

Generated: **2026-09-24 23:44 UTC** &middot; Commit: [`e73a1e6`](https://github.com/sbates130272/qemu-minimal/commit/e73a1e636fecf2258968a01225c7425fb98d6aa2)

## Hardware

| Resource | Value |
|---|---|
| CPU | AMD EPYC Processor |
| vCPUs | 4 |
| Threads/core | 1 |
| RAM | 3.8Gi total, 2.8Gi free |
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
/dev/vda13      989M  285M  638M  31% /boot
```

### NVMe

```
Node                  Generic               SN                   Model                                    Namespace  Usage                      Format           FW Rev  
--------------------- --------------------- -------------------- ---------------------------------------- ---------- -------------------------- ---------------- --------
/dev/nvme0n1          /dev/ng0n1            ocjitsu-report-nvme1 QEMU NVMe Ctrl                           0x1          1.10  TB /   1.10  TB    512   B +  0 B   11.1.1  
```

## PCI devices

```
00:00.0 Host bridge [0600]: Intel Corporation 82G33/G31/P35/P31 Express DRAM Controller [8086:29c0]
00:01.0 VGA compatible controller [0300]: Device [1234:1111] (rev 02)
00:02.0 Non-Volatile memory controller [0108]: Red Hat, Inc. QEMU NVM Express Controller [1b36:0010] (rev 02)
00:03.0 Ethernet controller [0200]: Red Hat, Inc. Virtio network device [1af4:1000]
00:04.0 Communication controller [0780]: Red Hat, Inc. Virtio console [1af4:1003]
00:05.0 SCSI storage controller [0100]: Red Hat, Inc. Virtio block device [1af4:1001]
00:06.0 Processing accelerators [1200]: Advanced Micro Devices, Inc. [AMD/ATI] Device [1002:75c1]
00:1f.0 ISA bridge [0601]: Intel Corporation 82801IB (ICH9) LPC Interface Controller [8086:2918] (rev 02)
00:1f.2 SATA controller [0106]: Intel Corporation 82801IR/IO/IH (ICH9R/DO/DH) 6 port SATA Controller [AHCI mode] [8086:2922] (rev 02)
00:1f.3 SMBus [0c05]: Intel Corporation 82801I (ICH9 Family) SMBus Controller [8086:2930] (rev 02)
```

## Debian Packages

### nvme-cli

```
nvme-cli	2.16-1
```

## ROCm

### Installed Debian packages

```
amdrocm-amdsmi	10.0.0-4
amdrocm-amdsmi10.0	10.0.0-4
amdrocm-base10.0	10.0.0-4
amdrocm-hipfile-dev	10.0.0-4
amdrocm-hipfile-dev10.0	10.0.0-4
amdrocm-hipfile10.0	10.0.0-4
amdrocm-llvm-dev10.0	10.0.0-4
amdrocm-llvm10.0	10.0.0-4
amdrocm-runtime-dev	10.0.0-4
amdrocm-runtime-dev10.0	10.0.0-4
amdrocm-runtime10.0	10.0.0-4
amdrocm-sysdeps10.0	10.0.0-4
```

### /opt/rocm/bin

```

```

### hipFile Debian packages

```
amdrocm-hipfile-dev	10.0.0-4
amdrocm-hipfile-dev10.0	10.0.0-4
amdrocm-hipfile10.0	10.0.0-4
```

## AMDGPU Debian Packages

```
amdgpu-dkms	1:7.1.9.31600000-2403767.26.04
amdgpu-dkms-firmware	1:31.60.0.0.31600000-2403767.26.04
amdgpu-exporter	1.5.2-10~24.04
libdrm-amdgpu1:amd64	2.4.131-1
```

## APT Sources

```
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
  chrony.service              loaded active running chrony, an NTP client/server
  cron.service                loaded active running Regular background program processing daemon
  dbus.service                loaded active running D-Bus System Message Bus
  getty@tty1.service          loaded active running Getty on tty1
  ModemManager.service        loaded active running Modem Manager
  multipathd.service          loaded active running Device-Mapper Multipath Device Controller
  networkd-dispatcher.service loaded active running Dispatcher daemon for systemd-networkd
  polkit.service              loaded active running Authorization Manager
  qemu-guest-agent.service    loaded active running QEMU Guest Agent
  rsyslog.service             loaded active running System Logging Service
  serial-getty@ttyS0.service  loaded active running Serial Getty on ttyS0
  ssh.service                 loaded active running OpenBSD Secure Shell server
  systemd-journald.service    loaded active running Journal Service
  systemd-logind.service      loaded active running User Login Management
  systemd-networkd.service    loaded active running Network Management
  systemd-resolved.service    loaded active running Network Name Resolution
  systemd-udevd.service       loaded active running Rule-based Manager for Device Events and Files
  udisks2.service             loaded active running Disk Manager
  unattended-upgrades.service loaded active running Unattended Upgrades Shutdown
  user@1000.service           loaded active running User Manager for UID 1000
```

## System Health

### Journal errors (this boot)

```
-- No entries --
```

## rocjitsu Benchmarks

| Metric | Value |
|---|---|
| GEMM | 823 kFLOP/s |
| hipFile read throughput | 123 MB/s |

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
sgemm-bench: seconds_per_iter=5.09669912 gflops=0.000822945185
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
gemm-hipfile-bench: read_bytes=33554432 seconds_per_pass=0.272470638 read_gbs=0.1231488
gemm-hipfile-bench: m=128 n=128 k=128
gemm-hipfile-bench: max_absolute_error=0 failures=0
gemm-hipfile-bench: gemm_seconds_per_iter=5.14463355 gflops=0.000815277504
gemm-hipfile-bench: PASS
```

