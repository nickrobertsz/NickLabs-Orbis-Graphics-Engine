"""Result model shared by every Orbis Graphics diagnostic (stage 0, qualify, health).

States (the only ones any tool may emit):
  PASS         the check ran and met its criterion
  FAIL         the check ran and did not meet it
  DEGRADED     it works, but below the expected level (e.g. software fallback in a
               non-critical path, throttling, partial feature)
  UNSUPPORTED  the platform/kernel/driver cannot do this today; `blocker` says why
  NOT_RUN      deliberately not executed (opt-in test, missing tool, precondition)
  BLOCKED      could not run because an earlier required check failed
"""
from __future__ import annotations

import json
import platform
import time
from dataclasses import asdict, dataclass, field

STATES = ("PASS", "FAIL", "DEGRADED", "UNSUPPORTED", "NOT_RUN", "BLOCKED")
SCHEMA = "orbis-gfx-report/1"
_RANK = {"FAIL": 5, "BLOCKED": 4, "DEGRADED": 3, "UNSUPPORTED": 1, "NOT_RUN": 1, "PASS": 0}


@dataclass
class Check:
    id: str
    title: str
    state: str
    detail: str = ""
    category: str = "general"
    required: bool = False
    blocker: str | None = None
    duration_s: float | None = None
    evidence: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError(f"invalid state {self.state!r} for {self.id}")
        if self.state in ("UNSUPPORTED", "BLOCKED") and not self.blocker:
            raise ValueError(f"{self.id}: state {self.state} needs a blocker explanation")


@dataclass
class Report:
    tool: str
    version: str
    stage: str
    checks: list[Check] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    time: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def get(self, cid: str) -> Check | None:
        return next((c for c in self.checks if c.id == cid), None)

    def passed(self, cid: str) -> bool:
        c = self.get(cid)
        return bool(c and c.state == "PASS")

    @property
    def counts(self) -> dict:
        return {s: sum(1 for c in self.checks if c.state == s) for s in STATES}

    @property
    def overall(self) -> str:
        """FAIL if anything failed; BLOCKED if a required check is blocked; DEGRADED if anything
        degraded or a required check is UNSUPPORTED/NOT_RUN; otherwise PASS. Never PASS when
        nothing PASSed."""
        if any(c.state == "FAIL" for c in self.checks):
            return "FAIL"
        if any(c.state == "BLOCKED" and c.required for c in self.checks):
            return "BLOCKED"
        if any(c.state == "DEGRADED" for c in self.checks) or \
           any(c.required and c.state in ("UNSUPPORTED", "NOT_RUN", "BLOCKED") for c in self.checks):
            return "DEGRADED"
        if not any(c.state == "PASS" for c in self.checks):
            return "NOT_RUN"
        return "PASS"

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, "tool": self.tool, "version": self.version, "stage": self.stage,
                "time": self.time, "host": {"kernel": platform.release(), "machine": platform.machine()},
                "context": self.context, "overall": self.overall, "counts": self.counts,
                "checks": [asdict(c) for c in self.checks]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=1, default=str)

    def render_text(self) -> str:
        lines = [f"NICKLABS / ORBIS GRAPHICS — {self.tool} {self.version} — {self.stage} — {self.time}", ""]
        cat = None
        for c in self.checks:
            if c.category != cat:
                cat = c.category
                lines.append(f"[{cat}]")
            dur = f" ({c.duration_s:.2f}s)" if c.duration_s is not None else ""
            lines.append(f"  {c.state:<11} {c.id:<24} {c.title}{dur}")
            if c.detail:
                lines.append(f"              {c.detail}")
            if c.blocker:
                lines.append(f"              blocker: {c.blocker}")
        lines += ["", "counts: " + ", ".join(f"{k}={v}" for k, v in self.counts.items() if v),
                  f"OVERALL: {self.overall}"]
        return "\n".join(lines) + "\n"


def worst(states: list[str]) -> str:
    return max(states, key=lambda s: _RANK[s]) if states else "NOT_RUN"
