# NickLabs Orbis — development catalog (implemented vs. pending)

As of 2026-10-26 17:56 PDT. Covers three directives:
* Graphics Engine 2.0;
* the gaming / RTC / Nobara-CachyOS performance directive;
* the Rust directive.

Status labels are the directive's:

| Label | Meaning |
|---|---|
| BUILT | compiled here |
| HOST-VERIFIED | tests pass on the x86 build host with fixtures |
| PS4-VERIFIED | worked on the CUH-1215A; nothing is at this level yet |
| BLOCKED | cannot be done in this environment, with the reason given |
| UNVERIFIED | written and checked as far as possible here; for patches, "applies" |

"Applies" means the patch is proven to apply in order to the pinned kernel tree (feeRnt 108ef97
+ Orbis 0001). No kernel code was compiled here: compiling it was refused in this environment.

## A. Implemented

### Kernel (series `kernel/patches/series`, 13 patches + fragments)
| Item | Where | Status |
|---|---|---|
| ge-0001 fan/thermal hwmon (EMC/ICC) | ge-6.15/0001 | UNVERIFIED (applies) |
| ge-0002 SCLK restore + `ps4_sclk` | ge-6.15/0002 | UNVERIFIED (applies) |
| ge-0003 GDDR5 type / real default clocks | ge-6.15/0003 | UNVERIFIED (applies) |
| ge-0004 HDMI audio DTO + DIG1 | ge-6.15/0004 | UNVERIFIED (applies) |
| ge-0005/0006 bridge A/B knobs, robustness, `ps4_bridge_status` | ge-6.15/0005-0006 | UNVERIFIED (applies; defaults = 0.2.0) |
| ge-0007 ps4-cpufreq | ge-6.15/0007 | UNVERIFIED (applies) |
| ge-0008 engine-busy telemetry + runtime GTT budget | ge-6.15/0008 | UNVERIFIED (applies) |
| ge-0009 SMU mailbox (opt-in) | ge-6.15/0009 | UNVERIFIED (applies) |
| ge-0010 wall clock from loader time (`orbis.time=`, `time=`) | ge-6.15/0010 | UNVERIFIED (applies); epoch parser HOST-VERIFIED (extracted + compiled) |
| ge-0011 Aeolia RTC class device `/dev/rtc0` (C, inside apcie) | ge-6.15/0011 | UNVERIFIED (applies); counter register still unknown |
| ge-0012 CachyOS subset (EEVDF defaults, le9uo, ADIOS, evdev RCU, userns sysctl, -O3 option) | ge-6.15/0012 | UNVERIFIED (applies) |
| ge-0013 Jaguar `-march=btver2` kernel option | ge-6.15/0013 | UNVERIFIED (applies) |
| BORE variant (CachyOS bore-cachy 6.15) | patches/variants/bore | UNVERIFIED (applies on top) |
| UVD research diff (feeRnt engine2) | patches/research | UNVERIFIED (applies on top); known to fault |
| Config: GE fragment (Black Box, cpufreq, PSI) | config/orbis-ge.fragment | UNVERIFIED |
| Config: perf fragment (sched_ext/BTF, NTSYNC, MGLRU, zram, dmem, cpusets, RTC, BBR/fq_codel, controllers, Jaguar) | config/orbis-perf.fragment | UNVERIFIED |
| Rust-for-Linux (`CONFIG_RUST=y`, pinned rustc 1.87.0 + rust-src + bindgen) | build-ge-kernel.sh | UNVERIFIED (build wiring; asserted at build) |
| Kernel build script (pahole for BTF, `--bore`, `--research-uvd`, `--no-rust`, option assertions, CMOS RTC forbidden) | kernel/build-ge-kernel.sh | UNVERIFIED (syntax-checked) |
| Kernel packages (`linux-orbis-ps4-ge`) | kernel/package | UNVERIFIED |

### Loader
| Item | Status |
|---|---|
| Exact VRAM MTRR split, validated `vram.txt`, `gtt.txt`, boot identity | HOST-VERIFIED (C helpers compiled from the patch text) |
| Time handoff `orbis.time=` + `orbis.time_tsc=`, 2048-byte cmdline cap | HOST-VERIFIED (compose tested); payload build BLOCKED here (Nobara) |

### Userspace (Python, Graphics Engine 2.0; preserved per the Rust directive)
| Item | Status |
|---|---|
| orbis_engine: telemetry, memory ownership + GTT policy (orbis-memd), controls, orbis-hwd API, Black Box, orbis-debug, video test/configure, AI Vulkan campaign, kdump, register decode | HOST-VERIFIED (45 engine tests) |
| orbis_gfx 2.0.0: GE qualify checks (sclk, fan, GTT budget, loader contract, cpufreq, bridge) | HOST-VERIFIED |
| Stage-0 overlay with GE evidence and engine telemetry | HOST-VERIFIED (script tests) |
| Black Box receiver for Nobara | UNVERIFIED (byte-compiled only) |

### Userspace (Rust, new components)
| Item | Status |
|---|---|
| orbis-core: JSON, sysfs/fs helpers, /proc, restore journal, syscalls, dates, args | BUILT + HOST-VERIFIED (9 tests) |
| orbis-time: status / boot policy / save / set / LAN-only NTP / RTC counter discovery | BUILT + HOST-VERIFIED (2 tests, more to add) |

