#!/usr/bin/env python3
"""Orbis graphics bug catalog updater — run on Nobara (internet), never on the PS4.

  research/catalog_update.py [--catalog research/catalog.json] [--watch research/watch.json]
                             [--since 2026-10-01] [--max-wait 900] [--report-dir research/reports] [--dry-run]

Polls the GitHub REST API for the repositories and issues in watch.json:
  * issues and pull requests updated since the last run (one endpoint; PRs are flagged),
  * releases, and commits on the listed branches,
  * the individually watched issues (state, comments count, timestamps).
Keeps items that match a keyword, de-duplicates (same URL; mirrored issues with the same normalised
title across repos are grouped), rates the evidence from the text (CUH-1215 / CUH-12xx / Baikal /
Pro ...), guesses a severity and whether it has a reproducer, and appends NEW items to
catalog["inbox"] with status "new-unreviewed". Reviewed entries are never changed except for
"last_checked" and the state/timestamps of their own sources. Nothing is downloaded besides
metadata and nothing is applied: a human moves an inbox item into "entries".

Authentication: GITHUB_TOKEN (or GH_TOKEN) if set (5000 requests/h), else anonymous (60/h).
Rate limits: honours X-RateLimit-Remaining/Reset and Retry-After, waits up to --max-wait seconds,
retries 5xx with exponential back-off, and uses ETags (cache in research/cache/etags.json) so
unchanged pages cost nothing.
Writes the catalog atomically and a Markdown report research/reports/catalog-update-<UTC>.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "https://api.github.com"
UA = "nicklabs-orbis-graphics-catalog/1.0.0"


class Fetcher:
    """GitHub GET with ETag cache, rate-limit waits and retries. Replaceable in tests."""

    def __init__(self, cache: Path, token: str | None, max_wait: int, log=print):
        self.cache_p = cache
        self.etags = json.loads(cache.read_text()) if cache.exists() else {}
        self.token, self.max_wait, self.log = token, max_wait, log
        self.requests = 0

    def save(self) -> None:
        self.cache_p.parent.mkdir(parents=True, exist_ok=True)
        self.cache_p.write_text(json.dumps(self.etags, indent=0))

    def get(self, url: str):
        for attempt in range(5):
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": UA,
                                                       "X-GitHub-Api-Version": "2022-11-28"})
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            cached = self.etags.get(url)
            if cached:
                req.add_header("If-None-Match", cached["etag"])
            try:
                self.requests += 1
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = json.loads(r.read().decode())
                    if r.headers.get("ETag"):
                        self.etags[url] = {"etag": r.headers["ETag"], "body": body}
                    self._pace(r.headers)
                    return body
            except urllib.error.HTTPError as e:
                if e.code == 304 and cached:
                    return cached["body"]
                if e.code == 404:
                    return None
                if e.code in (403, 429) and (e.headers.get("Retry-After") or e.headers.get("X-RateLimit-Remaining") == "0"):
                    wait = int(e.headers.get("Retry-After") or max(0, int(e.headers.get("X-RateLimit-Reset", "0")) - int(time.time())) + 1)
                    if wait > self.max_wait:
                        raise SystemExit(f"rate limited for {wait}s (> --max-wait {self.max_wait}); set GITHUB_TOKEN or retry later")
                    self.log(f"  rate limited; waiting {wait}s")
                    time.sleep(wait)
                    continue
                if e.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"GET {url}: HTTP {e.code}")
            except urllib.error.URLError as e:
                time.sleep(2 ** attempt)
                last = e
                continue
        raise SystemExit(f"GET {url}: giving up after retries")

    def _pace(self, h) -> None:
        if h.get("X-RateLimit-Remaining") == "0":
            wait = max(0, int(h.get("X-RateLimit-Reset", "0")) - int(time.time())) + 1
            if wait <= self.max_wait:
                self.log(f"  rate budget spent; waiting {wait}s")
                time.sleep(wait)


# ------------------------------------------------------------------ classification
EVIDENCE_RULES = [
    ("SAME-MODEL", r"cuh-?1215"),
    ("SAME-FAMILY", r"cuh-?12\d\d|liverpool|1002:992[023]|\bbelize\b|\baeolia\b"),
    ("BA", r"\bbaikal\b|cuh-?2\d{3}|\bslim\b"),
    ("PRO", r"\bgladius\b|1002:9924|cuh-?7\d{3}|\bps4 pro\b"),
]
SEVERITY_RULES = [
    ("P0", r"black ?screen|no signal|hang|freez|lock ?up|crash|corrupt|garbl|ring .*timeout|gpu reset|not boot|llvmpipe|software render"),
    ("P1", r"slow|stutter|regress|fail|error|vram|uvd|vce|vulkan|clock|thermal|throttl|edid|hotplug"),
]
REPRO = r"steps to reproduce|to reproduce|repro(duce|ducer|duction)?\b|how to trigger|\$ |```"


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def classify(text: str) -> dict:
    t = text.lower()
    ev = next((name for name, rx in EVIDENCE_RULES if re.search(rx, t)), "UNRATED")
    sev = next((name for name, rx in SEVERITY_RULES if re.search(rx, t)), "P2")
    return {"evidence": ev, "severity_guess": sev, "has_reproducer": bool(re.search(REPRO, t)),
            "mentions": sorted({m.group(0) for m in re.finditer(
                r"\b(6\.\d+(\.\d+)?|7\.\d+(\.\d+)?|5\.\d+\.\d+)\b|mesa[ -]?\d+\.\d+(\.\d+)?|cuh-?\d{4}[ab]?", t)})[:12]}


def matches(text: str, keywords: list[str]) -> list[str]:
    t = text.lower()
    return [k for k in keywords if k in t]


# ------------------------------------------------------------------ collection
def collect(fetch: Fetcher, watch: dict, since: str, log=print) -> list[dict]:
    kws = watch["keywords"]
    out = []
    for r in watch["repos"]:
        repo = r["repo"]
        log(f"== {repo}")
        meta = fetch.get(f"{API}/repos/{repo}")
        if meta is None:
            out.append({"kind": "repo-missing", "repo": repo, "url": f"https://github.com/{repo}",
                        "title": f"{repo} not reachable (404)", "updated": None, "keywords": []})
            continue
        page = 1
        while page <= 10:
            q = urllib.parse.urlencode({"state": "all", "since": since, "per_page": 100, "page": page,
                                        "sort": "updated", "direction": "desc"})
            items = fetch.get(f"{API}/repos/{repo}/issues?{q}") or []
            for it in items:
                text = f"{it.get('title', '')}\n{it.get('body') or ''}"
                kw = matches(text, kws)
                if not kw:
                    continue
                out.append({"kind": "pull" if "pull_request" in it else "issue", "repo": repo, "number": it["number"],
                            "url": it["html_url"], "title": it["title"], "state": it["state"],
                            "created": it.get("created_at"), "updated": it.get("updated_at"), "closed": it.get("closed_at"),
                            "comments": it.get("comments"), "labels": [l["name"] for l in it.get("labels", [])],
                            "keywords": kw, **classify(text)})
            if len(items) < 100:
                break
            page += 1
        for rel in fetch.get(f"{API}/repos/{repo}/releases?per_page=30") or []:
            if (rel.get("published_at") or "") < since:
                continue
            text = f"{rel.get('name') or rel.get('tag_name')}\n{rel.get('body') or ''}"
            kw = matches(text, kws)
            if kw:
                out.append({"kind": "release", "repo": repo, "url": rel["html_url"], "title": rel.get("name") or rel["tag_name"],
                            "tag": rel["tag_name"], "created": rel.get("published_at"), "updated": rel.get("published_at"),
                            "keywords": kw, **classify(text)})
        for br in r.get("branches", []):
            q = urllib.parse.urlencode({"sha": br, "since": since, "per_page": 100})
            for c in fetch.get(f"{API}/repos/{repo}/commits?{q}") or []:
                msg = c["commit"]["message"]
                kw = matches(msg, kws)
                if kw:
                    out.append({"kind": "commit", "repo": repo, "branch": br, "url": c["html_url"], "sha": c["sha"],
                                "title": msg.splitlines()[0][:200], "created": c["commit"]["author"]["date"],
                                "updated": c["commit"]["committer"]["date"], "keywords": kw, **classify(msg)})
    for w in watch.get("issues", []):
        m = re.match(r"https://github.com/([^/]+/[^/]+)/(issues|discussions)/(\d+)", w["url"])
        if not m or m.group(2) != "issues":
            continue  # discussions need GraphQL; listed for manual checking
        it = fetch.get(f"{API}/repos/{m.group(1)}/issues/{m.group(3)}")
        if it:
            out.append({"kind": "watched-issue", "repo": m.group(1), "number": it["number"], "url": it["html_url"],
                        "title": it["title"], "state": it["state"], "created": it.get("created_at"),
                        "updated": it.get("updated_at"), "closed": it.get("closed_at"), "comments": it.get("comments"),
                        "ledger": w.get("ledger"), "keywords": []})
    return out


def merge(catalog: dict, found: list[dict], now: str) -> dict:
    """Update catalog in place; return a summary of what changed."""
    known = {s["url"] for e in catalog["entries"] for s in e.get("sources", [])}
    inbox = {i["url"]: i for i in catalog.get("inbox", [])}
    by_title = {}
    for i in catalog.get("inbox", []):
        by_title.setdefault(norm_title(i["title"]), []).append(i["url"])
    new, updated, source_updates = [], [], []
    for f in found:
        url = f["url"]
        if url in known:
            for e in catalog["entries"]:
                for s in e.get("sources", []):
                    if s["url"] == url:
                        for k in ("state", "updated", "closed", "comments"):
                            if f.get(k) is not None and s.get(k) != f.get(k):
                                s[k] = f[k]
                                source_updates.append((e["id"], url, k, f[k]))
                        e["last_checked"] = now[:10]
            continue
        if f["kind"] == "watched-issue":
            continue
        if url in inbox:
            if inbox[url].get("updated") != f.get("updated"):
                inbox[url].update({k: v for k, v in f.items() if k not in ("status", "first_seen")})
                inbox[url]["last_checked"] = now
                updated.append(inbox[url])
            continue
        item = dict(f, status="new-unreviewed", first_seen=now, last_checked=now)
        dup = [u for u in by_title.get(norm_title(f["title"]), []) if u != url]
        if dup and f["kind"] in ("issue", "pull"):
            item["possible_duplicates"] = dup
        by_title.setdefault(norm_title(f["title"]), []).append(url)
        inbox[url] = item
        new.append(item)
    catalog["inbox"] = sorted(inbox.values(), key=lambda i: (i.get("severity_guess", "P9"), i.get("updated") or ""), reverse=False)
    catalog["generated"] = now
    return {"new": new, "updated": updated, "source_updates": source_updates}


def report(summary: dict, now: str, since: str, requests: int) -> str:
    L = [f"# Orbis graphics catalog update — {now}", "", f"Window: updated since {since}. GitHub requests: {requests}.",
         "Nothing below has been applied. Review each item; move real ones into `entries` with an evidence level.", ""]
    for title, items in (("New", summary["new"]), ("Updated (inbox)", summary["updated"])):
        L.append(f"## {title}: {len(items)}")
        for sev in ("P0", "P1", "P2", None):
            group = [i for i in items if i.get("severity_guess") == sev] if sev else [i for i in items if not i.get("severity_guess")]
            for i in group:
                L.append(f"- **{i.get('severity_guess', '?')}** [{i['kind']}] {i['repo']}: [{i['title']}]({i['url']}) "
                         f"— evidence {i.get('evidence', '?')}, reproducer {'yes' if i.get('has_reproducer') else 'no'}, "
                         f"keywords {', '.join(i.get('keywords', [])[:5])}"
                         + (f", possible duplicate of {', '.join(i['possible_duplicates'])}" if i.get("possible_duplicates") else ""))
        L.append("")
    L.append(f"## Changes on reviewed entries' sources: {len(summary['source_updates'])}")
    for eid, url, k, v in summary["source_updates"]:
        L.append(f"- {eid}: {url} {k} -> {v}")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--catalog", default=str(HERE / "catalog.json"))
    ap.add_argument("--watch", default=str(HERE / "watch.json"))
    ap.add_argument("--since", help="ISO date; default: the previous run's time (or 90 days)")
    ap.add_argument("--max-wait", type=int, default=900)
    ap.add_argument("--report-dir", default=str(HERE / "reports"))
    ap.add_argument("--dry-run", action="store_true", help="print the report; do not write the catalog")
    a = ap.parse_args(argv)
    cat_p = Path(a.catalog)
    catalog = json.loads(cat_p.read_text())
    watch = json.loads(Path(a.watch).read_text())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    since = a.since or catalog.get("cursor", {}).get("last_run") or time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 90 * 86400))
    if len(since) == 10:
        since += "T00:00:00Z"
    fetch = Fetcher(HERE / "cache/etags.json", os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"), a.max_wait)
    found = collect(fetch, watch, since)
    summary = merge(catalog, found, now)
    rep = report(summary, now, since, fetch.requests)
    if a.dry_run:
        print(rep)
        return 0
    catalog.setdefault("cursor", {})["last_run"] = now
    tmp = cat_p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(catalog, indent=1) + "\n")
    os.replace(tmp, cat_p)
    fetch.save()
    rd = Path(a.report_dir)
    rd.mkdir(parents=True, exist_ok=True)
    rp = rd / f"catalog-update-{now.replace(':', '')}.md"
    rp.write_text(rep)
    print(f"{len(summary['new'])} new, {len(summary['updated'])} updated, {len(summary['source_updates'])} source changes; report {rp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
