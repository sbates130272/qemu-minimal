# vfio-user-mi355x-node

An MI355X node's PCIe topology, in miniature: each VM is **one EPYC socket, 8
gfx1250 GPUs, 8 ernic NICs and 8 PCIe switches**, with one NIC and one GPU
below each switch and each switch on its own root port off the CPU.

Simplified against the real thing — single socket, no host-level NIC/GPU
asymmetry — but the tree the guest *enumerates* is the shape that matters.
ROCm topology detection, affinity heuristics and P2P path selection all read
the bus, and a flat tree reads differently from a switched one. Every other
stack in this repo gives a guest one NIC and one GPU, both flat, so there was
nothing here to exercise that against.

**`docker-compose.yml`, `prometheus.yml` and `fleet-inventory.yml` in this
directory are generated.** Edit [`qemu/env/mi355x-node`](../../env/mi355x-node)
and regenerate; a hand edit is lost next time and the `Gen Compose Check` CI
lane fails.

```bash
qemu-tool gen-compose --stack vfio-user-mi355x-node \
  --env-file qemu/env/mi355x-node
```

## Topology

Per VM, with `VM_PCIE_SWITCH_WIDTH=2`:

```
pcie.0  (q35 root complex, 1x EPYC socket)
├── pcie-root-port vfurp0 ── x3130-upstream vfusw0
│       ├── xio3130-downstream vfudp0.0 ── ernic-N-1     (vfio-user)
│       └── xio3130-downstream vfudp0.1 ── rocjitsu-N-1  (vfio-user)
├── pcie-root-port vfurp1 ── x3130-upstream vfusw1
│       ├── vfudp1.0 ── ernic-N-2
│       └── vfudp1.1 ── rocjitsu-N-2
│   ... 8 switches total ...
└── pcie-root-port vfurp7 ── x3130-upstream vfusw7
        ├── vfudp7.0 ── ernic-N-8
        └── vfudp7.1 ── rocjitsu-N-8
```

**Socket order is the pairing.** `run-vm` groups `--vfio-userdev` left to
right, `VM_PCIE_SWITCH_WIDTH` at a time, and knows nothing about what a
socket serves. `gen-compose` round-robins the two lists
(`ernic-N-1, rocjitsu-N-1, ernic-N-2, …`) so each switch gets one of each.
Changing that order, or setting unequal `ERNIC_PER_VM`/`ROCJITSU_PER_VM`,
still renders — it just stops being pairs.

Three details that make this work and are easy to lose:

- **`hotplug=off` on all 24 ports.** A hotplug-capable port reserves an
  MMIO/IO window for a device that might arrive later. Eight switches' worth
  of those reservations exhaust the 32-bit hole, and the guest then fails BAR
  assignment for devices that are *already present*. Nothing here is a
  hotplug target.
- **`VM_PCI_HOLE64_SIZE`.** Eight emulated GPUs' BARs do not fit the default
  64-bit hole. The size is bounded by the CPU's physical address width and
  QEMU refuses outright rather than degrading:
  `Address space limit 0xffffffffff < 0x1ffffffffff phys-bits too low (40)`.
  `-cpu EPYC` gives 40 bits; measured on QEMU 10.2.1, **512G is accepted and
  1T is not**. The committed value is that ceiling, not a measurement —
  see "Still to measure" below.
- **`VM_CPU_SOCKETS=1`.** Bare `-smp cpus=N` leaves QEMU to infer
  `sockets=N, cores=1`, so the guest reports N single-core sockets. Every
  other stack in this repo has always done that. One EPYC socket needs the
  count pinned.

## Addressing

At `ERNIC_PER_VM > 1`, per-NIC identity is keyed on the **global NIC ordinal**
`(vm-1) * ERNIC_PER_VM + d`, not the VM index — otherwise VM 2's first NIC
would collide with VM 1's. At the default of one NIC per VM the two are the
same number, which is what keeps the scale-out stack regenerating
byte-identical.

For this fleet's 32 NICs:

| | range |
|---|---|
| fleet address | `172.31.1.1` – `172.31.1.32` |
| MAC | `72:6f:63:6d:00:01` – `…:00:20` |
| guest IP | `192.168.100.11` – `192.168.100.42` |

Each NIC gets its own `ERNIC_TCP_GUEST_GIDS`, derived from its own MAC. That
is per-node by construction and must never be hoisted into the shared anchor
— getting it wrong is not a startup error, the mesh silently routes to
another node and the transfer fails with `Completion with error at client`
(AGENTS.md issue 18).

`ERNIC_MACS` and `ERNIC_GUEST_IPS` overrides, if used, must be **32** long
here, not 4.

