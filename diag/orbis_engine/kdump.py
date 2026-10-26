"""Optional crash-capture kernel (kdump) for the Black Box — EXPERIMENTAL on PS4.

On a panic the running kernel kexec's into a second copy of itself loaded in reserved memory
(crashkernel=). That capture kernel boots without the GPU (nomodeset), mounts ORBIS-ROOT,
writes the panicked kernel's log (vmcore-dmesg) to /var/lib/orbis-blackbox/kdump/<time>/ and
reboots. x86 kexec_file_load copies hardware_subarch, so the capture kernel still sees a PS4.
Whether Aeolia/Belize USB comes up again after a panic has not been tested; netconsole is
the first-line panic capture and kdump is the second.

  orbis-debug kdump enable    reserve crashkernel=256M in bootargs.txt (reboot), build the
                              capture initramfs, enable orbis-kdump.service
  orbis-debug kdump status
  orbis-debug kdump disable
"""
from __future__ import annotations

import gzip
import io
import os
import shutil
import stat
import subprocess
from pathlib import Path

from orbis_gfx import runner

from .memory import boot_dir
from .util import atomic_write, p, read

CRASHKERNEL = "crashkernel=256M"
CAPTURE = "usr/lib/orbis-graphics/kdump/capture.cpio.gz"
CAPTURE_INIT = r"""#!/bin/busybox sh
# NickLabs Orbis Black Box capture init (runs in the kdump kernel after a panic)
/bin/busybox mkdir -p /proc /sys /dev /root /bin /sbin /usr/bin /usr/sbin
/bin/busybox --install -s
mount -t proc proc /proc; mount -t sysfs sys /sys; mount -t devtmpfs dev /dev
echo "orbis-kdump: capture kernel up" > /dev/kmsg
r=""
i=0
while [ $i -lt 60 ]; do
  r=$(findfs LABEL=ORBIS-ROOT 2>/dev/null) && [ -n "$r" ] && break
  i=$((i + 1)); sleep 1
done
if [ -n "$r" ] && mount -o rw "$r" /root; then
  d=/root/var/lib/orbis-blackbox/kdump/$(date +%Y%m%d-%H%M%S)
  mkdir -p "$d"
  vmcore-dmesg /proc/vmcore > "$d/vmcore-dmesg.txt" 2> "$d/vmcore-dmesg.err"
  cat /proc/cmdline > "$d/capture-cmdline.txt"
  echo "captured $(date)" > "$d/STATUS"
  sync; umount /root
  echo "orbis-kdump: saved $d" > /dev/kmsg
else
  echo "orbis-kdump: ORBIS-ROOT not found; nothing saved" > /dev/kmsg
fi
sleep 2
echo b > /proc/sysrq-trigger
"""


def _newc(entries: list[tuple[str, int, bytes]]) -> bytes:
    buf = io.BytesIO()
    for ino, (name, mode, data) in enumerate(entries + [("TRAILER!!!", 0, b"")], 1):
        nb = name.encode() + b"\0"
        fields = [ino, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1, 0, len(data), 0, 0, 0, 0, len(nb), 0]
        buf.write(b"070701" + b"".join(f"{x:08X}".encode() for x in fields))
        buf.write(nb + b"\0" * ((-(110 + len(nb))) % 4))
        buf.write(data + b"\0" * ((-len(data)) % 4))
    return buf.getvalue()


def _libs(binary: str) -> list[str]:
    r = runner.run(["ldd", binary], timeout=10)
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("=>")
        path = (parts[1] if len(parts) > 1 else parts[0]).strip().split(" (")[0].strip()
        if path.startswith("/"):
            out.append(path)
    return out


def build_capture(out: Path | None = None) -> Path:
    """Capture initramfs from the installed busybox (static) and kexec-tools' vmcore-dmesg."""
    bb = shutil.which("busybox")
    vd = shutil.which("vmcore-dmesg")
    if not bb or not vd:
        raise FileNotFoundError("needs busybox and vmcore-dmesg (packages busybox, kexec-tools)")
    files = {"bin/busybox": bb, "usr/bin/vmcore-dmesg": vd}
    for lib in _libs(vd):
        files[lib.lstrip("/")] = lib
    dirs = sorted({str(Path(k).parent) for k in files} | {"proc", "sys", "dev", "root"})
    ents = []
    for d in dirs:
        acc = ""
        for part in Path(d).parts:
            acc = f"{acc}/{part}" if acc else part
            if (acc, stat.S_IFDIR | 0o755, b"") not in ents:
                ents.append((acc, stat.S_IFDIR | 0o755, b""))
    ents.append(("init", stat.S_IFREG | 0o755, CAPTURE_INIT.encode()))
    for dst, src in sorted(files.items()):
        ents.append((dst, stat.S_IFREG | 0o755, Path(os.path.realpath(src)).read_bytes()))
    data = gzip.compress(_newc(ents), mtime=0)
    out = out or p(CAPTURE)
    atomic_write(out, data)
    return out


def _bootargs() -> Path:
    return boot_dir() / "bootargs.txt"


def status() -> dict:
    ba = read(_bootargs()) or ""
    loaded = read(p("sys/kernel/kexec_crash_loaded"))
    size = read(p("sys/kernel/kexec_crash_size"))
    caps = sorted(x.name for x in p("var/lib/orbis-blackbox/kdump").glob("*")) if p("var/lib/orbis-blackbox/kdump").is_dir() else []
    return {"reserved_in_bootargs": CRASHKERNEL in ba.split(), "crash_kernel_loaded": loaded == "1",
            "reserved_bytes": int(size) if size and size.isdigit() else None,
            "capture_initramfs": p(CAPTURE).exists(), "captures": caps, "experimental": True}


def set_bootarg(enable: bool, dry_run: bool = False) -> dict:
    f = _bootargs()
    cur = (read(f) or "").splitlines()[0] if read(f) else ""
    toks = [t for t in cur.split() if not t.startswith("crashkernel=")]
    if enable:
        toks.append(CRASHKERNEL)
    new = " ".join(toks)
    act = {"file": str(f), "old": cur, "new": new, "dry_run": dry_run}
    if not dry_run and new != cur:
        atomic_write(f.with_name("bootargs.txt.orbis-prev"), cur + "\n")
        atomic_write(f, new + "\n")
    return act


def load(dry_run: bool = False) -> dict:
    """ExecStart of orbis-kdump.service: load the capture kernel (kexec_file_load)."""
    rel = read(p("proc/sys/kernel/osrelease"))
    kernel = p(f"usr/lib/modules/{rel}/vmlinuz")
    cl = [t for t in (read(p("proc/cmdline")) or "").split()
          if not t.startswith(("crashkernel=", "orbis.gfxdiag", "orbis.stage0", "netconsole="))]
    append = " ".join(cl + ["nr_cpus=1", "irqpoll", "reset_devices", "nomodeset", "amdgpu.modeset=0",
                            "orbis.capture=1", "panic=10", "systemd.unit=emergency.target"])
    argv = ["kexec", "-s", "-p", str(kernel), f"--initrd={p(CAPTURE)}", f"--append={append}"]
    if dry_run:
        return {"argv": argv}
    if not p(CAPTURE).exists():
        build_capture()
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    return {"argv": argv, "rc": r.returncode, "stderr": r.stderr[-2000:]}
