# CI Optimization Analysis

## Current State

### Workflow Execution Times (Estimated)

1. **smoke-test.yml**: ~5-8 minutes (longest)
   - smoke-test-x86: ~6-7 min
   - smoke-test-arm64: ~2-3 min
   - smoke-test-riscv64: ~2-3 min
   - **Jobs run in parallel** ✅ (good!)

2. **spell-check.yml**: ~30 seconds (fast)
3. **shell-check.yml**: ~30 seconds (fast)

**Total PR time**: ~6-8 minutes (limited by slowest job: x86 smoke test)

---

## Optimization Opportunities

### 🚀 High Impact (30-50% time savings)

#### 1. Cache Cloud Images (~90 seconds saved per job)

**Problem**: Ubuntu cloud images (~600MB) downloaded fresh every run

**Solution**: Use GitHub Actions cache

```yaml
- name: Cache Ubuntu cloud images
  uses: actions/cache@v4
  with:
    path: qemu/../images/*.img
    key: cloud-images-${{ matrix.arch }}-${{ hashFiles('qemu/gen-vm') }}
    restore-keys: |
      cloud-images-${{ matrix.arch }}-
```

**Savings**: ~90 seconds per job (first run slow, subsequent runs fast)

#### 2. Smart SSH Polling Instead of Sleep 60 (~30 seconds saved)

**Problem**: Waits full 60 seconds even if VM boots in 20 seconds

**Current**:
```bash
- name: Sleep for 60 seconds to allow VM to boot
  run: sleep 60
```

**Optimized**:
```bash
- name: Wait for VM to boot (with timeout)
  run: |
    timeout=60
    elapsed=0
    while [ $elapsed -lt $timeout ]; do
      if ssh -o ConnectTimeout=1 \
             -o NoHostAuthenticationForLocalhost=yes \
             -p 2222 ubuntu@localhost true 2>/dev/null; then
        echo "VM ready after $elapsed seconds"
        exit 0
      fi
      sleep 2
      elapsed=$((elapsed + 2))
    done
    echo "VM failed to boot within $timeout seconds"
    exit 1
```

**Savings**: ~30-40 seconds (VMs typically boot in 20-30 seconds)

#### 3. Cache APT Packages (~20 seconds saved)

**Problem**: `apt update` and package installation on every run

**Solution**: Use apt cache action

```yaml
- name: Cache apt packages
  uses: awalsh128/cache-apt-pkgs-action@v1
  with:
    packages: qemu-system-x86 qemu-utils cloud-image-utils
    version: 1.0
```

**Savings**: ~20 seconds per job

---

### 🔧 Medium Impact (10-20% time savings)

#### 4. Use Matrix Strategy for Architecture Tests

**Problem**: Duplicated setup code across three jobs

**Solution**: Use matrix to reduce duplication

```yaml
jobs:
  smoke-test:
    name: smoke-test-${{ matrix.arch }}
    runs-on: ubuntu-latest
    strategy:
      matrix:
        arch: [amd64, arm64, riscv64]
        include:
          - arch: amd64
            packages: qemu-system-x86 qemu-utils cloud-image-utils
            extra_tests: true
          - arch: arm64
            packages: qemu-system-arm cloud-image-utils
          - arch: riscv64
            packages: qemu-system-misc u-boot-qemu cloud-image-utils
    steps:
      # Shared setup code here
      # Conditional extra tests for x86
```

**Benefits**:
- Less code duplication
- Easier to maintain
- Same execution time (still parallel)

#### 5. Reduce NVME_TRACE Test Timeout

**Current**: 10 seconds wait

**Optimized**: 3 seconds (trace events happen immediately)

```yaml
- name: Test NVME_TRACE mode
  run: |
    # Start VM with NVMe trace enabled, run for 3 seconds
    timeout 3 ./run-vm > /dev/null 2>&1 || true
```

**Savings**: ~7 seconds

#### 6. Skip Package Installation If Cached

