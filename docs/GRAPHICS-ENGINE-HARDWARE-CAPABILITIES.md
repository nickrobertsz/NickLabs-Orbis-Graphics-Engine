# Hardware capability report — CUH-1215A under Orbis Graphics Engine 2.0

Legend for the columns:
* *Hardware*: what the silicon has.
* *0.2.0*: the Orbis 0.2.0 kernel.
* *GE 2.0*: what the Graphics Engine kernel and tools expose. This is the expected state; nothing
  has been verified on hardware. `orbis-hw capabilities` and `orbis-gpu qualify` report the real
  state on the console.

| Area | Hardware | 0.2.0 | GE 2.0 | Interface / note |
|---|---|---|---|---|
| GPU identity / amdgpu | Liverpool 1002:9920 (GFX7, 18 CU) | yes | yes | `orbis-gpu qualify` gpu.identity |
| GPU engine clock control | SCLK divider (CG_SCLK_CNTL) | no — stays at fused startup divider after kexec | **restore to full speed at boot + max/startup switch** | `ps4_sclk`, qualify `pm.sclk` |
| Standard DPM (power levels, pp_dpm_*) | SMU7-like, undocumented | no (no powerplay block) | no | `pm.clocks` UNSUPPORTED (reason given) |
| Memory P-states | SMU messages 0x57–0x59 (community) | no | expert read/command, opt-in | `amdgpu.ps4_smu_mailbox=1` |
| Overclocking / voltage | — | no | **no** (never touched) | — |
| Clock gating | CIK CG flags | as 0.2.0 | unchanged | ledger GE-B10 |
| GPU busy % | GRBM/SRBM status | no (`gpu_busy_percent` absent) | per-engine busy sampling | `orbis_engine_busy` |
| APU temperature | EMC sensor via ICC | no (k10temp CPU only) | yes | `ps4_fan` temp1_input |
| Fan control | EMC threshold via ICC | no | threshold 45–85 °C (Orbis range) | `ps4_fan` temp1_crit, `orbis-hw set fan-threshold` |
| Fan RPM | EMC field unreliable | no | no (removed upstream) | — |
| CPU frequency scaling | Jaguar HW P-states | no driver | `ps4-cpufreq` + governors | `/sys/devices/system/cpu/*/cpufreq` |
| VRAM carve-out | any size within the 8 GiB pool | loader, power-of-two sizes only | GE loader: any 16 MiB multiple that fits 5 MTRRs | `vram.txt`; `orbis-mem set-boot` |
| Runtime carve-out resize | — | no | **no** (not possible, see memory doc) | — |
| GTT size at boot | GART/GPUVM | kernel default | `gtt.txt` (GE loader) | `ttm.pages_limit`, `amdgpu.gttsize` |
| GTT budget at runtime | — | no | yes | `orbis_gtt_budget`, `orbis-memd` |
| Recoverable GPU faults (HMM/SVM) | not in GFX7 | no | **no** | — |
| UVD 4.2 decode (H.264, MPEG-2, VC-1, MPEG-4) | yes | no (IP block disabled) | no in the default kernel; research kernel only | `orbis-video test` decides |
| HEVC / VP9 / AV1 decode | **not in hardware** | software | software | — |
| VCE 2.0 encode | yes | no | no | — |
| HDMI output via MN864729 | yes | 1080p60 | 1080p60 + opt-in 720p fallback, A/B knobs | `ps4_bridge_status` |
| HDMI audio | yes | plays at ~0.615× speed (DTO not programmed) | **DTO programmed + DIG1** | listen test in qualification list |
| Hotplug detection | no HPD IRQ on Belize | as 0.2.0 | opt-in polling + DPCD-gated detect | `amdgpu.ps4_bridge_poll=1`, `…_dpcd_detect=1` |
| Vulkan | RADV GFX7 | yes | yes (Mesa 26.2.5-201) | qualify vk.* |
| OpenGL / GLES | radeonsi | yes | yes | qualify gl.* |
| OpenCL | RustiCL | yes (1.0.0 pack) | yes | qualify cl.rusticl |
| ROCm/HIP | needs amdkfd (GFX7 + HSA) | no | no | ledger ORB-010 |
| Kernel log over LAN | — | no | netconsole (dynamic) | `orbis-debug stream` |
| Crash capture kernel | — | no | kdump, experimental | `orbis-debug kdump` |
| GPU reset dumps | amdgpu devcoredump | built? | yes (`CONFIG_DEV_COREDUMP=y`) | Black Box harvests |
