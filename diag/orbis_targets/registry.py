"""Target registry: declarative board-support files under targets/ (Porting Kit, Part B).

Layers, most general first:
    targets/common/base.json         shared software and the default capability list
    targets/southbridge/<id>.json    Aeolia, Belize, Belize 2, Baikal: PCI IDs, subsys ids, kernel support
    targets/apu/<id>.json            Liverpool variants, Gladius: GPU PCI ID, HDMI bridge, memory
    targets/models/<id>.json         console families and models (extends: another model or "common")
    targets/generated/registry.json  the resolved registry (written by `orbis-ge target generate`)

Capability states (three, never more):
    verified           shown on that hardware, with evidence
    awaiting-hardware  implemented here, not yet shown on that hardware
    unsupported        with a reason
"""
from __future__ import annotations

import copy
import fnmatch
import json
import os
import re
from pathlib import Path

STATES = ("verified", "awaiting-hardware", "unsupported")
KERNEL_CAPS = ("boot", "southbridge-io", "icc-power-fan", "rtc", "cpufreq", "display-hdmi", "hdmi-audio", "gpu-3d",
               "gpu-clocks", "ge-telemetry", "ge-unified-memory", "wifi-bt", "video-decode")
KIND_DIRS = {"common": "common", "southbridge": "southbridge", "apu": "apu", "family": "models", "model": "models"}


def default_root() -> Path:
    env = os.environ.get("ORBIS_TARGETS_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "targets", Path("/usr/share/orbis/targets")):
        if (cand / "schema/target.schema.json").exists():
            return cand
    return here.parents[2] / "targets"


class RegistryError(ValueError):
    pass


# ------------------------------------------------------------------ schema (std-only subset)
def _validate(doc, schema, path, errors, defs):
    if "$ref" in schema:
        schema = defs[schema["$ref"].split("/")[-1]]
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}
    if t and not isinstance(doc, types[t]):
        errors.append(f"{path}: expected {t}")
        return
    if "enum" in schema and doc not in schema["enum"]:
        errors.append(f"{path}: {doc!r} not one of {schema['enum']}")
    if "pattern" in schema and isinstance(doc, str) and not re.search(schema["pattern"], doc):
        errors.append(f"{path}: {doc!r} does not match {schema['pattern']}")
    if isinstance(doc, dict):
        for k in schema.get("required", []):
            if k not in doc:
                errors.append(f"{path}: missing {k}")
        props = schema.get("properties", {})
        for k, v in doc.items():
            if k in props:
                _validate(v, props[k], f"{path}.{k}", errors, defs)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unknown key {k}")
    if isinstance(doc, list) and "items" in schema:
        for i, v in enumerate(doc):
            _validate(v, schema["items"], f"{path}[{i}]", errors, defs)


def validate(doc: dict, schema: dict, name: str = "") -> list[str]:
    errors: list[str] = []
    _validate(doc, schema, name or doc.get("id", "?"), errors, schema.get("$defs", {}))
    for cap, c in (doc.get("capabilities") or {}).items():
        _validate(c, schema["$defs"]["capability"], f"{name}.capabilities.{cap}", errors, schema.get("$defs", {}))
        if c.get("state") == "unsupported" and not c.get("reason"):
            errors.append(f"{name}.capabilities.{cap}: unsupported needs a reason")
        if c.get("state") == "verified" and not c.get("evidence"):
            errors.append(f"{name}.capabilities.{cap}: verified needs evidence")
    return errors


