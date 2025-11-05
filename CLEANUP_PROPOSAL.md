# QEMU-Minimal Repository Cleanup Proposal

**Date:** November 5, 2025 **Current Branch:** clean-up **Repository Status:**
Active (81 commits in last year, 44 in last 6 months)

## Executive Summary

This repository has evolved from its original Debian Jessie roots to a modern
Ubuntu Noble-based QEMU environment. The cleanup focuses on:
1. Archiving legacy content using `git-attic`
2. Removing temporary files
3. Updating documentation to reflect current state
4. Optimizing CI workflows
5. Improving project organization

## 1. Files to Archive with git-attic

### 1.1 Legacy Kernels (4.6, 4.8) - **~28MB**
These kernels are 7+ years old and reference outdated features:
```
kernels/bzImage-4.6
kernels/bzImage-4.6-withBtrfs
kernels/bzImage-4.8
kernels/bzImage-4.8-nvmf-soft-roce
kernels/kernel_config-4.5
kernels/kernel_config-4.6
kernels/kernel_config-4.6-withBtrfs
kernels/kernel_config-4.8
kernels/kernel_config-4.8-nvmf-soft-roce
kernels/rdma_rxe.v4.8.ko
```
**Rationale:** Last updated March 2025, not actively used. Modern kernels
available via gen-vm.

### 1.2 Legacy runqemu Script
```
runqemu
```
**Rationale:** Replaced by `qemu/gen-vm` and `qemu/run-vm`. Hardcoded for jessie
image and outdated NVMe options.

### 1.3 Legacy run-cxl-x86_64 Script
```
run-cxl-x86_64
```
**Rationale:** Superseded by `qemu/run-vm-cxl-nvme` which is more flexible and
maintained.

### 1.4 Debian-based Scripts
```
scripts/create
scripts/setup
scripts/shrink
scripts/busybox
scripts/simple
```
**Rationale:** These use debootstrap for Debian Jessie. Modern approach uses
cloud-init with Ubuntu cloud images via `qemu/gen-vm`.

### 1.5 Old libvirt Scripts
```
libvirt/centos7-vm.ks
libvirt/eideticom-vm.ks
libvirt/generic-vm.ks
```
**Rationale:** CentOS 7 is EOL. Generic kickstart files outdated. Modern
approach uses `libvirt/virt-install-ubuntu`.

### 1.6 Logan's Personal Scripts
```
scripts/logan/inst_kern
scripts/logan/make_kern
scripts/logan/reb_kern
scripts/logan/wait_for_host
```
**Rationale:** Personal workflow tools from original contributor, not documented
for general use.

### 1.7 Architecture-specific Run Scripts
```
scripts/run-arm
scripts/run-ppc64el
scripts/run-riscv64
scripts/run-temple
scripts/run.aarch64-uefi
```
**Rationale:** Superseded by `qemu/gen-vm` and `qemu/run-vm` with ARCH parameter
support.

### 1.8 Legacy NVMf/RoCE Scripts
```
scripts/nvmf-host
scripts/nvmf-target
```
**Rationale:** Based on kernel 4.8 soft-RoCE, not maintained since ~2016-2017
based on git history.

### 1.9 Multistrap Configs
```
scripts/multistrap-riscv64.conf
scripts/multistrap.conf
```
**Rationale:** Legacy cross-compilation approach, superseded by cloud-init
method.

## 2. Files to Delete (Not Archive)

### 2.1 Temporary/Backup Files
```
qemu/temp~
qemu/inst-kernel~
.git/COMMIT_EDITMSG~
```
**Rationale:** Editor backup files, no historical value.

### 2.2 Old Test Images (if not needed)
Consider reviewing:
```
images/stebates-test-vm-backing.qcow2 (2.4G)
images/stebates-test-vm.qcow2 (206M)
```
**Rationale:** Personal test VMs. Keep if actively used, otherwise can be
regenerated.

## 3. Documentation Updates Required

### 3.1 README.md Major Updates

