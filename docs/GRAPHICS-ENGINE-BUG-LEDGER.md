# Graphics Engine 2.0 bug ledger

This page extends [GRAPHICS-RESEARCH-AND-BUG-LEDGER.md](GRAPHICS-RESEARCH-AND-BUG-LEDGER.md)
(GFX-001…018, ORB-001…010; those entries still apply). GE-B entries were found while building 2.0.
Severity: P0 = blocks boot or display, P1 = wrong results or a lost feature, P2 = quality.
**None of these was observed on Nick's console**; their evidence is the source code and the
community reports cited.

| ID | Sev. | Area | Finding | Evidence | Status in 2.0 |
|---|---|---|---|---|---|
| GE-B01 | P1 | loader | Stock ps4-linux-loader computes one MTRR mask `~(size-1)`. Correct only for power-of-two VRAM sizes; 3072 MiB gives an invalid mask (bad WC MTRR over the carve-out). | loader source d0f4d4c `linux_boot.c` | fixed in the GE loader (≤5 aligned MTRR blocks); `orbis-mem` refuses non-power-of-two sizes with the stock loader |
| GE-B02 | P2 | Orbis 0.2.0 interplay | 0.2.0 correctly refuses non-power-of-two VRAM (`bootfs.validate_vram`, preset list 128…2048). With the GE loader, sizes such as 768 or 1536 become valid, but `orbis boot set-vram` still refuses them | 0.2.0 `platform/orbis/bootfs.py`, `orbis.toml` | by design: use `orbis-mem set-boot --vram-mb N` for GE-only sizes (it validates for the loader in use); 0.2.0 untouched |
| GE-B03 | P1 | GPU clock | After ps4-kexec's GFX soft reset, SCLK stays at the fused startup divider (GPU slower than under GameOS) | rmux liverpool_clk, sony-jaguar-devs GpuWork | ge-0002 restores DID 1 at init/resume/reset (`amdgpu.ps4_sclk_restore=0` to disable) |
| GE-B04 | P1 | HDMI audio | Audio plays at ~0.615× speed: the PS4 bridge path never runs afmt_setmode, so DCCG_AUDIO_DTO0 stays at its reset value | linux-psx 0175 (WALLCLK 14.778→23.966 MHz measured on one Baikal) | ge-0004 programs DTO0 in modeset; listen test pending on CUH-1215A |
| GE-B05 | P1 | HDMI audio | ATI HDMI converters need AC_DIG1_ENABLE or the bridge drops audio packets | linux-psx 0054 | ge-0004 |
| GE-B06 | P1 | thermal | Upstream fan driver forces a 75 °C threshold at probe, overriding the console's own value | rmux ps4_fan | ge-0001 keeps the console's value unless `ps4_fan.threshold_c=` is given |
| GE-B07 | P1 | Graphics Pack 1.0.0 | 1.0.0 `rollback-root.sh` downgrades Mesa before removing `orbis-graphics`, which depends on `mesa-orbis-ps4>=26.2.5-101`; pacman refuses the downgrade | 1.0.0 source review | fixed in the 2.0 rollback (remove pack packages first); 1.0.0 release left as published; on 1.0.0 run `pacman -Rns orbis-graphics-pack orbis-graphics opencl-mesa-orbis-ps4` before its rollback |
| GE-B08 | P1 | UVD | feeRnt 6.15 UVD engine2 bring-up faults (its own log) | feeRnt x_exp__6.15.4-uvd-engine2 @4345927 | research kernel only; never packed |
| GE-B09 | P2 | UVD firmware | UVD/VCE microcode not extracted by the loader | ORB-004 | documented; private per-console firmware needed for any UVD experiment |
| GE-B10 | P2 | clock gating | Liverpool CG/PG flags inherited from 0.2.0 were not reviewed against GameOS behaviour | source review | unchanged; RE workflow question |
| GE-B11 | P2 | telemetry | Liverpool has no `gpu_busy_percent` (no powerplay) | amdgpu source | ge-0008 `orbis_engine_busy` from GRBM/SRBM sampling |
| GE-B12 | P2 | AI | Vulkan prefill faults on long prompts, recovery fails | Carlos Georges report 2026-09-25 | `orbis-ai-gpu` campaign + gate; CPU stays default until a variant passes |
| GE-B13 | P2 | memory | amdgpu's GTT budget is fixed at init (`gtt_size`), so the CPU/GPU split cannot follow the workload | amdgpu_gtt_mgr | ge-0008 runtime budget + `orbis-memd` |
| GE-B14 | P2 | display | No HPD interrupt on Belize; hotplug is missed | rmux 0051 | opt-in polling + DPCD-gated detect (ge-0006) |
| GE-B15 | P2 | diagnostics | Without persistent journal or netconsole, a hard hang leaves no evidence | 0.2.0 defaults | Black Box, persistent journald, netconsole, kdump |
| GE-B16 | P2 | build env | Compiling the kernel series was refused in the authoring environment | this build | build on Nobara with `./orbis-ge kernel`; patches only proven to apply |
| GE-B17 | P2 | Orbis 0.2.0 ISA audit | `isa_audit.classify_insn` treats every `vp*` mnemonic on `%ymm` as AVX2, so the AVX1 float permutes `vperm2f128`/`vpermilps`/`vpermilpd` (valid on Jaguar) are flagged; Rust's slice sort emits `vperm2f128` under `-C target-cpu=btver2` | disassembly of the btver2 `orbis-perf` (32 hits, all `vperm2f128` in `core::slice::sort`) | `rust/tools/isa-check.py` imports the 0.2.0 audit unchanged and excludes the three AVX1 mnemonics; fix for 0.2.x: add them to an AVX1 allow-list |
| GE-B18 | P0 | time | Every boot starts at 1970: `ps4.c` returns 0 for the wall clock and no RTC driver exists (TLS, package dates, logs, Steam) | 0.2.0 kernel source | ge-0010 (loader time), ge-0011 (Aeolia RTC `/dev/rtc0`), loader time handoff, `orbis-time` policy |
| GE-B19 | P1 | 1.0 / 2.0 interplay | `orbis-gpu-profile@<mode>` (1.0) and `orbis-perf` would both set the GPU clock and fan threshold on a mode switch | unit review | `orbis-native` removes the 1.0 wants (restored on removal); `orbis-graphics` no longer adds them when `orbis-perf` is installed; `orbis-perf doctor` warns if they reappear |
| GE-B20 | P1 | runners (pre-release) | Lock-file lookup used a dotted JSON path, so file names with dots were never found and a tampered runner download would have been accepted as a "first download" | regression test while integrating | fixed before release; regression test `fetch_lock_detects_tampering` |
| GE-B21 | P1 | build | `./orbis-ge verify` ended with an error when no stage-0 image was built: `for f in IMG IMG0; do [ -f ] && ...; done` returns 1 under `set -e` | independent audit + review | fixed: explicit `if`; a named but missing `--base-stage0-image` is a clear error, leaving it out skips stage 0 |
| GE-B22 | P1 | build | steps were skipped when an output file existed, even after sources, patches, configs or containers changed; a loader build failure was swallowed | independent audit | fixed: per-step input fingerprints (`out/.fp`), per-package cache keyed by recipe + inputs + toolchain, every failure stops the run with its log |
| GE-B23 | P2 | docs | Nobara instructions ran `cargo test ... \|\| true`, hiding Rust test failures | independent audit | fixed: `./orbis-ge check` (reports a missing cargo explicitly, keeps failures) |
| GE-B24 | P2 | accounting | summing per-client fdinfo GTT counted buffers shared between clients once per client | review for D4 | fixed: private sizes summed exactly, shared buffers reported as a range; device counters stay authoritative |

## Community sources (pinned)

See `manifest/sources.json` → `community_sources`:
* linux-psx 2138063;
* sony-jaguar-devs GpuWork 79d8f7a;
* feeRnt uvd-engine2 4345927;
* aday1 5c1c2fc;
* chrisripp ps4-linux-payloads e1387e2;
* ps4-kexec e79feb8;
* orbis-ports ps4-mesa-docs deb1903 (reference only).