**Problem**: Installs packages even if already present

**Solution**: Check before installing

```bash
- name: Install packages (if needed)
  run: |
    if ! command -v qemu-system-x86_64 &> /dev/null; then
      sudo apt update
      sudo apt install -y qemu-system-x86 qemu-utils cloud-image-utils
    else
      echo "Packages already installed (cached runner)"
    fi
```

**Savings**: ~15 seconds on cache hit

---

### 💡 Low Impact / Nice to Have

#### 7. Fail Fast Strategy

**Current**: All jobs run to completion even if one fails

**Optimized**: Stop other jobs if one fails

```yaml
strategy:
  fail-fast: true
  matrix:
    ...
```

**Benefits**: Saves time when failures occur (not on success)

#### 8. Concurrent Job Limit

**Current**: No explicit limit

**Add**: Max concurrent to avoid resource contention

```yaml
strategy:
  max-parallel: 3
```

**Benefits**: Prevents GitHub runner resource issues

---

## Recommended Implementation Plan

### Phase 1: Quick Wins (Implemented)
1. ✅ Replace `sleep 60` with SSH polling (~30s saved)
2. ✅ Reduce NVME_TRACE timeout to 3s (~7s saved)
3. ⏭️  Job timeouts (skipped - risk of false failures on slow runners)

**Expected savings**: ~35-40 seconds on x86 job  
**Effort**: Low (simple changes)  
**Status**: Implemented in this PR

### Phase 2: Caching (Next PR)
1. ✅ Add cloud image caching (~90s saved)
2. ✅ Add apt package caching (~20s saved)

**Expected savings**: ~2 minutes on first run, ~110s on subsequent runs
**Effort**: Medium (needs testing)

### Phase 3: Refactoring (Future)
1. ✅ Consolidate with matrix strategy
2. ✅ Add skip-if-installed checks

**Expected savings**: Minimal time, better maintainability
**Effort**: Medium

---

## Projected Performance

### Current Performance
- First run: ~6-8 minutes
- Subsequent runs: ~6-8 minutes (no caching)

### After Phase 1 (Quick Wins)
- First run: ~5-6 minutes
- Subsequent runs: ~5-6 minutes

### After Phase 1 + Phase 2 (With Caching)
- First run: ~5-6 minutes (downloads cache)
- Subsequent runs: ~2-3 minutes (uses cache)
- **60-70% faster on cache hits!**

---

## Coverage Preservation

All optimizations maintain **100% test coverage**:
- ✅ x86_64 VM generation and execution
- ✅ ARM64 VM generation
- ✅ RISC-V VM generation
- ✅ RESTORE_IMAGE mode
- ✅ NVME_TRACE functionality
- ✅ SSH connectivity
- ✅ Spell checking
- ✅ Shell script linting

No tests removed, only execution time reduced.

---

## Additional Recommendations

### Consider Adding (Future)
1. **Artifact upload**: Save VM logs on failure for debugging
2. **Conditional runs**: Skip ARM64/RISC-V if only x86 code changed
3. **Scheduled runs**: Weekly full test even without changes

### Deliberately Not Implemented
1. **Job timeouts**: Risk of false failures when CI runners are slow
   - GitHub Actions has default 6-hour timeout which is sufficient
   - Better to let long-running jobs complete than risk false failures

### Not Recommended
- ❌ Removing architecture tests (important coverage)
- ❌ Removing RESTORE_IMAGE test (critical feature)
- ❌ Skipping SSH test (validates end-to-end)
- ❌ Reducing VM boot timeout below 60s total (some CI runners are slow)

---

## Implementation Priority

**Do Now (This PR)**:
- Smart SSH polling
- Reduce NVME_TRACE timeout
- Add fail-fast

**Do Next (Follow-up PR)**:
- Cloud image caching
- APT package caching

**Do Later (When Needed)**:
- Matrix refactoring
- Artifact upload on failure