**Sections to Archive/Remove:**
- "Summary" section references outdated tech (LightNVM, OpenChannel SSD)
- "New VM Creation" section listing "various stages of repair"
- "./runqemu" documentation (entire section)
- "Image Features" section about jessie.qcow2
- "Scripts Folder" section about create/setup/shrink
- "Simple Initramfs" and "Busybox Initramfs" sections
- "NVMf with Soft RoCE" section (deprecated, kernel 4.8 era)

**Sections to Add:**
- Modern VM creation workflow with gen-vm
- Documentation for run-vm and its features
- NVMe tracing capabilities (NVME_TRACE)
- PCIe device passthrough (PCI_HOSTDEV)
- CXL device emulation (run-vm-cxl-nvme)
- RESTORE_IMAGE mode for disaster recovery

**Sections to Update:**
- QEMU Executable: Update links (many are outdated/broken)
- VirtFS: Clarify modern usage with gen-vm/run-vm
- Kernel Debugging: Still relevant, verify accuracy

### 3.2 New Documentation Files Needed

1. **qemu/README.md** - Document gen-vm, run-vm, run-vm-cxl-nvme
2. **qemu/EXAMPLES.md** - Common usage patterns
3. **MIGRATION.md** - Guide for migrating from old runqemu to new scripts

## 4. CI Workflow Optimizations

### 4.1 Current State
- `.github/workflows/smoke-test.yml` (4.6KB, updated Nov 2025)
- `.github/workflows/spell-check.yml` (416B, updated May 2025)

### 4.2 Recommendations

**smoke-test.yml:**
- ✅ Good: Tests gen-vm for x86, arm64, riscv64
- ✅ Good: Tests RESTORE_IMAGE mode
- ✅ Good: Tests NVME_TRACE functionality
- ⚠️ Consider: Adding test for run-vm-cxl-nvme (if feasible without KVM)
- ⚠️ Consider: Adding test for PCI_HOSTDEV parameter validation
- ⚠️ Consider: Matrix testing different Ubuntu releases (jammy, noble)

**spell-check.yml:**
- ✅ Good: Triggers on markdown changes
- ⚠️ Consider: Running on all PRs to catch typos early
- Update trigger to not require .wordlist.txt path (rarely changes)

**New Workflow Ideas:**
1. **shellcheck.yml** - Lint all bash scripts for quality
2. **link-check.yml** - Verify external links in documentation
3. **image-size-check.yml** - Warn if images/ grows too large

## 5. .gitignore Updates

### Current Issues:
- Uses `*~` pattern (good)
- Missing common patterns

### Recommended Additions:
```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
.venv/
venv/

# Editors
.vscode/
.idea/
*.swp
*.swo
.DS_Store

# Build artifacts
*.o
*.a
*.so
*.ko.cmd
.tmp_versions/

# QEMU runtime
*.pid
*.log
/tmp/

# Cloud-init generated files
cloud-config-*
network-config-*
*-seed.qcow2

# Personal test VMs (pattern-based)
*-test-vm*.qcow2
```

## 6. git-attic Implementation Plan

### Installation
```bash
pipx install git-attic  # or pip3 install --user git-attic
```

### Usage Strategy
```bash
# Step 1: Archive old kernels
git attic kernels/bzImage-4.6 \
         kernels/bzImage-4.6-withBtrfs \
         kernels/bzImage-4.8 \
         kernels/bzImage-4.8-nvmf-soft-roce \
         kernels/kernel_config-4.5 \
         kernels/kernel_config-4.6 \
         kernels/kernel_config-4.6-withBtrfs \
         kernels/kernel_config-4.8 \
         kernels/kernel_config-4.8-nvmf-soft-roce \
         kernels/rdma_rxe.v4.8.ko

# Step 2: Archive legacy scripts
git attic runqemu \
         run-cxl-x86_64 \
         scripts/create \
         scripts/setup \
         scripts/shrink \
         scripts/busybox \
         scripts/simple \
         scripts/nvmf-host \
         scripts/nvmf-target \
         scripts/run-arm \
         scripts/run-ppc64el \
         scripts/run-riscv64 \
         scripts/run-temple \
         scripts/run.aarch64-uefi \
         scripts/multistrap-riscv64.conf \
         scripts/multistrap.conf

# Step 3: Archive logan directory
git attic scripts/logan/

# Step 4: Archive old libvirt kickstart files
git attic libvirt/centos7-vm.ks \
         libvirt/eideticom-vm.ks \
         libvirt/generic-vm.ks

# Step 5: Archive cross compilation script
git attic scripts/cross

# Step 6: Archive gen-image if not used
git attic scripts/gen-image  # Verify not in use first
```

