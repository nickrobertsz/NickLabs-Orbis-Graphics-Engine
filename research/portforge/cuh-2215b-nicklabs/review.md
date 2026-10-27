# PortForge review: cuh-2215b-nicklabs

Match: **confirmed** cuh-22xx-baikal  base target: cuh-22xx-baikal
Southbridge: baikal  GPU: 0x9923

## Gaps
- [kernel] no working kernel flavor for baikal (aeolia-belize-6.15) -> PortForge gap report: port the 5.4.247-baikal-dfaus Baikal patch set onto 6.15.4, or pin and review the 7.0.8 source once published

## Candidates
- c001 (community, sha256 b0e8b6040e21): review **unreviewed**; apply-check True; register/MMIO access in diff: True

## Capabilities on this target (registry state; nothing is verified until qualified on hardware)
- boot: unsupported
- cpufreq: unsupported
- display-hdmi: unsupported
- ge-telemetry: unsupported
- ge-unified-memory: unsupported
- gpu-3d: unsupported
- gpu-clocks: unsupported
- hdmi-audio: unsupported
- icc-power-fan: unsupported
- rtc: unsupported
- southbridge-io: unsupported
- video-decode: unsupported
- wifi-bt: unsupported

Nothing here has run on a PS4. Approve candidates with `portforge review`, then build on Nobara with recipe.sh.
