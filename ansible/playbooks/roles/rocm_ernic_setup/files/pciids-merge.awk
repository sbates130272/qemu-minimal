# Insert the emulated NIC into pci.ids, emitting the merged file on stdout.
#
#   mode=sub   the vendor and device lines exist; add the subsystem line
#              directly under the device line
#   mode=dev   only the vendor line exists; add both the device line and the
#              subsystem line directly under the vendor line
#
# Exits 3 if it inserted nothing, so a silent no-op cannot pass for a merge.
#
# Three things about pci.ids make this positional rather than keyed, each
# measured against pciutils 3.14 rather than assumed:
#
#   - indentation is TABS. Spaces are "lspci: Parse error at <file>, line N",
#     rc 1, not a style difference.
#   - a subsystem line is only legal under a device line, which is only legal
#     under a vendor line, in that order. A subsystem line with no device line
#     above it is a parse error.
#   - a duplicate at any level is "lspci: Duplicate entry at <file>, line N",
#     rc 1 -- and then lspci resolves no names for ANY device on the bus, not
#     just this one. That is why the caller scans first and why nothing here
#     inserts a line that the scan said was already present.
#
# The insert goes directly under its parent rather than being appended,
# because pci.ids is parsed sequentially: an entry at the end of the file
# lands under the class section and never matches. Position within the
# subsystem list does not matter -- pciutils requires nesting and uniqueness,
# not sort order.
#
#   awk -f pciids-merge.awk -v vend=1dd8 -v dev=1002 -v mode=sub \
#       -v devline='1002  DSC Ethernet Controller' \
#       -v subline='1dd8 5400  ROCm Emulated RDMA NIC' \
#       /usr/share/misc/pci.ids

!done && $0 ~ "^" vend "[ \t]" {
    print
    blk = 1
    if (mode == "dev") {
        print "\t" devline
        print "\t\t" subline
        done = 1
    }
    next
}

mode == "sub" && !done && blk && $0 ~ "^\t" dev "[ \t]" {
    print
    print "\t\t" subline
    done = 1
    next
}

/^[^\t]/ { blk = 0 }

{ print }

END { if (!done) exit 3 }
