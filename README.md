# Minimal QEMU Environment

## Summary

This repository contains an (almost) stand-alone environment for
running qemu as well as some scripts for creating and managing the
image and a script to run qemu. This environemnt is targetted at
emulated NVM Express (NVMe), Persistent Memory (PMEM) and LightNVM
(aka OpenChannel) SSD testing but can be used for many other things.

There are some minimalist kernel config in this repo as well that can
be used as a starting point for building a suitable kernel.

To run qemu using this image from the command should be:

```
./runqemu <path_to_bzimage>
```

You can run
```
./runqemu -h
```
to get the command line arguments supported.

This script will automatically create a snapshot image so you can revert
to the original image by deleting images/jessie.qcow2.

## QEMU Executable

This repo does not include the QEMU executable. You can specific a
path to the exe you want to run using the -q option. This is useful if
you are using a fork (or your own branch) of QEMU that has support for
specific hardware (e.g. LightNVM SSDs). Some useful links include the
[upstream](http://git.qemu-project.org/qemu.git) QEMU repo, Keith
Busch's [NVMe](git://git.infradead.org/users/kbusch/qemu-nvme.git)
repo and Matias Bjorling's
[LightNVM](https://github.com/OpenChannelSSD/qemu-nvme) fork.

Note you might want to track all three of these and install them all
since certain things are only supported in certain forks. With luck,
over time, support for all things will end up upstream ;-).

Note that by default KVM support is turned on. Use the -k switch to
turn this off (and suffer the wrath of slowness or if you are already
running inside some form of a VM).

## Image Features

The runqemu script will automatically boot queitly and login as root
giving a shell over stdio (which is managable but can have some rough
edges). When the user logs out it will automatically powerdown the
machin and exit qemu. An SSH login is also available, while running,
forwarded to localhost port 3324. The root password is 'awhisten'.

When the NVMe option is chosen there is an nvme drive mounted on
/mnt/nvme with the corresponding image in images/nvme.qcow2. This
image just contains an ext4 partition with a single 4MB random test
file. It is snapshotted so changes do not get saved run to run.

A second NVMe drive exists at /dev/nvme1 but this is not mounted. This
second drive has a Controller Memory Buffer (CMB) advertised on it.

The host's /home file is also passthrough mounted to the guests /home
directory so test scripts, etc can be stored and run directly from the
users home directory on the host. In order for this to work you will
use to make sure that QEMU is configured with VirtFS enabled and that
the kernel you are running has the relevant Plan9 support.

QEmu's gdb feature is turned on so you may debug the kernel using
gdb. Note that you run the first command below from a shell prompt and
the second from within gdb. Note you should run these two command
before or after invoking QEMU.

```
(shell) gdb vmlinux
(gdb)   target remote :1234
```
If you are trying to debug a kernel module you need to use the gdb
add-symbol-file command. This command needs to point to the .ko and
provide memory offsets for .bbs, .data and .text sections. For example
something like this:
```
(gdb) add-symbol-file <path to module> <text_addr> -s .data
<data_addr> -s .bss <bss_addr>

```
Note you can get the addresses from the running kernel in
/sys/modules/<module name>/sections directory and that the kernel has
to be running before these addresses can be determined.

## Scripts Folder

There are a few scripts in the scripts sub-folder that can be used to
re-generate the jessie-clean.qcow2 image. To regenerate this file or
create a new base image use the following steps:

   1. sudo modprobe nbd max_part=8 - this makes sure that Network
   Block Device kernel module is loaded. We need this for step 2, 3
   and 4.

   2. ./create <image name> - this creates the bare qcow2 image,
   partitions the image and then uses deboostrap to setup the image as
   a chroot with a basic Debian Jessie install.

   3. ./setup <image name> - this configures the Debian Jessie
   install. Setups up networking, machine name and some other key
   attributes. Note you can change the machine name and root password
   by editing this script.

   4. ./shrink <image name> - This script shrinks the image file as
   much as possible by doing zerofree and then running qemu-img
   convert.

We are happy to consider PRs for other -clean images as long as they
utilize the Large File Storage (LFS) feature or are pulled in via curl
or wget.

## Large File Storage

This repo utilizes git Large File Storage (lfs) in order to avoid
having to host the large jessie-clean.qcow2 image inside the repo. See
[here](https://git-lfs.github.com/) for more information.

## Simple Initramfs

The simple script in the scripts folder generates a really simple
initramfs with a statically linked rootfs. Useful for really simple
sanity testing. To build and run this simple initramfs do the
following:

  1. cd scripts
  2. ./simple
  3. cd ..
  4. qemu-system-x86_64 -m 512 -kernel kernels/bzImage-4.8 -initrd
  scripts/simple.cpio.gz -append "console=ttyS0" -serial mon:stdio
  -nographic

## Busybox Initramfs

For a more intresting initramfs example you can run busybox. To do
this perform the following steps.

  1. Download the busyboz source and build it to include all the tools
  that you want. Make sure this is a statically linked executable.
  2. cd scripts
  3. ./busybox <path to busybox exe>
  4. cd ..
  5. emu-system-x86_64 -m 512 -kernel kernels/bzImage-4.8 -initrd
  scripts/initramfs.cpio.gz -append "console=ttyS0" -serial mon:stdio
  -nographic

This should boot into busybox shell and you can execute your installed
command from there. Enjoy! Note this will not then pass on to a
subsequent root filesystem (yet).

## Virtfs

By default we map the /home folder on the host to the /home folder on
the guest using Plan 9 folder sharing over VirtFS. However this
assumes the host has the kernel support to do this. To disable this
option use the -f switch (if you do this you might want to attach a
image to replace the /home folder). Note you also need to make sure
the QEMU executable you are using was compiled with VirtFS support
enabled.

## Kernel Debugging

There are some good websites on how to do OS debug via QEMU. See for
example:

http://stackoverflow.com/questions/11408041/how-to-debug-the-linux-kernel-with-gdb-and-qemu

A good command to start gdb would be:

gdb -ex 'target remote localhost:1234' -ex 'set architecture i386:x86-64:intel' \
  -ex 'break <function>' -ex c ./vmlinux

Make sure you have CONFIG_DEBUG_INFO set in the .config when you build
the quest kernel.

## NVMf with Soft RoCE

Phew. After quite a bit of fun and games I can get a NVMf setup
running using two QEMU guests. The kernel config files are checked in
(v4.8-rc5 for now). There are two helper scripts in the scripts
folders and the jessie image needs updating with some of the libs
(including the librxe).

For a few reasons it is easier to keep the rdma_rxe.ko module seperate
to the kernel. Also you need the linux headers inside the VM root
filesystem to compile librxe. You currenly need to copy (or symlink)
/usr/lib64/* to /usr/lib. You also have to setup VM to VM networking
for which I (for now) used -netdev socket. QEMU perfers ntap but
that's alot more work. The IPv4 addresses on the eth0 interfaces of
the two VMs are statically configured using techniques discussed in
http://csortu.blogspot.ca/2009/12/building-virtual-network-with-qemu.html.

I started the nvmef target VM using the following command:

./runqemu -v -m 2048 -t -i images/nvmf-target.qcow2 \
  ./kernels/bzImage-4.8-nvmf-soft-roce

and the nvmef host VM using the following command:

./runqemu -v -m 2048 -s 3235 -n -i images/nvmf-host.qcow2 \
  ./kernels/bzImage-4.8-nvmf-soft-roce

I then logged into the target system and executed the nvmf-target
script in the scripts folder using the target VMs assigned IP address
as the input argument. On the host side I executed the nvmf-host
script with the IP address of the target as the first argument.

nvme discover worked.
nvme connect worked.

IO on the system caused some panics so need to investigate that. Note
that the NVMf connection should be established between the eth1
interfaces and not the eth0 interfaces.

Note you need the rdma_rxe module installed on both target and host
right now as there is an issue with monolithic kernels and rxe we need
to root-cause.
