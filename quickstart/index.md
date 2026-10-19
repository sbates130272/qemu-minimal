# Two-VM quickstart report

Two guests from the published `ernic-rocjitsu` image, each with an
emulated gfx1250, an emulated NVMe and an emulated RDMA NIC, booted
by `scripts/quickstart-2vm.sh`.

- Guest image: `20261014.geea0485-vm.resolute-ernic-rocjitsu-qm.c5d51ce-qcow2`
- RDMA bytes across the mesh: 65536000
- Generated: **2026-10-19 23:22 UTC** &middot; Commit: `unknown` &middot; Status: **pass**

## quickstart-1

```
kernel:  7.2.4-070204-generic
rocm:    /opt/rocm/core-10.1
gpu:     gfx1250
hca_id:	rocep1s0
	transport:			InfiniBand (0)
	fw_ver:				rocm-ernic-1.0
	node_guid:			706f:63ff:fe6d:0001
	sys_image_guid:			706f:63ff:fe6d:0001
	vendor_id:			0x1dd8
	vendor_part_id:			4098
	hw_ver:				0x0
--- block devices (nvme0n1 is the emulated NVMe)
NAME     SIZE TYPE
sr0     1024M rom
vda      512G disk
nvme0n1    1T disk
--- addresses
lo               UNKNOWN        127.0.0.1/8 ::1/128 
enp0s5           UP             10.0.2.15/24 metric 100 fec0::5054:ff:fe00:8ae/64 fe80::5054:ff:fe00:8ae/64 
rocm-ernic0      UP             192.168.100.11/24 fe80::706f:63ff:fe6d:1/64 
```

### GPU test

```
device=gfx1250 CUs=32 n=4096
mismatches=0
vector-add: PASS
```

### RDMA test (client)

```
---------------------------------------------------------------------------------------
                    Send BW Test
 Dual-port       : OFF		Device         : rocep1s0
 Number of qps   : 1		Transport type : IB
 Connection type : RC		Using SRQ      : OFF
 PCIe relax order: ON		Lock-free      : OFF
 ibv_wr* API     : OFF		Using DDP      : OFF
 TX depth        : 128
 CQ Moderation   : 1
 Mtu             : 1024[B]
 Link type       : Ethernet
 GID index       : 1
 Max inline data : 0[B]
 rdma_cm QPs	 : OFF
 Data ex. method : Ethernet
---------------------------------------------------------------------------------------
 local address: LID 0000 QPN 0x0002 PSN 0xf71ed6
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:192:168:100:11
 remote address: LID 0000 QPN 0x0002 PSN 0x5bd298
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:192:168:100:12
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[MiB/sec]    BW average[MiB/sec]   MsgRate[Mpps]
 65536      1000             1125.46            1116.73		     0.017868
---------------------------------------------------------------------------------------
```

## quickstart-2

```
kernel:  7.2.4-070204-generic
rocm:    /opt/rocm/core-10.1
gpu:     gfx1250
hca_id:	rocep1s0
	transport:			InfiniBand (0)
	fw_ver:				rocm-ernic-1.0
	node_guid:			706f:63ff:fe6d:0002
	sys_image_guid:			706f:63ff:fe6d:0002
	vendor_id:			0x1dd8
	vendor_part_id:			4098
	hw_ver:				0x0
--- block devices (nvme0n1 is the emulated NVMe)
NAME     SIZE TYPE
sr0     1024M rom
vda      512G disk
nvme0n1    1T disk
--- addresses
lo               UNKNOWN        127.0.0.1/8 ::1/128 
enp0s5           UP             10.0.2.15/24 metric 100 fec0::5054:ff:fe00:8ae/64 fe80::5054:ff:fe00:8ae/64 
rocm-ernic0      UP             192.168.100.12/24 fe80::706f:63ff:fe6d:2/64 
```

### GPU test

```
device=gfx1250 CUs=32 n=4096
mismatches=0
vector-add: PASS
```


## ernic counters
```
ernic-1:   total_bytes_sent        : 65536000
ernic-1:   total_bytes_received    : 0
ernic-1:   total_bytes_rdma_read   : 0
ernic-1:   total_bytes_rdma_write  : 0
ernic-2:   total_bytes_sent        : 0
ernic-2:   total_bytes_received    : 65536000
ernic-2:   total_bytes_rdma_read   : 0
ernic-2:   total_bytes_rdma_write  : 0
```
