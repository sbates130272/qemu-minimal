# Minimal QEMU Environment

## Summary

This repository contains an (almost) stand-aloneenvironment for
running qemu as well as some scripts for creating and managing the
image and a script to run qemu. This environemnt is targetted at
emulated NVM Express (NVMe) and LightNVM (aka OpenChannel) SSD testing
but can be used for many other things.

There is a minimalist kernel config in this repo as well that can be used as
a starting point for building a suitable kernel.

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
turn this off (and suffer the wrath of slowness).

## Image Features

The runqemu script will automatically boot queitly and login as root
giving a shell over stdio (which is managable but can have some rough
edges). When the user logs out it will automatically powerdown the
machin and exit qemu. An SSH login is also available, while running,
forwarded to localhost port 3324. The root password is 'awhisten'.

There's an nvme drive mounted on /mnt/nvme with the corresponding
image in images/nvme.qcow2. This image just contains an ext4 partition
with a single 4MB random test file. It is snapshotted so changes do not
get saved run to run.

The host's /home file is also passthrough mounted to the guests /home
directory so test scripts, etc can be stored and run directly from the
users home directory on the host.

QEmu's gdb feature is turned on so you may debug the kernel using
gdb. Note that you run the first command below from a shell prompt and
the second from within gdb. Note you should run these two command
before invoking QEMU.

```
(shell) gdb vmlinux
(gdb)   target remote :1234
```
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
image to replace the /home folder).

## Kernel Debugging

There are some good websites on how to do OS debug via QEMU. See for
example:

http://stackoverflow.com/questions/11408041/how-to-debug-the-linux-kernel-with-gdb-and-qemu

A good command to start gdb would be:

gdb -ex 'target remote localhost:1234' -ex 'break <function>' -ex c ./vmlinux

Make sure you have CONFIG_DEBUG_INFO set in the .config when you build
the quest kernel.