### Recovery
If any archived file is needed later:
```bash
git attic list                    # Show all archived files
git attic show <file>             # View archived file content
git attic restore <file>          # Restore a file
```

## 7. Project Structure - Before and After

### Before (Current):
```
qemu-minimal/
├── runqemu (legacy)
├── run-cxl-x86_64 (legacy)
├── images/ (2.6GB+ with test VMs)
├── kernels/ (old 4.6/4.8 kernels)
├── qemu/ (modern scripts)
├── scripts/ (mix of old/new)
├── libvirt/ (mix of old/new)
└── packages.d/
```

### After (Proposed):
```
qemu-minimal/
├── README.md (modernized)
├── MIGRATION.md (new)
├── qemu/
│   ├── README.md (new)
│   ├── EXAMPLES.md (new)
│   ├── gen-vm
│   ├── run-vm
│   ├── run-vm-cxl-nvme
│   └── vfio-setup
├── libvirt/
│   ├── virt-install-ubuntu
│   ├── create-gcp-nested
│   ├── create-nvme
│   ├── create-raid
│   └── virt-clone-many
├── images/
│   ├── jessie-clean.qcow2 (LFS, keep for legacy)
│   └── noble-server-cloudimg-amd64.img
├── kernels/
│   └── kernel_config-4.15-default (keep as reference)
├── packages.d/
│   ├── packages-default
│   └── packages-minimal
└── .github/workflows/
    ├── smoke-test.yml
    ├── spell-check.yml
    └── shellcheck.yml (new)
```

## 8. Implementation Order

1. **Phase 1: Safety First**
   - Ensure git-attic is installed
   - Create feature branch from clean-up
   - Test git-attic on one file first

2. **Phase 2: Archive with git-attic**
   - Archive legacy kernels
   - Archive deprecated scripts
   - Archive logan/ directory
   - Archive old libvirt configs

3. **Phase 3: Delete Temp Files**
   - Remove *~ files
   - Clean up any local test artifacts

4. **Phase 4: Update .gitignore**
   - Add modern patterns
   - Test with `git status`

5. **Phase 5: Documentation**
   - Update main README.md
   - Create qemu/README.md
   - Create qemu/EXAMPLES.md
   - Create MIGRATION.md

6. **Phase 6: CI Enhancements**
   - Add shellcheck workflow
   - Update spell-check trigger
   - Consider link-checker

7. **Phase 7: Testing & PR**
   - Run smoke tests
   - Verify nothing breaks
   - Create PR from clean-up to main

## 9. Risk Assessment

### Low Risk:
- Archiving old kernels (already in git history)
- Removing temp files
- Updating .gitignore
- Documentation updates

### Medium Risk:
- Archiving runqemu (some users might still use it)
- Archiving scripts/create, setup, shrink (Debian workflow)

### Mitigation:
- All archived files remain in git history
- git-attic makes restoration trivial
- Add MIGRATION.md to guide users
- Announce changes in PR description

## 10. Success Metrics

- [ ] Repository size reduced (working tree, not git history)
- [ ] All CI tests pass
- [ ] Documentation covers modern workflows
- [ ] No broken links in documentation
- [ ] Shellcheck passes on all scripts
- [ ] Clear migration path for existing users

## 11. Estimated Impact

**Disk Space Savings (working tree):**
- Legacy kernels: ~28MB
- Legacy scripts: ~50KB
- Total: ~28-30MB reduction in checkout size

**Maintainability:**
- Clearer project structure
- Better documentation
- Easier onboarding for new contributors
- Reduced confusion about which scripts to use

**User Experience:**
- Clear documentation of modern approach
- Migration guide for legacy users
- Better CI coverage = more confidence

## Next Steps

1. Review and approve this proposal
2. Install git-attic (via pipx or pip --user)
3. Execute Phase 1-7 implementation
4. Create PR with comprehensive description
5. Merge to main after review

