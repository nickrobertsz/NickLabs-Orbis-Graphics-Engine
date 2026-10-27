#!/usr/bin/env python3
"""Strict Jaguar ISA gate for the NickLabs Orbis native (Rust) binaries.

Uses Orbis 0.2.0's own audit (platform/orbis/isa_audit.py, imported read-only, never modified)
with one correction: 0.2.0 treats every `vp*` mnemonic on %ymm registers as AVX2 integer SIMD,
which also catches the AVX1 *float* permutes vperm2f128 / vpermilps / vpermilpd. Jaguar has AVX1,
so those are safe; Rust's core::slice::sort emits vperm2f128 under -C target-cpu=btver2.

Our binaries are compiled for btver2, so nothing v3-only may appear at all: unlike distribution
packages there is no runtime-dispatch excuse. Exit 0 only when every file is "safe".

  isa-check.py --platform <orbis-0.2.0>/platform FILE...
"""
import argparse
import sys

AVX1_VP = {"vperm2f128", "vpermilps", "vpermilpd"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True, help="Orbis 0.2.0 platform/ directory")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    sys.path.insert(0, a.platform)
    try:
        from orbis import isa_audit  # noqa: E402
    except ImportError as e:
        print(f"isa-check: cannot import Orbis 0.2.0 isa_audit from {a.platform}: {e}", file=sys.stderr)
        return 3
    orig = isa_audit.classify_insn

    def classify(mnem, operands):
        if mnem.split(".")[0] in AVX1_VP:
            return None
        return orig(mnem, operands)

    isa_audit.classify_insn = classify
    bad = 0
    for f in a.files:
        r = isa_audit.audit_file(f, deep=True)
        d = r.get("disasm") or {}
        ok = r.get("verdict") == "safe" and not d.get("instructions") and not d.get("error")
        print(f"{'PASS' if ok else 'FAIL'}  {r.get('verdict'):20s} {f}  v3+ instructions={d.get('instructions', {})}"
              f" note={r.get('elf_note') or '-'}{' error=' + d['error'] if d.get('error') else ''}")
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
