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
the second from within gdb.

```
(shell) gdb vmlinux
(gdb)   target remote :1234
```
## Scripts Folder

There are a few scripts in the scripts sub-folder that can be used to
re-generate the jessie-clean.qcow2 image. To regenerate this file or
create a new base image use the following:
```
Logan to add here.
```