### Graphics packages, integration, docs
| Item | Status |
|---|---|
| Mesa 26.2.5-201 + linux-psx 0002 (UVD AVC caps, fail-closed) | UNVERIFIED (0002 dry-run applied to pinned Mesa source) |
| Packages: orbis-graphics 2.0.0, orbis-graphics-engine 2.0.0, meta 2.0.0 | UNVERIFIED (recipes) |
| Pack assembly (kernel/loader info, receiver), integrator (GE kernel staging, loader payloads, stage-0 with GE kernel), rollback order fix (GE-B07) | HOST-VERIFIED (integrator tests) |
| One command `./orbis-ge all` | UNVERIFIED (syntax-checked; runs on Nobara) |
| Docs: engineering report, memory, video, Black Box, AI, RE workflow, capability report, GE bug ledger | written |
| Checkpoints: orbis-pre-gaming-rtc-nobara-20261026, orbis-pre-rust-20261026 | saved |

## B. Pending, in priority order

P0 means needed for a coherent, bootable release; P1 adds features from the directives; P2 is
quality and extras.

| # | Prio | Item | Language | Notes |
|---|---|---|---|---|
| 1 | P0 | **orbis-perf** controller + daemon: mode policy (governor, scheduler, sysctl/vm, THP, MGLRU, zram, I/O scheduler, IRQ affinity, cgroup slices/cpusets), adaptive GPU clock + thermal guard, restore journal, owns knobs the 1.0 profile units also touch | Rust | single daemon; orbis-memd keeps only the GTT budget (no overlap) |
| 2 | P0 | sched_ext management: available scx schedulers, `orbis-scx@.service`, switching, failure detection → EEVDF fallback, BORE detection | Rust + systemd | scx-scheds from the Arch snapshot (Rust schedulers) |
| 3 | P0 | systemd units + packaging for the Rust tools (orbis-native: orbis-time, orbis-perf, orbis-games/orbis-run, orbis-isa-check; btver2 + LTO build; time units/timer) | PKGBUILD | wire into the pack, meta package and integrator |
| 4 | P0 | Conflict handling: retire orbis-gpu-profile@ wiring when orbis-perf is present; detect falcond/gamemode/power-profiles/irqbalance | Rust + install | prevents competing daemons |
| 5 | P0 | Integrate with `orbis status` / `orbis doctor` without modifying 0.2.0: 0.2.0 has no plugin hook, so add `orbis-status`/`orbis-doctor-ext` and an `/usr/local/bin/orbis` wrapper that appends the GE/perf/time sections | Rust or shell | 0.2.0 stays untouched |
| 6 | P1 | **Gaming**: Vulkan capability probe (vulkaninfo JSON) → DXVK 3.x / 2.x / 1.10.3 / WineD3D and VKD3D-Proton eligibility; runner catalog (Proton 11/10, GE-Proton11-11/10-34/7-55, Wine, Lutris) with pins | Rust | pins from upstream tags (done in research) |
| 7 | P1 | `orbis-run` launcher (Steam launch option / Lutris wrapper): per-game profile → env (runner, DXVK, renderer, shader cache, audio, logging), systemd scope in orbis-game.slice, perf hand-off, log classifier (driver / Wine / Proton / launcher / DRM / anti-cheat / game / network-404) | Rust | |
| 8 | P1 | Game detection in the daemon (Steam reaper AppId, *.exe under Wine, orbis-run markers, profile names), per-game perf profile, dmem.low protection, restoration on exit | Rust | falcond-equivalent |
| 9 | P1 | Gaming package set + offline bundle: steam, wine, lutris, gamescope, mangohud, lib32 deps from the Arch snapshot; Proton-GE/DXVK tarballs with sha512 (GE) or first-fetch SHA-256 lock; mirrors; LAN-only network policy (Steam needs internet: explicit, documented options) | build scripts | |
| 10 | P1 | Jaguar ISA guard `orbis-isa-check` (objdump-based AVX2/BMI2/FMA/AVX-512/SSE4a scan) in pack build + doctor; 0.2.0 already has isa_audit.py, which is reused as reference | Rust | |
| 11 | P1 | Scheduler/latency measurement `orbis-perf bench` (wakeup latency under load, per scheduler) → measured "auto" choice per workload | Rust | short, bounded runs only |
| 12 | P1 | AI-mode memory safety: MemoryHigh/low for the AI service, background ManagedOOM, zram zstd sizing | Rust + units | |
| 13 | P1 | orbis-time systemd units (boot, save timer, shutdown), Python Black Box records the time source | units | |
| 14 | P2 | Tests: Rust fixture tests for perf/games/time boot policy; Python cross-checks | Rust/Python | |
| 15 | P2 | Docs: performance architecture (Nobara/CachyOS mapping), gaming guide, RTC/time doc, Rust toolchain doc, release status with the 5 labels, CHANGELOG/README, Nobara commands, rollback | docs | |
| 16 | P2 | Final packaging: source archive, git bundle, SHA-256 manifests, memory checkpoint, delivery | — | |

## C. Blocked or hardware-limited (honest limits)

| Item | Why |
|---|---|
| Compiling kernel / payloads / Mesa / packages here | refused or unavailable in this environment; exact commands are in `./orbis-ge` |
| Native RTC calendar without any loader time | the counter's register is undocumented: `orbis-time rtc-probe` finds it on the console; the calendar offset then persists |
| Hardware UVD decode by default | no working 6.15 UVD bring-up (research kernel only) |
| Runtime VRAM carve-out resize, HMM/SVM | not possible on GFX7 / with a loader-fixed carve-out (see memory doc) |
| Steam online features with a LAN-only console | Steam needs internet for login/downloads; Orbis offers explicit options, never opens it silently |
| PS4 secure clock | Sony's; not reachable or changed from Linux |
