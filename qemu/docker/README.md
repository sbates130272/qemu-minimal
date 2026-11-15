# `qemu/docker/`

Dockerfiles this repo owns, one directory per image. Build context is the
image's own directory.

| Directory | Image | Used by |
|---|---|---|
| [`sdxi/`](sdxi/) | `qemu-tool/sdxi:latest`, the rocm-sdxi vfio-user server | any fleet with `sdxi-per-vm` set: `qemu/xml/rocm-golden.xml` and `qemu/xml/helios-tray.xml` |

Not to be confused with
[`qemu/compose/vfio-user-ernic-rocjitsu-scale-out/tapsetup/`](../compose/vfio-user-ernic-rocjitsu-scale-out/tapsetup/),
which is three lines built by compose itself as part of one stack. Anything
here is built by hand and referenced by tag.

## `sdxi/`

The Dockerfile takes the `rocm-sdxi` binary through a named build context
rather than building it, so extract it from the SDXI CI image first:

```bash
tmp=$(mktemp -d)
cid=$(docker create "$SDXI_SOURCE_IMAGE")
docker cp "$cid:/usr/local/bin/rocm-sdxi" "$tmp/"
docker rm "$cid"
docker build --build-context "sdxi-bin=$tmp" -t qemu-tool/sdxi:latest qemu/docker/sdxi
```
