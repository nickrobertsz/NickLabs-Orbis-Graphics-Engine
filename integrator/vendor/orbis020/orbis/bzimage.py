"""x86 bzImage inspection for the PS4 kexec loader.

Why this exists: a published PS4 HDMI investigation (aday1/ps4-hdmi-blackscreen)
observed that a kernel built with boot protocol 2.13 hard-locked the console,
while 2.15 kernels booted. The PS4 loader (ps4-linux-loader, ps4-kexec-common)
copies the setup header, sets ``hardware_subarch = X86_SUBARCH_PS4`` and jumps
to the 64-bit entry point, so a kernel must:

  * carry a valid setup header ("HdrS") with a protocol we have evidence for,
  * advertise XLF_KERNEL_64 (64-bit entry point),
  * have been built with CONFIG_X86_PS4 (otherwise the subarch is meaningless).

Header offsets follow Documentation/arch/x86/boot.rst.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Protocol versions with direct PS4 boot evidence (see BUG-LEDGER: BOOT-001).
KNOWN_GOOD_PROTOCOLS = {(2, 15)}
KNOWN_BAD_PROTOCOLS = {(2, 13)}

XLF_KERNEL_64 = 1 << 0
XLF_CAN_BE_LOADED_ABOVE_4G = 1 << 1

_COMPRESSION_MAGIC = [
    (b"\x1f\x8b", "gzip"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"\x02\x21\x4c\x18", "lz4"),
    (b"BZh", "bzip2"),
    (b"\x5d\x00\x00", "lzma"),
    (b"\x89LZO", "lzo"),
]


@dataclass
class BzImageReport:
    path: str
    size: int
    valid_header: bool = False
    protocol: tuple[int, int] | None = None
    kernel_version: str | None = None
    setup_sects: int | None = None
    loadflags: int | None = None
    xloadflags: int | None = None
    cmdline_size: int | None = None
    relocatable: bool | None = None
    init_size: int | None = None
    payload_compression: str | None = None
    ikconfig: dict[str, str] | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.valid_header and not self.errors

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["protocol"] = f"{self.protocol[0]}.{self.protocol[1]:02d}" if self.protocol else None
        d["ok"] = self.ok
        return d


def _cstr(buf: bytes, off: int, limit: int = 256) -> str | None:
    if off <= 0 or off >= len(buf):
        return None
    end = buf.find(b"\x00", off, off + limit)
    if end < 0:
        return None
    return buf[off:end].decode("ascii", "replace")


def _decompress(kind: str, data: bytes, max_out: int = 256 << 20) -> bytes | None:
    try:
        if kind == "gzip":
            return gzip.GzipFile(fileobj=io.BytesIO(data)).read(max_out)
        if kind == "xz":
            return lzma.LZMADecompressor(format=lzma.FORMAT_XZ).decompress(data, max_out)
        if kind == "lzma":
            return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(data, max_out)
        if kind == "bzip2":
            return bz2.BZ2Decompressor().decompress(data)
        if kind == "zstd" and shutil.which("zstd"):
            r = subprocess.run(["zstd", "-dc"], input=data, capture_output=True, timeout=120)
            return r.stdout if r.stdout else None
    except (OSError, EOFError, lzma.LZMAError, subprocess.SubprocessError):
        return None
    return None


def _extract_ikconfig(vmlinux: bytes) -> dict[str, str] | None:
    start = vmlinux.find(b"IKCFG_ST")
    if start < 0:
        return None
    end = vmlinux.find(b"IKCFG_ED", start)
    blob = vmlinux[start + 8:end if end > 0 else None]
    try:
        text = gzip.decompress(blob).decode("utf-8", "replace")
    except (OSError, EOFError):
        return None
    out = {}
    for line in text.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            out[line[2:-11]] = "n"
    return out


def inspect(path: str | Path, deep: bool = True) -> BzImageReport:
    p = Path(path)
    data = p.read_bytes()
    rep = BzImageReport(path=str(p), size=len(data))
    if len(data) < 0x270:
        rep.errors.append("file too small to be a bzImage")
        return rep
    if data[0x1FE:0x200] != b"\x55\xaa":
        rep.errors.append("missing 0xAA55 boot flag")
    if data[0x202:0x206] != b"HdrS":
        rep.errors.append("missing 'HdrS' setup header magic (not a bzImage?)")
        return rep
    rep.valid_header = True
    rep.setup_sects = data[0x1F1] or 4
    ver = struct.unpack_from("<H", data, 0x206)[0]
    rep.protocol = (ver >> 8, ver & 0xFF)
    kv_ptr = struct.unpack_from("<H", data, 0x20E)[0]
    rep.kernel_version = _cstr(data, kv_ptr + 0x200) if kv_ptr else None
    rep.loadflags = data[0x211]
    if rep.protocol >= (2, 5):
        rep.relocatable = bool(data[0x234])
    if rep.protocol >= (2, 12):
        rep.xloadflags = struct.unpack_from("<H", data, 0x236)[0]
    if rep.protocol >= (2, 6):
        rep.cmdline_size = struct.unpack_from("<I", data, 0x238)[0]
    if rep.protocol >= (2, 10):
        rep.init_size = struct.unpack_from("<I", data, 0x260)[0]

    # ---- policy checks for the PS4 kexec loader
    if rep.protocol in KNOWN_BAD_PROTOCOLS:
        rep.errors.append(
            f"boot protocol {rep.protocol[0]}.{rep.protocol[1]:02d} hard-locked a PS4 in a "
            "published report; refusing")
    elif rep.protocol not in KNOWN_GOOD_PROTOCOLS:
        rep.warnings.append(
            f"boot protocol {rep.protocol[0]}.{rep.protocol[1]:02d} has no PS4 boot evidence "
            f"(known good: {', '.join(f'{a}.{b:02d}' for a, b in sorted(KNOWN_GOOD_PROTOCOLS))})")
    if rep.xloadflags is None or not (rep.xloadflags & XLF_KERNEL_64):
        rep.errors.append("XLF_KERNEL_64 not set: no 64-bit entry point for the PS4 loader")
    if not (rep.loadflags & 0x01):
        rep.errors.append("LOADED_HIGH not set (zImage-style kernel)")

    # ---- payload
    if rep.protocol >= (2, 8):
        setup_size = (rep.setup_sects + 1) * 512
        poff, plen = struct.unpack_from("<II", data, 0x248)
        payload = data[setup_size + poff: setup_size + poff + plen]
        for magic, kind in _COMPRESSION_MAGIC:
            if payload.startswith(magic):
                rep.payload_compression = kind
                break
        if deep and rep.payload_compression:
            vm = _decompress(rep.payload_compression, payload)
            if vm is None:
                rep.warnings.append(
                    f"could not decompress {rep.payload_compression} payload; "
                    "embedded config not checked")
            else:
                rep.ikconfig = _extract_ikconfig(vm)
                if rep.ikconfig is None:
                    rep.warnings.append("no embedded IKCONFIG; cannot confirm CONFIG_X86_PS4")
                else:
                    if rep.ikconfig.get("CONFIG_X86_PS4") != "y":
                        rep.errors.append("embedded config lacks CONFIG_X86_PS4=y (not a PS4 kernel)")
                    fw = rep.ikconfig.get("CONFIG_EXTRA_FIRMWARE", '""')
                    if fw not in ('""', "", "n"):
                        rep.warnings.append(
                            f"kernel embeds firmware {fw}; check redistribution licenses "
                            "before publishing this binary")
    return rep


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Check a bzImage against the PS4 loader contract")
    ap.add_argument("bzimage")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--shallow", action="store_true", help="skip payload decompression")
    a = ap.parse_args(argv)
    rep = inspect(a.bzimage, deep=not a.shallow)
    if a.json:
        d = rep.to_dict()
        if d.get("ikconfig"):
            d["ikconfig"] = {k: v for k, v in d["ikconfig"].items()
                             if k in ("CONFIG_X86_PS4", "CONFIG_LOCALVERSION",
                                      "CONFIG_EXTRA_FIRMWARE", "CONFIG_CC_VERSION_TEXT")}
        print(json.dumps(d, indent=2))
    else:
        print(f"{rep.path}: {'OK' if rep.ok else 'REJECT'}")
        print(f"  protocol      {rep.protocol[0]}.{rep.protocol[1]:02d}" if rep.protocol else "  protocol      ?")
        print(f"  version       {rep.kernel_version}")
        print(f"  xloadflags    {rep.xloadflags:#06x}" if rep.xloadflags is not None else "  xloadflags    -")
        print(f"  cmdline_size  {rep.cmdline_size}")
        print(f"  compression   {rep.payload_compression}")
        if rep.ikconfig is not None:
            print(f"  X86_PS4       {rep.ikconfig.get('CONFIG_X86_PS4', 'absent')}")
        for e in rep.errors:
            print(f"  ERROR: {e}")
        for w in rep.warnings:
            print(f"  WARN:  {w}")
    return 0 if rep.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
