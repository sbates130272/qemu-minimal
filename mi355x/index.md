# MI355X-shaped node report

One guest with 8 GPUs, 8 emulated RDMA NICs and
8 PCIe switches -- one NIC and one GPU below each switch, each
switch on its own root port. Every other stack in this repo hangs one NIC
and one GPU flat off the root complex, so this is the only lane that
exercises software reading a realistic PCIe tree.

- Guest image: `20261021.g0f1f43f-vm.resolute-ernic-rocjitsu-qm.b4eb552-qcow2`
- Fleet size: 1 VM (the lab stack is 4; a runner fits one)
- Addressed NICs: 1 of 8. All 8 RDMA devices come up, but
  `99-rocm-ernic.rules` names the Ethernet interface `rocm-ernic%n` and `%n`
  is 0 for every one of them, so a single NIC wins that name and the other
  seven keep kernel names. Only the named one carries an address.
- Generated: **2026-10-23 05:31 UTC** &middot; Commit: `unknown` &middot; Status: **pass**

## The PCIe tree the guest enumerates

Each switch carries exactly one Pensando NIC and one GPU, so their
traffic has a path that does not cross the root complex.

```
-[0000:00]-+-00.0  Intel Corporation 82G33/G31/P35/P31 Express DRAM Controller
           +-01.0  Device 1234:1111
           +-02.0  Red Hat, Inc. QEMU NVM Express Controller
           +-03.0  Red Hat, Inc. QEMU NVM Express Controller
           +-04.0-[01-04]----00.0-[02-04]--+-00.0-[03]----00.0  AMD Pensando Systems DSC Ethernet Controller
           |                               \-01.0-[04]----00.0  Advanced Micro Devices, Inc. [AMD/ATI] Device 75c1
           +-05.0-[05-08]----00.0-[06-08]--+-00.0-[07]----00.0  AMD Pensando Systems DSC Ethernet Controller
           |                               \-01.0-[08]----00.0  Advanced Micro Devices, Inc. [AMD/ATI] Device 75c1
           +-06.0-[09-0c]----00.0-[0a-0c]--+-00.0-[0b]----00.0  AMD Pensando Systems DSC Ethernet Controller
           |                               \-01.0-[0c]----00.0  Advanced Micro Devices, Inc. [AMD/ATI] Device 75c1
           +-07.0-[0d-10]----00.0-[0e-10]--+-00.0-[0f]----00.0  AMD Pensando Systems DSC Ethernet Controller
           |                               \-01.0-[10]----00.0  Advanced Micro Devices, Inc. [AMD/ATI] Device 75c1
           +-08.0-[11-14]----00.0-[12-14]--+-00.0-[13]----00.0  AMD Pensando Systems DSC Ethernet Controller
           |                               \-01.0-[14]----00.0  Advanced Micro Devices, Inc. [AMD/ATI] Device 75c1
           +-09.0-[15-18]----00.0-[16-18]--+-00.0-[17]----00.0  AMD Pensando Systems DSC Ethernet Controller
           |                               \-01.0-[18]----00.0  Advanced Micro Devices, Inc. [AMD/ATI] Device 75c1
           +-0a.0-[19-1c]----00.0-[1a-1c]--+-00.0-[1b]----00.0  AMD Pensando Systems DSC Ethernet Controller
           |                               \-01.0-[1c]----00.0  Advanced Micro Devices, Inc. [AMD/ATI] Device 75c1
           +-0b.0-[1d-20]----00.0-[1e-20]--+-00.0-[1f]----00.0  AMD Pensando Systems DSC Ethernet Controller
           |                               \-01.0-[20]----00.0  Advanced Micro Devices, Inc. [AMD/ATI] Device 75c1
           +-0c.0  Red Hat, Inc. Virtio network device
           +-0d.0  Red Hat, Inc. Virtio console
           +-0e.0  Red Hat, Inc. Virtio block device
           +-1f.0  Intel Corporation 82801IB (ICH9) LPC Interface Controller
           +-1f.2  Intel Corporation 82801IR/IO/IH (ICH9R/DO/DH) 6 port SATA Controller [AHCI mode]
           \-1f.3  Intel Corporation 82801I (ICH9 Family) SMBus Controller
```

## Devices

```
sockets:  1
cores:    4
GPUs:     8 bound, 9 KFD nodes
RDMA:     8 HCAs found:
NVMe:     /dev/nvme0n1 /dev/nvme1n1 
NAME     SIZE TYPE
sr0     1024M rom
vda      512G disk
nvme0n1    1T disk
nvme1n1    1T disk
```

## RDMA

**Loopback, not inter-node.** With one guest there is no second node to
send to, so this proves the verbs and the queue pairs work. It is not a
mesh bandwidth figure and must not be read as one.

```

== RDMA on guest 1 ==
 remote address: LID 0000 QPN 0x0002 PSN 0xcf8b92
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:192:168:100:11
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[MiB/sec]    BW average[MiB/sec]   MsgRate[Mpps]
 65536      200              2698.39            2692.29		     0.043077
---------------------------------------------------------------------------------------
```

## What qemu-tool reports about the live guest

```
VM: mi355x-ci-1  (pid 10058)
  SSH:       ssh -p 2222 ubuntu@localhost   (the accepted key has no private half here)
  User:      ubuntu
  Key:       SHA256:a56c2mbgJwKvkrLWcQhEoB2EHvcXLJZsVnmMO2nd4vk batesste-ci-images guest key (/var/lib/qemu-tool/images/keys/mi355x_id_rsa.pub, accepted by guest)
  Arch:      amd64  vCPUs: 4  Memory: 6144 MiB
  GPU:       gfx1250 (emulated, rocjitsu-1-1.sock)
  GPU:       gfx1250 (emulated, rocjitsu-1-2.sock)
  GPU:       gfx1250 (emulated, rocjitsu-1-3.sock)
  GPU:       gfx1250 (emulated, rocjitsu-1-4.sock)
  GPU:       gfx1250 (emulated, rocjitsu-1-5.sock)
  GPU:       gfx1250 (emulated, rocjitsu-1-6.sock)
  GPU:       gfx1250 (emulated, rocjitsu-1-7.sock)
  GPU:       gfx1250 (emulated, rocjitsu-1-8.sock)
  Image:     /var/lib/qemu-tool/images/mi355x-ci-1.qcow2
  Container: mi355x-qemu-1-1
  Uptime:    3m
  VFIO:      ernic-1-1.sock, rocjitsu-1-1.sock, ernic-1-2.sock, rocjitsu-1-2.sock, ernic-1-3.sock, rocjitsu-1-3.sock, ernic-1-4.sock, rocjitsu-1-4.sock, ernic-1-5.sock, rocjitsu-1-5.sock, ernic-1-6.sock, rocjitsu-1-6.sock, ernic-1-7.sock, rocjitsu-1-7.sock, ernic-1-8.sock, rocjitsu-1-8.sock
  GA:        /tmp/qga-mi355x-ci-1-2222.sock (available)
  Hostname:  ubuntu-ci-vm
  IP:        127.0.0.1 (lo)
  IP:        10.0.2.15 (enp0s12)
  IP:        192.168.100.11 (rocm-ernic0)
  Boot Disk: /dev/vda        512.0 GiB  emulated (virtio)  /boot/efi,/boot,/
  Disk:      /dev/nvme0n1      1.0 TiB  emulated (nvme)
  Disk:      /dev/nvme1n1      1.0 TiB  emulated (nvme)
  Disk:      /dev/sr0       1024.0 MiB  emulated (sata)
  Login:     ubuntu from 172.31.0.2 (since 05:30, 1m)

```
