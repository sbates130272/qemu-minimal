---
title: VM Report
---

# VM Report — qemu-minimal

Generated: **2026-09-30 14:48 UTC** &middot; Commit: [`86cb532`](https://github.com/sbates130272/qemu-minimal/commit/86cb5326295af3fae22e3a162b99781a1f7ca88e) &middot; Status: **pass**

## Hardware

| Resource | Value |
|---|---|
| CPU | AMD EPYC Processor |
| vCPUs | 2 |
| Threads/core | 1 |
| RAM | 3.8Gi total, 3.3Gi free |
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
vda        8G disk 
├─vda1   6.9G part /
├─vda13 1023M part /boot
├─vda14    4M part 
└─vda15  106M part /boot/efi
nvme0n1    1T disk 
```

### Disk usage

```
Filesystem      Size  Used Avail Use% Mounted on
/dev/vda1       6.7G  4.3G  2.4G  65% /
/dev/vda13      989M   98M  824M  11% /boot
```

### NVMe

```
Node                  Generic               SN                   Model                                    Namespace  Usage                      Format           FW Rev  
--------------------- --------------------- -------------------- ---------------------------------------- ---------- -------------------------- ---------------- --------
/dev/nvme0n1          /dev/ng0n1            vm-report-nvme1      QEMU NVMe Ctrl                           0x1          1.10  TB /   1.10  TB    512   B +  0 B   11.1.1  
```

## PCI devices

```
00:00.0 Host bridge [0600]: Intel Corporation 82G33/G31/P35/P31 Express DRAM Controller [8086:29c0]
00:01.0 VGA compatible controller [0300]: Device [1234:1111] (rev 02)
00:02.0 Non-Volatile memory controller [0108]: Red Hat, Inc. QEMU NVM Express Controller [1b36:0010] (rev 02)
00:03.0 Ethernet controller [0200]: Red Hat, Inc. Virtio network device [1af4:1000]
00:04.0 Communication controller [0780]: Red Hat, Inc. Virtio console [1af4:1003]
00:05.0 SCSI storage controller [0100]: Red Hat, Inc. Virtio block device [1af4:1001]
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
  snapd.service               loaded active running Snap Daemon
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


