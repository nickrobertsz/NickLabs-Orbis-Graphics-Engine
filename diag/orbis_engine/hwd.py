"""orbis-hwd: the hardware settings/telemetry API for the future Orbis settings UI.

HTTP + JSON, stdlib only. Default bind 127.0.0.1:8421. Reads are open on loopback; every write
needs the token in /etc/orbis/hwd-token (created on first start, root-only). With a LAN bind
(/etc/orbis/hwd.json {"bind": "0.0.0.0"}) every request needs the token and clients outside
private/link-local/loopback ranges are refused, so the console's LAN-only policy holds.

GET  /v1/status            versions, capabilities, short telemetry
GET  /v1/telemetry         full snapshot (?cpu_sample=0.5 for CPU utilisation)
GET  /v1/memory            pool ownership (carve-out, GTT, Linux, per-process GPU memory)
GET  /v1/faults            GPU faults/resets since boot, devcoredumps
GET  /v1/profiles          profiles and the active one
GET  /v1/blackbox          recent Black Box boots and the last-crash verdict
POST /v1/profile           {"name": "performance"} | {"reset": true}
POST /v1/control           {"gpu_sclk": "max", "fan_threshold_c": 70, "cpu_governor": "...",
                            "gtt_budget_mb": 2048} (any subset) | {"reset": true}
POST /v1/memory/boot-plan  {"mode": "ai"} or {"vram_mb": 768, "gtt_mb": 4096} (+ "dry_run")
"""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from orbis_gfx import profile

from . import __version__, blackbox, controls, memory, telemetry
from .util import atomic_write, p, read

DEFAULT_PORT = 8421
_lock = threading.Lock()


def config() -> dict:
    try:
        return json.loads(p("etc/orbis/hwd.json").read_text())
    except (OSError, ValueError):
        return {}


def token(create: bool = True) -> str | None:
    f = p("etc/orbis/hwd-token")
    t = read(f)
    if not t and create:
        t = secrets.token_urlsafe(24)
        atomic_write(f, t + "\n", mode=0o600)
    return t


def client_allowed(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


class Handler(BaseHTTPRequestHandler):
    server_version = f"orbis-hwd/{__version__}"
    lan_bind = False
    tok: str | None = None

    def log_message(self, fmt, *args):  # quiet; systemd journal gets errors only
        pass

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, indent=1, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, write: bool) -> bool:
        if not client_allowed(self.client_address[0]):
            return False
        need = write or self.lan_bind
        if not need:
            return True
        hdr = self.headers.get("Authorization", "")
        return bool(self.tok) and secrets.compare_digest(hdr, f"Bearer {self.tok}")

    def do_GET(self):
        if not self._authorized(False):
            return self._send(401, {"error": "unauthorized"})
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/v1/status":
                return self._send(200, {"api": 2, "version": __version__, "versions": telemetry.versions(),
                                        "capabilities": telemetry.capabilities(),
                                        "gpu": telemetry.gpu(include_busy=True), "thermal": telemetry.thermal(),
                                        "memory": telemetry.memory()})
            if u.path == "/v1/telemetry":
                cs = min(2.0, float(q.get("cpu_sample", ["0"])[0]))
                return self._send(200, telemetry.snapshot(include_busy=True, cpu_sample_s=cs))
            if u.path == "/v1/memory":
                return self._send(200, memory.ownership())
            if u.path == "/v1/faults":
                return self._send(200, telemetry.faults())
            if u.path == "/v1/profiles":
                return self._send(200, {"profiles": {k: v["description"] for k, v in profile.PROFILES.items()},
                                        "state": profile.load_state(), "capabilities": profile.capabilities()})
            if u.path == "/v1/blackbox":
                return self._send(200, {"boots": blackbox.list_boots()[-10:], "last_crash": blackbox.last_crash()})
        except Exception as e:  # report, never crash the daemon
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"error": "unknown endpoint"})

    def do_POST(self):
        if not self._authorized(True):
            return self._send(401, {"error": "unauthorized (Authorization: Bearer <token from /etc/orbis/hwd-token>)"})
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "body must be JSON"})
        dry = bool(req.pop("dry_run", False))
        try:
            with _lock:
                if u.path == "/v1/profile":
                    if req.get("reset"):
                        return self._send(200, profile.reset(dry_run=dry))
                    return self._send(200, profile.apply(req["name"], reason=req.get("reason", "orbis-hwd"), dry_run=dry))
                if u.path == "/v1/control":
                    if req.get("reset"):
                        return self._send(200, controls.reset(dry_run=dry))
                    return self._send(200, {"results": controls.apply_request(req, dry_run=dry)})
                if u.path == "/v1/memory/boot-plan":
                    return self._send(200, memory.apply_boot(req.get("mode"), req.get("vram_mb"), req.get("gtt_mb"),
                                                             dry_run=dry))
        except (KeyError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        except OSError as e:
            return self._send(409, {"error": f"kernel refused: {e}"})
        return self._send(404, {"error": "unknown endpoint"})


def serve(bind: str | None = None, port: int | None = None) -> None:
    cfg = config()
    bind = bind or cfg.get("bind", "127.0.0.1")
    port = int(port or cfg.get("port", DEFAULT_PORT))
    Handler.lan_bind = bind not in ("127.0.0.1", "::1", "localhost")
    Handler.tok = token(create=True)
    httpd = ThreadingHTTPServer((bind, port), Handler)
    print(f"orbis-hwd {__version__} on {bind}:{port} (token in /etc/orbis/hwd-token)", flush=True)
    httpd.serve_forever()
