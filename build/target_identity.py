#!/usr/bin/env python3
"""Is this Orbis 0.2.0 base image built for the target we are building? (r3.1, audit P2-6)

    target_identity.py check <target> <inspect.json> <base-image> [--stage0] [--override PROFILE]
        exit 0 accepted (match | compatible | override), 8 rejected; the decision JSON goes to stdout

Evidence, all read from the image and the 0.2.0 release metadata, never from file names:
  * orbis-boot.json inside ORBIS-BOOT (`orbis-graphics inspect --image`): the initramfs profile
    and its firmware groups, written by 0.2.0 make_usb_image.py;
  * when present next to the base image: <image>.json (make_usb_image output) and
    release-manifest.json (0.2.0 release.sh), whose profile must agree with the image.
The registry (targets/common/base.json -> orbis020_profiles) says which targets each 0.2.0
profile serves and which firmware groups it must carry.

Decisions:
  match       the image's profile is the target's own profile
  compatible  a generic profile that covers the target's southbridge
  mismatch    the profile belongs to other targets, or misses required firmware   -> rejected
  missing     no profile in the image                                             -> rejected
  contradictory  the sidecar metadata names another profile, or a stage-0 image is
                 given where a full image is expected (or the reverse)            -> rejected
  unknown     a profile the registry does not know                                -> rejected
  override    --override PROFILE given by the operator: accepted only when PROFILE is the
              profile actually found, and recorded as an operator decision
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "diag"))
from orbis_targets.registry import Registry  # noqa: E402


def _json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def decide(reg: Registry, target: str, inspect: dict, base: str, want_stage0: bool, override: str | None) -> dict:
    eff = reg.resolve(target)
    profiles = (reg.raw.get("common") or {}).get("orbis020_profiles") or {}
    boot = inspect.get("orbis_boot") or {}
    ird = boot.get("initramfs") or {}
    profile = inspect.get("profile") or ird.get("profile")
    groups = sorted(ird.get("firmware_groups") or [])
    ev = {"image_profile": profile, "firmware_groups": groups, "stage0": inspect.get("stage0"),
          "kernel_sha256": inspect.get("kernel_sha256"), "sources": ["orbis-boot.json (ORBIS-BOOT)"]}
    out = {"schema": "orbis-ge-base-target-check/1", "target": target, "base_image": base, "evidence": ev}

    def fin(decision: str, why: str, accepted: bool):
        out.update(decision=decision, why=why, accepted=accepted)
        return out

    contradictions = []
    side = _json(Path(base + ".json"))
    if side:
        ev["sources"].append(os.path.basename(base) + ".json")
        sp = ((side.get("boot") or {}).get("initramfs") or {}).get("profile")
        if sp and profile and sp != profile:
            contradictions.append(f"{os.path.basename(base)}.json names profile {sp!r}, the image {profile!r}")
    rel = _json(Path(base).parent / "release-manifest.json")
    if rel and rel.get("profile"):
        ev["sources"].append("release-manifest.json")
        # only a statement about this image when the manifest's own image name is this file
        if Path(base).name == f"orbis-{rel.get('version')}-{rel['profile']}.img" or Path(base).name.endswith(f"-{rel['profile']}.img"):
            if profile and rel["profile"] != profile:
                contradictions.append(f"release-manifest.json names profile {rel['profile']!r}, the image {profile!r}")
    if inspect.get("stage0") is not None and bool(inspect.get("stage0")) != want_stage0:
        contradictions.append("a stage-0 image was given as the full base image" if inspect.get("stage0")
                              else "a full image was given as the stage-0 base image")
    if contradictions:
        return fin("contradictory", "; ".join(contradictions), False)
    if not profile:
        return fin("missing", "the image carries no initramfs profile (orbis-boot.json); its target cannot be established", False)
    if override:
        if override != profile:
            return fin("mismatch", f"--base-profile-override {override!r} does not match the image's profile {profile!r}", False)
        out["operator_override"] = True
        return fin("override", f"operator accepted profile {profile!r} for {target} without registry support", True)
    p = profiles.get(profile)
    if p is None:
        return fin("unknown", f"profile {profile!r} is not in the registry (targets/common/base.json orbis020_profiles)", False)
    missing = [g for g in p.get("required_firmware", []) if g not in groups]
    if missing:
        return fin("mismatch", f"profile {profile!r} requires firmware groups {missing} that the image does not carry", False)
    if target in (p.get("targets") or []):
        return fin("match", f"the image was built with {target}'s own profile {profile!r}", True)
    if eff.get("southbridge") in (p.get("southbridges") or []):
        return fin("compatible", f"generic profile {profile!r} covers southbridge {eff.get('southbridge')}", True)
    owners = p.get("targets") or [f"southbridges {p.get('southbridges')}"]
    return fin("mismatch", f"profile {profile!r} is for {owners}, not {target}", False)


def main(argv: list[str]) -> int:
    args = argv[1:]
    if len(args) < 4 or args[0] != "check":
        print(__doc__, file=sys.stderr)
        return 2
    target, insp, base = args[1], args[2], args[3]
    want_stage0 = "--stage0" in args
    override = args[args.index("--override") + 1] if "--override" in args else None
    inspect = _json(Path(insp))
    if not isinstance(inspect, dict):
        d = {"schema": "orbis-ge-base-target-check/1", "target": target, "base_image": base, "decision": "missing",
             "accepted": False, "why": f"the base image could not be inspected ({insp} unreadable)"}
    else:
        d = decide(Registry(), target, inspect, base, want_stage0, override)
    print(json.dumps(d, indent=1))
    if not d["accepted"]:
        print(f"!!! base image rejected for {target}: {d['decision']}: {d['why']}", file=sys.stderr)
    return 0 if d["accepted"] else 8


if __name__ == "__main__":
    sys.exit(main(sys.argv))
