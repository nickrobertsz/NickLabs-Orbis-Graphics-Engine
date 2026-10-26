# Changelog

## 1.0.0 (source release, 2026-10-26)

First release of the NickLabs Orbis Graphics Pack for Orbis 0.2.0 on the CUH-1215A.

- Mesa 26.2.5 stable line (`mesa-orbis-ps4` 26.2.5-101, `opencl-mesa-orbis-ps4`, `lib32-mesa-orbis-ps4`)
  with PS4 patches 0001/0003/0005; RustiCL, VA-API and the Vulkan overlay layer enabled.
- Experimental Mesa line (main @ f7dd61f), recorded as BLOCKED when the patches do not apply.
- `orbis-gpu` (qualify, health, profile, capabilities) with result states PASS / FAIL / DEGRADED /
  UNSUPPORTED / NOT_RUN / BLOCKED and JSON schema `orbis-gfx-report/1`; `orbis-gpu-qualify` and
  `orbis-gpu-health` aliases; `orbis-qualify` extended with a graphics receipt.
- `orbis-gfx-probe` 1.0.0: GL pattern shader, 1080p frame timing, draw calls; Vulkan raster
  (gradient, depth) and compute oracles; pipeline cache; OpenCL integer oracle.
- Stage-0 GPU diagnosis: initramfs overlay (`orbis.gfxdiag=1`) and a GPU mini-root on the USB stick.
- Reversible performance profiles with state and a JSONL change log; no overclocking.
- Nobara integrator `orbis-graphics`: verify, inspect, integrate (image or staging rootfs), stage0,
  rollback, dry runs and receipts.
- Pack build wiring for Nobara (`build/build-pack.sh`), deterministic pack archive.
- Kernel research and optional `linux-orbis-ps4-gfx` wiring; opt-in bridge A/B patch 0101.
- Bug catalog (28 entries) and a Nobara-side GitHub updater.

Not yet built into packages or tested on a PS4; see docs/GRAPHICS-RELEASE-STATUS.md.
