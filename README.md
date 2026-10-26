# NickLabs Orbis Graphics Pack 1.0.0

A separate graphics layer for **NickLabs Orbis Linux 0.2.0** on the PS4 **CUH-1215A** (Belize
southbridge, Liverpool GPU). Orbis 0.2.0 is not changed: the pack installs into a *copy* of an
Orbis 0.2.0 image, and everything it changes is recorded and can be rolled back.

**Status: built and tested on Nobara and the PS4 by Nick, not by the author.** The code here was
tested on a Linux host with synthetic inputs only (see `docs/GRAPHICS-RELEASE-STATUS.md`).
Nothing in this repository has run on a PS4.

## What is in it

| Area | Where | What |
|---|---|---|
| Mesa, stable line | `mesa/stable`, `mesa/lib32` | Mesa 26.2.5 (radeonsi, RADV, EGL/GBM/GLES, VA-API, RustiCL OpenCL) + 3 pinned PS4 patches; 32-bit Mesa from the same source |
| Mesa, experimental line | `mesa/experimental` | Mesa main at a pinned commit; recorded as BLOCKED when the patches stop applying |
| Diagnostics | `diag/` | `orbis-gpu qualify / health / profile / capabilities`, `orbis-gfx-probe` (GL, Vulkan raster/compute, pipeline cache, OpenCL, all with numeric or pixel checks), JSON schema |
| Stage-0 GPU diagnosis | `diag/stage0`, `diag/miniroot` | initramfs overlay (`orbis.gfxdiag=1`) + a mini-root on the USB stick that runs the real GPU tests before a full install |
| Kernel | `kernel/` | no replacement kernel by default; scripts to review community GPU kernel work and build an optional `linux-orbis-ps4-gfx` |
| Nobara integrator | `orbis-graphics`, `integrator/` | `verify`, `inspect`, `integrate` (image or staging rootfs), `stage0`, `rollback`, with receipts |
| Pack build | `build/` | one command on Nobara: packages, dependency closure, mini-root, `ORBIS-GRAPHICS-PACK-v1.0.0.tar.zst` |
| Bug intelligence | `research/` | catalog of known PS4 GPU issues + a GitHub updater that runs on Nobara and never applies anything |

## Commands (on Nobara)

```sh
build/build-pack.sh --orbis-src ~/orbis-release/nicklabs-ps4-orbis-linux
./orbis-graphics verify --pack out/release/ORBIS-GRAPHICS-PACK-v1.0.0.tar.zst
./orbis-graphics integrate --base-image <orbis-0.2.0 image> --pack out/release/ORBIS-GRAPHICS-PACK-v1.0.0.tar.zst --output <new image>
./orbis-graphics stage0 --base-stage0-image <orbis-0.2.0 stage-0 image> --pack out/release/ORBIS-GRAPHICS-PACK-v1.0.0.tar.zst --output <new image>
```

The full step-by-step is `docs/GRAPHICS-INTEGRATION-NOBARA.md`.

## Documents

- `docs/GRAPHICS-RELEASE-STATUS.md`: what is built, tested, wired or blocked
- `docs/GRAPHICS-INTEGRATION-NOBARA.md`: exact Nobara commands
- `docs/GRAPHICS-HARDWARE-QUALIFICATION.md`: what to run on the PS4 and what to send back
- `docs/GRAPHICS-KERNEL-MESA-COMPATIBILITY.md`: kernel / libdrm / Mesa contract
- `docs/GRAPHICS-PERFORMANCE-PLAN.md`: profiles, measurements, what is not possible on this kernel
- `docs/GRAPHICS-RESEARCH-AND-BUG-LEDGER.md`: the ledger and how it is kept current
- `docs/GRAPHICS-CHECKPOINT.md`: where the project stands, for the next session

## Licence and firmware

MIT for the Orbis-authored code (`LICENSE`). Mesa and the Arch packages keep their own licences
(`manifest/licenses.json`). **The pack contains no PS4 firmware.** An image built from it keeps
whatever firmware the Orbis 0.2.0 base image already has, so it stays private like that image.
NickLabs Orbis is an independent community project, not affiliated with Sony or AMD.
