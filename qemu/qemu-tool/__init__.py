from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("qemu-tool")
except PackageNotFoundError:
    __version__ = "unknown"
