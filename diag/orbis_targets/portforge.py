"""PortForge: a reviewable workflow for porting the Graphics Engine to another PS4 board.

    portforge new <id> --probe probe.json [--log FILE ...] [--model CUH-....] [--base-target ID]
    portforge candidate <id> --patch FILE --origin community|manual --source "repo@ref:path" [--note T]
    portforge assist <id> --assistant "cmd:<command>" [--ask TEXT]      (optional local model; see below)
    portforge diff-trees <id> --base DIR --other DIR --paths p1,p2 --source "repo@ref"
    portforge check <id> --kernel-tree DIR                             (apply, static and compile-recipe checks)
    portforge review <id> <candidate> --approve|--reject --reviewer NAME [--notes T]
    portforge report <id>                                              (review report + build recipe)
    portforge promote <id>                                             (draft target -> targets/models, all reviewed)

Workspace: out/portforge/<id>/ (inventory, match, comparison, gap report, candidates/, checks, review.md,
recipe.sh, target-draft.json, provenance.jsonl).

Rules that are enforced, not just documented:
  * Every candidate change has a provenance record (origin, source or assistant command, input and
    output SHA-256, time) and starts "unreviewed".
  * The assistant is any local command (for example llama.cpp in Orbis AI mode); no proprietary
    service is needed and none is called by default. Its output is only ever a candidate.
  * recipe.sh is a Nobara build recipe; it refuses to run while any candidate is unreviewed, and
    nothing PortForge writes is meant to be executed on a PS4.
  * promote refuses unless every candidate is approved and the draft validates against the schema.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import compat
from . import match as matcher
from . import probe as prober
from .registry import KERNEL_CAPS, Registry, validate


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ws_root() -> Path:
    return Path(os.environ.get("ORBIS_PORTFORGE_DIR") or "out/portforge")


class Workspace:
    def __init__(self, tid: str, create: bool = False):
        if not tid or not all(c.isalnum() or c == "-" for c in tid) or tid != tid.lower():
            raise ValueError(f"target id {tid!r}: lowercase letters, digits and '-' only")
        self.id = tid
        self.dir = ws_root() / tid
        if create:
            (self.dir / "candidates").mkdir(parents=True, exist_ok=True)
        elif not self.dir.is_dir():
            raise FileNotFoundError(f"no PortForge workspace {self.dir}: run `portforge new {tid}` first")

    def write(self, name: str, obj) -> Path:
        f = self.dir / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(obj if isinstance(obj, str) else json.dumps(obj, indent=1, default=sorted) + "\n")
        return f

    def read(self, name: str):
        return json.loads((self.dir / name).read_text())

    def log(self, rec: dict) -> None:
        with open(self.dir / "provenance.jsonl", "a") as f:
            f.write(json.dumps({"time": _now(), **rec}) + "\n")

    def candidates(self) -> list[dict]:
        out = []
        for f in sorted((self.dir / "candidates").glob("*.json")):
            out.append(json.loads(f.read_text()))
        return out


# ------------------------------------------------------------------ new: inventory, match, comparison, gaps
def _nearest(reg: Registry, f: dict, m: dict) -> str | None:
    """The registered target that shares the most strong identifiers (deterministic tie-break by id)."""
    if m.get("result") in ("confirmed", "probable"):
        return m["target"]
    best, score = None, -1
    for t in reg.targets():
        e = reg.resolve(t)
        s = 0
        if f.get("gpu_pci_device") and (e["apu_info"].get("gpu") or {}).get("pci_device") == f["gpu_pci_device"]:
            s += 2
        sbpci = {v for k, v in (e["southbridge_info"].get("pci") or {}).items() if k not in ("vendor", "note")}
        if f.get("southbridge_pci") and set(f["southbridge_pci"]) & sbpci:
            s += 2
        if any(t in reg.find_by_model(mo) for mo in f.get("models") or []):
            s += 1
        if s > score or (s == score and best and t < best):
            best, score = t, s
    return best if score > 0 else None


def gap_report(reg: Registry, f: dict, base: str | None, m: dict) -> dict:
    eff = reg.resolve(base) if base else None
    gaps, known = [], []
    sb = None
    sbs = matcher._sb_by_pci(reg)
    for dev in f.get("southbridge_pci") or []:
        if dev in sbs:
            sb = sorted(sbs[dev])[0] if len(sbs[dev]) == 1 else sb
    sub = matcher._sb_by_subsys(reg)
    for sid in f.get("subsys_subids") or []:
        sb = sub.get(sid, sb)
    if sb is None and eff:
        sb = eff.get("southbridge")
    sb_doc = reg.raw.get(sb or "", {})
    flavors = sb_doc.get("kernel") or {}
    flavor_ok = [k for k, v in flavors.items() if v.get("state") != "unsupported"]
    if not sb:
        gaps.append({"layer": "southbridge", "gap": "southbridge not identified", "action": "import lspci -nn or a boot log; add targets/southbridge/<id>.json"})
    elif not flavor_ok:
        for k, v in flavors.items():
            gaps.append({"layer": "kernel", "gap": f"no working kernel flavor for {sb} ({k})", "reason": v.get("reason"), "action": v.get("path")})
        for c in sb_doc.get("community") or []:
            known.append({"layer": "kernel", "community": c})
    else:
        known.append({"layer": "kernel", "flavor": flavor_ok[0], "state": flavors[flavor_ok[0]].get("state")})
    gpu = f.get("gpu_pci_device")
    apus = [i for i, d in reg.raw.items() if d.get("kind") == "apu" and (d.get("gpu") or {}).get("pci_device") == gpu]
    if not gpu:
        gaps.append({"layer": "apu", "gap": "GPU PCI ID unknown", "action": "probe on the console (lspci -nn)"})
    elif not apus:
        gaps.append({"layer": "apu", "gap": f"GPU {gpu} not in the registry", "action": "add targets/apu/<id>.json; check amdgpu_drv.c and ps4_bridge.c support it"})
    else:
        known.append({"layer": "apu", "apu": apus[0]})
    unrec = sorted(d for d in f.get("southbridge_pci") or [] if d not in sbs)
    if unrec:
        gaps.append({"layer": "southbridge-io", "gap": f"Sony PCI functions without a known driver ID: {unrec}",
                     "action": "map each function (lspci -nnk) and add IDs to the matching drivers (apcie, ahci, xhci, sky2, sdhci)"})
    caps = {}
    if eff:
        for cap, c in eff["capabilities"].items():
            st = c.get("state")
            if not flavor_ok and cap in KERNEL_CAPS:
                st = "unsupported"
            caps[cap] = st
    return {"schema": "portforge-gap-report/1", "base_target": base, "southbridge": sb, "gpu": gpu, "match": m.get("result"),
            "gaps": gaps, "known": known, "capabilities_if_ported": caps}


def cmd_new(a) -> int:
    reg = Registry()
    ws = Workspace(a.id, create=True)
    if a.id in reg.raw:
        print(f"portforge: {a.id} already exists in the registry ({reg.files[a.id]}); choose a new id", file=sys.stderr)
        return 2
    doc = json.loads(Path(a.probe).read_text()) if a.probe else prober.probe()
    for lf in a.log or []:
        doc.setdefault("logs", []).append(prober.import_log(Path(lf).read_text(errors="replace"), os.path.basename(lf)))
    if a.model:
        doc.setdefault("hints", {})["model"] = a.model.upper()
    f = prober.facts(doc)
    m = matcher.match(reg, f)
    base = a.base_target or _nearest(reg, f, m)
    if base and base not in reg.targets():
        print(f"portforge: unknown base target {base}", file=sys.stderr)
        return 2
    ws.write("inventory.json", doc)
    ws.write("match.json", m)
    cmpd = compat.compatibility(reg, base, doc) if base else {"base": None}
    ws.write("comparison.json", cmpd)
    gr = gap_report(reg, f, base, m)
    ws.write("gap-report.json", gr)
    eff = reg.resolve(base) if base else {}
    draft = {"schema": "orbis-target/1", "id": a.id, "kind": "model", "name": f"{a.id} (PortForge draft)",
             "extends": base or "common", "models": sorted(f.get("models") or []) or ["CUH-????"],
             "southbridge": gr.get("southbridge") or eff.get("southbridge") or "belize",
             "apu": (gr["known"] and next((k["apu"] for k in gr["known"] if k.get("apu")), None)) or eff.get("apu") or "liverpool-12xx",
             "notes": [f"PortForge draft from {a.probe or 'live probe'} ({_now()}); every capability awaits hardware; review before promote"]}
    ws.write("target-draft.json", draft)
    ws.log({"event": "new", "probe_sha256": _sha(json.dumps(doc, sort_keys=True).encode()), "match": m.get("result"), "base": base})
    print(json.dumps({"workspace": str(ws.dir), "match": m.get("result"), "base_target": base, "gaps": len(gr["gaps"])}, indent=1))
    return 0


# ------------------------------------------------------------------ candidates and provenance
def _add_candidate(ws: Workspace, patch: bytes, origin: str, prov: dict, note: str = "") -> dict:
    n = len(ws.candidates()) + 1
    cid = f"c{n:03d}"
    pf = ws.dir / "candidates" / f"{cid}.patch"
    pf.write_bytes(patch)
    rec = {"schema": "portforge-candidate/1", "id": cid, "file": pf.name, "origin": origin, "sha256": _sha(patch),
           "created": _now(), "provenance": prov, "note": note,
           "review": {"state": "unreviewed", "reviewer": None, "time": None, "notes": None},
           "may_run_on_ps4": False}
    (ws.dir / "candidates" / f"{cid}.json").write_text(json.dumps(rec, indent=1) + "\n")
    ws.log({"event": "candidate", "id": cid, "origin": origin, "sha256": rec["sha256"], "provenance": prov})
    return rec


def cmd_candidate(a) -> int:
    ws = Workspace(a.id)
    data = Path(a.patch).read_bytes()
    rec = _add_candidate(ws, data, a.origin, {"source": a.source, "input_file": os.path.basename(a.patch), "input_sha256": _sha(data)}, a.note or "")
    print(f"candidate {rec['id']} ({a.origin}, unreviewed)")
    return 0


def cmd_diff_trees(a) -> int:
    """Candidate = difference of the given paths between two trees (files only in --other become new files)."""
    import tempfile
    ws = Workspace(a.id)
    paths = [x.strip("/") for x in a.paths.split(",") if x.strip("/")]
    with tempfile.TemporaryDirectory() as t:
        for side, top in (("a", a.base), ("b", a.other)):
            for pth in paths:
                src = Path(top) / pth
                dst = Path(t) / side / pth
                if src.is_dir():
                    shutil.copytree(src, dst, symlinks=True)
                elif src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            (Path(t) / side).mkdir(exist_ok=True)
        r = subprocess.run(["git", "diff", "--no-index", "--binary", "--no-prefix", "--src-prefix=a/", "--dst-prefix=b/", "--", "a", "b"],
                           cwd=t, capture_output=True)
    if r.returncode not in (0, 1) or r.stderr.strip():
        print(r.stderr.decode(errors="replace"), file=sys.stderr)
        return 1
    # paths in the diff are a/a/<p> and b/b/<p>: strip the side directory
    out = (r.stdout.replace(b"diff --git a/b/", b"diff --git a/").replace(b" a/a/", b" a/").replace(b" b/b/", b" b/")
           .replace(b"--- a/a/", b"--- a/").replace(b"+++ b/b/", b"+++ b/"))
    if not out:
        print("no differences in the given paths")
        return 0
    missing = [pth for pth in paths if not (Path(a.base) / pth).exists() and not (Path(a.other) / pth).exists()]
    rec = _add_candidate(ws, out, "community", {"source": a.source, "base_tree": a.base, "other_tree": a.other, "paths": paths,
                                                "paths_in_neither_tree": missing})
    print(f"candidate {rec['id']} from tree diff ({len(out)} bytes, unreviewed)")
    return 0


def cmd_assist(a) -> int:
    """Ask a local assistant command for a candidate diff. Off unless a command is given."""
    ws = Workspace(a.id)
    if not a.assistant.startswith("cmd:"):
        print("portforge: --assistant must be cmd:<command> (a local program reading the prompt on stdin, writing a unified diff)", file=sys.stderr)
        return 2
    prompt = json.dumps({"task": a.ask or "Propose a minimal kernel patch for the first gap.",
                         "gap_report": ws.read("gap-report.json"), "match": ws.read("match.json")}, indent=1)
    cmd = a.assistant[4:]
    try:
        r = subprocess.run(shlex.split(cmd), input=prompt.encode(), capture_output=True, timeout=a.timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"portforge: assistant failed: {e}", file=sys.stderr)
        return 1
    if r.returncode != 0 or not r.stdout.strip():
        print(f"portforge: assistant returned {r.returncode} and {len(r.stdout)} bytes; nothing recorded", file=sys.stderr)
        return 1
    rec = _add_candidate(ws, r.stdout, "assistant", {"assistant_command": cmd, "prompt_sha256": _sha(prompt.encode()),
                                                     "output_sha256": _sha(r.stdout), "exit_code": r.returncode},
                         note="AI-generated: must be reviewed line by line")
    print(f"candidate {rec['id']} (assistant output, unreviewed)")
    return 0


def cmd_review(a) -> int:
    ws = Workspace(a.id)
    f = ws.dir / "candidates" / f"{a.candidate}.json"
    rec = json.loads(f.read_text())
    patch = (ws.dir / "candidates" / rec["file"]).read_bytes()
    if _sha(patch) != rec["sha256"]:
        print(f"portforge: {a.candidate} changed after it was recorded; re-add it", file=sys.stderr)
        return 1
    if not a.reviewer:
        print("portforge: --reviewer NAME is required", file=sys.stderr)
        return 2
    rec["review"] = {"state": "approved" if a.approve else "rejected", "reviewer": a.reviewer, "time": _now(), "notes": a.notes,
                     "reviewed_sha256": rec["sha256"]}
    f.write_text(json.dumps(rec, indent=1) + "\n")
    ws.log({"event": "review", "id": a.candidate, "state": rec["review"]["state"], "reviewer": a.reviewer})
    print(f"{a.candidate}: {rec['review']['state']} by {a.reviewer}")
    return 0


# ------------------------------------------------------------------ checks
def _apply_check(tree: Path, patch: Path) -> dict:
    r = subprocess.run(["git", "-C", str(tree), "apply", "--check", "--verbose", str(patch.resolve())], capture_output=True, text=True)
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr)[-3000:]}


def _static(patch: Path, tree: Path) -> dict:
    out = {}
    cp = tree / "scripts/checkpatch.pl"
    if cp.exists() and shutil.which("perl"):
        r = subprocess.run(["perl", str(cp), "--no-tree", "--no-signoff", "-q", str(patch.resolve())], capture_output=True, text=True)
        errors = [l for l in r.stdout.splitlines() if l.startswith("ERROR")]
        out["checkpatch"] = {"errors": len(errors), "warnings": r.stdout.count("WARNING"), "first": errors[:5]}
    else:
        out["checkpatch"] = {"skipped": "scripts/checkpatch.pl or perl not available"}
    text = patch.read_text(errors="replace")
    out["touches"] = sorted({l[6:] for l in text.splitlines() if l.startswith("+++ b/")})
    out["adds_scripts"] = [p for p in out["touches"] if p.endswith((".sh", ".py")) or "/bin/" in p]
    out["mmio_or_registers"] = any(k in text for k in ("ioremap", "readl(", "writel(", "RREG32", "WREG32", "outb(", "inb("))
    return out


def cmd_check(a) -> int:
    ws = Workspace(a.id)
    tree = Path(a.kernel_tree)
    res = {"schema": "portforge-checks/1", "time": _now(), "kernel_tree": str(tree), "candidates": {}}
    for c in ws.candidates():
        pf = ws.dir / "candidates" / c["file"]
        res["candidates"][c["id"]] = {"apply": _apply_check(tree, pf) if (tree / ".git").exists() or (tree / "Makefile").exists() else
                                      {"ok": None, "output": "not a kernel tree"},
                                      "static": _static(pf, tree), "review": c["review"]["state"]}
    res["compile"] = {"run": False, "why": "compile checks run on the Nobara build machine through the recipe (orbis-ge kernel --target)",
                      "command": f"./orbis-ge kernel --orbis-src <0.2.0 checkout> --target {a.id}"}
    ws.write("checks.json", res)
    bad = [k for k, v in res["candidates"].items() if v["apply"]["ok"] is False]
    print(json.dumps({"apply_failed": bad, "candidates": len(res["candidates"])}, indent=1))
    return 1 if bad else 0


# ------------------------------------------------------------------ report, recipe, promote
def cmd_report(a) -> int:
    ws = Workspace(a.id)
    m, gr = ws.read("match.json"), ws.read("gap-report.json")
    cands = ws.candidates()
    checks = ws.read("checks.json") if (ws.dir / "checks.json").exists() else None
    unrev = [c["id"] for c in cands if c["review"]["state"] != "approved"]
    lines = [f"# PortForge review: {a.id}", "", f"Match: **{m.get('result')}** {m.get('target') or ''}  base target: {gr.get('base_target')}",
             f"Southbridge: {gr.get('southbridge')}  GPU: {gr.get('gpu')}", "", "## Gaps"]
    lines += [f"- [{g['layer']}] {g['gap']}" + (f" -> {g.get('action')}" if g.get("action") else "") for g in gr["gaps"]] or ["- none found"]
    lines += ["", "## Candidates"]
    for c in cands:
        ck = (checks or {}).get("candidates", {}).get(c["id"], {})
        lines.append(f"- {c['id']} ({c['origin']}, sha256 {c['sha256'][:12]}): review **{c['review']['state']}**"
                     f"{' by ' + c['review']['reviewer'] if c['review'].get('reviewer') else ''}; apply-check "
                     f"{(ck.get('apply') or {}).get('ok')}; register/MMIO access in diff: {(ck.get('static') or {}).get('mmio_or_registers')}")
    if not cands:
        lines.append("- none")
    lines += ["", "## Capabilities on this target (registry state; nothing is verified until qualified on hardware)"]
    lines += [f"- {k}: {v}" for k, v in sorted(gr.get("capabilities_if_ported", {}).items())]
    lines += ["", "Nothing here has run on a PS4. Approve candidates with `portforge review`, then build on Nobara with recipe.sh."]
    ws.write("review.md", "\n".join(lines) + "\n")
    patches = " ".join(f"candidates/{c['file']}" for c in cands if c["review"]["state"] == "approved")
    recipe = f"""#!/usr/bin/env bash
