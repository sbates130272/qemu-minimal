---
title: VM Report — hipFile fio VM
---

# VM Report — qemu-minimal — hipFile fio VM

Generated: **2026-09-26 00:39 UTC** &middot; Commit: [`fa6d855`](https://github.com/sbates130272/qemu-minimal/commit/fa6d8556572e6a71cc99a193f1624f7c529c4a20)

## Hardware

| Resource | Value |
|---|---|
| CPU | AMD EPYC Processor |
| vCPUs | 4 |
| Threads/core | 1 |
| RAM | 3.8Gi total, 2.7Gi free |
| Swap | 0B |

## Kernel

```
7.2.4-070204-generic
```

<details><summary>Full version string</summary>

```
Linux version 7.2.4-070204-generic (kernel@balboa) (x86_64-linux-gnu-gcc-15 (Ubuntu 15.2.0-12ubuntu1) 15.2.0, GNU ld (GNU Binutils for Ubuntu) 2.46) #202609072054 SMP PREEMPT_DYNAMIC Thu Sep 10 15:53:25 UTC 2026
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
/dev/vda1        61G   11G   50G  18% /
/dev/vda13      989M  371M  551M  41% /boot
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


## hipFile fio Benchmark

| Metric | Value |
|---|---|
| fio libhipfile read throughput | 10.6 MB/s |

This is fio's own `libhipfile` ioengine reading from the guest NVMe straight
into VRAM, rather than the hand-written benchmark the rocjitsu report uses. fio
is built in the guest from a pinned master commit, because the engine is not in
any fio release tag. Same caveat as the other lanes: the GPU is emulated over
vfio-user, so watch the trend between runs rather than the absolute number.

### fio output

```
rocm: /opt/rocm/core-10.0
building fio 6bc57a931f04fa3f50348d8c8f087187f050c6e1
FIO_VERSION = fio-3.42
fio: fio-3.42 commit=6bc57a931f04fa3f50348d8c8f087187f050c6e1
fio-hipfile-bench: read_bytes=67108864 seconds=6.356 read_gbs=0.010558348
```
