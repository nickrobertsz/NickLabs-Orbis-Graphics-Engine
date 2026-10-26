"""orbis-ai-gpu: find a Vulkan configuration of llama.cpp that is numerically correct on Liverpool,
including long prompts, and apply it to Orbis' AI mode.

The failure class to beat (Carlos Georges, PS4 report, 2026-09-25): short prompts give the exact
expected output on Vulkan, a longer prompt faults the GPU during prefill and recovery fails.
His mitigations were narrower matrix dispatches and a simpler attention path. Each variant below
is one concrete, reversible hypothesis about that fault, expressed with knobs llama.cpp b11845
really has (verified in ggml-vulkan.cpp at 9ff0d7e):

  ub128 / ub64   smaller prefill micro-batches -> narrower matmul dispatches
  nodes32        GGML_VK_MAX_NODES_PER_SUBMIT=32: shorter command buffers per submission
  serialize      GGML_VK_SERIALIZE_SUBMISSIONS=1: one submission in flight at a time
  hostmem        GGML_VK_PREFER_HOST_MEMORY=1: buffers in GTT (system pages) instead of the carve-out
  maxalloc       GGML_VK_FORCE_MAX_ALLOCATION_SIZE / _MAX_BUFFER_SIZE = 512 MiB: smaller buffers
  nof16          GGML_VK_DISABLE_F16=1
  nofusion       GGML_VK_DISABLE_FUSION=1: no fused kernels (simpler shader paths)
  nommvq         GGML_VK_DISABLE_MMVQ=1 + GGML_VK_DISABLE_INTEGER_DOT_PRODUCT=1

Every variant runs through the Orbis 0.2.0 inference harness (orbis-infer-harness): CPU build as
the reference, KL divergence on the same tokens, context sweep with bisection, intermediate
tensor comparison (llama-eval-callback) to name the first diverging tensor, fsync'd journal so a
hang + power cycle resumes, kernel log capture per step. `recommend` picks the variant with the
longest numerically correct prefill on a hardware GPU; `apply` writes it into the AI service and
the GPU gate receipt that Orbis' auto backend reads. CPU stays the fallback for everything that
does not pass.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
from pathlib import Path

from .util import atomic_write, now_iso, p, read

HARNESS = "/usr/bin/orbis-infer-harness"
CPU_BIN_DIR = "/usr/bin"
VK_BIN_DIR = "/usr/lib/orbis/llama/vulkan"
MiB = 1 << 20

VARIANTS = {
    "baseline": {"ubatch": [512], "env": {}},
    "ub128": {"ubatch": [128], "env": {}},
    "ub64": {"ubatch": [64], "env": {}},
    "nodes32": {"ubatch": [512], "env": {"GGML_VK_MAX_NODES_PER_SUBMIT": "32"}},
    "serialize": {"ubatch": [512], "env": {"GGML_VK_SERIALIZE_SUBMISSIONS": "1"}},
    "hostmem": {"ubatch": [512], "env": {"GGML_VK_PREFER_HOST_MEMORY": "1"}},
    "maxalloc": {"ubatch": [512], "env": {"GGML_VK_FORCE_MAX_ALLOCATION_SIZE": str(512 * MiB),
                                          "GGML_VK_FORCE_MAX_BUFFER_SIZE": str(512 * MiB)}},
    "nof16": {"ubatch": [512], "env": {"GGML_VK_DISABLE_F16": "1"}},
    "nofusion": {"ubatch": [512], "env": {"GGML_VK_DISABLE_FUSION": "1"}},
    "nommvq": {"ubatch": [512], "env": {"GGML_VK_DISABLE_MMVQ": "1", "GGML_VK_DISABLE_INTEGER_DOT_PRODUCT": "1"}},
    "ub64-hostmem-nodes32": {"ubatch": [64], "env": {"GGML_VK_PREFER_HOST_MEMORY": "1",
                                                     "GGML_VK_MAX_NODES_PER_SUBMIT": "32"}},
}
DEFAULT_ORDER = ["baseline", "ub128", "ub64", "nodes32", "hostmem", "maxalloc", "serialize", "nofusion",
                 "nommvq", "nof16", "ub64-hostmem-nodes32"]
CTX = [128, 256, 512, 1024, 2048, 4096]
DROPIN = "etc/systemd/system/orbis-llama.service.d/90-orbis-graphics-engine.conf"
TUNING = "var/lib/orbis-graphics/ai-vulkan-tuning.json"

_WORDS = ("the of and to in is was for on that with as by at from his her an it be which or are this had not "
          "but were all they one their have has been more when who will no if out so said what up its about "
          "into than them can only other new some could time these two may then do first any my now such like "
          "our over man me even most made after also did many before must through back years where much your way "
          "well down should because each just those people how too little state good very make world still own "
          "see men work long get here between both life being under never day same another know while last might "
          "us great old year off come since against go came right used take three").split()


def corpus(path: Path, words: int = 30000, seed: int = 1215) -> Path:
    """Deterministic text for KL comparisons (CPU vs GPU on the same tokens; quality is irrelevant)."""
    rnd = random.Random(seed)
    out, sent = [], []
    for i in range(words):
        sent.append(rnd.choice(_WORDS))
        if len(sent) >= rnd.randint(8, 18):
            out.append(" ".join(sent).capitalize() + ".")
            sent = []
    atomic_write(path, " ".join(out) + "\n")
    return path


def harness_config(variant: str, model: str, corpus_path: Path, remote_log: str | None = None,
                   ctx: list[int] | None = None) -> dict:
    v = VARIANTS[variant]
    return {
        "pins": {"model": model, "llama_cpu_bin_dir": CPU_BIN_DIR, "llama_vk_bin_dir": VK_BIN_DIR,
                 "llama_build": read(p("usr/share/orbis/llama-build"))},
        "corpus": str(corpus_path),
        "axes": {"ctx": ctx or CTX, "ubatch": v["ubatch"], "ngl": ["all"], "chunks": 2},
        "candidate": {"env": v["env"]}, "reference": {"env": {}},
        "thresholds": {"max_mean_kld": 0.01, "min_same_top_p": 99.0, "max_kld_p99": 0.1},
        "timeouts": {"step_s": 900}, "bisect": True, "bisect_hangs": False,
        "tensors": {"enabled": True, "prompt_tokens": [32, 512]},
        # the gate (and so the AI service) requires the mixed-length server check for every variant
        "server": {"enabled": True, "ctx": 2048},
        "remote_log": remote_log,
        "orbis_variant": variant,
    }


def campaign_dir(base: Path, variant: str) -> Path:
    return base / variant


def run_campaign(model: str, out: Path, variants: list[str] | None = None, remote_log: str | None = None,
                 dry_run: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    cp = corpus(out / "corpus.txt") if not (out / "corpus.txt").exists() else out / "corpus.txt"
    plan = []
    for v in variants or DEFAULT_ORDER:
        if v not in VARIANTS:
            raise ValueError(f"unknown variant {v}")
        d = campaign_dir(out, v)
        d.mkdir(exist_ok=True)
        cfg = harness_config(v, model, cp, remote_log)
        atomic_write(d / "config.json", json.dumps(cfg, indent=1) + "\n")
        argv = [HARNESS, "run", "--config", str(d / "config.json"), "--out", str(d)]
        plan.append({"variant": v, "argv": argv})
    state = {"schema": "orbis-ai-campaign/1", "time": now_iso(), "model": model, "variants": [x["variant"] for x in plan]}
    atomic_write(out / "campaign.json", json.dumps(state, indent=1) + "\n")
    if dry_run:
        return {"dry_run": True, "plan": plan}
    results = []
    for step in plan:
        d = campaign_dir(out, step["variant"])
        if (d / "DONE").exists():
            results.append({"variant": step["variant"], "skipped": "already finished"})
            continue
        (d / "STARTED").write_text(now_iso() + "\n")
        r = subprocess.run(step["argv"])
        (d / "DONE").write_text(f"{now_iso()} rc={r.returncode}\n")
        results.append({"variant": step["variant"], "rc": r.returncode})
    return {"campaign": str(out), "results": results}


def _report(d: Path) -> dict | None:
    if not (d / "journal.jsonl").exists() or not shutil.which(HARNESS) and not os.path.exists(HARNESS):
        return None
    r = subprocess.run([HARNESS, "report", "--out", str(d)], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def score(rep: dict) -> dict:
    """Longest numerically correct prefill for one variant, from the harness report."""
    table = rep.get("kld_table") or []
    passes = [r for r in table if r.get("outcome") == "PASS"]
    fails = [r for r in table if r.get("outcome") != "PASS"]
    max_ctx = max([r.get("ctx", 0) for r in passes] or [0])
    tens = rep.get("tensors") or []
    first_div = next((t.get("first_divergence") for t in tens if t.get("first_divergence")), None)
    t_at_max = next((r.get("elapsed_s") for r in passes if r.get("ctx") == max_ctx), None)
    return {"max_pass_ctx": max_ctx, "failures": [{k: r.get(k) for k in ("ctx", "ubatch", "outcome")} for r in fails][:6],
            "tensors_ok": bool(tens) and all(t.get("outcome") == "PASS" for t in tens),
            "first_tensor_divergence": first_div, "elapsed_at_max_s": t_at_max,
            "fault_outcomes": sorted({r.get("outcome") for r in fails})}


def recommend(out: Path, reports: dict | None = None) -> dict:
    reports = reports if reports is not None else {
        v: _report(campaign_dir(out, v)) for v in VARIANTS if campaign_dir(out, v).is_dir()}
    scored = {v: score(r) for v, r in reports.items() if r}
    if not scored:
        return {"recommendation": None, "why": "no finished variant reports"}
    best = max(scored, key=lambda v: (scored[v]["max_pass_ctx"], scored[v]["tensors_ok"],
                                      -(scored[v]["elapsed_at_max_s"] or 1e9)))
    base = scored.get("baseline", {})
    rec = {"schema": "orbis-ai-recommendation/1", "time": now_iso(), "variant": best,
           "settings": VARIANTS[best], "scores": scored,
           "improvement_over_baseline_ctx": scored[best]["max_pass_ctx"] - (base.get("max_pass_ctx") or 0),
           "use_vulkan": scored[best]["max_pass_ctx"] > 0 and scored[best]["tensors_ok"]}
    if not rec["use_vulkan"]:
        rec["why"] = "no variant produced numerically correct output on the GPU: AI mode stays on the CPU"
    atomic_write(out / "recommendation.json", json.dumps(rec, indent=1) + "\n")
    return rec


def apply(out: Path, dry_run: bool = False) -> dict:
    try:
        rec = json.loads((out / "recommendation.json").read_text())
    except (OSError, ValueError):
        raise FileNotFoundError("run `orbis-ai-gpu recommend` first")
    acts = []
    env = rec["settings"]["env"] if rec.get("use_vulkan") else {}
    text = ("# NickLabs Orbis Graphics Engine: Vulkan settings chosen by orbis-ai-gpu "
            f"({rec['variant']}, {rec['time']})\n[Service]\n" +
            "".join(f"Environment={k}={v}\n" for k, v in sorted(env.items())))
    acts.append({"file": str(p(DROPIN)), "env": env})
    ub = rec["settings"]["ubatch"][0]
    acts.append({"command": ["orbis", "ai", "set", "ubatch", str(ub)]})
    gate = [HARNESS, "gate", "--out", str(out / rec["variant"]), "--write-receipt", str(p("var/lib/orbis/gpu-gate.json"))]
    acts.append({"command": gate})
    if not dry_run:
        atomic_write(p(DROPIN), text)
        if rec.get("use_vulkan"):
            subprocess.run(["orbis", "ai", "set", "ubatch", str(ub)])
            subprocess.run(gate)
        subprocess.run(["systemctl", "daemon-reload"])
        atomic_write(p(TUNING), json.dumps(rec, indent=1) + "\n")
    return {"applied": not dry_run, "use_vulkan": rec.get("use_vulkan"), "actions": acts,
            "next": "orbis mode ai   (the AI service picks Vulkan only when the gate receipt passed)"}


def status() -> dict:
    vk = Path(VK_BIN_DIR) / "llama-server"
    try:
        tuning = json.loads(p(TUNING).read_text())
    except (OSError, ValueError):
        tuning = None
    try:
        gate = json.loads(p("var/lib/orbis/gpu-gate.json").read_text())
    except (OSError, ValueError):
        gate = None
    return {"llama_build": read(p("usr/share/orbis/llama-build")), "vulkan_build": vk.exists(),
            "harness": os.path.exists(HARNESS), "tuning": tuning,
            "gate": (gate or {}).get("inference_gate"), "dropin": read(p(DROPIN)),
            "variants": {k: v for k, v in VARIANTS.items()}}
