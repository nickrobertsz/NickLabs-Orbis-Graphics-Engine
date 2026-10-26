# Graphics performance plan (Graphics Pack 1.0.0)

Rules: every change is reversible and recorded; nothing is overclocked; Vulkan compute is never
disabled globally; numbers are only claimed when measured on the CUH-1215A with the exact versions
written next to them. Sony's 1.84 TFLOPS is a theoretical figure, not a target.

## What can be tuned on the 0.2.0 kernel

| Knob | Available | How |
|---|---|---|
| GPU performance level / clocks | **no** (no powerplay for Liverpool) | skipped with the reason in every profile action |
| CPU governor | only if a cpufreq driver is present | `/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor` |
| Session environment for graphics | yes | `/etc/environment.d/90-orbis-graphics.conf`, applies to new sessions |
| GPU memory carve-out | yes, at boot | recommendation only; changing it is `orbis boot set-vram <MB>` + reboot (0.2.0) |

## Profiles (`orbis-gpu profile`)

| Profile | GPU level | CPU governor | Session environment | VRAM advice |
|---|---|---|---|---|
| balanced (PC default) | unchanged | unchanged | — | — |
| performance (GAMING default) | high (skipped on 0.2.0) | performance | `MESA_SHADER_CACHE_MAX_SIZE=4G` | 2048 MB |
| quiet | low (skipped on 0.2.0) | powersave | — | — |
| compute (AI default) | auto (skipped on 0.2.0) | performance | — | 1024 MB |
| diagnostic | unchanged | unchanged | `MESA_SHADER_CACHE_DISABLE=true` (cold, repeatable measurements) | — |

```sh
orbis-gpu profile list --json                 # machine-readable, for a future GUI
sudo orbis-gpu profile set performance --dry-run
sudo orbis-gpu profile set performance --reason "testing a game"
sudo orbis-gpu profile reset                  # restores the exact values recorded before the first change
orbis-gpu capabilities --json                 # which knobs this kernel actually offers
```

State: `/var/lib/orbis-graphics/profile-state.json` (original values). Every change is a line in
`/var/log/orbis/graphics-profile.jsonl` (time, profile, reason, each action or the reason it was
skipped). Personality defaults (PC→balanced, GAMING→performance, AI→compute) are in
`/etc/orbis/graphics.json` and applied by `orbis-gpu-profile@<mode>.service` when Orbis switches
personality. Removing the package resets the profile.

Per-application workarounds from the community (`RADV_DEBUG=nocompute`, `amdgpu.abmlevel=0`, ...)
are **not** set by any profile; use them per application while diagnosing, e.g.
`RADV_DEBUG=nocompute %command%` in one game's launch options.

## What is measured

| Measurement | Where | Default |
|---|---|---|
| 1080p frame time, median and p95 | `gl.frame_timing` (probe `--gl-bench`, 120 frames) | yes |
| draw-call throughput (5000 small draws) | `gl.frame_timing` evidence | yes |
| Vulkan pipeline creation with/without cache | `vk.pipeline_cache` | yes |
| Vulkan compute / OpenCL correctness and time | `vk.compute`, `cl.rusticl` | yes |
| sustained load: frame-time drift, temperatures | `perf.sustained` (`--sustained SECONDS`) | opt-in |
| graphics after PC/GAMING/AI switches | `modes.transitions` | opt-in |
| GPU reset recovery | `gpu.recovery` | opt-in, explicit consent flag |

`gl.frame_timing` is DEGRADED when p95 > 2 × median (stutter). `perf.sustained` FAILs on any GPU
fault and is DEGRADED when the frame rate drops by more than 15 % between start and end.

## How to compare two configurations

Run the same command on the same console, display mode, VRAM setting and profile, at room
temperature, three times each, and compare `evidence.frame_ms.median` / `p95` in the JSON receipts:

```sh
for i in 1 2 3; do sudo orbis-gpu qualify --json-out /tmp/a-$i.json --quiet; done
python3 -c "import json,sys; [print(f, [c['evidence'].get('frame_ms') for c in json.load(open(f))['checks'] if c['id']=='gl.frame_timing']) for f in sys.argv[1:]]" /tmp/a-*.json
```

Report results with: kernel release, Mesa line and version, `vram.txt`, profile, resolution,
ambient temperature.

## Later (needs a kernel with powerplay)

Clock/DPM control, a measured fan/thermal policy and per-profile GPU levels become possible only
with a kernel that gives Liverpool a powerplay block (research candidates: sony-jaguar-devs 6.18
SCLK work). Until then the pack reports `pm.clocks` UNSUPPORTED instead of pretending.
