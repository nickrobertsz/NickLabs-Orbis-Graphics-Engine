# Graphics Pack 1.0.0: release status

Date: 2026-10-26. Base: Orbis 0.2.0 (`v0.2.0`, d094ebd), kernel 6.15.4 crashnt-4.7 (bzImage
SHA-256 `cbc11420…c157`), profile `cuh-1215a`, Arch snapshot 2026/10/20.

**Overall: source complete, packages not yet built, nothing tested on a PS4.** The compiled pack
is produced on Nobara by `build/build-pack.sh`. Building Mesa, the loader and kernel code was not
permitted in the authoring environment, so that work is wired and left to Nobara as agreed.

## States used here

| State | Meaning |
|---|---|
| TESTED-HOST | ran on a Linux host with synthetic inputs (fixtures, fake images, llvmpipe) |
| WIRED | script/recipe written and syntax-checked; it runs for the first time on Nobara |
| BLOCKED | cannot work on the 0.2.0 kernel; the reason is reported at runtime |
| NOT-RUN | needs the PS4 |

## Components

| Component | State | Notes |
|---|---|---|
| `orbis_gfx` library + `orbis-gpu` CLI (qualify, health, profile, capabilities) | TESTED-HOST | 24 tests on a fixture `/sys` modelled on Liverpool (PCI IDs 1002:9920/9922/9923) |
| `orbis-gfx-probe` 1.0.0 | TESTED-HOST (partly) | compiles warning-free with and without the raster shaders; GL path ran on llvmpipe (shader oracle OK, frame timing, draw calls); RADV, Vulkan raster and OpenCL paths compile but need a real GPU |
| Stage-0 overlay `orbis-gfx-stage0` | TESTED-HOST | 5 tests under `sh` with a fixture root: report files, numbering, watchdog, mini-root hand-off, fallback JSON |
| Stage-0 mini-root builder | WIRED | `diag/miniroot/build-miniroot.sh` (pacstrap in the build container) |
| Mesa 26.2.5 stable (`mesa-orbis-ps4`, `opencl-mesa-orbis-ps4`) | WIRED | pinned tarball SHA-256 and patch hashes; patches 0001/0003/0005 apply to 26.2.5 with `-F0` (checked earlier); not compiled here |
| 32-bit Mesa (`lib32-mesa-orbis-ps4`) | WIRED | same source and patches |
| Experimental Mesa (main @ f7dd61f) | WIRED | a patch failure is recorded as `BLOCKED` in the pack manifest and never fails the stable pack |
| `orbis-graphics` package, `orbis-graphics-pack` meta package | WIRED | |
| Dependency closure, signing, repo DB | WIRED | resolved against the 0.2.0 repo + Arch snapshot; packages already on the 0.2.0 image are left out |
| Pack assembler (`build/assemble_pack.py`) | TESTED-HOST | deterministic archive, manifest `orbis-graphics-pack/1`, SHA256SUMS over every file |
| Integrator `verify` / `inspect` / dry-runs / refusals | TESTED-HOST | 12 tests on synthetic Orbis 0.2.0 images built with the vendored 0.2.0 image builder |
| Integrator `stage0 --host` (compose a graphics stage-0 image) | TESTED-HOST | end to end on a synthetic image: base unchanged, overlay appended, mini-root on ORBIS-ROOT, e2fsck clean, receipt |
| Integrator `integrate` / `rollback` on an image (container, loop mounts, pacman) | WIRED | needs `orbis-build:0.2.0` and loop devices; first real run is on Nobara |
| On-console integration (`integrate-root.sh --root /`) | WIRED | same script as the image path |
| Replacement kernel | none by default | the 0.2.0 kernel is kept; see the compatibility document |
| Kernel research / optional `-gfx` kernel | WIRED | `kernel/research-deltas.sh`, `kernel/build-gfx-kernel.sh`, `kernel/package` |
| Bridge A/B patch `kernel/patches/available/0101` | WIRED, opt-in | applies to the 0.2.0 kernel tree with `-F0`; not compiled; not in the series |
| Bug catalog + updater | TESTED-HOST | 28 seeded entries; updater tested against a fake GitHub API (filtering, classification, de-duplication, no edits to reviewed entries) |

Unit tests: `python3 -m unittest discover -s tests/unit -t tests/unit`, 44 tests, all passing
(the stage-0 compose test needs root, dosfstools, e2fsprogs and zstd; it is skipped otherwise).

## Blocked on the 0.2.0 kernel (reported, not hidden)

| Check | Result on 0.2.0 | Why |
|---|---|---|
| `pm.clocks` | UNSUPPORTED | `cik.c` gives CHIP_LIVERPOOL no powerplay (pp_smu) block: no DPM, no clock or performance-level control |
| `video.decode` | UNSUPPORTED | UVD 4.2 / VCE 2.0 IP blocks are commented out for Liverpool, and the loader does not supply UVD/VCE firmware |
| ROCm / HIP | not offered | no `CONFIG_HSA_AMD`; AMD's ROCm list excludes GCN of this age anyway |

## Not run (needs the PS4)

Everything in `docs/GRAPHICS-HARDWARE-QUALIFICATION.md`: boot of an integrated image, real
radeonsi/RADV renderer, raster and compute oracles on Liverpool, RustiCL, presentation, sustained
runs, GPU recovery, profile changes.