# ------------------------------------------------------------------ loading and resolution
def _merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = copy.deepcopy(base)
        for k, v in over.items():
            out[k] = _merge(out.get(k), v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


class Registry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else default_root()
        self.schema = json.loads((self.root / "schema/target.schema.json").read_text())
        self.raw: dict[str, dict] = {}
        self.files: dict[str, str] = {}
        errors = []
        for sub in ("common", "southbridge", "apu", "models"):
            for f in sorted((self.root / sub).glob("*.json")):
                try:
                    d = json.loads(f.read_text())
                except ValueError as e:
                    errors.append(f"{f}: {e}")
                    continue
                rel = str(f.relative_to(self.root))
                errors += validate(d, self.schema, rel)
                if KIND_DIRS.get(d.get("kind")) != sub:
                    errors.append(f"{rel}: kind {d.get('kind')} does not belong in {sub}/")
                if d.get("id") in self.raw:
                    errors.append(f"{rel}: duplicate id {d['id']} (also {self.files[d['id']]})")
                self.raw[d["id"]] = d
                self.files[d["id"]] = rel
        for i, d in self.raw.items():
            for ref, kind in (("southbridge", "southbridge"), ("apu", "apu")):
                if d.get(ref) and self.raw.get(d[ref], {}).get("kind") != kind:
                    errors.append(f"{self.files[i]}: {ref} {d[ref]} is not a known {kind}")
            if d.get("extends") and d["extends"] not in self.raw:
                errors.append(f"{self.files[i]}: extends unknown {d['extends']}")
        if errors:
            raise RegistryError("\n".join(errors))

    # targets are the model-kind entries (families with kind=family are abstract)
    def targets(self) -> list[str]:
        return sorted(i for i, d in self.raw.items() if d.get("kind") == "model" and not d.get("abstract"))

    def reference(self) -> str | None:
        refs = [i for i in self.targets() if self.raw[i].get("reference")]
        return refs[0] if len(refs) == 1 else None

    def _chain(self, tid: str) -> list[dict]:
        chain, seen = [], set()
        cur = tid
        while cur:
            if cur in seen:
                raise RegistryError(f"extends cycle at {cur}")
            seen.add(cur)
            d = self.raw[cur]
            chain.append(d)
            cur = d.get("extends")
        return list(reversed(chain))

    def resolve(self, tid: str) -> dict:
        if tid not in self.raw:
            raise KeyError(f"unknown target {tid}; known: {', '.join(self.targets())}")
        eff: dict = {}
        for d in self._chain(tid):
            layer = {k: v for k, v in d.items() if k not in ("extends", "abstract", "reference", "kind", "id", "name", "schema",
                                                             "orbis020_profiles")}
            eff = _merge(eff, layer)
        own = self.raw[tid]
        eff.update({"id": tid, "name": own["name"], "kind": own["kind"], "reference": bool(own.get("reference")),
                    "file": self.files[tid]})
        sb = copy.deepcopy(self.raw.get(eff.get("southbridge"), {}))
        ap = copy.deepcopy(self.raw.get(eff.get("apu"), {}))
        eff["southbridge_info"] = {k: sb.get(k) for k in ("id", "name", "part_numbers", "pci", "subsys_subid", "kernel", "community") if k in sb}
        eff["apu_info"] = {k: ap.get(k) for k in ("id", "name", "gpu", "hdmi_bridge", "memory", "notes") if k in ap}
        eff["kernel"] = self._kernel(eff, sb)
        eff["capabilities"] = self._caps(eff, ap)
        eff["status"] = self.status_of(eff)
        return eff

    def _kernel(self, eff: dict, sb: dict) -> dict:
        k = copy.deepcopy(eff.get("kernel") or {})
        if "state" in k:          # model override (Baikal boards)
            return k
        flavors = (sb.get("kernel") or {})
        if not flavors:
            return {"flavor": None, "state": "unsupported", "reason": "southbridge without a kernel flavor"}
        name, info = next(iter(flavors.items()))
        out = {"flavor": name if info.get("state") != "unsupported" else None, "state": info.get("state")}
        for f in ("evidence", "reason", "path"):
            if info.get(f):
                out[f] = info[f]
        out["base"] = k.get("base")
        return out

    def _caps(self, eff: dict, ap: dict) -> dict:
        caps = copy.deepcopy(eff.get("capabilities") or {})
        kern = eff["kernel"]
        for c in KERNEL_CAPS:
            if c not in caps:
                continue
            if kern.get("state") == "unsupported" and caps[c].get("state") != "unsupported":
                caps[c] = {"state": "unsupported", "reason": f"kernel: {kern.get('reason')}",
                           "implemented_by": caps[c].get("implemented_by", [])}
        bridge = ap.get("hdmi_bridge")
        if bridge and "display-hdmi" in caps and caps["display-hdmi"]["state"] != "unsupported":
            caps["display-hdmi"]["path"] = f"ps4_bridge {bridge} path (GPU {((ap.get('gpu') or {}).get('pci_device'))})"
        return caps

    @staticmethod
    def status_of(eff: dict) -> str:
        states = [c.get("state") for c in eff["capabilities"].values()]
        if eff["kernel"].get("state") == "unsupported":
            return "unsupported"
        if states and all(s == "verified" for s in states if s != "unsupported"):
            return "verified"
        return "awaiting-hardware"

    def generate(self) -> dict:
        return {"schema": "orbis-target-registry/1", "reference": self.reference(),
                "targets": {t: self.resolve(t) for t in self.targets()},
                "sources_files": dict(sorted(self.files.items()))}

    def find_by_model(self, model: str) -> list[str]:
        m = model.strip().upper()
        return [t for t in self.targets() if any(fnmatch.fnmatchcase(m, pat) for pat in (self.resolve(t).get("models") or []))]


def generated_path(root: Path | None = None) -> Path:
    return (Path(root) if root else default_root()) / "generated/registry.json"


def write_generated(reg: Registry) -> Path:
    out = generated_path(reg.root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(reg.generate(), indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    return out
