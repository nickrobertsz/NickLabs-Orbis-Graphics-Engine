# Graphics Pack checkpoint (2026-10-26)

For the next session, human or AI. Read this, then GRAPHICS-RELEASE-STATUS.md.

## Where things are

- Project: **NickLabs Orbis Graphics Pack 1.0.0**, separate from Orbis 0.2.0 (`v0.2.0`, d094ebd,
  untouched). Target CUH-1215A (Belize, Liverpool), profile `cuh-1215a`.
- Source: git repository `nicklabs-orbis-graphics` (bundle `orbis-graphics-1.0.0.bundle`, tag
  `v1.0.0-src`), and `NICKLABS-ORBIS-GRAPHICS-SOURCE-1.0.0.tar.gz`.
- The compiled pack `ORBIS-GRAPHICS-PACK-v1.0.0.tar.zst` is built on Nobara by
  `build/build-pack.sh --orbis-src <0.2.0 checkout>`. It was not built in the authoring session
  (building Mesa/kernel code was not permitted there); Nick agreed to run the build on Nobara.
- Tests: `python3 -m unittest discover -s tests/unit -t tests/unit`, 44 tests, all pass on the host.

## Decisions taken

1. No replacement kernel in 1.0.0. The 0.2.0 kernel already exposes everything the diagnostics
   read; nothing in the community kernels has been tested on a CUH-1215A.
2. Stable Mesa = 26.2.5 (same as 0.2.0) with pkgrel 101, plus RustiCL (own package), VA-API, overlay
   layer. Experimental = Mesa main @ f7dd61f with the same patches; BLOCKED is recorded, not hidden.
3. No firmware in the pack, ever. Integrated images inherit the base image's firmware and privacy.
4. Integration always works on a copy, in the `orbis-build:0.2.0` container, offline, with receipts
   and a rollback; stage-0 images get a reversible initramfs overlay plus a mini-root on ORBIS-ROOT.
5. GPU clocks/DPM and UVD/VCE are UNSUPPORTED on the 0.2.0 kernel (source-verified); profiles skip
   those knobs with the reason. No overclocking. `RADV_DEBUG=nocompute` never global.
6. Bug catalog: reviewed `entries` are edited by people only; the Nobara updater fills `inbox`.

## Open items, in order

1. Nick: `build/build-pack.sh` on Nobara; send back `out/pack/logs/` on any failure.
2. Nick: graphics stage-0 image → `orbis-logs/stage0-*` (GRAPHICS-HARDWARE-QUALIFICATION.md Q0).
3. Nick: integrated full image → Q1 receipts.
4. With Q0/Q1 results: fix what fails; decide on the bridge A/B patch 0101 (GFX-001) only if a
   black screen after modeset is seen; review `kernel/research-deltas.sh` output for UVD/DPM.
5. ORB-007: re-diff linux-psx Mesa patches (main moved to 21380639) before any Mesa bump.
6. First run of `integrate`/`rollback` on a real image: watch `…integration.log` (loop devices,
   pacman offline config, signature level).

## Unverified assumptions to check first if something breaks

- The Arch snapshot 2026/10/20 has libdrm ≥ 2.4.133 (the build checks and stops if not).
- `pacman -Sp` with the 0.2.0 repo resolves the whole closure offline-installable (the integrator
  installs with an offline config; a missing package shows up as a pacman error in the log).
- The 0.2.0 image mounts ORBIS-BOOT at `/boot/ps4` (0.2.0 tmpfiles create it; the pack relies on it
  for on-console integration and rollback).
- The mini-root runs from a read-only bind of ORBIS-ROOT inside the initramfs; Python and Mesa start
  there without systemd (fixture-tested only).
