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
| GE-B25 | P0 | kernel (ge-0014) | Shrink moved buffers out of GTT via `ttm_bo_validate()` (move_notify `evict=false`), so per-VM always-valid buffers were only marked moved, never revalidated: GPU access to an unpopulated buffer | `amdgpu_vm_bo_invalidate()` puts always-valid BOs on `vm->evicted` only when `evicted` is true; radeonsi uses VM_ALWAYS_VALID widely | fixed in ge-0016 (`amdgpu_ttm_orbis_evict_to_system` + `ttm_bo_shrink(.allow_move=false)`); Awaiting Nobara compilation |
| GE-B26 | P0 | kernel (TTM) | CVE-2026-52949: `ttm_bo_shrink()` re-added a buffer whose backup failed ahead of the LRU cursor: infinite walk | upstream 1d59f36e95f7 (+ prerequisite b2ed01e7ad) | backported as stable-6.15/0001-0002, first in the series |
| GE-B27 | P1 | kernel (ge-0014) | Active trim reported success after stopping at the first unmovable buffer; the requested budget was not distinguishable from the effective one | ge-0014 `orbis_gtt_budget_store` ignored trim errors | fixed in ge-0016 (GE-A02) |
| GE-B28 | P1 | userspace (r2) | Per-game dmem limits could never exist on a systemd scope: nothing enabled the dmem controller in `cgroup.subtree_control` below the root (systemd does not manage dmem) | code review: no `subtree_control` write anywhere in r2 | fixed: orbis-perf enables dmem on every ancestor before writing `dmem.max`, and reads the value back |
| GE-B29 | P1 | userspace (r2) | Retroactive limits targeted the first detected process's cgroup, for orbis-run games the caller's (Steam client) cgroup, limiting Steam itself; and limits came after the game's first allocations | `games.rs` root_cgroup; GE-A03 | fixed: pre-launch admission into the game's own scope; retroactive limits only on cgroups holding nothing but the game |
| GE-B30 | P2 | probe (r2) | Host-pointer import used the raw size and the first importable type; no flush/invalidate for non-coherent memory; a zero-copy FAIL did not change the exit code; partial allocations leaked | `orbis-gfx-probe.c` r2 | fixed (GE-A04/A08/A10) |
| GE-B31 | P2 | accounting (r2) | DRM clients keyed by (pid, client id): one client inherited by a child process counted twice | `memory.gpu_clients` r2 | fixed (GE-A07): (drm-pdev, client id) with PID sets + device-counter check |
| GE-B32 | P1 | build (r2) | `build-pack.sh` ran `make -C` inside the 0.2.0 checkout and mounted its keys read-write (gpg writes its home); an old BORE kernel or stage-0 image could enter a release | GE-A05/A06 | fixed: private copies, checkout guard, explicit artifact selection with provenance |
| GE-B33 | P0 | build (r3) | `fresh()` reused an output when its input fingerprint matched, without checking its SHA-256; `verify` dropped an image that failed provenance and still succeeded, so `all` could print Done without a verified main image | r3.1 audit P0 | fixed: provenance + checksum reuse, `prov()` fails missing outputs (exit 7), `release-status` gate, FAILED receipt, no Done |
| GE-B34 | P1 | probe (r3) | Non-coherent imports were synchronized through the Vulkan mapping while verification used the original pointer, without establishing that the two alias; non-host-visible imports were accessed; the initial fill before the import could leave dirty lines that overwrite GPU results | r3.1 audit P1; VK_EXT_external_memory_host | fixed: host-visible required, alias verified or INCOMPATIBLE, fill after import + flush |
| GE-B35 | P1 | kernel (ge-0008/0016) | Runtime GTT budget written into `ttm_resource_manager.size` (documented constant; read by validate_size, info ioctl, sysfs) | r3.1 audit P1 | fixed in ge-0017: separate atomic budget, constant capacity |
| GE-B36 | P2 | kernel (ge-0016) | Budget mutex destroyed before eviction and skipped on the early-return path | r3.1 audit P1 | fixed in ge-0017 |
| GE-B37 | P1 | admission (r3) | Request metadata checked, then the path re-opened; PID not bound to a process; replies world-readable (0755 dir, default file mode); predictable temporary names in the sticky directory; scope ownership and membership not verified | r3.1 audit P1 | fixed: fd-based reads, start-time binding, private replies, O_EXCL temporaries, scope ownership/membership |
| GE-B38 | P2 | build (r3) | Image named for a target was not checked against the base image's own profile | r3.1 audit P2 | fixed: `build/target_identity.py` (exit 8, receipt evidence) |
| GE-B39 | P2 | build (r3) | `out/RELEASE-MANIFEST.json` kept targets of earlier runs next to current ones | r3.1 audit P2 | fixed: `current_run` (one run id) vs `history` |
| GE-B40 | P1 | build (r2) | Package cache keys missed inputs (orbis-graphics read `integrator/root`, `manifest/sources.json`, `diag/schema`… outside its key: stale reuse); a documentation change recompiled the probe; the pack fingerprint covered the whole tree | found while fixing r3.1 P2-8 | fixed: keys = exactly what each PKGBUILD reads (test), `orbis-graphics-docs`, `build/fingerprint.py` |

## Community sources (pinned)

See `manifest/sources.json` → `community_sources`:
* linux-psx 2138063;
* sony-jaguar-devs GpuWork 79d8f7a;
* feeRnt uvd-engine2 4345927;
* aday1 5c1c2fc;
* chrisripp ps4-linux-payloads e1387e2;
* ps4-kexec e79feb8;
* orbis-ports ps4-mesa-docs deb1903 (reference only).
