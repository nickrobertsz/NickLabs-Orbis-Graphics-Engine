"""Command-line front ends of the Porting Kit.

orbis-hw probe | identify | compatibility | qualify       (on the console or on Nobara with files)
orbis-ge target list | inspect <id> | generate | check   (python3 -m orbis_targets.cli target ...)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import compat, match, probe
from .registry import Registry, RegistryError, generated_path, write_generated


def _probe_doc(a) -> dict:
    if getattr(a, "probe", None):
        doc = json.loads(Path(a.probe).read_text())
    else:
        doc = probe.probe()
    for lf in getattr(a, "log", None) or []:
        doc.setdefault("logs", []).append(probe.import_log(Path(lf).read_text(errors="replace"), os.path.basename(lf)))
    if getattr(a, "model", None):
        doc.setdefault("hints", {})["model"] = a.model.upper()
    return doc


def recovery(eff: dict) -> dict:
    if eff["status"] == "unsupported":
        return {"ge_image": None, "advice": "no Graphics Engine image exists for this target; keep OrbisOS, or a community kernel listed in the southbridge file",
                "community": eff["southbridge_info"].get("community")}
    return {"ge_image": "out/images/orbis-0.2.0+ge-<ver>.img (copy of the target's 0.2.0 image)",
            "stage0": "out/images/orbis-0.2.0-stage0+ge-<ver>.img (diagnostics, built only with --base-stage0-image)",
            "rollback": ["flash the original 0.2.0 image (never modified)", "sudo orbis boot rollback-kernel",
                         "sudo /usr/lib/orbis-graphics/rollback-root.sh --root / --boot /boot/ps4"],
            "kernel_flavor": eff["kernel"].get("flavor")}


def hw_main(cmd: str, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog=f"orbis-hw {cmd}")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--log", action="append", help="bootloader/serial/dmesg text to import (redacted)")
    ap.add_argument("--model", help="CUH model from the label (weak evidence)")
    if cmd != "probe":
        ap.add_argument("--probe", help="probe JSON from `orbis-hw probe --json` (default: probe this machine)")
    if cmd in ("compatibility", "qualify"):
        ap.add_argument("--target", help="target id (default: the identified target, only when confirmed)")
    ap.add_argument("--out", help="also write the JSON here")
    a = ap.parse_args(argv)
    try:
        reg = Registry()
    except RegistryError as e:
        print(f"orbis-hw: target registry invalid:\n{e}", file=sys.stderr)
        return 2
    doc = _probe_doc(a)
    rc = 0
    if cmd == "probe":
        out = doc
    elif cmd == "identify":
        out = match.match(reg, probe.facts(doc))
        rc = {"confirmed": 0, "probable": 0, "unknown": 4, "conflicting": 5}[out["result"]]
    else:
        tid = a.target
        if not tid:
            m = match.match(reg, probe.facts(doc))
            if m["result"] != "confirmed":
                print(f"orbis-hw {cmd}: this console is {m['result']} ({m.get('target') or m.get('candidates')}); "
                      "pass --target <id> (no target is assumed)", file=sys.stderr)
                return 4
            tid = m["target"]
        if tid not in reg.targets():
            print(f"orbis-hw: unknown target {tid}", file=sys.stderr)
            return 2
        if cmd == "compatibility":
            out = compat.compatibility(reg, tid, doc)
            out["recovery"] = recovery(reg.resolve(tid))
        else:
            out = compat.qualify(reg, tid, doc)
            rc = {"PASS": 0, "INCOMPLETE": 0, "FAIL": 1, "BLOCKED": 4}[out["result"]]
            d = Path(os.environ.get("ORBIS_GFX_ROOT", "/")) / "var/lib/orbis-graphics/qualification"
            try:
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{out['id']}.json").write_text(json.dumps(out, indent=1) + "\n")
                out["saved"] = str(d / f"{out['id']}.json")
            except OSError:
                pass
    text = json.dumps(out, indent=1, default=sorted)
    if a.out:
        Path(a.out).write_text(text + "\n")
    if a.json or cmd == "probe":
        print(text)
    else:
        _human(cmd, out)
    return rc


def _human(cmd: str, out: dict) -> None:
    if cmd == "identify":
        print(f"result: {out['result']}" + (f"  target: {out['target']} ({out.get('status')})" if out.get("target") else ""))
        for k in ("why", "missing_evidence", "needed_evidence", "conflicts", "candidates"):
            if out.get(k):
                print(f"{k}: {out[k]}")
    elif cmd == "compatibility":
        print(f"{out['target']}: {out['name']}  status {out['status']}  kernel {out['kernel'].get('flavor')} ({out['kernel'].get('state')})")
        for c, v in sorted(out["capabilities"].items()):
            o = v.get("observed_now")
            print(f"  {c:18} {v['state']:18} {v.get('reason') or ''}{('  now: ' + o['observed'] + ' ' + o['detail']) if o else ''}")
    else:
        print(f"qualification {out.get('id')}: {out['result']}  {out.get('why') or ''}")
        for c, v in sorted((out.get("checks") or {}).items()):
            print(f"  {c:18} {v['result']:12} {v.get('detail') or v.get('how') or v.get('reason') or ''}")


def target_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="orbis-ge target")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    i = sub.add_parser("inspect")
    i.add_argument("id")
    sub.add_parser("generate")
    sub.add_parser("check", help="registry valid and generated/registry.json up to date")
    r = sub.add_parser("resolve", help="print one field of a target (for scripts)")
    r.add_argument("id")
    r.add_argument("field", choices=("status", "kernel-flavor", "kernel-state", "reference", "buildable"))
    a = ap.parse_args(argv)
    try:
        reg = Registry()
    except RegistryError as e:
        print(f"target registry invalid:\n{e}", file=sys.stderr)
        return 2
    if a.cmd == "list":
        print(f"{'target':18} {'family':6} {'southbridge':11} {'gpu':7} {'kernel flavor':20} status")
        for t in reg.targets():
            e = reg.resolve(t)
            print(f"{t:18} {e.get('family', ''):6} {e.get('southbridge', ''):11} {(e['apu_info'].get('gpu') or {}).get('pci_device', ''):7} "
                  f"{str(e['kernel'].get('flavor')):20} {e['status']}{'  (reference)' if e['reference'] else ''}")
        print("\nstates: verified | awaiting-hardware | unsupported (with reason). Nothing is 'verified' until a qualification record cites it.")
        return 0
    if a.cmd == "inspect":
        try:
            e = reg.resolve(a.id)
        except KeyError as x:
            print(x, file=sys.stderr)
            return 2
        e["recovery"] = recovery(e)
        print(json.dumps(e, indent=1))
        return 0
    if a.cmd == "generate":
        print(write_generated(reg))
        return 0
    if a.cmd == "check":
        gp = generated_path(reg.root)
        want = json.dumps(reg.generate(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        have = gp.read_text() if gp.exists() else ""
        if want != have:
            print(f"{gp} is stale: run ./orbis-ge target generate", file=sys.stderr)
            return 1
        print(f"registry OK: {len(reg.targets())} targets, reference {reg.reference()}")
        return 0
    if a.id not in reg.targets():
        print(f"unknown target {a.id}; known: {' '.join(reg.targets())}", file=sys.stderr)
        return 2
    e = reg.resolve(a.id)
    val = {"status": e["status"], "kernel-flavor": e["kernel"].get("flavor") or "", "kernel-state": e["kernel"].get("state"),
           "reference": "yes" if e["reference"] else "no", "buildable": "no" if e["kernel"].get("state") == "unsupported" else "yes"}[a.field]
    print(val)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "target":
        return target_main(argv[1:])
    if argv and argv[0] in ("probe", "identify", "compatibility", "qualify"):
        return hw_main(argv[0], argv[1:])
    if argv and argv[0] == "port":
        from . import portforge
        return portforge.main(argv[1:])
    print("usage: python -m orbis_targets.cli {target|probe|identify|compatibility|qualify|port} ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
