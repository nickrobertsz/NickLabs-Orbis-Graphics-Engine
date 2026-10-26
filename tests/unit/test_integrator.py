"""Pack assembly, verification and the Nobara integrator on synthetic Orbis 0.2.0 images.

Synthetic inputs only: a fake bzImage that passes the 0.2.0 loader check, a gzip newc initramfs,
dummy package files and a tiny mini-root. Image tests need dosfstools + e2fsprogs + zstd, and the
stage-0 compose test needs root (the mini-root must stay root-owned); they skip otherwise.
"""
import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "build"))
sys.path.insert(0, str(REPO / "integrator"))
import assemble_pack  # noqa: E402
import make_overlay_cpio  # noqa: E402
import orbis_graphics_tool as ogt  # noqa: E402

_dosfs = Path("/home/claude/tools/dosfs-inst/sbin")
if _dosfs.is_dir():
    os.environ["PATH"] = f"{_dosfs}:{os.environ['PATH']}"
HAVE_IMG_TOOLS = all(shutil.which(t) for t in ("mkfs.fat", "mke2fs", "debugfs", "zstd"))
IS_ROOT = os.geteuid() == 0
VENDOR = REPO / "integrator/vendor/orbis020"


def make_bzimage(version="6.15.4-orbis-ps4 (test)") -> bytes:
    buf = bytearray(0x4000); buf[0x1F1] = 4; buf[0x1FE:0x200] = b"\x55\xaa"; buf[0x202:0x206] = b"HdrS"
    struct.pack_into("<H", buf, 0x206, (2 << 8) | 15); struct.pack_into("<H", buf, 0x20E, 0x300)
    v = version.encode() + b"\x00"; buf[0x500:0x500 + len(v)] = v; buf[0x211] = 0x01; buf[0x234] = 1
    struct.pack_into("<H", buf, 0x236, 0x23); struct.pack_into("<I", buf, 0x238, 2047)
    struct.pack_into("<II", buf, 0x248, 0, 0)
    return bytes(buf)


def newc_gz(files: dict) -> bytes:
    import stat
    ents = [(n, stat.S_IFREG | 0o755, d) for n, d in sorted(files.items())]
    raw = make_overlay_cpio.newc(ents)
    z = io.BytesIO()
    with gzip.GzipFile(fileobj=z, mode="wb", mtime=0) as g:
        g.write(raw)
    return z.getvalue()


def quiet(fn, *a):
    o, e = io.StringIO(), io.StringIO()
    with redirect_stdout(o), redirect_stderr(e):
        rc = fn(*a)
    return rc, o.getvalue(), e.getvalue()


def zstd_tar(dst: Path, members: dict) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as t:
        for name, data in sorted(members.items()):
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = 0o644
            t.addfile(ti, io.BytesIO(data))
    r = subprocess.run(["zstd", "-q", "-o", str(dst)], input=raw.getvalue(), capture_output=True)
    assert r.returncode == 0, r.stderr


def fake_staging(d: Path) -> Path:
    s = d / "staging"
    (s / "repo").mkdir(parents=True)
    for f in ("mesa-orbis-ps4-26.2.5-101-x86_64", "opencl-mesa-orbis-ps4-26.2.5-101-x86_64",
              "lib32-mesa-orbis-ps4-26.2.5-101-x86_64", "orbis-graphics-1.0.0-1-x86_64",
              "orbis-graphics-pack-1.0.0-1-any", "libdrm-2.4.133-1-x86_64", "clinfo-3.0.25.02.14-1-x86_64"):
        (s / "repo" / f"{f}.pkg.tar.zst").write_bytes(f.encode())
    (s / "repo/orbis-graphics.db.tar.zst").write_bytes(b"db")
    (s / "miniroot").mkdir()
    if shutil.which("zstd"):
        zstd_tar(s / "miniroot/orbis-gfxdiag-miniroot.tar.zst",
                 {"MINIROOT": b"orbis-gfxdiag 1.0.0 mesa-orbis-ps4 26.2.5-101 snapshot 2026/10/20\n",
                  "usr/bin/orbis-gpu": b"#!/bin/sh\n", "orbis-gfxdiag-packages.txt": b"mesa-orbis-ps4 26.2.5-101\n"})
    else:
        (s / "miniroot/orbis-gfxdiag-miniroot.tar.zst").write_bytes(b"x")
    (s / "miniroot/orbis-gfxdiag-miniroot.manifest.json").write_text(json.dumps(
        {"schema": "orbis-gfxdiag-miniroot/1", "marker": "orbis-gfxdiag 1.0.0", "packages": {"mesa-orbis-ps4": "26.2.5-101"}}))
    return s


