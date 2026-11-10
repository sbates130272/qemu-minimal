# SDXI Testing in the Helios-Tray VM

This document records how to build, install and exercise the SDXI kernel
driver and userspace CLI against the emulated SDXI device in a
`vfio-user-helios-tray` guest.

## Prerequisites

| Item | Detail |
|------|--------|
| VM | `stebates-helios-1`, SSH port 2322 |
| Kernel | `7.2.6-070206-generic` (mainline, pre-installed) |
| Source | `ROCm/rocm-sdxi` SHA `aed041b480abc963c761f4d516e2dff378943b9c` |
| Build tools | `build-essential`, `linux-headers-$(uname -r)` |

```bash
# on the host: start the stack
qemu-tool compose --fleet-file qemu/xml/helios-tray.xml \
    --stack vfio-user-helios-tray up -d
# wait for SSH
qemu-tool ssh stebates-helios-1 -- hostname
```

## Build and install

### 1. Package source

On the host, make a tarball of the two directories from the rocm-sdxi tree:

```bash
# inside the rocm-sdxi checkout
tar czf /tmp/sdxi-src.tar.gz \
    emulation/sdxi/drivers/sdxi_char \
    emulation/sdxi/tools/sdxi-cli
```

### 2. Copy to the guest

```bash
qemu-tool ssh stebates-helios-1 -- 'cat > /tmp/sdxi-src.tar.gz' \
    < /tmp/sdxi-src.tar.gz
qemu-tool ssh stebates-helios-1 -- 'cd /tmp && tar xf sdxi-src.tar.gz'
```

### 3. Build the kernel module

```bash
qemu-tool ssh stebates-helios-1 -- bash -s << 'EOF'
cd /tmp/emulation/sdxi/drivers/sdxi_char
make KDIR=/lib/modules/$(uname -r)/build
EOF
```

### 4. Build the CLI

```bash
qemu-tool ssh stebates-helios-1 -- bash -s << 'EOF'
cd /tmp/emulation/sdxi/tools/sdxi-cli
make
EOF
```

### 5. Install

```bash
qemu-tool ssh stebates-helios-1 -- bash -s << 'EOF'
# kernel module
sudo mkdir -p /lib/modules/$(uname -r)/extra
sudo cp /tmp/emulation/sdxi/drivers/sdxi_char/sdxi_char.ko \
    /lib/modules/$(uname -r)/extra/
sudo depmod -a
sudo modprobe sdxi_char

# userspace CLI
sudo install -m 0755 /tmp/emulation/sdxi/tools/sdxi-cli/sdxi-cli \
    /usr/local/bin/
EOF
```

## Verify

```bash
qemu-tool ssh stebates-helios-1 -- bash -s << 'EOF'
# PCI device present and driver bound
lspci -k -s 15:00.0
# device node
ls -l /dev/sdxi0
# dmesg confirmation
dmesg | grep sdxi | tail -5
EOF
```

Expected output:

```
15:00.0 Class 1201: Advanced Micro Devices, Inc. [AMD] Device 149f
        Kernel driver in use: sdxi_char
crw------- 1 root root 10, 265 ... /dev/sdxi0
[...] sdxi_char 0000:15:00.0: sdxi_char: registered /dev/sdxi0
```

> **Note** — `/dev/sdxi0` is root-only by default.  The `sdxi_guest` Ansible
> role installs the udev rule and adds the user to the `sdxi` group for
> persistent non-root access (takes effect on the next `modprobe`/reboot).
> For an ad-hoc session without Ansible: `sudo chmod a+rw /dev/sdxi0`.

## Running tests

All commands below assume the user is in the `sdxi` group (from the Ansible
role) or the device has been made world-writable for the session.
Pass `-d /dev/sdxi0` to override the tool's default of `/dev/sdxi`.

### Copy (MEMCPY)

```bash
# functional check
echo "hello sdxi" > /tmp/in.txt
sudo sdxi-cli -d /dev/sdxi0 copy /tmp/in.txt /tmp/out.txt
diff /tmp/in.txt /tmp/out.txt && echo PASS

# bandwidth benchmark at several sizes
for sz in 4096 65536 1048576 4194304; do
    sudo sdxi-cli -d /dev/sdxi0 bench copy $sz 20
done
```

Measured throughput on the helios-tray emulation:

| Size | Throughput |
|------|-----------|
| 4 KiB | ~52 MB/s |
| 64 KiB | ~496 MB/s |
| 1 MiB | ~1214 MB/s |
| 4 MiB | ~1511 MB/s |

### Compress

```bash
# benchmark
sudo sdxi-cli -d /dev/sdxi0 bench compress 1048576 10
# → bench compress: 1048576 bytes × 10 iters, avg 3.99 ms, 250.6 MB/s
```

### Roundtrip / Decompress

The emulated backend at SHA `aed041b` does not implement
`SDXI_IOC_DECOMPRESS` — the ioctl returns `ENOTTY`.  Roundtrip and
the `stress` subcommand therefore fail on decompress cases; copy and
compress pass cleanly.

```
sdxi-cli: SDXI_IOC_DECOMPRESS: Inappropriate ioctl for device
```

This is a known limitation of the current emulation, not a driver or
install problem.

## Make the module persistent

To reload automatically after a reboot:

```bash
echo sdxi_char | sudo tee /etc/modules-load.d/sdxi.conf
```

## Topology reference

In the helios-tray topology the SDXI device is the "remainder" device
that falls off the end of the four PCIe switches (each switch holds 2
ernics + 1 GPU).  It lands on a plain PCIe root port:

```
15:00.0  Class 1201 [AMD 1022:149f]  SNIA SDXI Controller
         Kernel driver in use: sdxi_char
```

The PCI address (`15:00.0`) is fixed by the topology and will not
change between boots of the same stack.
