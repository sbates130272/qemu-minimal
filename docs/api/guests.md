# Guests

One guest at a time: `config` is the `VMConfig` every command here works from, `gen_vm` makes an image, `run_vm` builds the QEMU command line and starts it, and `caps` asks the QEMU binary what it supports. `identity` is how a guest is marked as started by qemu-tool, `qcow` looks after overlay images, and `libvirt_xml` converts a `VMConfig` to and from a libvirt domain file.

::: qemu_tool.config

::: qemu_tool.gen_vm

::: qemu_tool.run_vm

::: qemu_tool.caps

::: qemu_tool.identity

::: qemu_tool.qcow

::: qemu_tool.libvirt_xml
