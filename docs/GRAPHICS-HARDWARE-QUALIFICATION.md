# Graphics Pack 1.0.0: hardware qualification on the CUH-1215A

Nothing in the pack has run on a PS4 yet. This is the order to test it in, what each step shows,
and what to send back. Every result is one of:

| State | Meaning |
|---|---|
| PASS | the check ran and the result was verified (pixels, numbers, or kernel state) |
| FAIL | the check ran and the result was wrong, or the component is missing |
| DEGRADED | it works with a limitation (for example only a CPU OpenCL device, or unstable frame times) |
| UNSUPPORTED | this kernel/hardware cannot do it; the report names the blocker |
| NOT_RUN | opt-in check that was not requested, or no display/sample was available |
| BLOCKED | could not run because something earlier failed (for example amdgpu not bound) |

`orbis-gpu qualify` exits 0 on overall PASS, 1 on DEGRADED/NOT_RUN, 2 otherwise. The JSON follows
`/usr/share/orbis-graphics/schema/orbis-gfx-report-1.schema.json` (schema `orbis-gfx-report/1`).

## Before Linux: is HDMI physically fine?

The PS4 menu must show a picture on the same TV and cable at 1080p (Settings → Sound and Screen →
Video Output: 1080p, Deep Colour Off, HDR Off). A dead HDMI transmitter or port is a known CUH-1215A
repair case (ledger GFX-017) and no software fixes it.

## Q0: graphics stage-0 image (no installation)

Build and boot it as in GRAPHICS-INTEGRATION-NOBARA.md section 4. The checks:

| Check | What it verifies |
|---|---|
| `gpu.identity` | a Liverpool PCI function (1002:9920/9922/9923) is present; exact IDs and revision recorded |
| `gpu.driver` | `amdgpu` is bound |
| `gpu.firmware` | debugfs shows a loaded version for CE, PFP, ME, MEC, MEC2, RLC, SDMA0, SDMA1 |
| `gpu.rings` | GFX/compute/SDMA rings exist |
| `gpu.ib_test` | NOT_RUN unless `orbis.gfxdiag.ibtest=1` |
| `gpu.memory` | VRAM/GTT sizes vs. the loader's `vram.txt` |
| `display.connector`, `display.edid`, `display.link` | a connected connector with a 1920x1080 mode, valid EDID header, no DP link errors |
| `pm.clocks` | UNSUPPORTED on the 0.2.0 kernel (no powerplay for Liverpool) |
| `sensors.gpu` | hwmon/k10temp readings that exist |
| `health.since_boot` | GPU faults, ring timeouts, resets in the kernel log |
| `gl.renderer`, `gl.draw`, `gl.shader`, `gl.frame_timing` | radeonsi (FAIL if llvmpipe), a pixel-exact integer pattern shader, 1080p frame time median/p95, 5000 small draws |
| `vk.device`, `vk.raster`, `vk.compute`, `vk.pipeline_cache` | RADV on the GPU (not a CPU device), gradient and depth-test pixel oracles, integer compute oracle checked on the CPU, cache effect |
| `cl.rusticl` | RustiCL on radeonsi with an integer kernel checked on the CPU |
| `video.decode` | UNSUPPORTED on the 0.2.0 kernel (no UVD) |

Send back: everything in `orbis-logs/` (the `stage0-NNN-*` files).

If the screen goes black and nothing is written: `stage0-NNN-gpu-progress.txt` (if present) names
the last step. If even the 0.2.0 report is missing, the kernel never reached USB; that is a 0.2.0
issue, not the pack.

## Q1: integrated full image, first boot

Flash the integrated image (GRAPHICS-INTEGRATION-NOBARA.md section 5) to the SSD, boot, log in over SSH:

```sh
orbis-gpu version
sudo orbis-gpu health                              # OK / FAULTED / FIRMWARE_PROBLEM / DRIVER_NOT_BOUND / NO_GPU
sudo orbis-gpu qualify                             # all default checks, ~1-2 min
sudo orbis-qualify --model CUH-1215A               # 0.2.0 qualification + graphics receipt
glxinfo -B | grep -E 'renderer|version'            # must name AMD / radeonsi, not llvmpipe
vulkaninfo --summary | grep -E 'deviceName|driverID'
RUSTICL_ENABLE=radeonsi clinfo -l
cat /var/lib/orbis-graphics/integration.json | head -40
```

In a terminal on the PS4's own desktop (PC personality, keyboard on the console), so the session's
DISPLAY/WAYLAND_DISPLAY and XAUTHORITY are set:

```sh
sudo -E orbis-gpu qualify                          # adds vk.present (vkcube, 300 frames)
```

## Q2: opt-in checks (only when Q1 is fine)

```sh
sudo orbis-gpu qualify --sustained 600             # 10 min GL load: frame-time drift, temperatures
sudo orbis-gpu qualify --transitions               # personality switches with a GPU check after each
sudo orbis-gpu qualify --ring-test                 # kernel IB test on every ring
sudo orbis-gpu qualify --recovery --i-accept-gpu-reset   # forces a GPU reset; may freeze the console
sudo orbis-gpu profile list
sudo orbis-gpu profile set performance --dry-run
sudo orbis-gpu profile set performance && sudo orbis-gpu profile reset
```

Do `--recovery` last, with nothing unsaved; a hard power-off may be needed.

## Rollback checks

1. `sudo /usr/lib/orbis-graphics/rollback-root.sh --root / --boot /boot/ps4 --dry-run`, then for real,
   reboot: the 0.2.0 Mesa is back (`pacman -Q mesa-orbis-ps4` shows the 0.2.0 version).
2. If an optional `-gfx` kernel was installed: `sudo orbis boot rollback-kernel` restores the
   previous bzImage and initramfs.

## What to send back

- `orbis-logs/stage0-*` from the stage-0 stick;
- `/var/lib/orbis/receipts/*-graphics.json` and `.txt`, `/var/lib/orbis-graphics/last-qualify.json`,
  `/var/lib/orbis-graphics/integration.json`;
- `journalctl -b -k > kernel.txt` after any GPU problem;
- the `.integration.json` receipts from Nobara.

## Acceptance

Graphics is **qualified** for 1.0.0 when, on the CUH-1215A: the integrated image boots and keeps its
fallback, no firmware is missing, 1080p60 is stable, `gl.*` PASS with radeonsi, `vk.device`,
`vk.raster` and `vk.compute` PASS, and every unavailable feature is reported UNSUPPORTED with a
reason. If OpenGL works but Vulkan or compute fails, the release is **DEGRADED**, never qualified.
