"""research/catalog_update.py with a fake GitHub (no network)."""
import copy
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "research"))
import catalog_update as cu  # noqa: E402

API = cu.API


class FakeFetch:
    def __init__(self, pages):
        self.pages, self.requests, self.urls = pages, 0, []

    def get(self, url):
        self.requests += 1
        self.urls.append(url)
        if re.fullmatch(re.escape(API) + r"/repos/[^/]+/[^/]+", url):   # repository metadata
            return copy.deepcopy(self.pages.get(url, {"full_name": url}))
        for prefix, body in self.pages.items():
            if url.startswith(prefix):
                return copy.deepcopy(body)
        return []


def issue(n, title, body="", pr=False, updated="2026-10-20T00:00:00Z", repo="o/r"):
    d = {"number": n, "title": title, "body": body, "state": "open", "html_url": f"https://github.com/{repo}/issues/{n}",
         "created_at": "2026-10-01T00:00:00Z", "updated_at": updated, "closed_at": None, "comments": 1, "labels": []}
    if pr:
        d["pull_request"] = {}
    return d


WATCH = {"keywords": ["amdgpu", "black screen", "vulkan", "hdmi"],
         "repos": [{"repo": "o/r", "branches": ["main"]}, {"repo": "m/mirror", "branches": []}, {"repo": "gone/x", "branches": []}],
         "issues": [{"url": "https://github.com/v/s/issues/2490", "ledger": "GFX-003"}]}


def pages():
    return {
        f"{API}/repos/gone/x": None,
        f"{API}/repos/o/r/issues?": [
            issue(1, "Black screen after login on CUH-1215A", "Steps to reproduce: start sddm. kernel 6.15.4"),
            issue(2, "Typo in README"),
            issue(3, "amdgpu: fix ring timeout", pr=True)],
        f"{API}/repos/m/mirror/issues?": [issue(9, "Black screen after login on CUH-1215A!", "Baikal", repo="m/mirror")],
        f"{API}/repos/o/r/releases": [{"name": "v1", "tag_name": "v1", "html_url": "https://github.com/o/r/releases/v1",
                                       "published_at": "2026-10-10T00:00:00Z", "body": "HDMI fixes"}],
        f"{API}/repos/o/r/commits?": [{"sha": "abc", "html_url": "https://github.com/o/r/commit/abc",
                                       "commit": {"message": "drm/amdgpu: liverpool vulkan fix\n\nbody",
                                                  "author": {"date": "2026-10-11T00:00:00Z"},
                                                  "committer": {"date": "2026-10-11T00:00:00Z"}}}],
        f"{API}/repos/v/s/issues/2490": {"number": 2490, "title": "corruption", "state": "closed",
                                         "html_url": "https://github.com/vinegarhq/sober/issues/2490",
                                         "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-10-21T00:00:00Z",
                                         "closed_at": "2026-10-21T00:00:00Z", "comments": 7},
    }


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.cat = json.loads((REPO / "research/catalog.json").read_text())

    def test_seed_catalog_is_consistent(self):
        ids = [e["id"] for e in self.cat["entries"]]
        self.assertEqual(len(ids), len(set(ids)))
        for e in self.cat["entries"]:
            self.assertIn(e["evidence"], self.cat["evidence_levels"])
            self.assertIn(e["status"], self.cat["statuses"])
            self.assertIn(e["severity"], ("P0", "P1", "P2"))
        self.assertTrue({f"GFX-{i:03d}" for i in range(1, 19)} <= set(ids))
        w = json.loads((REPO / "research/watch.json").read_text())
        self.assertTrue(all("/" in r["repo"] for r in w["repos"]))

    def test_collect_filter_classify(self):
        f = FakeFetch(pages())
        found = cu.collect(f, WATCH, "2026-10-01T00:00:00Z", log=lambda *a: None)
        urls = {x["url"]: x for x in found}
        self.assertNotIn("https://github.com/o/r/issues/2", urls)          # no keyword
        i1 = urls["https://github.com/o/r/issues/1"]
        self.assertEqual(i1["evidence"], "SAME-MODEL")
        self.assertEqual(i1["severity_guess"], "P0")
        self.assertTrue(i1["has_reproducer"])
        self.assertIn("6.15.4", i1["mentions"])
        self.assertEqual(urls["https://github.com/o/r/issues/3"]["kind"], "pull")
        self.assertEqual(urls["https://github.com/m/mirror/issues/9"]["evidence"], "SAME-MODEL")  # most specific wins
        self.assertEqual(cu.classify("vulkan hang on Baikal")["evidence"], "BA")
        self.assertEqual(cu.classify("Gladius 1002:9924 issue")["evidence"], "PRO")
        self.assertEqual(cu.classify("slow vulkan")["severity_guess"], "P1")
        self.assertEqual(urls["https://github.com/o/r/releases/v1"]["kind"], "release")
        self.assertEqual(urls["https://github.com/o/r/commit/abc"]["evidence"], "SAME-FAMILY")
        self.assertEqual(urls["https://github.com/gone/x"]["kind"], "repo-missing")
        self.assertEqual(urls["https://github.com/vinegarhq/sober/issues/2490"]["kind"], "watched-issue")

    def test_merge_dedupe_and_never_touches_reviewed_text(self):
        f = FakeFetch(pages())
        found = cu.collect(f, WATCH, "2026-10-01T00:00:00Z", log=lambda *a: None)
        before = {e["id"]: (e["title"], e["status"], e["pack_action"]) for e in self.cat["entries"]}
        s = cu.merge(self.cat, found, "2026-10-26T12:00:00Z")
        self.assertEqual({e["id"]: (e["title"], e["status"], e["pack_action"]) for e in self.cat["entries"]}, before)
        # the watched Sober issue updates its source state on GFX-003
        g3 = next(e for e in self.cat["entries"] if e["id"] == "GFX-003")
        self.assertEqual(g3["sources"][0]["state"], "closed")
        self.assertTrue(any(u[0] == "GFX-003" for u in s["source_updates"]))
        inbox = {i["url"]: i for i in self.cat["inbox"]}
        self.assertTrue(all(i["status"] == "new-unreviewed" for i in inbox.values()))
        self.assertNotIn("https://github.com/vinegarhq/sober/issues/2490", inbox)
        mirror = inbox["https://github.com/m/mirror/issues/9"]
        self.assertEqual(mirror["possible_duplicates"], ["https://github.com/o/r/issues/1"])
        # second run with nothing changed: no new items
        s2 = cu.merge(self.cat, cu.collect(FakeFetch(pages()), WATCH, "2026-10-01T00:00:00Z", log=lambda *a: None),
                      "2026-10-27T00:00:00Z")
        self.assertEqual(s2["new"], [])
        self.assertEqual(s2["updated"], [])
        rep = cu.report(s, "now", "since", 3)
        self.assertIn("Nothing below has been applied", rep)
        self.assertIn("**P0**", rep)


if __name__ == "__main__":
    unittest.main()
