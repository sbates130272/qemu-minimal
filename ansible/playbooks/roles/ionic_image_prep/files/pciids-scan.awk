# Is the emulated NIC already in pci.ids, and at which of the three levels?
#
# Prints three words: vendor|novendor device|nodevice subsys|nosubsys.
#
# The question has to be asked block-scoped, at all three levels. pci.ids
# nests vendor -> device (one tab) -> subsystem (two tabs), and the names are
# only correct within their parent, so a bare `grep 1002` matches the same
# four hex digits under a dozen other vendors and a grep for the entry text
# misses upstream's spelling of it. Asking for the subsystem line specifically
# is what distinguishes "hwdata already names this device" (1002, a real
# Pensando NIC, which has been there for years) from "hwdata already names
# OUR device" (the 1dd8 5400 subsystem under it, which is the emulation).
# Stopping at the device level would have skipped forever and never written
# the one line that is ours.
#
# Spelt with -v and dynamic regexes rather than literals so the shape of the
# program does not depend on the ids, and with character classes rather than
# intervals because mawk is Ubuntu's default awk and does not do interval
# expressions.
#
#   awk -f pciids-scan.awk -v vend=1dd8 -v dev=1002 \
#       -v subv=1dd8 -v subd=5400 /usr/share/misc/pci.ids

$0 ~ "^" vend "[ \t]" { blk = 1; vendor = 1; indev = 0; next }

# Any other line at column zero ends the vendor's block: a different vendor,
# a comment, or the class section at the end of the file.
/^[^\t]/ { blk = 0; indev = 0; next }

blk && $0 ~ "^\t" dev "[ \t]" { device = 1; indev = 1; next }

# A different device under the same vendor ends this device's subsystem list.
blk && $0 ~ "^\t[^\t]" { indev = 0; next }

blk && indev && $0 ~ "^\t\t" subv "[ \t]+" subd "[ \t]" { subsys = 1 }

END {
    print (vendor ? "vendor" : "novendor"), \
          (device ? "device" : "nodevice"), \
          (subsys ? "subsys" : "nosubsys")
}
