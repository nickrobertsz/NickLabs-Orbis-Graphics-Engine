# NickLabs Orbis Graphics Engine 2.0.0-r3 on Nobara: exact commands

Everything below runs on the Nobara machine, except section 3 onward, which runs on the console.
Nothing touches:
- the Orbis 0.2.0 checkout: it is only read, and checked unchanged before and after each step;
- its images: they are copied first;
- the Graphics Pack 1.0.0 release;
- any PS4 drive.

`sudo` is used only for podman and loop devices.

```sh
ORBIS=~/orbis-release/nicklabs-ps4-orbis-linux          # the 0.2.0 checkout you built 0.2.0 from
BASE=$ORBIS/out/release/orbis-0.2.0-cuh-1215a.img        # your 0.2.0 full image (name as you have it)
BASE0=$ORBIS/out/release/orbis-0.2.0-stage0.img          # optional: the 0.2.0 stage-0 image (if you have one)
STAGE0=(); [ -f "$BASE0" ] && STAGE0=(--base-stage0-image "$BASE0")   # passed only when the file exists
sudo dnf install -y podman git zstd python3               # once
```

## 1. Source

```sh
mkdir -p ~/orbis-release && cd ~/orbis-release          # put the delivered files here
sha256sum -c SHA256SUMS                                   # every delivered file must say OK
git clone -b engine-2.0 nicklabs-orbis-graphics-engine-2.0.0-r3.bundle nicklabs-orbis-graphics-engine
#   (or: tar -xzf NICKLABS-ORBIS-GRAPHICS-ENGINE-SOURCE-2.0.0-r3.tar.gz && mv nicklabs-orbis-graphics-engine-2.0.0-r3 nicklabs-orbis-graphics-engine)
cd nicklabs-orbis-graphics-engine
./orbis-ge check        # Python tests, target registry check, Rust tests when cargo is installed (a failure stops here)
./orbis-ge target list  # the board-support registry; cuh-1215a is the reference target
```

## 2. Everything in one command

```sh
./orbis-ge all --orbis-src $ORBIS --target cuh-1215a --base-image $BASE "${STAGE0[@]}"   # add --bore for the BORE kernel package
```

How the steps run:
* A step is skipped only when its output exists **and** its input fingerprint is unchanged. The
  fingerprint covers: sources, patches, config fragments, locks, the 0.2.0 checkout, the build
  containers, the options and the target. `--rebuild` forces a step.
* Inside the pack step every package has its own cache (`out/pkgcache/`), so changing a document
  does not rebuild Mesa.
* Every step writes `out/logs/<step>.log`; the pack's per-package logs are in `out/pack/logs/`. A
  failing step stops the run with its exit code.
* Every output gets a `<file>.provenance.json`. The pack takes only the artifacts selected for this
  run (`out/artifact-selection.json`).
* `./orbis-ge status --target cuh-1215a` shows which outputs are current and which are stale.

| Step | Command alone | Output | Time |
|---|---|---|---|
| kernel | `./orbis-ge kernel --orbis-src $ORBIS` | `out/kernel-ge/` (6.15.4-orbis-ps4-ge: 2 stable TTM backports + 16 GE patches, Rust-for-Linux, sched_ext/BTF, RTC, CachyOS subset) | 20-60 min |
| loader | `./orbis-ge loader` | `out/loader-ge/` (payloads with the memory contract and time handoff) | 1 min |
| pack | `./orbis-ge pack --orbis-src $ORBIS` | `out/release/ORBIS-GRAPHICS-ENGINE-v2.0.0.tar.zst` | 1-3 h |
| image | `./orbis-ge image --target cuh-1215a --base-image $BASE` | `out/targets/cuh-1215a/images/orbis-0.2.0+ge-2.0.0-cuh-1215a.img` (a copy; the base is unchanged) | 5-10 min |
| stage0 | `./orbis-ge stage0 --target cuh-1215a --base-stage0-image $BASE0` | `out/targets/cuh-1215a/images/orbis-0.2.0-stage0+ge-2.0.0-cuh-1215a.img` | 2 min |
| verify | `./orbis-ge verify --target cuh-1215a --base-image $BASE` | `out/targets/cuh-1215a/images/SHA256SUMS`, `out/targets/cuh-1215a/ORBIS-GE-RELEASE-RECEIPT.json`, `out/RELEASE-MANIFEST.json` | 1 min |

The pack contains:
- Mesa 26.2.5-201 (+lib32);
- orbis-graphics;
- orbis-graphics-engine (now with the target registry and `orbis-hw identify`);
- **orbis-native**: Rust, btver2 + LTO, ISA-gated;
- **orbis-gaming**;
- the GE kernel package.

Optional extras:

```sh
./orbis-ge kernel --orbis-src $ORBIS --bore                  # BORE kernel -> out/kernel-ge-bore/; packed only when the pack step also gets --bore
./orbis-ge kernel --orbis-src $ORBIS --research-uvd          # research UVD kernel, never packed
./orbis-ge kernel --orbis-src $ORBIS --research-sgdisplay    # research GTT-scanout kernel (R-M1), never packed
./orbis-ge runners                                           # Proton/DXVK/VKD3D offline bundle -> out/runners/ (several GB)
git add gaming/runners.lock.json && git commit -m "runner checksums"   # after reviewing the new SHA-256 lines
```

