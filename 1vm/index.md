---
title: VM Report
---

# VM Report — qemu-minimal

Generated: **2026-09-22 15:42 UTC** &middot; Commit: [`unknown`](https://github.com/sbates130272/qemu-minimal/commit/unknown)

## Hardware

| Resource | Value |
|---|---|
| CPU | AMD EPYC Processor |
| vCPUs | 2 |
| Threads/core | 1 |
| RAM | 3.8Gi total, 3.5Gi free |
| Swap | 0B |

## Kernel

```
6.8.0-139-generic
```

<details><summary>Full version string</summary>

```
Linux version 6.8.0-139-generic (buildd@lcy02-amd64-036) (x86_64-linux-gnu-gcc-13 (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0, GNU ld (GNU Binutils for Ubuntu) 2.42) #139-Ubuntu SMP PREEMPT_DYNAMIC Sat Aug  1 03:52:05 UTC 2026
```

</details>

## Storage

```
NAME     SIZE TYPE MOUNTPOINT
sr0     1024M rom  
vda        8G disk 
├─vda1     7G part /
├─vda14    4M part 
├─vda15  106M part /boot/efi
└─vda16  913M part /boot
nvme0n1    1T disk 
```

### Disk usage

```
Filesystem      Size  Used Avail Use% Mounted on
/dev/vda1       6.8G  1.9G  4.9G  28% /
/dev/vda16      881M   64M  756M   8% /boot
```

### NVMe

```
nvme-cli unavailable
```

## ROCm

### Installed packages (sample)

```

```

### /opt/rocm/bin

```

```

### hipFile

```
(not available)
```

## AMDGPU Kernel Driver

```
ii  libdrm-amdgpu1:amd64            2.4.125-1ubuntu0.1~24.04.2                       amd64        Userspace interface to amdgpu-specific kernel DRM services -- runtime
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
  cron.service                loaded active running Regular background program processing daemon
  dbus.service                loaded active running D-Bus System Message Bus
  getty@tty1.service          loaded active running Getty on tty1
  ModemManager.service        loaded active running Modem Manager
  multipathd.service          loaded active running Device-Mapper Multipath Device Controller
  polkit.service              loaded active running Authorization Manager
  qemu-guest-agent.service    loaded active running QEMU Guest Agent
  rsyslog.service             loaded active running System Logging Service
  serial-getty@ttyS0.service  loaded active running Serial Getty on ttyS0
  snapd.service               loaded active running Snap Daemon
  ssh.service                 loaded active running OpenBSD Secure Shell server
  systemd-fsckd.service       loaded active running File System Check Daemon to report status
  systemd-journald.service    loaded active running Journal Service
  systemd-logind.service      loaded active running User Login Management
  systemd-networkd.service    loaded active running Network Configuration
  systemd-resolved.service    loaded active running Network Name Resolution
  systemd-timedated.service   loaded active running Time & Date Service
  systemd-timesyncd.service   loaded active running Network Time Synchronization
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

