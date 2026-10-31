#!/usr/bin/env python3
"""orbis-graphics — integrate NickLabs Orbis Graphics releases (Graphics Engine 2.0.0, Graphics Pack
1.0.0) with Orbis 0.2.0 images on Nobara.

  orbis-graphics verify    --pack ORBIS-GRAPHICS-ENGINE-v2.0.0.tar.zst
  orbis-graphics inspect   (--image IMG | --pack PACK | --rootfs DIR) [--json]
  orbis-graphics integrate (--base-image IMG | --base-rootfs DIR) --pack PACK [--output OUT.img]
                           [--experimental] [--dry-run] [--engine podman|docker] [--container IMAGE]
  orbis-graphics stage0    --base-stage0-image IMG --pack PACK --output OUT.img [--size 4G] [--dry-run]
                           [--host] [--keep-base-kernel]
  orbis-graphics rollback  --image INTEGRATED.img --output OUT.img [--dry-run]

Guarantees:
  * inputs are verified (pack SHA256SUMS + manifest; base image identity) before anything is written;
  * the base image is never opened for writing: integrate/rollback work on a sparse copy (--output);
  * root-level work (loop mounts, pacman in the Arch root) runs inside the privileged Orbis build
    container, never on the Nobara host system; nothing touches a PS4 internal drive;
  * every run writes OUT.integration.json (what was installed/changed, all hashes) and OUT.sha256.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "vendor" / "orbis020"))
import fat32  # noqa: E402  (vendored, unmodified Orbis 0.2.0)

VERSION = "2.0.0"
DEFAULT_CONTAINER = "orbis-build:0.2.0"


class Refused(Exception):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ pack
def unpack(pack: Path, cache: Path | None = None) -> Path:
    """Extract (once) and return the pack directory."""
    if pack.is_dir():
        return pack
    digest = sha256(pack)[:16]
    base = cache or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "orbis-graphics"
    dest = base / f"pack-{digest}"
    if (dest / ".complete").exists():
        return next(p for p in dest.iterdir() if p.is_dir())
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    r = subprocess.run(["tar", "--zstd" if pack.name.endswith(".zst") else "-z", "-xf", str(pack), "-C", str(dest)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Refused(f"cannot extract {pack}: {r.stderr.strip()}")
    (dest / ".complete").write_text(digest + "\n")
    dirs = [p for p in dest.iterdir() if p.is_dir()]
    if len(dirs) != 1:
        raise Refused("pack archive must contain exactly one top-level directory")
    return dirs[0]


def verify_pack(pack: Path) -> dict:
    d = unpack(pack)
    side = Path(str(pack) + ".sha256")
    archive_ok = None
    if pack.is_file() and side.exists():
        want = side.read_text().split()[0]
        archive_ok = sha256(pack) == want
        if not archive_ok:
            raise Refused(f"{pack.name}: SHA-256 differs from {side.name}")
    man_p = d / "pack-manifest.json"
    if not man_p.exists():
        raise Refused("pack-manifest.json missing")
    man = json.loads(man_p.read_text())
    if man.get("schema") != "orbis-graphics-pack/1":
        raise Refused(f"unknown pack schema {man.get('schema')}")
    sums = d / "SHA256SUMS"
    if not sums.exists():
        raise Refused("SHA256SUMS missing")
    bad, n = [], 0
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        want, name = line.split(None, 1)
        name = name.lstrip("*")
        f = d / name
        n += 1
        if not f.is_file() or sha256(f) != want:
            bad.append(name)
    if bad:
        raise Refused(f"{len(bad)} file(s) fail SHA256SUMS: {', '.join(bad[:5])}")
    listed = {line.split(None, 1)[1].lstrip("*") for line in sums.read_text().splitlines() if line.strip()}
    unlisted = [str(p.relative_to(d)) for p in d.rglob("*") if p.is_file() and p.name != "SHA256SUMS"
                and str(p.relative_to(d)) not in listed]
    if unlisted:
        raise Refused(f"files not covered by SHA256SUMS: {', '.join(unlisted[:5])}")
    missing = [k for k in ("repo/orbis-graphics.db.tar.zst", "stage0/orbis-gfx-overlay.cpio.gz") if not (d / k).exists()]
    if missing:
        raise Refused(f"incomplete pack, missing: {', '.join(missing)}")
    return {"pack_dir": str(d), "archive_sha256_ok": archive_ok, "files_verified": n, "manifest": man}


# ------------------------------------------------------------------ images
def mbr_partitions(img: Path) -> dict:
    with open(img, "rb") as f:
        b = f.read(512)
    if b[510:512] != b"\x55\xaa":
        raise Refused(f"{img}: no MBR signature")
    out = {}
    for i in range(4):
        boot, _, ptype, _, start, length = struct.unpack_from("<B3sB3sII", b, 446 + 16 * i)
        if ptype in (0x0B, 0x0C):
            out["boot"] = (start * 512, length * 512)
        elif ptype == 0x83:
            out["root"] = (start * 512, length * 512)
    if "boot" not in out:
        raise Refused(f"{img}: no FAT32 partition (not an Orbis image)")
    return out


def fat_root_names(img: Path, offset: int) -> set[str]:
    """Names (files and directories) in the FAT root directory."""
    with open(img, "rb") as fh:
        fs = fat32.Fat32(fh, offset)
        _, d = fs.read_dir()
        return {e["name"] for e in fat32._parse_dir(d) if not e["attr"] & fat32.ATTR_VOLUME}


@contextlib.contextmanager
def readonly_ext4_partition(img: Path, offset: int, length: int):
    """Expose an exact partition to e2fsprogs without privilege or writing the image.

    debugfs and e2fsck cannot interpret `disk.img?offset=N`.  The privileged
    integration container instead uses bounded loop devices (image-op.sh).
    Host-side inspection uses a temporary *sparse* ext4 partition file; it
    copies allocated data extents, not the unused space of a whole-disk image.
    """
    size = img.stat().st_size
    if offset < 512 or length <= 0 or offset % 512 or length % 512 or offset + length > size:
        raise Refused(f"invalid ext4 partition bounds: offset={offset} length={length} image={size}")
    with tempfile.TemporaryDirectory(prefix="orbis-ext4-readonly-") as td:
        part = Path(td) / "root.ext4"
        with open(img, "rb", buffering=0) as source, open(part, "wb", buffering=0) as target:
            src_fd, dst_fd = source.fileno(), target.fileno()
            os.ftruncate(dst_fd, length)
            position, end = offset, offset + length
            sparse_supported = True
            while position < end:
                if sparse_supported:
                    try:
                        data_pos = os.lseek(src_fd, position, os.SEEK_DATA)
                        if data_pos >= end:
                            break
                        hole = min(end, os.lseek(src_fd, data_pos, os.SEEK_HOLE))
                    except OSError as exc:
                        if exc.errno == errno.ENXIO:
                            break  # remaining image is a hole
                        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                            raise
                        sparse_supported = False
                        data_pos, hole = position, end
                else:
                    data_pos, hole = position, end
                position = data_pos
                while position < hole:
                    chunk = os.pread(src_fd, min(4 << 20, hole - position), position)
                    if not chunk:
                        raise Refused("short read while exposing ext4 partition")
                    if chunk.strip(b"\0"):
                        os.pwrite(dst_fd, chunk, position - offset)
                    position += len(chunk)
            target.flush()
        yield part


def debugfs_cat(part: Path, path: str) -> str | None:
    if not shutil.which("debugfs"):
        raise Refused("debugfs is required for verified Orbis rootfs inspection (install e2fsprogs)")
    r = subprocess.run(["debugfs", "-R", f"cat {path}", str(part)], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 and r.stdout else None


def inspect_image(img: Path) -> dict:
    parts = mbr_partitions(img)
    boff, blen = parts["boot"]
    files = fat32.read_root(img, boff)
    info = {"image": str(img), "bytes": img.stat().st_size, "partitions": {k: {"offset": v[0], "length": v[1]} for k, v in parts.items()},
            "boot_files": sorted(files)}
    bj = files.get("orbis-boot.json")
    if not bj:
        raise Refused(f"{img}: ORBIS-BOOT has no orbis-boot.json (not built by Orbis 0.2.0)")
    boot = json.loads(bj)
    info["orbis_boot"] = boot
    info["stage0"] = bool(boot.get("stage0"))
    info["kernel_sha256"] = hashlib.sha256(files.get("bzImage", b"")).hexdigest()
    info["initramfs_sha256"] = hashlib.sha256(files.get("initramfs.cpio.gz", b"")).hexdigest()
    info["bootargs"] = files.get("bootargs.txt", b"").decode(errors="replace").splitlines()[0] if files.get("bootargs.txt") else None
    info["vram"] = files.get("vram.txt", b"").decode().strip() or None
    info["profile"] = (boot.get("initramfs") or {}).get("profile")
    info["gfx_integrated"] = "initramfs.cpio.gz.orbis-gfx-prev" in files
    info["ge_kernel_staged"] = "bzImage.orbis-gfx-prev" in files
    info["ge_loader_payloads"] = "orbis-ge-loader" in fat_root_names(img, boff)
    if "root" in parts:
        roff, rlen = parts["root"]
        with readonly_ext4_partition(img, roff, rlen) as ext4:
            pk = debugfs_cat(ext4, "/usr/share/orbis/packages.txt")
            info["root_packages"] = dict(l.split() for l in pk.splitlines() if len(l.split()) == 2) if pk else None
            rec = debugfs_cat(ext4, "/var/lib/orbis-graphics/integration.json")
            info["graphics_receipt"] = json.loads(rec) if rec else None
            mr = debugfs_cat(ext4, "/orbis-gfxdiag/MINIROOT")
            info["miniroot"] = mr.strip() if mr else None
    return info


def check_base(info: dict, man: dict, want_stage0: bool) -> list[str]:
    """Return refusal reasons (empty = compatible)."""
    why = []
    base = man.get("base", {})
    if want_stage0 != info["stage0"]:
        why.append("expected a stage-0 image" if want_stage0 else "this is a stage-0 image; use `orbis-graphics stage0`")
    if base.get("kernel_bzimage_sha256") and info["kernel_sha256"] != base["kernel_bzimage_sha256"]:
        why.append(f"kernel {info['kernel_sha256'][:16]} is not the Orbis 0.2.0 kernel this pack was built for "
                   f"({base['kernel_bzimage_sha256'][:16]})")
    if base.get("profile") and info.get("profile") != base["profile"]:
        why.append(f"initramfs profile {info.get('profile')!r} != pack profile {base['profile']!r}")
    if info.get("gfx_integrated"):
        why.append("the Graphics Pack is already integrated into this image")
    if not want_stage0:
        pk = info.get("root_packages")
        if pk is None:
            why.append("cannot read /usr/share/orbis/packages.txt from ORBIS-ROOT (debugfs missing or not an Orbis root)")
        elif not str(pk.get("orbis-platform", "")).startswith("0.2.0"):
            why.append(f"orbis-platform {pk.get('orbis-platform')} is not 0.2.0")
    return why


def sparse_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        raise Refused(f"{dst} exists; refusing to overwrite (choose another --output)")
    if src.resolve() == dst.resolve():
        raise Refused("--output must differ from the base image")
    r = subprocess.run(["cp", "--sparse=always", "--", str(src), str(dst)], capture_output=True, text=True)
    if r.returncode != 0:
        raise Refused(f"copy failed: {r.stderr.strip()}")


def container_run(engine: str, image: str, mounts: list[tuple[str, str, str]], argv: list[str], log: Path) -> int:
    if not shutil.which(engine):
        raise Refused(f"{engine} not found (sudo dnf install podman)")
    pre = [] if os.geteuid() == 0 else ["sudo"]
    insp = subprocess.run(pre + [engine, "image", "inspect", image], capture_output=True, text=True)
    if insp.returncode != 0:
        raise Refused(f"container image {image} not found. Build it from the Orbis 0.2.0 source: "
                      f"cd nicklabs-ps4-orbis-linux && sudo make container ENGINE={engine}")
    cmd = pre + [engine, "run", "--rm", "--privileged", "-v", "/dev:/dev"]
    for src, dst, mode in mounts:
        cmd += ["-v", f"{src}:{dst}:{mode}"]
    cmd += ["-w", "/", image] + argv
    with open(log, "a") as lf:
        lf.write("$ " + " ".join(cmd) + "\n")
        lf.flush()
        p = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
    return p.returncode


def fsck_host(img: Path) -> dict:
    parts = mbr_partitions(img)
    res = {}
    if shutil.which("e2fsck") and "root" in parts:
        roff, rlen = parts["root"]
        with readonly_ext4_partition(img, roff, rlen) as ext4:
            r = subprocess.run(["e2fsck", "-fn", str(ext4)], capture_output=True, text=True)
            res["ext4"] = "clean" if r.returncode == 0 else f"problems (rc {r.returncode}): {(r.stderr or r.stdout)[-300:]}"
    ff = shutil.which("fsck.fat")
    if ff:
        boff, blen = parts["boot"]
        with tempfile.NamedTemporaryFile(suffix=".fat") as t:
            with open(img, "rb") as s:
                s.seek(boff)
                left = blen
                while left:
                    c = s.read(min(4 << 20, left))
                    if not c:
                        break
                    t.write(c)
                    left -= len(c)
            t.flush()
            r = subprocess.run([ff, "-n", t.name], capture_output=True, text=True)
            res["fat"] = "clean" if r.returncode == 0 else f"problems (rc {r.returncode})"
    return res


def write_receipt(out: Path, rec: dict) -> None:
    rec["output_sha256"] = sha256(out) if out.exists() else None
    Path(str(out) + ".integration.json").write_text(json.dumps(rec, indent=1) + "\n")
    if out.exists():
        Path(str(out) + ".sha256").write_text(f"{rec['output_sha256']}  {out.name}\n")


# ------------------------------------------------------------------ commands
def cmd_verify(a) -> int:
    r = verify_pack(Path(a.pack))
    m = r["manifest"]
    print(json.dumps({"ok": True, "pack": m.get("name"), "version": m.get("version"), "build_id": m.get("build_id"),
                      "files_verified": r["files_verified"], "archive_sha256_ok": r["archive_sha256_ok"],
                      "base": m.get("base"), "packages": len(m.get("packages", [])),
                      "experimental_line": m.get("experimental", {}).get("state")}, indent=1))
    return 0


def cmd_inspect(a) -> int:
    if a.pack:
        d = verify_pack(Path(a.pack))
        out = d["manifest"]
    elif a.rootfs:
        root = Path(a.rootfs)
        pk = root / "usr/share/orbis/packages.txt"
        rec = root / "var/lib/orbis-graphics/integration.json"
        out = {"rootfs": str(root), "orbis_platform": next((l.split()[1] for l in pk.read_text().splitlines()
                                                             if l.startswith("orbis-platform ")), None) if pk.exists() else None,
               "graphics_receipt": json.loads(rec.read_text()) if rec.exists() else None}
    else:
        out = inspect_image(Path(a.image))
        out.pop("root_packages", None) if not a.json else None
    print(json.dumps(out, indent=1, default=str))
    return 0


def _plan_common(a, stage0: bool):
    v = verify_pack(Path(a.pack))
    man, pdir = v["manifest"], Path(v["pack_dir"])
    return v, man, pdir


def cmd_integrate(a) -> int:
    v, man, pdir = _plan_common(a, False)
    if a.experimental and man.get("experimental", {}).get("state") != "BUILT":
        raise Refused(f"the experimental Mesa line is {man.get('experimental', {}).get('state', 'absent')} in this pack")
    log = Path((a.output or a.base_rootfs) + ".integration.log") if (a.output or a.base_rootfs) else Path("integration.log")
    rec = {"schema": "orbis-graphics-run/1", "tool": f"orbis-graphics {VERSION}", "op": "integrate",
           "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pack": {"file": a.pack, "manifest": {k: man.get(k) for k in ("name", "version", "build_id")}},
           "experimental": bool(a.experimental)}
    if a.base_rootfs:
        root = Path(a.base_rootfs).resolve()
        plan = ["verify pack", f"integrate-root.sh --root {root} (inside {a.container})", "write receipt"]
        if a.dry_run:
            print(json.dumps({"dry_run": True, "plan": plan}, indent=1))
            return 0
        rc = container_run(a.engine, a.container, [(str(root), "/target", "rw"), (str(pdir), "/pack", "ro"),
                                                    (str(HERE), "/integrator", "ro")],
                           ["/integrator/root/integrate-root.sh", "--root", "/target", "--pack", "/pack"]
                           + (["--experimental"] if a.experimental else []), log)
        rec["base_rootfs"] = str(root)
        rec["container_rc"] = rc
        recp = root / "var/lib/orbis-graphics/integration.json"
        rec["in_root_receipt"] = json.loads(recp.read_text()) if recp.exists() else None
        Path(str(root) + ".integration.json").write_text(json.dumps(rec, indent=1) + "\n")
        print(f"{'OK' if rc == 0 else 'FAILED'}: rootfs {root}; log {log}")
        return 0 if rc == 0 else 1
    base = Path(a.base_image)
    if not a.output:
        raise Refused("--output is required (the base image is never modified)")
    out = Path(a.output).resolve()
    info = inspect_image(base)
    why = check_base(info, man, want_stage0=False)
    if why:
        raise Refused("base image refused:\n  " + "\n  ".join(why))
    plan = {"base": {"image": str(base), "sha256": None if a.dry_run else sha256(base), "kernel": info["kernel_sha256"],
                     "profile": info["profile"], "orbis_platform": (info.get("root_packages") or {}).get("orbis-platform")},
            "output": str(out), "steps": ["sparse copy base -> output", f"container {a.container}: loop-mount copy, "
                                           "install orbis-graphics-pack offline (GE kernel staged as bzImage when the pack has one), initramfs overlay, "
                                           "GE loader payloads, receipt, umount, e2fsck/fsck.fat",
                                           "host: re-read FAT, fsck, write OUT.integration.json + OUT.sha256"]}
    if a.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan}, indent=1))
        return 0
    rec["base"] = plan["base"]
    sparse_copy(base, out)
    rc = container_run(a.engine, a.container, [(str(out.parent), "/work", "rw"), (str(pdir), "/pack", "ro"),
                                                (str(HERE), "/integrator", "ro")],
                       ["/integrator/container/image-op.sh", "integrate", "--image", f"/work/{out.name}", "--pack", "/pack"]
                       + (["--experimental"] if a.experimental else []), Path(str(out) + ".integration.log"))
    rec["container_rc"] = rc
    if rc != 0:
        rec["result"] = "FAILED"
        write_receipt(out, rec)
        print(f"FAILED (container rc {rc}); see {out}.integration.log. The base image is untouched; delete {out}.")
        return 1
    after = inspect_image(out)
    rec["result"] = "OK"
    rec["output_boot"] = {"initramfs_sha256": after["initramfs_sha256"], "gfx_integrated": after["gfx_integrated"],
                          "kernel_sha256": after["kernel_sha256"], "ge_kernel_staged": after["ge_kernel_staged"],
                          "ge_loader_payloads": after["ge_loader_payloads"]}
    want_k = pack_kernel(pdir, man)
    if want_k and hashlib.sha256(want_k[0]).hexdigest() != after["kernel_sha256"]:
        rec["result"] = "FAILED"
        rec["why"] = "the boot partition's bzImage is not the pack's Graphics Engine kernel"
        write_receipt(out, rec)
        print(f"FAILED: {rec['why']}; see {out}.integration.log")
        return 1
    rec["in_image_receipt"] = after.get("graphics_receipt")
    rec["fsck_host"] = fsck_host(out)
    write_receipt(out, rec)
    print(f"OK: {out}\n  receipt {out}.integration.json\n  sha256  {rec['output_sha256']}")
    return 0


def pack_kernel(pdir: Path, man: dict) -> tuple[bytes, str] | None:
    """(bzImage bytes, release) of the Graphics Engine kernel package in the pack, if any."""
    k = (man.get("kernel") or {}).get("package")
    if not k:
        return None
    pkg = pdir / "repo" / f"{k['name']}-{k['version']}-x86_64.pkg.tar.zst"
    if not pkg.exists():
        raise Refused(f"pack manifest names {pkg.name} but it is not in repo/")
    r = subprocess.run(["tar", "--zstd", "-tf", str(pkg)], capture_output=True, text=True)
    names = [n for n in r.stdout.splitlines() if n.startswith("usr/lib/modules/") and n.endswith("/vmlinuz")]
    if r.returncode != 0 or len(names) != 1:
        raise Refused(f"cannot find exactly one vmlinuz in {pkg.name}")
    data = subprocess.run(["tar", "--zstd", "-xOf", str(pkg), names[0]], capture_output=True).stdout
    if len(data) < 0x300 or data[0x202:0x206] != b"HdrS" or data[0x1FE:0x200] != b"\x55\xaa":
        raise Refused(f"{names[0]} in {pkg.name} is not an x86 bzImage")
    return data, names[0].split("/")[3]


def build_stage0_work(base: Path, pdir: Path, man: dict, work: Path, keep_base_kernel: bool = False) -> dict:
    info = inspect_image(base)
    why = check_base(info, man, want_stage0=True)
    if why:
        raise Refused("base stage-0 image refused:\n  " + "\n  ".join(why))
    files = fat32.read_root(base, info["partitions"]["boot"]["offset"])
    work.mkdir(parents=True, exist_ok=True)
    ge = None if keep_base_kernel else pack_kernel(pdir, man)
    (work / "bzImage").write_bytes(ge[0] if ge else files["bzImage"])
    info["stage0_kernel"] = {"source": f"pack ({ge[1]})" if ge else "base stage-0 image",
                             "sha256": hashlib.sha256(ge[0] if ge else files["bzImage"]).hexdigest()}
    overlay = (pdir / "stage0/orbis-gfx-overlay.cpio.gz").read_bytes()
    _overlay_conflicts(files["initramfs.cpio.gz"], overlay)
    (work / "initramfs.cpio.gz").write_bytes(files["initramfs.cpio.gz"] + overlay)
    boot = info["orbis_boot"]
    ird = boot.get("initramfs") or {}
    (work / "initramfs.cpio.gz.manifest.json").write_text(json.dumps({
        "redistributable": bool(boot.get("redistributable")), "profile": {"name": ird.get("profile")},
        "firmware": [{"group": g} for g in ird.get("firmware_groups", [])],
        "orbis_graphics_overlay_sha256": hashlib.sha256(overlay).hexdigest()}) + "\n")
    args = info["bootargs"] or ""
    for w in ("orbis.stage0=1", "orbis.gfxdiag=1"):
        if w not in args.split():
            args += " " + w
    (work / "bootargs.txt").write_text(args.strip() + "\n")
    (work / "vram.txt").write_text((info["vram"] or "1024") + "\n")
    mr = pdir / "miniroot/orbis-gfxdiag-miniroot.tar.zst"
    if not mr.exists():
        raise Refused("pack has no mini-root (miniroot/orbis-gfxdiag-miniroot.tar.zst)")
    shutil.copyfile(mr, work / "miniroot.tar.zst")
    return info


def _overlay_conflicts(base_gz: bytes, overlay_gz: bytes) -> None:
    import gzip
    def names(gz):
        data = gzip.decompress(gz)
        out, off = set(), 0
        while off + 110 <= len(data) and data[off:off + 6] in (b"070701", b"070702"):
            f = [int(data[off + 6 + i * 8: off + 14 + i * 8], 16) for i in range(13)]
            name = data[off + 110: off + 110 + f[11] - 1].decode()
            off = (off + 110 + f[11] + 3) & ~3
            off = (off + f[6] + 3) & ~3
            if name == "TRAILER!!!":
                break
            out.add((name, f[1] & 0o170000))
        return out
    base = {n for n, m in names(base_gz) if m != 0o040000}
    ov = {n for n, m in names(overlay_gz) if m != 0o040000}
    clash = sorted(base & ov)
    if clash:
        raise Refused("graphics overlay would replace files of the base initramfs: " + ", ".join(clash))


def cmd_stage0(a) -> int:
    v, man, pdir = _plan_common(a, True)
    base = Path(a.base_stage0_image)
    out = Path(a.output).resolve()
    if out.exists():
        raise Refused(f"{out} exists; refusing to overwrite")
    work = Path(tempfile.mkdtemp(prefix="orbis-gfx-stage0-", dir=str(out.parent)))
    info = build_stage0_work(base, pdir, man, work, keep_base_kernel=a.keep_base_kernel)
    plan = {"base": str(base), "output": str(out), "size": a.size,
            "bootargs": (work / "bootargs.txt").read_text().strip(), "vram_mb": (work / "vram.txt").read_text().strip(),
            "initramfs": {"base_sha256": info["initramfs_sha256"], "with_overlay_sha256": sha256(work / "initramfs.cpio.gz")},
            "kernel": info.get("stage0_kernel"),
            "miniroot": man.get("miniroot")}
    if a.dry_run:
        shutil.rmtree(work)
        print(json.dumps({"dry_run": True, "plan": plan}, indent=1))
        return 0
    script = HERE / "container/compose-stage0.sh"
    if a.host:
        if os.geteuid() != 0:
            raise Refused("--host needs root (sudo) because the mini-root must keep root ownership")
        r = subprocess.run([str(script), "--work", str(work), "--size", a.size, "--output", str(out)])
        rc = r.returncode
    else:
        rc = container_run(a.engine, a.container, [(str(out.parent), "/work", "rw"), (str(HERE), "/integrator", "ro")],
                           ["/integrator/container/compose-stage0.sh", "--work", f"/work/{work.name}", "--size", a.size,
                            "--output", f"/work/{out.name}"], Path(str(out) + ".integration.log"))
    rec = {"schema": "orbis-graphics-run/1", "tool": f"orbis-graphics {VERSION}", "op": "stage0",
           "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pack": {k: man.get(k) for k in ("name", "version", "build_id")},
           "plan": plan, "compose_rc": rc}
    if rc == 0:
        after = inspect_image(out)
        rec["result"] = "OK"
        rec["output_boot"] = after["orbis_boot"]
        rec["miniroot_marker"] = after.get("miniroot")
        rec["fsck_host"] = fsck_host(out)
    else:
        rec["result"] = "FAILED"
    shutil.rmtree(work, ignore_errors=True)
    write_receipt(out, rec)
    print(f"{rec['result']}: {out}\n  receipt {out}.integration.json")
    return 0 if rc == 0 else 1


def cmd_rollback(a) -> int:
    src = Path(a.image)
    info = inspect_image(src)
    if not info.get("gfx_integrated"):
        raise Refused(f"{src} does not carry the Graphics Pack (no initramfs.cpio.gz.orbis-gfx-prev)")
    out = Path(a.output).resolve()
    if a.dry_run:
        print(json.dumps({"dry_run": True, "plan": {"source": str(src), "output": str(out),
                                                    "steps": ["sparse copy", "container: rollback-root.sh on the copy",
                                                              "fsck", "receipt"]}}, indent=1))
        return 0
    sparse_copy(src, out)
    rc = container_run(a.engine, a.container, [(str(out.parent), "/work", "rw"), (str(HERE), "/integrator", "ro")],
                       ["/integrator/container/image-op.sh", "rollback", "--image", f"/work/{out.name}"],
                       Path(str(out) + ".integration.log"))
    rec = {"schema": "orbis-graphics-run/1", "tool": f"orbis-graphics {VERSION}", "op": "rollback",
           "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source": str(src), "container_rc": rc,
           "result": "OK" if rc == 0 else "FAILED"}
    if rc == 0:
        rec["fsck_host"] = fsck_host(out)
        rec["output_boot"] = {"gfx_integrated": inspect_image(out)["gfx_integrated"]}
    write_receipt(out, rec)
    print(f"{rec['result']}: {out}")
    return 0 if rc == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orbis-graphics", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=f"orbis-graphics {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify"); v.add_argument("--pack", required=True)
    i = sub.add_parser("inspect"); g = i.add_mutually_exclusive_group(required=True)
    g.add_argument("--image"); g.add_argument("--pack"); g.add_argument("--rootfs"); i.add_argument("--json", action="store_true")
    for name in ("integrate", "stage0", "rollback"):
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--engine", default="podman" if shutil.which("podman") else "docker")
        p.add_argument("--container", default=DEFAULT_CONTAINER)
        if name == "integrate":
            gg = p.add_mutually_exclusive_group(required=True)
            gg.add_argument("--base-image"); gg.add_argument("--base-rootfs")
            p.add_argument("--pack", required=True); p.add_argument("--output")
            p.add_argument("--experimental", action="store_true")
        elif name == "stage0":
            p.add_argument("--base-stage0-image", required=True); p.add_argument("--pack", required=True)
            p.add_argument("--output", required=True); p.add_argument("--size", default="4G")
            p.add_argument("--host", action="store_true", help="compose on this host as root instead of in the container")
            p.add_argument("--keep-base-kernel", action="store_true",
                           help="boot the stage-0 image with the base image's Orbis 0.2.0 kernel instead of the pack's GE kernel")
        else:
            p.add_argument("--image", required=True); p.add_argument("--output", required=True)
    a = ap.parse_args(argv)
    try:
        return {"verify": cmd_verify, "inspect": cmd_inspect, "integrate": cmd_integrate, "stage0": cmd_stage0,
                "rollback": cmd_rollback}[a.cmd](a)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