Other targets: see `docs/REPRODUCIBLE-BUILD-RECIPES.md`. Baikal boards are refused with exit 6;
`docs/TARGET-PORTING-GUIDE.md` explains why and what comes next.

If a step fails:
- **pack:** its last 25 log lines are printed; full logs are in `out/pack/logs/`.
- **orbis-native in `check()`:** the Rust tests or the ISA gate failed. `out/pack/logs/orbis-native.log`
  names the binary and the instructions found.
- **exit 5 (pack refuses):** an artifact is stale or missing. The selection output above it names
  which one.
- **exit 9:** the 0.2.0 checkout changed during the build. Send `out/logs/` to NickLabs.

## 3. First boot on the CUH-1215A

1. Write `out/targets/cuh-1215a/images/orbis-0.2.0-stage0+ge-2.0.0-cuh-1215a.img`, or the full
   image, to the USB drive. Use Fedora Media Writer, or `dd` to the **whole USB device**, never a
   PS4 drive.
2. Boot as with 0.2.0: GoldHEN, then the payload `orbis-ge-<N>mb.bin` (default 1024 MB VRAM).
3. On the console:

```sh
sudo orbis-hw identify                 # must say: confirmed cuh-1215a (GPU 0x9922 + Belize PCI IDs + your model)
sudo orbis-gpu qualify                 # graphics qualification (PASS/FAIL/DEGRADED/UNSUPPORTED per check)
sudo orbis-mem bench --mb 64           # zero-copy + bandwidth; exit 0 PASS, 2 NOT_RUN, 4 unsupported here, 1/3/5 failures
sudo orbis-hw qualify --target cuh-1215a   # qualification record in /var/lib/orbis-graphics/qualification/
sudo orbis-perf doctor                 # kernel features, schedulers, dmem regions (vram/gtt), time, Vulkan
orbis-time status                      # should show source "loader" (or "rtc-hardware")
sudo orbis-games caps --save           # Vulkan → DXVK/VKD3D line (also runs at boot)
sudo orbis-debug collect --hardware    # evidence bundle for NickLabs (probe + identification included)
```

The original `orbis status` and `orbis doctor` from 0.2.0 still work unchanged.

## 4. Gaming setup on the console

```sh
scp -r out/runners <user>@<ps4-ip>:~/runners                       # on the Nobara
orbis-games install --bundle ~/runners && orbis-games runners      # on the console
# Steam game → Properties → Launch Options:  orbis-run %command%
orbis-games last "<game>"                                          # diagnosis after a failure
sudo orbis-perf sched bench --seconds 8 && sudo orbis-perf sched set auto     # optional, measured
sudo orbis-time ntp enable --server <Nobara LAN IP>                # optional LAN time (chronyd on Nobara: allow <LAN>/24)
```

Per-game memory limits live in the game profile:
- `memory.dmem_max_mb` and `memory.gtt_max_mb` set the limits;
- `memory.require_limits: true` makes them mandatory.

`orbis-run` applies these limits to the game's own scope before the game starts. When required
limits cannot be applied, the game is not started (exit 75), and `orbis-games last` shows why.

Steam itself needs the internet once; see `GAMING.md` §6 and `orbis-games network`. The PS4 stays
LAN-only. External gaming access waits for Nick's decision.

## 5. Rollback

* Flash the original 0.2.0 image again. It was never modified.
* Or turn the GE image back into a 0.2.0-equivalent one on the Nobara:
  `./orbis-graphics rollback --image out/targets/cuh-1215a/images/orbis-0.2.0+ge-2.0.0-cuh-1215a.img --output out/rolled-back.img`
* On a running console: `sudo /usr/lib/orbis-graphics/rollback-root.sh --root / --boot /boot/ps4`
* Kernel only: `sudo orbis boot rollback-kernel` (0.2.0 keeps the previous kernel as
  `bzImage.orbis-prev`). The same command undoes a switch to the BORE kernel.
* Performance changes only: `sudo orbis-perf restore` (also done when `orbis-perf.service` stops).

## 6. Adding the pack to an installed console (no reflash)

```sh
scp out/release/ORBIS-GRAPHICS-ENGINE-v2.0.0.tar.zst <user>@<ps4-ip>:/tmp/
ssh <user>@<ps4-ip>
cd /tmp && tar --zstd -xf ORBIS-GRAPHICS-ENGINE-v2.0.0.tar.zst && cd ORBIS-GRAPHICS-ENGINE-v2.0.0
sha256sum -c SHA256SUMS --quiet && echo PACK-OK
sudo ./integrator/root/integrate-root.sh --root / --pack . --boot /boot/ps4 --dry-run
sudo ./integrator/root/integrate-root.sh --root / --pack . --boot /boot/ps4
sudo reboot
```
