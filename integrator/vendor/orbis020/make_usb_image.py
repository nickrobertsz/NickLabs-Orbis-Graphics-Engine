#!/usr/bin/env python3
"""Build a flashable Orbis USB image (or write it to a disk) without loop devices or mounts.

Layout (MBR):
  p1  FAT32  label ORBIS-BOOT  (default 1 GiB)  bzImage, initramfs.cpio.gz,
                                                 bootargs.txt, vram.txt  — read by the PS4 payload
  p2  ext4   label ORBIS-ROOT  (rest)            the root filesystem

  make_usb_image.py image --rootfs-dir ROOT --kernel bzImage --initramfs initramfs.cpio.gz \
                          --size 16G -o orbis.img [--plan]
  make_usb_image.py image --stage0 --kernel bzImage --initramfs initramfs.cpio.gz --size 2G -o stage0.img
  make_usb_image.py write --image orbis.img --device /dev/sdX [--yes-erase /dev/sdX]

--stage0 adds orbis.stage0=1: the initramfs writes a hardware report to orbis-logs/ on the
FAT partition and stops in its rescue shell, so a console can be qualified before a root
filesystem exists. Without --rootfs-dir the ext4 partition is created empty.

Tools: mkfs.fat (dosfstools >= 4.2, for --offset and --invariant), mke2fs (e2fsprogs >= 1.43, -d).
Files go onto the FAT partition with the built-in writer (packaging/image/fat32.py), which is
deterministic and needs no mtools; --fat-writer mtools uses mcopy instead. Either way the
partition is read back and compared byte for byte, and fsck.fat checks it when available.
The image is deterministic for identical inputs (fixed volume ids, UUIDs and timestamps).
Nothing is written to any block device unless `write` is given the device path twice
(--device and --yes-erase) and the device is not mounted, not the running root, and removable
or explicitly allowed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

SECTOR = 512
ALIGN = 2048  # 1 MiB


def parse_size(s: str) -> int:
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    s = s.strip().upper().rstrip("B").rstrip("I")
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s)


def layout(total_bytes: int, boot_bytes: int, min_root: int = 1 << 30) -> dict:
    total = total_bytes // SECTOR
    p1_start = ALIGN
    p1_len = boot_bytes // SECTOR
    p2_start = ((p1_start + p1_len + ALIGN - 1) // ALIGN) * ALIGN
    p2_len = total - p2_start
    if p2_len < min_root // SECTOR:
        raise ValueError(f"image too small: root partition would be < {min_root >> 20} MiB")
    return {"total_sectors": total, "boot": (p1_start, p1_len), "root": (p2_start, p2_len)}


def mbr(lay: dict, disk_id: int = 0x4F524253) -> bytes:  # "ORBS"
    b = bytearray(SECTOR)
    struct.pack_into("<I", b, 440, disk_id)

    def entry(boot, ptype, start, length):
        # CHS fields set to the LBA-only marker (0xFE,0xFF,0xFF)
        return struct.pack("<B3sB3sII", boot, b"\xfe\xff\xff", ptype, b"\xfe\xff\xff", start, length)
    b[446:462] = entry(0x80, 0x0C, *lay["boot"])   # FAT32 LBA, active
    b[462:478] = entry(0x00, 0x83, *lay["root"])   # Linux
    b[510:512] = b"\x55\xaa"
    return bytes(b)


def need(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        raise SystemExit(f"missing tool: {tool}")
    return p


DEFAULT_BOOTARGS = ("pci=nocrs intremap=off root=LABEL=ORBIS-ROOT rootfstype=ext4 rootwait rw "
                    "video=HDMI-A-1:1920x1080@60 consoleblank=0 net.ifnames=0")
EPOCH = 1767225600  # 2026-01-01T00:00:00Z, used for all timestamps


def _sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _readme(stage0: bool) -> str:
    return "\r\n".join([
        "NickLabs PS4 Orbis Linux - boot partition (ORBIS-BOOT)",
        "",
        "bzImage, initramfs.cpio.gz : kernel and initramfs read by the ps4-linux-loader payload",
        "bootargs.txt               : kernel command line (first line only). orbis.mode=pc|gaming|ai",
        "vram.txt                   : GPU memory carve-out in MB (32..4609). Changes need a payload relaunch.",
        "orbis-boot.json            : what this partition was built from (hashes, profile, licence status)",
        "orbis-logs/                : stage-0 hardware reports (written by the console when orbis.stage0=1)",
        "",
        "Boot: start GoldHEN, plug this disk into a PS4 USB port, send the Linux payload.",
        "The loader reads each file from /mnt/usb0, then usb1, then /data/linux/boot, then",
        "/user/system/boot, separately for every file: keep all four files on this disk.",
        "",
        ("STAGE 0: orbis.stage0=1 is set. The console writes a report to orbis-logs/ and stops in a"
         " rescue shell. Remove orbis.stage0=1 from bootargs.txt to boot the installed system." if stage0 else
         "Recovery from any PC: edit bootargs.txt (e.g. orbis.mode=ai for headless), or rename"
         " bzImage.orbis-prev / initramfs.cpio.gz.orbis-prev back to roll back a kernel update."),
        "",
        "If initramfs.cpio.gz contains firmware (see orbis-boot.json), do not redistribute this disk image.",
        ""])


def cmd_image(a) -> int:
    boot_size = a.boot_size or ("512M" if a.stage0 else "1G")
    lay = layout(parse_size(a.size), parse_size(boot_size), (256 << 20) if a.stage0 else (1 << 30))
    bootargs = a.bootargs or (DEFAULT_BOOTARGS + " orbis.mode=" + a.mode + (" orbis.stage0=1" if a.stage0 else ""))
    if "\n" in bootargs.strip():
        raise SystemExit("bootargs must be one line (the loader reads only the first line)")
    plan = {"output": a.output, "layout_sectors": lay, "bootargs": bootargs, "vram_mb": a.vram,
            "stage0": a.stage0, "files": {"bzImage": a.kernel, "initramfs.cpio.gz": a.initramfs},
            "rootfs_dir": a.rootfs_dir, "fat_writer": a.fat_writer}
    if a.plan:
        print(json.dumps(plan, indent=2))
        return 0
    if not (32 <= a.vram <= 4609):
        raise SystemExit("--vram must be 32..4609 (loader limits)")
    for f in (a.kernel, a.initramfs):
        if not Path(f).is_file():
            raise SystemExit(f"missing {f}")
    if not a.rootfs_dir and not a.stage0:
        raise SystemExit("no --rootfs-dir: build a full image from a rootfs, or pass --stage0 for a hardware-report image")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from orbis import bzimage  # noqa: E402
    import fat32  # noqa: E402
    rep = bzimage.inspect(a.kernel)
    if not rep.ok:
        raise SystemExit("kernel fails the PS4 loader check: " + "; ".join(rep.errors))
    ird_manifest = Path(str(a.initramfs) + ".manifest.json")
    ird = json.loads(ird_manifest.read_text()) if ird_manifest.is_file() else None
    redistributable = bool(ird and ird.get("redistributable"))
    boot_json = {
        "schema": 1, "builder": "NickLabs Orbis make_usb_image.py", "stage0": a.stage0,
        "bootargs": bootargs, "vram_mb": a.vram,
        "kernel": {"sha256": _sha(a.kernel), "version": rep.kernel_version,
                   "loader_check": "ok", "x86_ps4": rep.ikconfig.get("CONFIG_X86_PS4") if rep.ikconfig else None},
        "initramfs": {"sha256": _sha(a.initramfs),
                      "profile": (ird or {}).get("profile", {}).get("name"),
                      "firmware_groups": sorted({f["group"] for f in (ird or {}).get("firmware", [])}),
                      "manifest": "present" if ird else "missing (unknown firmware content)"},
        "redistributable": redistributable,
    }
    out = Path(a.output)
    with open(out, "wb") as fh:
        fh.truncate(lay["total_sectors"] * SECTOR)
        fh.write(mbr(lay))
    bs, bl = lay["boot"]
    rs, rl = lay["root"]
    subprocess.run([need("mkfs.fat"), "-F", "32", "-n", "ORBIS-BOOT", "-i", "4F524253", "--invariant",
                    "--offset", str(bs), str(out), str(bl * SECTOR // 1024)], check=True, capture_output=True)
    work = out.with_suffix(".bootfiles")
    work.mkdir(exist_ok=True)
    (work / "bootargs.txt").write_text(bootargs + "\n")
    (work / "vram.txt").write_text(f"{a.vram}\n")
    (work / "README-ORBIS.txt").write_text(_readme(a.stage0))
    (work / "orbis-boot.json").write_text(json.dumps(boot_json, indent=1) + "\n")
    files = [("bzImage", a.kernel), ("initramfs.cpio.gz", a.initramfs), ("bootargs.txt", work / "bootargs.txt"),
             ("vram.txt", work / "vram.txt"), ("README-ORBIS.txt", work / "README-ORBIS.txt"),
             ("orbis-boot.json", work / "orbis-boot.json")]
    if a.fat_writer == "mtools":
        env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
        for name, src in files:
            subprocess.run([need("mcopy"), "-m", "-i", f"{out}@@{bs * SECTOR}", str(src), f"::{name}"],
                           check=True, env=env)
    else:
        fat32.add_files(out, bs * SECTOR, files)
    got = fat32.read_root(out, bs * SECTOR)
    for name, src in files:
        if got.get(name) != Path(src).read_bytes():
            raise SystemExit(f"FAT read-back mismatch for {name}")
    fsck = _fsck_fat(out, bs * SECTOR, bl * SECTOR)
    shutil.rmtree(work)
    env = dict(os.environ, E2FSPROGS_FAKE_TIME=str(EPOCH))
    uid = "4f524253-0000-4000-8000-" + hashlib.sha256(json.dumps(boot_json, sort_keys=True).encode()).hexdigest()[:12]
    args = [need("mke2fs"), "-q", "-F", "-t", "ext4", "-L", "ORBIS-ROOT", "-U", uid, "-E",
            f"offset={rs * SECTOR},root_owner=0:0,hash_seed={uid}", "-O", "^metadata_csum_seed"]
    if a.rootfs_dir:
        args += ["-d", a.rootfs_dir]
    args += [str(out), str(rl * SECTOR // 1024)]
    subprocess.run(args, check=True, env=env)
    plan.update({"boot": boot_json, "fat_readback": "ok", "fsck_fat": fsck, "sha256": _sha(out)})
    if not redistributable:
        print("NOTE: this image contains firmware without established redistribution rights "
              "(or an initramfs without a manifest). Keep it private.", file=sys.stderr)
    print(json.dumps(plan, indent=2))
    return 0


def _fsck_fat(img: Path, offset: int, length: int) -> str:
    tool = shutil.which("fsck.fat")
    if not tool:
        return "not run (fsck.fat not installed)"
    part = img.with_suffix(".bootpart")
    with open(img, "rb") as src, open(part, "wb") as dst:
        dst.truncate(length)
        src.seek(offset)
        done = 0
        while done < length:
            chunk = src.read(min(4 << 20, length - done))
            if not chunk:
                break
            if chunk.strip(b"\0"):
                dst.seek(done)
                dst.write(chunk)
            done += len(chunk)
    r = subprocess.run([tool, "-n", str(part)], capture_output=True, text=True)
    part.unlink()
    if r.returncode != 0:
        raise SystemExit("fsck.fat found problems on the boot partition:\n" + r.stdout + r.stderr)
    return "clean"


def _device_safety(dev: str, allow_fixed: bool) -> list[str]:
    problems = []
    name = Path(os.path.realpath(dev)).name
    if not Path(f"/sys/class/block/{name}").exists():
        problems.append(f"{dev} is not a block device")
        return problems
    if Path(f"/sys/class/block/{name}/partition").exists():
        problems.append(f"{dev} is a partition; give the whole disk")
    removable = Path(f"/sys/class/block/{name}/removable")
    is_usb = "/usb" in os.path.realpath(f"/sys/class/block/{name}")
    if not allow_fixed and not (is_usb or (removable.exists() and removable.read_text().strip() == "1")):
        problems.append(f"{dev} is not a USB/removable disk (use --allow-fixed-disk if you are sure)")
    mounts = Path("/proc/mounts").read_text()
    for line in mounts.splitlines():
        src = line.split()[0]
        if Path(src).name.startswith(name):
            problems.append(f"{src} is mounted ({line.split()[1]})")
    return problems


def cmd_write(a) -> int:
    img = Path(a.image)
    problems = _device_safety(a.device, a.allow_fixed_disk)
    plan = {"image": str(img), "bytes": img.stat().st_size if img.exists() else None,
            "device": a.device, "problems": problems,
            "will_erase": a.yes_erase == a.device and not problems}
    print(json.dumps(plan, indent=2))
    if problems:
        return 3
    if a.yes_erase != a.device:
        print(f"dry run: nothing written. Repeat with --yes-erase {a.device} to ERASE it.", file=sys.stderr)
        return 0
    with open(img, "rb") as src, open(a.device, "r+b", buffering=0) as dst:
        while True:
            chunk = src.read(4 << 20)
            if not chunk:
                break
            dst.write(chunk)
        os.fsync(dst.fileno())
    print("written; you can now copy additional files or eject", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("image")
    i.add_argument("--rootfs-dir")
    i.add_argument("--kernel", required=True)
    i.add_argument("--initramfs", required=True)
    i.add_argument("--size", default="16G")
    i.add_argument("--boot-size", default=None, help="FAT boot partition size (default 1G, stage0 512M)")
    i.add_argument("--mode", default="pc", choices=("pc", "gaming", "ai"))
    i.add_argument("--vram", type=int, default=1024)
    i.add_argument("--bootargs")
    i.add_argument("--stage0", action="store_true", help="hardware-report image (orbis.stage0=1); rootfs optional")
    i.add_argument("--fat-writer", choices=("builtin", "mtools"), default="builtin")
    i.add_argument("-o", "--output", required=True)
    i.add_argument("--plan", action="store_true")
    w = sub.add_parser("write")
    w.add_argument("--image", required=True)
    w.add_argument("--device", required=True)
    w.add_argument("--yes-erase")
    w.add_argument("--allow-fixed-disk", action="store_true")
    a = ap.parse_args()
    return cmd_image(a) if a.cmd == "image" else cmd_write(a)


if __name__ == "__main__":
    raise SystemExit(main())