## The mesh ceiling moves, and it is the binding one

**Each rocm-ernic is its own mesh node, so 8 NICs per VM is 8 nodes per VM.**
This is the fact that sizes the fleet:

| `VM_COUNT` | mesh nodes | |
|---|---|---|
| 4 | 32 | proven — the size issue 18's GID fix was re-verified at |
| 5 | 40 | at the measured registration wall; expect losses |
| 6 | 48 | `gen-compose` warns; workers will not all register |
| 8 | 64 | `gen-compose` refuses (protocol ceiling) |

`gen-compose` names the *product* in both messages, because `VM_COUNT=6` is
not the number that is too large.

The registration chain runs over the global ordinal, so it serialises across
VM boundaries too (`ernic-2-1` waits on `ernic-1-8`). That is what
registration order has to be — node ids are handed out in connection order —
and it costs roughly 56 s of boot at 32 nodes.

Other ceilings move with it: 4 VMs x 8 vCPU wants a host of ~40 cores by the
issue-14 heartbeat rule, and 4 x 16g is 64g of `/dev/shm`. **This is a lab-node
stack**, not something a 32-core workstation runs.

## Bringing it up

```bash
qemu-tool gen-vm --env-file qemu/env/mi355x-node \
    --vm-name stebates-mi355x-1 --backing-image "$VM_BACKING_IMAGE"
for n in 2 3 4; do
  qemu-tool gen-vm --env-file qemu/env/mi355x-node \
      --vm-name stebates-mi355x-$n \
      --backing-file /var/lib/qemu-tool/images/stebates-mi355x-1-backing.qcow2
done
qemu-tool gen-compose --stack vfio-user-mi355x-node \
  --env-file qemu/env/mi355x-node
qemu-tool compose --env-file qemu/env/mi355x-node \
    --stack vfio-user-mi355x-node --profile metrics up -d
```

Confirm the topology from inside a guest before anything else — a flat tree
boots exactly as happily as a switched one:

```bash
lspci -tv                 # 8 switches, one NIC + one GPU under each
lscpu | grep -i socket    # Socket(s): 1
ibv_devinfo -l            # 8 HCAs
dmesg | grep -i ionic     # a real link speed, not "Unknown x255"
```

## Why the fork QEMU image

`QEMU_IMAGE` pins the `-sbates-fork` variant deliberately. It carries
`31e3d06a` *"hw/vfio: assign region fd to RAMBlock for vfio-user P2P DMA"*;
without it a peer device's BAR is DMA_MAP-ed with no fd, arrives as
`vaddr=(nil)`, and every WQE touching GPU memory dies in `vfu_sgl_get()` as
`could not gather N bytes`. Pairing a GPU with a NIC under one switch and
then having no working P2P between them would miss the point of the stack.

Verify a candidate with `-device help | grep pci-mmio-bridge`, present only in
fork builds, and `/build/qemu-commit.txt`.

## Still to measure

- **`VM_PCI_HOLE64_SIZE=512G` is the phys-bits ceiling, not a measured
  requirement.** Read the rocjitsu BARs with `lspci -vv` in a guest and
  multiply by 8. If eight gfx1250 BAR sets need more than 512G, the fix is
  `host-phys-bits=on` or an explicit `phys-bits=` on `-cpu`, not a larger
  value here — QEMU will simply refuse.
- **P2P DMA across the switch is untested.** AGENTS.md issue 19 records the
  fork tag as carrying the patch, read from the image name; nothing in this
  repo has yet run a P2P workload against it.

## What this stack does NOT do

**The guest-side ansible configures one NIC, not eight.**
`ernic_guest_setup`'s configure play assigns one address to `rocm-ernic0`, and
`fleet-inventory.yml`'s `fleet_guest_ip`/`lmcache_guest_ernic_mac` are single
values per host. The generator emits the remaining pairs as
`fleet_guest_nics` so they are recorded where a role that loops over them
will look, but **nothing in `ansible/` consumes that yet**.

So a guest here *enumerates* 8 switches, 8 NICs and 8 GPUs correctly and can
drive NIC 0 as today. Driving all eight — and inter-node RDMA across all
eight — needs the roles extending, with two constraints that multiply by
eight on the way: UCX cannot open an IB device whose name contains `-` or `_`
(issue 21), and SRQ needs the DKMS build with `ernic_srq_dkms_reload` true
(issue 20).

LMCache is off (`LMCACHE_ENABLE=false`) for the same reason: its guest-side
half assumes one ernic per guest, and it is orthogonal to the topology this
stack exists to test.
