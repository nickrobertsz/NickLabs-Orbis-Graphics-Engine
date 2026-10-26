# Graphics Pack 1.0.0 on Nobara: exact commands

Everything below runs on the Nobara PC. The PS4 stays LAN-only; it never downloads anything.
Nothing here writes to Nobara's own system (sudo is only used for podman, loop mounts inside the
container, and `dd` to the USB device you name) and nothing touches the PS4's internal drive.

Variables used below (change the paths if yours differ):

```sh
REL=~/orbis-release                               # where the release files are
ORBIS=$REL/nicklabs-ps4-orbis-linux               # the Orbis 0.2.0 checkout you built 0.2.0 from
GFX=$REL/nicklabs-orbis-graphics                  # this source tree
PACK=$GFX/out/release/ORBIS-GRAPHICS-PACK-v1.0.0.tar.zst
```

## 0. What must already exist (from Orbis 0.2.0)

| Needed | Made by | Used for |
|---|---|---|
| `$ORBIS/out/release/orbis-0.2.0-cuh-1215a.img` | `packaging/release.sh` | base for `integrate` |
| `$ORBIS/out/release/orbis-0.2.0-stage0-cuh-1215a.img` (unxz the `.img.xz`) | `make release-stage0` | base for `stage0` |
| `$ORBIS/out/repo/` | 0.2.0 package build | dependency resolution (only read) |
| `$ORBIS/out/rootfs/packages.txt` | 0.2.0 rootfs build | leave out what the image already has |
| `$ORBIS/out/keys/` (optional) | `packaging/scripts/orbis-repo-key.sh` | sign the pack's packages with the same key |
| podman image `orbis-build:0.2.0` | `make container` (built automatically if missing) | all root-level work |

The integrator only accepts images whose kernel is exactly the 0.2.0 kernel and whose profile is
`cuh-1215a`; anything else is refused with the reason.

## 1. Get the source and check it

```sh
cd $REL
sha256sum -c SHA256SUMS --ignore-missing
git clone orbis-graphics-1.0.0.bundle nicklabs-orbis-graphics      # or: tar -xzf NICKLABS-ORBIS-GRAPHICS-SOURCE-1.0.0.tar.gz
cd $GFX
python3 -m unittest discover -s tests/unit -t tests/unit           # ~1 min; must end with OK
```

## 2. Build the pack (1-3 hours, about 40 GB free disk)

```sh
cd $GFX
build/build-pack.sh --orbis-src $ORBIS                 # add --experimental to also try Mesa main
```

It builds Mesa 26.2.5 (64- and 32-bit, RustiCL), the tools, the meta package, downloads the
dependency closure from the pinned Arch snapshot, signs everything with the 0.2.0 key when present,
builds the stage-0 GPU mini-root, assembles and verifies the pack. Logs: `out/pack/logs/*.log`.

If it stops:

| Message | What to do |
|---|---|
| `libdrm too old for Mesa 26.2.5` | the Arch snapshot's libdrm is < 2.4.133; send the log back |
| a `makepkg` failure | the last 25 lines are printed; the full log is in `out/pack/logs/` |
| `ORBIS-EXPERIMENTAL-BLOCKED` | only with `--experimental`; recorded as BLOCKED in the pack, the stable pack is still built |

## 3. Verify and inspect

```sh
./orbis-graphics verify  --pack $PACK
./orbis-graphics inspect --pack $PACK | less                 # manifest: packages, hashes, Mesa lines, mini-root
./orbis-graphics inspect --image $ORBIS/out/release/orbis-0.2.0-cuh-1215a.img
```

## 4. The graphics stage-0 image (first thing to boot)

```sh
./orbis-graphics stage0 --base-stage0-image $ORBIS/out/release/orbis-0.2.0-stage0-cuh-1215a.img \
  --pack $PACK --output $REL/orbis-0.2.0-gfx-1.0.0-stage0-cuh-1215a.img --dry-run
./orbis-graphics stage0 --base-stage0-image $ORBIS/out/release/orbis-0.2.0-stage0-cuh-1215a.img \
  --pack $PACK --output $REL/orbis-0.2.0-gfx-1.0.0-stage0-cuh-1215a.img
```

Result: a new image (the 0.2.0 stage-0 image is not changed) with the same kernel, the 0.2.0
initramfs plus the graphics overlay, `orbis.stage0=1 orbis.gfxdiag=1` in `bootargs.txt`, and the
GPU mini-root on ORBIS-ROOT. Receipt: `…stage0-cuh-1215a.img.integration.json`.

Flash it and boot it exactly like the 0.2.0 stage-0 image (0.2.0 `docs/INSTALL-NOBARA.md`, 3b-3c):

```sh
IMG=$REL/orbis-0.2.0-gfx-1.0.0-stage0-cuh-1215a.img
lsblk -o NAME,SIZE,MODEL,TRAN,RM,MOUNTPOINTS
for p in /dev/sdX?*; do sudo umount "$p" 2>/dev/null; done
python3 $ORBIS/packaging/image/make_usb_image.py write --image "$IMG" --device /dev/sdX
sudo python3 $ORBIS/packaging/image/make_usb_image.py write --image "$IMG" --device /dev/sdX --yes-erase /dev/sdX
sync
python3 $ORBIS/packaging/scripts/send_payload.py <PS4-IP> $ORBIS/out/references/linux-1024mb.elf
```