# PortForge build recipe for {a.id} (Nobara build machine only; never run on a PS4).
set -euo pipefail
cd "$(dirname "$0")"
UNREVIEWED="{' '.join(unrev)}"
if [ -n "$UNREVIEWED" ]; then
  echo "refusing: candidates not approved: $UNREVIEWED (portforge review {a.id} <id> --approve --reviewer NAME)" >&2
  exit 3
fi
for c in candidates/*.json; do      # every approved file must still be the reviewed one
  python3 -c 'import json,hashlib,sys; r=json.load(open(sys.argv[1])); d=open("candidates/"+r["file"],"rb").read(); sys.exit(0 if r["review"]["state"]!="approved" or hashlib.sha256(d).hexdigest()==r["review"].get("reviewed_sha256") else 1)' "$c" \\
    || {{ echo "refusing: $c changed after review" >&2; exit 3; }}
done
GE="${{GE:-../../..}}"          # the nicklabs-orbis-graphics-engine checkout
echo "approved patches: {patches or '(none)'}"
echo "1. copy them into $GE/kernel/patches/port-{a.id}/ and list them in kernel/patches/series (after review in git)"
echo "2. promote the target:  python3 -m orbis_targets.portforge promote {a.id}"
echo "3. build:  (cd $GE && ./orbis-ge kernel --orbis-src <0.2.0 checkout> --target {a.id})"
"""
    f = ws.write("recipe.sh", recipe)
    f.chmod(0o755)
    ws.log({"event": "report", "unreviewed": unrev})
    print(f"{ws.dir}/review.md, {ws.dir}/recipe.sh ({'blocked: ' + ' '.join(unrev) if unrev else 'all candidates approved'})")
    return 0


def cmd_promote(a) -> int:
    ws = Workspace(a.id)
    reg = Registry()
    unrev = [c["id"] for c in ws.candidates() if c["review"]["state"] != "approved"]
    if unrev:
        print(f"portforge: refusing to promote {a.id}: not approved: {' '.join(unrev)}", file=sys.stderr)
        return 3
    draft = ws.read("target-draft.json")
    errs = validate(draft, reg.schema, "target-draft.json")
    if errs:
        print("portforge: draft does not validate:\n  " + "\n  ".join(errs), file=sys.stderr)
        return 1
    dst = reg.root / "models" / f"{a.id}.json"
    if dst.exists():
        print(f"portforge: {dst} exists", file=sys.stderr)
        return 1
    dst.write_text(json.dumps(draft, indent=1) + "\n")
    try:
        Registry()
    except Exception as e:
        dst.unlink()
        print(f"portforge: registry rejects the draft: {e}", file=sys.stderr)
        return 1
    ws.log({"event": "promote", "file": str(dst)})
    print(f"promoted to {dst}; regenerate: ./orbis-ge target generate")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="portforge", description="PortForge: reviewable porting workflow for PS4 targets")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("id")
    n.add_argument("--probe")
    n.add_argument("--log", action="append")
    n.add_argument("--model")
    n.add_argument("--base-target")
    c = sub.add_parser("candidate")
    c.add_argument("id")
    c.add_argument("--patch", required=True)
    c.add_argument("--origin", choices=("community", "manual"), required=True)
    c.add_argument("--source", required=True)
    c.add_argument("--note")
    d = sub.add_parser("diff-trees")
    d.add_argument("id")
    d.add_argument("--base", required=True)
    d.add_argument("--other", required=True)
    d.add_argument("--paths", required=True)
    d.add_argument("--source", required=True)
    s = sub.add_parser("assist")
    s.add_argument("id")
    s.add_argument("--assistant", required=True)
    s.add_argument("--ask")
    s.add_argument("--timeout", type=int, default=600)
    k = sub.add_parser("check")
    k.add_argument("id")
    k.add_argument("--kernel-tree", required=True)
    r = sub.add_parser("review")
    r.add_argument("id")
    r.add_argument("candidate")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", action="store_true")
    r.add_argument("--reviewer")
    r.add_argument("--notes")
    sub.add_parser("report").add_argument("id")
    sub.add_parser("promote").add_argument("id")
    a = ap.parse_args(argv)
    fn = {"new": cmd_new, "candidate": cmd_candidate, "diff-trees": cmd_diff_trees, "assist": cmd_assist,
          "check": cmd_check, "review": cmd_review, "report": cmd_report, "promote": cmd_promote}[a.cmd]
    try:
        return fn(a)
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"portforge: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
