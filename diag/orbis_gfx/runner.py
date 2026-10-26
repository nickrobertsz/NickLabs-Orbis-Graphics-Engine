"""Bounded execution of external tools (the probe, vulkaninfo, vainfo, ...).

A GPU hang must never wedge the caller: each tool runs in its own process group with a
deadline and is killed (SIGKILL to the group) when it expires."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Run:
    argv: list
    rc: int | None
    stdout: str
    stderr: str
    timed_out: bool
    seconds: float
    missing: bool = False

    def json(self):
        try:
            return json.loads(self.stdout)
        except (ValueError, TypeError):
            return None


def run(argv: list, timeout: float = 60.0, env: dict | None = None) -> Run:
    exe = argv[0]
    if not (os.path.isabs(exe) and os.access(exe, os.X_OK)) and not shutil.which(exe):
        return Run(argv, None, "", f"{exe}: not installed", False, 0.0, missing=True)
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    t0 = time.monotonic()
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=full_env, start_new_session=True)
    except OSError as exc:
        return Run(argv, None, "", str(exc), False, 0.0, missing=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return Run(argv, p.returncode, out, err, False, time.monotonic() - t0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = p.communicate()
        return Run(argv, None, out or "", err or "", True, time.monotonic() - t0)


def probe_path() -> str:
    for c in (os.environ.get("ORBIS_GFX_PROBE"), "/usr/lib/orbis-graphics/orbis-gfx-probe",
              str(Path(__file__).resolve().parents[1] / "probe" / "orbis-gfx-probe")):
        if c and os.access(c, os.X_OK):
            return c
    return "/usr/lib/orbis-graphics/orbis-gfx-probe"
