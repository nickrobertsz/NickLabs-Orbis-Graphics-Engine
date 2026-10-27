# NickLabs Orbis — Rust components and toolchain

Following the Rust directive, Orbis now writes new components in Rust. The existing working
implementations were kept as they are: Orbis 0.2.0's Python platform, the Graphics Engine 2.0
Python tools, and the C kernel and loader patches.

## Userspace workspace (`rust/`)

| Crate | Binaries | What |
|---|---|---|
| orbis-core | — | JSON, sysfs/fs helpers (`ORBIS_ROOT` re-rooting for fixtures), /proc, restore journal, syscalls (glibc symbols declared directly, no libc crate), dates, args |
| orbis-time | `orbis-time` | PS4 time policy (`TIME.md`) |
| orbis-perf | `orbis-perf` | unified performance controller, sched_ext management, game sessions, doctor (`PERFORMANCE.md`) |
| orbis-games | `orbis-games`, `orbis-run` | Vulkan capabilities → DXVK/VKD3D/WineD3D, runner catalog + offline bundle, launcher, failure classifier (`GAMING.md`) |

* **Dependencies:** standard library only, with no crates.io packages, so `cargo build --offline
  --locked` works from the source tree. External programs are used where they are the source of
  truth: `vulkaninfo`, `systemctl`, `curl`, `tar`, `sha256sum`/`sha512sum`.
* **Release profile:** `opt-level=3`, `lto="fat"`, `codegen-units=1`, `panic="abort"`,
  `strip=true`. Binaries are 0.5 to 0.8 MB each.
* **Jaguar build:** `RUSTFLAGS="-C target-cpu=btver2"` (in `packages/orbis-native/PKGBUILD`).
* **ISA gate:** `rust/tools/isa-check.py` runs Orbis 0.2.0's own `isa_audit.py`, imported
  read-only and never modified. It fails the package build on any AVX2/FMA/BMI2/AVX-512
  instruction.
  * Checked here: the btver2 build passes (`safe`, 0 v3 instructions in all four binaries), and
    an `x86-64-v3` build of orbis-perf fails (164 BMI2 + 80 AVX2).
  * Correction: 0.2.0's audit counts the AVX1 float permutes `vperm2f128`/`vpermilps`/`vpermilpd`
    on `%ymm` as AVX2, because they start with `vp`. Rust's `core::slice::sort` emits
    `vperm2f128` under btver2. The gate excludes these three; the finding is recorded for a later
    0.2.x fix (bug ledger GE-B17).
* **Tests:** `cargo test --offline` runs 28 tests: core 9, games 8 (one needs curl and is skipped
  without it), perf 8, time 3. `makepkg` runs them in `check()`.

Rust toolchain on this build host: rustc 1.97.0. In the pack build, Arch's `rust` from the pinned
snapshot (2026/10/20) is used inside the graphics build container. MSRV is declared as 1.80.

## Kernel: Rust-for-Linux

`kernel/build-ge-kernel.sh` enables `CONFIG_RUST=y` by default (`--no-rust` turns it off):

* The kernel's minimum toolchain is pinned: rustup installs `rustc 1.87.0` with `rust-src`
  (`--profile minimal`, overridable with `RUST_KERNEL_TOOLCHAIN`), plus Arch's `rust-bindgen`.
  The script removes Arch's `rust` package in the kernel container so the two cannot be mixed.
* `make rustavailable` must pass, and the script asserts `CONFIG_RUST=y` after `olddefconfig`.
* BTF (needed by sched_ext) and Rust together need `!DEBUG_INFO_BTF || (PAHOLE_HAS_LANG_EXCLUDE
  && !LTO)`. pahole is installed so the kernel is not built with LTO.
* Integrity: static.rust-lang.org cannot be reached from this environment. On the Nobara, rustup
  verifies the toolchain against its signed channel manifests.

Which kernel code is Rust:
* The PS4 drivers stay in C. The Aeolia RTC is a sub-function of the C `apcie` driver, and 6.15
  has no Rust RTC abstraction.
* The schedulers are Rust: `scx_lavd`, `scx_bpfland`, `scx_rusty` and `scx_flash` (from
  `scx-scheds` v1.1.4 in the Arch snapshot) are Rust userspace loaders with BPF schedulers.
* Rust-for-Linux is enabled so later Orbis kernel drivers can be written in Rust.

Status:
* The kernel Rust wiring is UNVERIFIED: no kernel was compiled here, since that was refused in
  this environment.
* The userspace crates are BUILT + HOST-VERIFIED.
