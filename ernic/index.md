---
title: VM Report — VM 1
---

# VM Report — qemu-minimal — VM 1

Generated: **2026-10-02 14:42 UTC** &middot; Commit: [`b7b730b`](https://github.com/sbates130272/qemu-minimal/commit/b7b730b74e9acd51993aa1eb2adc6e124f1ddee4) &middot; Status: **pass**

## Hardware

| Resource | Value |
|---|---|
| CPU | AMD EPYC Processor |
| vCPUs | 4 |
| Threads/core | 1 |
| RAM | 5.6Gi total, 5.1Gi free |
| Swap | 0B |

## Kernel

```
7.0.0-38-generic
```

<details><summary>Full version string</summary>

```
Linux version 7.0.0-38-generic (buildd@lcy02-amd64-070) (x86_64-linux-gnu-gcc (Ubuntu 15.2.0-16ubuntu1) 15.2.0, GNU ld (GNU Binutils for Ubuntu) 2.46) #38-Ubuntu SMP PREEMPT_DYNAMIC Fri Sep  4 09:10:14 UTC 2026
```

</details>

## Storage

```
NAME     SIZE TYPE MOUNTPOINT
sr0     1024M rom  
vda        8G disk 
├─vda1   6.9G part /
├─vda13 1023M part /boot
├─vda14    4M part 
└─vda15  106M part /boot/efi
```

### Disk usage

```
Filesystem      Size  Used Avail Use% Mounted on
/dev/vda1       6.7G  4.9G  1.8G  74% /
/dev/vda13      989M  183M  739M  20% /boot
```

### NVMe

```
Node                  Generic               SN                   Model                                    Namespace  Usage                      Format           FW Rev  
--------------------- --------------------- -------------------- ---------------------------------------- ---------- -------------------------- ---------------- --------
```

## PCI devices

```
00:00.0 Host bridge [0600]: Intel Corporation 82G33/G31/P35/P31 Express DRAM Controller [8086:29c0]
00:01.0 VGA compatible controller [0300]: Device [1234:1111] (rev 02)
00:02.0 Ethernet controller [0200]: Red Hat, Inc. Virtio network device [1af4:1000]
00:03.0 Communication controller [0780]: Red Hat, Inc. Virtio console [1af4:1003]
00:04.0 SCSI storage controller [0100]: Red Hat, Inc. Virtio block device [1af4:1001]
00:05.0 Ethernet controller [0200]: AMD Pensando Systems DSC Serial Port Controller [1dd8:100a]
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

```

### /opt/rocm/bin

```

```

### hipFile Debian packages

```

```

## AMDGPU Debian Packages

```
libdrm-amdgpu1:amd64	2.4.131-1
```

## APT Sources

```
ubuntu.sources
```

## User Groups

```
ubuntu : ubuntu users admin
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
  systemd-timedated.service   loaded active running Time & Date Service
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


