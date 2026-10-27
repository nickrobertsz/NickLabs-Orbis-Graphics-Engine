# NickLabs Orbis Graphics Engine 2.0.0 on Nobara: exact commands

Everything below runs on the Nobara machine. Nothing touches the Orbis 0.2.0 checkout (it is only
read), its images (copied first), the Graphics Pack 1.0.0 release or any PS4 drive. `sudo` is used
only for podman and loop devices.

```sh
ORBIS=~/orbis-release/nicklabs-ps4-orbis-linux          # the 0.2.0 checkout you built 0.2.0 from
BASE=$ORBIS/out/release/orbis-0.2.0-cuh-1215a.img        # your 0.2.0 full image (name as you have it)
BASE0=$ORBIS/out/release/orbis-0.2.0-stage0.img          # optional: the 0.2.0 stage-0 image
sudo dnf install -y podman git zstd python3               # once
```

## 1. Source

```sh
mkdir -p ~/orbis-release && cd ~/orbis-release
git clone nicklabs-orbis-graphics-engine-2.0.0.bundle nicklabs-orbis-graphics-engine   # or unpack the source archive
cd nicklabs-orbis-graphics-engine
sha256sum -c ../SHA256SUMS --ignore-missing               # the delivered manifest
python3 -m unittest discover -s tests/unit -q             # 92 host tests, about 1-2 min
( cd rust && cargo test --offline -q ) || true             # only if Rust is installed on the Nobara (not required)
```

## 2. Everything in one command

```sh
./orbis-ge all --orbis-src $ORBIS --base-image $BASE --base-stage0-image $BASE0
```

These steps run in order. A step whose output already exists is skipped; `--rebuild` forces it.

| Step | Command alone | Output | Time |
|---|---|---|---|
| kernel | `./orbis-ge kernel --orbis-src $ORBIS` | `out/kernel-ge/` (6.15.4-orbis-ps4-ge, Rust-for-Linux, sched_ext/BTF, RTC, CachyOS subset) | 20-60 min |
| loader | `./orbis-ge loader` | `out/loader-ge/` (payloads with the memory contract + time handoff) | 1 min |
| pack | `./orbis-ge pack --orbis-src $ORBIS` | `out/release/ORBIS-GRAPHICS-ENGINE-v2.0.0.tar.zst`: Mesa 26.2.5-201 (+lib32), orbis-graphics, orbis-graphics-engine, **orbis-native** (Rust, btver2 + LTO, ISA-gated), **orbis-gaming**, GE kernel package | 1-3 h |
| image | `./orbis-ge image --base-image $BASE` | `out/images/orbis-0.2.0+ge-2.0.0.img` (a copy; base unchanged) | 5-10 min |
| stage0 | `./orbis-ge stage0 --base-stage0-image $BASE0` | `out/images/orbis-0.2.0-stage0+ge-2.0.0.img` | 2 min |
| verify | `./orbis-ge verify` | `out/images/SHA256SUMS`, `out/ORBIS-GE-RELEASE-RECEIPT.json` | 1 min |

Optional extras:

```sh
kernel/build-ge-kernel.sh --orbis-src $ORBIS --bore          # BORE kernel -> out/kernel-ge-bore/ (packed as linux-orbis-ps4-ge-bore, not installed by default)
./orbis-ge kernel --orbis-src $ORBIS --research-uvd          # research UVD kernel, never packed
./orbis-ge runners                                           # Proton/DXVK/VKD3D offline bundle -> out/runners/ (several GB)
git add gaming/runners.lock.json && git commit -m "runner checksums"   # after reviewing the new SHA-256 lines
```

If the pack step fails, its last 25 log lines are printed; full logs are in `out/pack/logs/`. If
`orbis-native` fails in `check()`, the Rust tests or the ISA gate failed:
`out/pack/logs/orbis-native.log` names the binary and the instructions found.

## 3. First boot on the CUH-1215A

1. Write `out/images/orbis-0.2.0-stage0+ge-2.0.0.img` (or the full image) to the USB drive: Fedora
   Media Writer, or `dd` to the **whole USB device**, never a PS4 drive.
2. Boot as with 0.2.0 (GoldHEN → payload `orbis-ge-<N>mb.bin`, default 1024 MB VRAM).
3. On the console:

```sh
sudo orbis-gpu qualify                 # graphics qualification (PASS/FAIL/DEGRADED/UNSUPPORTED per check)
sudo orbis-perf doctor                 # kernel features, schedulers, time, Vulkan, single-manager check
orbis-time status                      # should show source "loader" (or "rtc-hardware")
sudo orbis-games caps --save           # Vulkan → DXVK/VKD3D line (also runs at boot)
orbis-perf status
sudo orbis-debug collect               # evidence bundle for NickLabs (or orbis-debug export)
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

Steam itself needs the internet once; see `GAMING.md` §6 and `orbis-games network`.

## 5. Rollback

* Flash the original 0.2.0 image again. It was never modified.
* Or turn the GE image back into a 0.2.0-equivalent one on the Nobara:
  `./orbis-graphics rollback --image out/images/orbis-0.2.0+ge-2.0.0.img --output out/images/rolled-back.img`
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
