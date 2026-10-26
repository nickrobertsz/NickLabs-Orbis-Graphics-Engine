"""Add files to the root directory of a freshly made FAT32 filesystem inside an image file.

Used by make_usb_image.py so that building the PS4 boot partition needs no mtools,
no loop device and no mount. Scope is deliberately small:

* FAT32 only (as made by `mkfs.fat -F 32`), files in the root directory only;
* each file gets one contiguous cluster chain; the root directory is extended
  when needed; every FAT copy and the FSInfo hints are updated;
* names keep their case through VFAT long-name entries (bzImage,
  initramfs.cpio.gz, bootargs.txt), each with a unique 8.3 alias;
* timestamps are fixed (default 2026-01-01 00:00), so the result is deterministic;
* an existing name is refused, not overwritten.

`read_root()` is an independent reader used by the image self-check and tests.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

EOC = 0x0FFFFFFF
LFN_ATTR = 0x0F
ATTR_ARCHIVE = 0x20
ATTR_VOLUME = 0x08
ATTR_DIR = 0x10
VALID_SHORT = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$%'-_@~`!(){}^#&")


class FatError(Exception):
    pass


@dataclass
class Geometry:
    bps: int
    spc: int
    reserved: int
    nfats: int
    fat_sectors: int
    root_cluster: int
    total_sectors: int
    fsinfo_sector: int
    base: int  # byte offset of the filesystem in the image

    @property
    def cluster_bytes(self) -> int:
        return self.bps * self.spc

    @property
    def fat_offset(self) -> int:
        return self.base + self.reserved * self.bps

    @property
    def data_offset(self) -> int:
        return self.base + (self.reserved + self.nfats * self.fat_sectors) * self.bps

    @property
    def cluster_count(self) -> int:
        data_sectors = self.total_sectors - self.reserved - self.nfats * self.fat_sectors
        return data_sectors // self.spc

    def cluster_offset(self, c: int) -> int:
        return self.data_offset + (c - 2) * self.cluster_bytes


def geometry(fh, base: int) -> Geometry:
    fh.seek(base)
    bs = fh.read(512)
    if bs[510:512] != b"\x55\xaa":
        raise FatError("no boot sector signature")
    bps, spc, reserved, nfats = struct.unpack_from("<HBHB", bs, 11)
    root_entries, total16 = struct.unpack_from("<HH", bs, 17)
    fat16_size = struct.unpack_from("<H", bs, 22)[0]
    total32 = struct.unpack_from("<I", bs, 32)[0]
    fat32_size, = struct.unpack_from("<I", bs, 36)
    root_cluster, fsinfo = struct.unpack_from("<IH", bs, 44)
    if fat16_size != 0 or root_entries != 0 or bs[82:90] != b"FAT32   ":
        raise FatError("not a FAT32 filesystem")
    if bps not in (512, 1024, 2048, 4096) or spc == 0 or spc & (spc - 1):
        raise FatError("implausible FAT32 geometry")
    return Geometry(bps, spc, reserved, nfats, fat32_size, root_cluster, total16 or total32, fsinfo, base)


def _dos_datetime(ts: tuple[int, int, int, int, int, int]) -> tuple[int, int]:
    y, mo, d, h, mi, s = ts
    return ((y - 1980) << 9) | (mo << 5) | d, (h << 11) | (mi << 5) | (s // 2)


def _short_base(name: str) -> tuple[bytes, bytes]:
    up = name.upper()
    if "." in up and not up.startswith("."):
        base, ext = up.rsplit(".", 1)
    else:
        base, ext = up, ""

    def clean(s):
        return bytes(c if c in VALID_SHORT else ord("_") for c in s.replace(".", "").replace(" ", "").encode("ascii", "replace"))
    return clean(base), clean(ext)[:3]


def _is_exact_short(name: str) -> bool:
    b, e = _short_base(name)
    return name == name.upper() and len(b) <= 8 and (b + (b"." + e if e else b"")).decode() == name


def short_name(name: str, taken: set[bytes]) -> bytes:
    base, ext = _short_base(name)
    if _is_exact_short(name):
        cand = base.ljust(8) + ext.ljust(3)
        if cand not in taken:
            return cand
    for n in range(1, 1000000):
        tail = b"~%d" % n
        cand = (base[:8 - len(tail)] + tail).ljust(8) + ext.ljust(3)
        if cand not in taken:
            return cand
    raise FatError("no free short name")


def lfn_checksum(short: bytes) -> int:
    s = 0
    for c in short:
        s = (((s & 1) << 7) + (s >> 1) + c) & 0xFF
    return s


def lfn_entries(name: str, short: bytes) -> list[bytes]:
    u = name.encode("utf-16-le")
    chars = [u[i:i + 2] for i in range(0, len(u), 2)]
    if len(chars) > 255:
        raise FatError("name too long")
    chars.append(b"\0\0")
    while len(chars) % 13:
        chars.append(b"\xff\xff")
    chk = lfn_checksum(short)
    parts = [chars[i:i + 13] for i in range(0, len(chars), 13)]
    if parts[-1] == [b"\xff\xff"] * 13:  # name was an exact multiple of 13: no terminator part needed
        parts.pop()
    out = []
    for i, p in enumerate(parts, 1):
        seq = i | (0x40 if i == len(parts) else 0)
        e = bytes([seq]) + b"".join(p[0:5]) + bytes([LFN_ATTR, 0, chk]) + b"".join(p[5:11]) + b"\0\0" + b"".join(p[11:13])
        out.append(e)
    return list(reversed(out))


def short_entry(short: bytes, attr: int, cluster: int, size: int, ts) -> bytes:
    d, t = _dos_datetime(ts)
    return struct.pack("<11sBBBHHHHHHHI", short, attr, 0, 0, t, d, d, cluster >> 16, t, d, cluster & 0xFFFF, size)


class Fat32:
    def __init__(self, fh, base: int):
        self.fh = fh
        self.g = geometry(fh, base)
        fh.seek(self.g.fat_offset)
        raw = fh.read(self.g.fat_sectors * self.g.bps)
        self.fat = list(struct.unpack(f"<{len(raw) // 4}I", raw))
        self.dirty = False

    def chain(self, c: int) -> list[int]:
        out = []
        while 2 <= c < 0x0FFFFFF8:
            if c in out or len(out) > self.g.cluster_count:
                raise FatError("cluster loop")
            out.append(c)
            c = self.fat[c] & 0x0FFFFFFF
        return out

    def alloc(self, n: int) -> list[int]:
        """n contiguous free clusters (first fit)."""
        last = self.g.cluster_count + 1
        run = 0
        for c in range(2, last + 1):
            run = run + 1 if (self.fat[c] & 0x0FFFFFFF) == 0 else 0
            if run == n:
                cl = list(range(c - n + 1, c + 1))
                for a, b in zip(cl, cl[1:]):
                    self.fat[a] = (self.fat[a] & 0xF0000000) | b
                self.fat[cl[-1]] = (self.fat[cl[-1]] & 0xF0000000) | EOC
                self.dirty = True
                return cl
        raise FatError(f"no room for {n} contiguous clusters")

    def read_dir(self) -> tuple[list[int], bytearray]:
        cl = self.chain(self.g.root_cluster)
        buf = bytearray()
        for c in cl:
            self.fh.seek(self.g.cluster_offset(c))
            buf += self.fh.read(self.g.cluster_bytes)
        return cl, buf

    def write_dir(self, cl: list[int], buf: bytearray) -> None:
        cb = self.g.cluster_bytes
        for i, c in enumerate(cl):
            self.fh.seek(self.g.cluster_offset(c))
            self.fh.write(bytes(buf[i * cb:(i + 1) * cb]))

    def flush(self) -> None:
        if not self.dirty:
            return
        raw = struct.pack(f"<{len(self.fat)}I", *self.fat)
        for i in range(self.g.nfats):
            self.fh.seek(self.g.fat_offset + i * self.g.fat_sectors * self.g.bps)
            self.fh.write(raw)
        free = sum(1 for c in range(2, self.g.cluster_count + 2) if (self.fat[c] & 0x0FFFFFFF) == 0)
        nxt = next((c for c in range(2, self.g.cluster_count + 2) if (self.fat[c] & 0x0FFFFFFF) == 0), 0xFFFFFFFF)
        if self.g.fsinfo_sector not in (0, 0xFFFF):
            off = self.g.base + self.g.fsinfo_sector * self.g.bps
            self.fh.seek(off)
            fsi = bytearray(self.fh.read(512))
            if fsi[0:4] == b"RRaA" and fsi[484:488] == b"rrAa":
                struct.pack_into("<II", fsi, 488, free, nxt)
                self.fh.seek(off)
                self.fh.write(fsi)
        self.dirty = False


def add_files(image: str | Path, base: int, files: list[tuple[str, str | Path]],
              timestamp=(2026, 1, 1, 0, 0, 0)) -> list[dict]:
    """Copy files into the root directory. Returns [{name, short, cluster, size}]."""
    with open(image, "r+b") as fh:
        fs = Fat32(fh, base)
        cl, d = fs.read_dir()
        names = {e["name"].lower() for e in _parse_dir(d)}
        taken = {bytes(d[i:i + 11]) for i in range(0, len(d), 32) if d[i] not in (0, 0xE5) and d[i + 11] != LFN_ATTR}
        end = next((i for i in range(0, len(d), 32) if d[i] == 0), len(d))
        result = []
        for name, src in files:
            if name.lower() in names:
                raise FatError(f"{name} already exists")
            data = Path(src).read_bytes()
            n = max(1, -(-len(data) // fs.g.cluster_bytes)) if data else 0
            chain = fs.alloc(n) if n else []
            first = chain[0] if chain else 0
            if chain:
                fh.seek(fs.g.cluster_offset(first))
                fh.write(data)
                pad = n * fs.g.cluster_bytes - len(data)
                if pad:
                    fh.write(b"\0" * pad)
            short = short_name(name, taken)
            taken.add(short)
            ents = ([] if _is_exact_short(name) else lfn_entries(name, short)) + [
                short_entry(short, ATTR_ARCHIVE, first, len(data), timestamp)]
            need = end + 32 * len(ents) + 32  # keep one terminating free entry
            while need > len(d):
                c = fs.alloc(1)[0]
                fs.fat[cl[-1]] = (fs.fat[cl[-1]] & 0xF0000000) | c
                cl.append(c)
                d += b"\0" * fs.g.cluster_bytes
            for e in ents:
                d[end:end + 32] = e
                end += 32
            names.add(name.lower())
            result.append({"name": name, "short": short.decode(), "cluster": first, "size": len(data)})
        fs.write_dir(cl, d)
        fs.flush()
    return result


def _parse_dir(d: bytes) -> list[dict]:
    """Independent directory parser: LFN reassembly with checksum validation."""
    out, lfn, chk = [], {}, None
    for i in range(0, len(d), 32):
        e = d[i:i + 32]
        if e[0] == 0:
            break
        if e[0] == 0xE5:
            lfn, chk = {}, None
            continue
        if e[11] == LFN_ATTR:
            seq = e[0] & 0x1F
            if e[0] & 0x40:
                lfn, chk = {}, e[13]
            raw = e[1:11] + e[14:26] + e[28:32]
            lfn[seq] = raw
            continue
        short = bytes(e[0:11])
        attr = e[11]
        name = None
        if lfn and chk == lfn_checksum(short):
            raw = b"".join(lfn[k] for k in sorted(lfn))
            name = raw.decode("utf-16-le").split("\0")[0]
        if name is None:
            b, x = short[:8].decode().rstrip(), short[8:].decode().rstrip()
            name = b + ("." + x if x else "")
        hi, lo, size = struct.unpack_from("<H", e, 20)[0], struct.unpack_from("<H", e, 26)[0], struct.unpack_from("<I", e, 28)[0]
        out.append({"name": name, "short": short.decode(errors="replace"), "attr": attr,
                    "cluster": (hi << 16) | lo, "size": size})
        lfn, chk = {}, None
    return out


def read_root(image: str | Path, base: int) -> dict[str, bytes]:
    """{name: content} for the regular files in the root directory."""
    with open(image, "rb") as fh:
        fs = Fat32(fh, base)
        _, d = fs.read_dir()
        out = {}
        for e in _parse_dir(d):
            if e["attr"] & (ATTR_VOLUME | ATTR_DIR):
                continue
            data = bytearray()
            for c in fs.chain(e["cluster"]) if e["cluster"] else []:
                fh.seek(fs.g.cluster_offset(c))
                data += fh.read(fs.g.cluster_bytes)
            out[e["name"]] = bytes(data[:e["size"]])
        return out
