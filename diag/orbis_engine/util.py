"""Small shared helpers (stdlib only)."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from orbis_gfx import sysfs

p = sysfs.p
read = sysfs.read
read_int = sysfs.read_int


def kv(text: str | None) -> dict:
    """Parse 'a=1 b=0x2 c=text' (one line or several) into a dict; ints where possible."""
    out = {}
    if not text:
        return out
    for tok in re.findall(r"(\w+)=(\S+)", text):
        k, v = tok
        try:
            out[k] = int(v, 0)
        except ValueError:
            out[k] = v
    return out


def colon_kv(text: str | None) -> dict:
    """Parse 'key: value' lines (debugfs style) into a dict; ints where possible."""
    out = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition(":")
        if not sep:
            continue
        v = v.strip()
        try:
            out[k.strip()] = int(v, 0)
        except ValueError:
            out[k.strip()] = v
    return out


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def atomic_write(path: Path, data: str | bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path: Path, rec: dict, fsync: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        if fsync:
            fh.flush()
            os.fsync(fh.fileno())


def write_sysfs(path: Path, value: str) -> None:
    """Write one value to a sysfs/procfs file (raises OSError with the kernel's errno)."""
    with open(path, "w") as fh:
        fh.write(value)


def cmdline_params() -> dict:
    out = {}
    for tok in sysfs.cmdline().split():
        k, sep, v = tok.partition("=")
        out[k] = v if sep else True
    return out


def hwmon_by_name(name: str) -> Path | None:
    base = p("sys/class/hwmon")
    for h in sorted(base.glob("hwmon*")) if base.is_dir() else []:
        if read(h / "name") == name:
            return h
    return None