def build_pack(d: Path, kernel_sha: str | None = None, exp=("NOT_BUILT", "not requested")) -> Path:
    s = fake_staging(d)
    arc = assemble_pack.assemble(s, d / "rel", exp[0], exp[1], None, None, now="20261026T000000Z")
    if kernel_sha:
        top = d / "rel" / assemble_pack.TOP
        m = json.loads((top / "pack-manifest.json").read_text())
        m["base"]["kernel_bzimage_sha256"] = kernel_sha
        (top / "pack-manifest.json").write_text(json.dumps(m, indent=1) + "\n")
        arc = assemble_pack.finalize(top)
    return arc


def base_image(d: Path, name: str, stage0: bool, kernel: bytes, initramfs: bytes, packages: str | None = None,
               profile="cuh-1215a") -> Path:
    w = d / f"{name}.in"; w.mkdir()
    (w / "bzImage").write_bytes(kernel)
    (w / "initramfs.cpio.gz").write_bytes(initramfs)
    (w / "initramfs.cpio.gz.manifest.json").write_text(json.dumps(
        {"redistributable": False, "profile": {"name": profile}, "firmware": [{"group": "liverpool-gpu"}]}))
    cmd = [sys.executable, str(VENDOR / "make_usb_image.py"), "image", "--kernel", str(w / "bzImage"),
           "--initramfs", str(w / "initramfs.cpio.gz"), "--size", "800M" if stage0 else "2200M", "-o", str(d / name)]
    if stage0:
        cmd += ["--stage0"]
    if packages is not None:
        rd = w / "rootfs"; (rd / "usr/share/orbis").mkdir(parents=True)
        (rd / "usr/share/orbis/packages.txt").write_text(packages)
        cmd += ["--rootfs-dir", str(rd)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return d / name


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self._cache = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(self.d / "cache")

    def tearDown(self):
        if self._cache is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._cache
        self.tmp.cleanup()


class PackTest(Base):
    def test_assemble_and_verify(self):
        arc = build_pack(self.d)
        self.assertTrue(arc.name == "ORBIS-GRAPHICS-PACK-v1.0.0.tar.zst")
        rc, out, _ = quiet(ogt.main, ["verify", "--pack", str(arc)])
        self.assertEqual(rc, 0)
        v = json.loads(out)
        self.assertTrue(v["archive_sha256_ok"])
        self.assertEqual(v["experimental_line"], "NOT_BUILT")
        m = ogt.verify_pack(arc)["manifest"]
        self.assertIn("mesa-orbis-ps4", m["orbis_built"])
        self.assertIn("libdrm", m["dependency_closure"])
        self.assertNotIn("mesa-orbis-ps4", m["dependency_closure"])
        self.assertFalse(m["kernel"]["replacement"])
        self.assertTrue(m["firmware"].startswith("none"))
        self.assertNotIn("packages", m["miniroot"])
        lic = ogt.unpack(arc) / "LICENSES/licenses.json"
        self.assertTrue(lic.exists())

    def test_deterministic(self):
        a = build_pack(self.d / "a" if (self.d / "a").mkdir() is None else None)
        b = build_pack(self.d / "b" if (self.d / "b").mkdir() is None else None)
        self.assertEqual(ogt.sha256(a), ogt.sha256(b))

    def test_tampered_file_refused(self):
        arc = build_pack(self.d)
        top = self.d / "rel" / assemble_pack.TOP
        (top / "repo/libdrm-2.4.133-1-x86_64.pkg.tar.zst").write_bytes(b"evil")
        with self.assertRaises(ogt.Refused) as c:
            ogt.verify_pack(top)
        self.assertIn("fail SHA256SUMS", str(c.exception))

    def test_unlisted_file_refused(self):
        build_pack(self.d)
        top = self.d / "rel" / assemble_pack.TOP
        (top / "repo/extra.pkg.tar.zst").write_bytes(b"x")
        with self.assertRaises(ogt.Refused) as c:
            ogt.verify_pack(top)
        self.assertIn("not covered", str(c.exception))

    def test_archive_hash_mismatch_refused(self):
        arc = build_pack(self.d)
        Path(str(arc) + ".sha256").write_text("0" * 64 + "  x\n")
        rc, _, err = quiet(ogt.main, ["verify", "--pack", str(arc)])
        self.assertEqual(rc, 2)
        self.assertIn("SHA-256 differs", err)

    def test_experimental_refused_unless_built(self):
        arc = build_pack(self.d, exp=("BLOCKED", "ORBIS-EXPERIMENTAL-BLOCKED: 0001 does not apply"))
        rc, _, err = quiet(ogt.main, ["integrate", "--base-rootfs", str(self.d), "--pack", str(arc), "--experimental", "--dry-run"])
        self.assertEqual(rc, 2)
        self.assertIn("BLOCKED", err)

    def test_overlay_contents(self):
        ov = make_overlay_cpio.build(REPO / "diag/stage0/overlay")
        self.assertEqual(ov, make_overlay_cpio.build(REPO / "diag/stage0/overlay"))
        raw = gzip.decompress(ov)
        self.assertIn(b"bin/orbis-gfx-stage0", raw)
        self.assertIn(b"hooks/pre_newroot_mount/4-orbis-gfx-stage0", raw)
        self.assertNotIn(b"/lib/firmware", raw)

    def test_vendored_unmodified(self):
        v = json.loads((VENDOR / "VENDORED.json").read_text())
        for f, meta in v["files"].items():
            self.assertEqual(hashlib.sha256((VENDOR / f).read_bytes()).hexdigest(), meta["sha256"], f)


@unittest.skipUnless(HAVE_IMG_TOOLS, "dosfstools/e2fsprogs/zstd not installed")
class ImageTest(Base):
    def setUp(self):
        super().setUp()
        self.kernel = make_bzimage()
        self.ksha = hashlib.sha256(self.kernel).hexdigest()
        self.ird = newc_gz({"init": b"#!/bin/sh\n", "hooks/pre_newroot_mount/1-x": b"#!/bin/sh\n"})

    def test_integrate_dry_run_and_refusals(self):
        arc = build_pack(self.d, self.ksha)
        full = base_image(self.d, "full.img", False, self.kernel, self.ird, "orbis-platform 0.2.0-1\nmesa-orbis-ps4 26.2.5-100\n")
        rc, out, err = quiet(ogt.main, ["integrate", "--base-image", str(full), "--pack", str(arc),
                                        "--output", str(self.d / "o.img"), "--dry-run"])
        self.assertEqual(rc, 0, err)
        plan = json.loads(out)["plan"]
        self.assertEqual(plan["base"]["orbis_platform"], "0.2.0-1")
        self.assertFalse((self.d / "o.img").exists())
        # missing --output
        rc, _, err = quiet(ogt.main, ["integrate", "--base-image", str(full), "--pack", str(arc)])
        self.assertEqual(rc, 2); self.assertIn("--output is required", err)
        # wrong kernel
        arc2 = build_pack(self.d / "p2" if (self.d / "p2").mkdir() is None else None, "ab" * 32)
        rc, _, err = quiet(ogt.main, ["integrate", "--base-image", str(full), "--pack", str(arc2),
                                      "--output", str(self.d / "o.img"), "--dry-run"])
        self.assertEqual(rc, 2); self.assertIn("not the Orbis 0.2.0 kernel", err)
        # wrong platform version
        old = base_image(self.d, "old.img", False, self.kernel, self.ird, "orbis-platform 0.1.0-1\n")
        rc, _, err = quiet(ogt.main, ["integrate", "--base-image", str(old), "--pack", str(arc),
                                      "--output", str(self.d / "o.img"), "--dry-run"])
        self.assertEqual(rc, 2); self.assertIn("is not 0.2.0", err)
        # a stage-0 image is not a base for integrate
        s0 = base_image(self.d, "s0.img", True, self.kernel, self.ird)
        rc, _, err = quiet(ogt.main, ["integrate", "--base-image", str(s0), "--pack", str(arc),
                                      "--output", str(self.d / "o.img"), "--dry-run"])
        self.assertEqual(rc, 2); self.assertIn("stage-0 image", err)
        # sparse copy never overwrites
        (self.d / "exists.img").write_bytes(b"x")
        with self.assertRaises(ogt.Refused):
            ogt.sparse_copy(full, self.d / "exists.img")
        with self.assertRaises(ogt.Refused):
            ogt.sparse_copy(full, full)
        # rollback needs an integrated image
        rc, _, err = quiet(ogt.main, ["rollback", "--image", str(full), "--output", str(self.d / "r.img")])
        self.assertEqual(rc, 2); self.assertIn("does not carry", err)

    def test_stage0_overlay_conflict_refused(self):
        arc = build_pack(self.d, self.ksha)
        bad = newc_gz({"init": b"x", "bin/orbis-gfx-stage0": b"old"})
        s0 = base_image(self.d, "s0.img", True, self.kernel, bad)
        rc, _, err = quiet(ogt.main, ["stage0", "--base-stage0-image", str(s0), "--pack", str(arc),
                                      "--output", str(self.d / "g.img"), "--dry-run"])
        self.assertEqual(rc, 2); self.assertIn("would replace", err)

    def test_stage0_dry_run(self):
        arc = build_pack(self.d, self.ksha)
        s0 = base_image(self.d, "s0.img", True, self.kernel, self.ird)
        rc, out, err = quiet(ogt.main, ["stage0", "--base-stage0-image", str(s0), "--pack", str(arc),
                                        "--output", str(self.d / "g.img"), "--dry-run"])
        self.assertEqual(rc, 0, err)
        plan = json.loads(out)["plan"]
        self.assertIn("orbis.gfxdiag=1", plan["bootargs"].split())
        self.assertIn("orbis.stage0=1", plan["bootargs"].split())
        self.assertFalse((self.d / "g.img").exists())
        self.assertEqual([p for p in self.d.iterdir() if p.name.startswith("orbis-gfx-stage0-")], [])

    @unittest.skipUnless(IS_ROOT, "compose needs root (mini-root ownership)")
    def test_stage0_compose_host(self):
        arc = build_pack(self.d, self.ksha)
        s0 = base_image(self.d, "s0.img", True, self.kernel, self.ird)
        before = ogt.sha256(s0)
        out = self.d / "g.img"
        rc, o, err = quiet(ogt.main, ["stage0", "--base-stage0-image", str(s0), "--pack", str(arc),
                                      "--output", str(out), "--size", "800M", "--host"])
        self.assertEqual(rc, 0, o + err)
        self.assertEqual(ogt.sha256(s0), before, "base image must not change")
        info = ogt.inspect_image(out)
        self.assertTrue(info["stage0"])
        self.assertIn("orbis.gfxdiag=1", info["bootargs"].split())
        self.assertTrue(info["miniroot"].startswith("orbis-gfxdiag 1.0.0"))
        files = ogt.fat32.read_root(out, info["partitions"]["boot"]["offset"])
        ov = (ogt.unpack(arc) / "stage0/orbis-gfx-overlay.cpio.gz").read_bytes()
        self.assertEqual(files["initramfs.cpio.gz"], self.ird + ov)
        self.assertEqual(files["bzImage"], self.kernel)
        rec = json.loads(Path(str(out) + ".integration.json").read_text())
        self.assertEqual(rec["result"], "OK")
        self.assertEqual(rec["output_sha256"], ogt.sha256(out))
        self.assertEqual(rec["fsck_host"].get("ext4"), "clean")
        # second run must refuse to overwrite
        rc, _, err = quiet(ogt.main, ["stage0", "--base-stage0-image", str(s0), "--pack", str(arc),
                                      "--output", str(out), "--host"])
        self.assertEqual(rc, 2); self.assertIn("refusing to overwrite", err)


if __name__ == "__main__":
    unittest.main()
