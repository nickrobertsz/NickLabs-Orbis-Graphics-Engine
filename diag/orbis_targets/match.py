"""Deterministic target matching (Porting Kit): probe facts -> confirmed | probable | conflicting | unknown.

Evidence classes
  strong: GPU PCI device ID (identifies the APU family), southbridge PCI device IDs, hw.sce_subsys_subid
          (identifies the southbridge stepping, including Belize 2), motherboard name from a log
  weak:   a CUH model number (from the user or a log), a southbridge name written in a log

Rules (applied in this order, no scoring heuristics):
  1. A target is a candidate when no fact contradicts it.
  2. When two facts each fit some target but no target fits both -> conflicting (both facts named).
  3. No candidate -> unknown (hardware not in the registry).
  4. Exactly one candidate (a family and its own model both fitting count once: the model when a
     model number was given, the family otherwise):
       confirmed when the GPU *and* the southbridge are identified by strong evidence,
       probable otherwise (the missing evidence is listed).
  5. Several candidates -> unknown, with the candidates and the evidence that would separate them.
The reference console (CUH-1215A) gets no special treatment: it is never a default.
"""
from __future__ import annotations

from .registry import Registry



def _sb_by_pci(reg: Registry) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for i, d in reg.raw.items():
        if d.get("kind") != "southbridge":
            continue
        for k, v in (d.get("pci") or {}).items():
            if k in ("vendor", "note") or not isinstance(v, str):
                continue
            out.setdefault(v.lower(), set()).add(i)
    # undocumented Belize 2 IDs: a Belize PCI ID is consistent with Belize 2 as well
    for v, sbs in out.items():
        if "belize" in sbs:
            sbs.add("belize2")
    return out


def _sb_by_subsys(reg: Registry) -> dict[str, str]:
    out = {}
    for i, d in reg.raw.items():
        if d.get("kind") == "southbridge":
            for k in (d.get("subsys_subid") or {}):
                out[k.lower()] = i
    return out


def match(reg: Registry, f: dict) -> dict:
    sb_pci, sb_sub = _sb_by_pci(reg), _sb_by_subsys(reg)
    targets = {t: reg.resolve(t) for t in reg.targets()}
    checks = []   # (fact name, strong?, predicate(target)->bool or None when no info)

    gpu = f.get("gpu_pci_device")
    if gpu:
        checks.append((f"GPU PCI {gpu}", True, lambda e, g=gpu: (e["apu_info"].get("gpu") or {}).get("pci_device") == g))
    sbs = set()
    for dev in sorted(f.get("southbridge_pci") or []):
        if dev in sb_pci:
            sbs |= sb_pci[dev]
    unknown_sb_pci = sorted(d for d in (f.get("southbridge_pci") or []) if d not in sb_pci)
    if f.get("southbridge_pci") and sbs:
        checks.append((f"southbridge PCI {','.join(sorted(f['southbridge_pci']))}", True,
                       lambda e, s=frozenset(sbs): e.get("southbridge") in s))
    for sid in sorted(f.get("subsys_subids") or []):
        sb = sb_sub.get(sid)
        if sb:
            checks.append((f"subsys_subid {sid} ({sb})", True, lambda e, s=sb: e.get("southbridge") == s))
        else:
            unknown_sb_pci.append(f"subsys {sid}")
    for mb in sorted(f.get("motherboards") or []):
        checks.append((f"motherboard {mb}", True, lambda e, m=mb: any(x.split(" ")[0] == m for x in e.get("motherboards") or [])))
    for model in sorted(f.get("models") or []):
        ids = set(reg.find_by_model(model))
        checks.append((f"model {model}", False, lambda e, s=frozenset(ids): e["id"] in s))
    for w in sorted(f.get("southbridge_words") or []):
        checks.append((f"log names southbridge '{w}'", False, lambda e, w=w: e.get("southbridge") == w))

    cands = [t for t, e in targets.items() if all(pred(e) for _, _, pred in checks)]
    # a family and one of its own models both fit: only a model number can pick the model; hardware
    # alone identifies the family (so the reference CUH-1215A is never chosen by default)
    if any(n.startswith("model ") for n, _, _ in checks):
        cands = [t for t in cands if not any(reg.raw[c].get("extends") == t for c in cands)]
    else:
        cands = [t for t in cands if reg.raw[t].get("extends") not in cands]
    res = {"schema": "orbis-target-match/1", "facts_used": [n for n, _, _ in checks],
           "unrecognized": unknown_sb_pci, "candidates": cands}

    if not checks:
        return {**res, "result": "unknown", "why": "no identifying evidence (no GPU/southbridge PCI IDs, no logs, no model)",
                "needed_evidence": _needed(f)}
    if not cands:
        # which facts disagree? find a pair where each fits something but not together
        fits = {n: {t for t, e in targets.items() if pred(e)} for n, _, pred in checks}
        pairs = [(a, b) for i, a in enumerate(fits) for b in list(fits)[i + 1:]
                 if fits[a] and fits[b] and not (fits[a] & fits[b])]
        if pairs:
            return {**res, "result": "conflicting", "conflicts": [{"a": a, "b": b, "a_fits": sorted(fits[a]), "b_fits": sorted(fits[b])} for a, b in pairs],
                    "why": "the evidence describes different consoles; check the model label, the logs and lspci"}
        return {**res, "result": "unknown", "why": "no registered target matches this hardware" +
                (f" (unrecognized: {', '.join(unknown_sb_pci)})" if unknown_sb_pci else ""),
                "next": "orbis-ge port new <id> --probe <file> (PortForge) to describe the new target"}
    if len(cands) == 1:
        e = targets[cands[0]]
        strong_gpu = bool(gpu)
        strong_sb = bool(sbs) or any(n.startswith(("subsys_subid", "motherboard")) for n, s, _ in checks if s)
        if strong_gpu and strong_sb:
            return {**res, "result": "confirmed", "target": cands[0], "status": e["status"]}
        missing = ([] if strong_gpu else ["GPU PCI ID (lspci -nn, or the probe on the console)"]) + \
                  ([] if strong_sb else ["southbridge PCI IDs or hw.sce_subsys_subid"])
        return {**res, "result": "probable", "target": cands[0], "status": e["status"], "missing_evidence": missing}
    return {**res, "result": "unknown", "why": f"{len(cands)} targets fit the evidence",
            "needed_evidence": _separating(targets, cands)}


def _needed(f: dict) -> list[str]:
    return ["run `orbis-hw probe --json` on the console (GPU and southbridge PCI IDs)",
            "or import a boot/serial log with --log FILE", "or give the CUH model with --model (weak evidence)"]


def _separating(targets: dict, cands: list[str]) -> list[str]:
    out = []
    sbs = sorted({targets[c].get("southbridge") for c in cands})
    if len(sbs) > 1:
        out.append(f"southbridge ({' / '.join(sbs)}): lspci -nn on the console, or hw.sce_subsys_subid / the motherboard label")
    gpus = sorted({(targets[c]["apu_info"].get("gpu") or {}).get("pci_device") for c in cands})
    if len(gpus) > 1:
        out.append(f"GPU PCI ID ({' / '.join(str(g) for g in gpus)})")
    if not out:
        out.append("the exact CUH model number")
    return out