The PS4 writes the 0.2.0 hardware report and then the GPU report; allow up to 6 minutes (the
mini-root run has a 300 s watchdog), then power off. Optional bootargs (edit `bootargs.txt` on
ORBIS-BOOT): `orbis.gfxdiag.ibtest=1` (kernel IB test on every ring), `orbis.gfxdiag.timeout=600`.

Read the report on Nobara:

```sh
ls /run/media/$USER/ORBIS-BOOT/orbis-logs/
#   stage0-001.txt, stage0-001-dmesg.txt          the 0.2.0 report (unchanged)
#   stage0-002-gpu.txt / stage0-002-gpu.json     the GPU report (PASS/FAIL/DEGRADED/UNSUPPORTED/NOT_RUN/BLOCKED)
#   stage0-002-gpu-kernel.txt                    PCI, driver, firmware versions, rings, connectors/EDID, GPU log lines
#   stage0-002-gpu-progress.txt                  last line = last step reached (useful after a hang)
#   stage0-002-gpu-qualify.log                   output of the mini-root run
```

(The number is the next free one, so earlier reports are never overwritten.)

## 5. The full image with the graphics stack

```sh
./orbis-graphics integrate --base-image $ORBIS/out/release/orbis-0.2.0-cuh-1215a.img \
  --pack $PACK --output $REL/orbis-0.2.0-gfx-1.0.0-cuh-1215a.img --dry-run
./orbis-graphics integrate --base-image $ORBIS/out/release/orbis-0.2.0-cuh-1215a.img \
  --pack $PACK --output $REL/orbis-0.2.0-gfx-1.0.0-cuh-1215a.img
```

It copies the base image (sparse), loop-mounts the copy inside the `orbis-build:0.2.0` container,
installs `orbis-graphics-pack` offline from the pack's repository (no network), appends the
diagnostic overlay to the boot initramfs (backup kept as `initramfs.cpio.gz.orbis-gfx-prev`),
writes `/var/lib/orbis-graphics/integration.json` inside the image, unmounts, runs `e2fsck -fn` and
`fsck.fat -n`, and writes `…img.integration.json`, `…img.sha256` and `…img.integration.log`
next to the output. On failure the output is left for inspection; the base image is untouched.

Add `--experimental` to install the experimental Mesa line instead (only if the pack says BUILT).

Flash `$REL/orbis-0.2.0-gfx-1.0.0-cuh-1215a.img` like the 0.2.0 full image (0.2.0 docs, section 5).

## 6. Rollback

On Nobara, from an integrated image to a 0.2.0-equivalent one (pack packages removed, 0.2.0 Mesa
reinstalled from the image's own 0.2.0 repo, files restored):

```sh
./orbis-graphics rollback --image $REL/orbis-0.2.0-gfx-1.0.0-cuh-1215a.img --output $REL/orbis-rolled-back.img
```

Or simply flash the original 0.2.0 image again; it was never modified.

On a running console that has the pack:

```sh
sudo /usr/lib/orbis-graphics/rollback-root.sh --root / --boot /boot/ps4
```

## 7. Alternative: add the pack to an already installed console (no reflash)

```sh
scp $PACK orbis@<PS4-IP>:/tmp/                                         # on Nobara
ssh orbis@<PS4-IP>
cd /tmp && tar --zstd -xf ORBIS-GRAPHICS-PACK-v1.0.0.tar.zst && cd ORBIS-GRAPHICS-PACK-v1.0.0
sha256sum -c SHA256SUMS --quiet && echo PACK-OK
sudo ./integrator/root/integrate-root.sh --root / --pack . --boot /boot/ps4 --dry-run
sudo ./integrator/root/integrate-root.sh --root / --pack . --boot /boot/ps4
sudo reboot                                                            # then resend the payload
```

## 8. Staging rootfs (before a 0.2.0 image exists)

```sh
sudo mkdir $REL/staging-root
sudo tar --zstd -xpf $ORBIS/out/rootfs/orbis-rootfs.tar.zst -C $REL/staging-root --numeric-owner
./orbis-graphics integrate --base-rootfs $REL/staging-root --pack $PACK --dry-run
./orbis-graphics integrate --base-rootfs $REL/staging-root --pack $PACK
```

The receipt is written to `$REL/staging-root.integration.json` and inside the root. Build the image
from that directory with the 0.2.0 image builder (`packaging/image/make_usb_image.py image
--rootfs-dir $REL/staging-root …`, same arguments `packaging/release.sh` uses).

## 9. Optional: graphics kernel

Only after reviewing community kernel work (see GRAPHICS-KERNEL-MESA-COMPATIBILITY.md):

```sh
kernel/research-deltas.sh                                              # 2-4 GB of git data; writes out/kernel-research/
echo available/0101-ps4_bridge-ab-knobs-for-unmute-fix.patch >> kernel/patches/series   # example: the bridge A/B knobs
kernel/build-gfx-kernel.sh --orbis-src $ORBIS                          # 30-90 min
build/build-pack.sh --orbis-src $ORBIS                                 # the pack now also carries linux-orbis-ps4-gfx
```

## 10. Keeping the bug catalog current

```sh
export GITHUB_TOKEN=<a read-only token, optional>   # without it: 60 requests/hour
research/catalog_update.py --dry-run                 # report only
research/catalog_update.py                           # updates research/catalog.json (inbox only) + research/reports/
```
