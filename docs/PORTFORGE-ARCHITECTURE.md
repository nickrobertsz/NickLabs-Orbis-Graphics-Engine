# PortForge architecture

PortForge turns hardware evidence into reviewed kernel changes and a target definition. It needs
no proprietary AI service. Every change it records carries its origin and stays unreviewed until a
named person approves it. Nothing it produces is run on a PS4.

## Components

| Module | Role |
|---|---|
| `targets/` | Declarative board-support files (see below) and the JSON schema |
| `diag/orbis_targets/registry.py` | Loads and validates the files (std-only schema subset, plus rules: `unsupported` needs a reason, `verified` needs evidence); resolves `extends`; attaches the southbridge and APU; derives the kernel flavor and capability states; writes `generated/registry.json` |
| `probe.py` | Read-only inventory: sysfs PCI attributes, procfs, GE sysfs files and imported logs. No MMIO, no register access, no writes. Logs are redacted |
| `match.py` | Deterministic matching: confirmed / probable / conflicting / unknown |
| `compat.py` | Compatibility (registry next to the current observations) and qualification records with a proposed registry patch |
| `portforge.py` | The workflow: new, candidate, diff-trees, assist, check, review, report, promote |
| `cli.py` | `orbis-hw probe/identify/compatibility/qualify`, `orbis-ge target ...` |
| `orbis-ge port ...` | Entry point on Nobara; workspaces go in `out/portforge/<id>/` |

The registry files are layered from general to specific:

```
targets/
  schema/target.schema.json      orbis-target/1
  common/base.json               shared software, default capability list, sources
  southbridge/{aeolia,belize,belize2,baikal}.json   PCI IDs, subsys ids, boards, kernel flavors, community kernels
  apu/{liverpool-fat,liverpool-12xx,liverpool-slim,gladius}.json   GPU PCI ID, chip, HDMI bridge, memory
  models/*.json                  families and models (extends), boards, Wi-Fi, unified-memory assessment
  generated/registry.json        resolved view (checked by ./orbis-ge check; stale = failure)
```

## Workspace (out/portforge/&lt;id&gt;/)

| File | Content |
|---|---|
| inventory.json | the probe, with imported logs (redacted) |
| match.json | the matcher result |
| comparison.json | compatibility of the nearest registered target with this inventory |
| gap-report.json | gaps per layer (southbridge, kernel, APU, PCI functions) with the action for each, known components, and capability states on that target |
| candidates/cNNN.patch + .json | a candidate change and its record |
| checks.json | per candidate: `git apply --check` against a kernel tree, checkpatch counts, a flag for register access, files touched |
| review.md | the review report |
| recipe.sh | the Nobara build recipe; refuses while anything is unreviewed or changed after review |
| target-draft.json | the new target; validated again on promote |
| provenance.jsonl | append-only log of every event |

## The candidate record

```json
{"schema": "portforge-candidate/1", "id": "c001", "origin": "community|manual|assistant",
 "sha256": "...", "created": "...",
 "provenance": {"source": "repo@branch (commit)", "...": "...",
                "assistant_command": "...", "prompt_sha256": "...", "output_sha256": "..."},
 "review": {"state": "unreviewed|approved|rejected", "reviewer": "...", "time": "...",
            "reviewed_sha256": "..."},
 "may_run_on_ps4": false}
```

## Rules enforced in code (each has a test)

1. **The assistant is optional and local.** `assist --assistant cmd:<program>` sends the gap
   report on standard input and expects a unified diff on standard output. Any program will do, for
   example llama.cpp in Orbis AI mode. Nothing is called by default, and any other value is
   refused. Its output becomes an unreviewed candidate with the prompt and output hashes.
2. **Review is explicit.** `review` needs `--reviewer` and records the hash it approved. A file
   changed after recording is rejected by `review`; a file changed after review makes `recipe.sh`
   refuse.
3. **No unreviewed change goes anywhere.** `recipe.sh` exits 3 and `promote` refuses while any
   candidate is unapproved, rejected candidates included.
4. **Nothing runs on the console.** PortForge produces Nobara build inputs. The only console-side
   tools are the read-only `orbis-hw probe/identify/compatibility/qualify`.
5. **Verified needs evidence.** The schema rejects `verified` without evidence. `qualify` proposes
   verified states with the record id and never writes the registry.
6. **No default target.** Matching never falls back to the reference console. Hardware evidence
   alone picks the family; only a model number picks a model.

## Matching rules

These rules are in `match.py`, and the tests cover each outcome.

1. A target is a candidate when no fact contradicts it.
2. If two facts each fit some target but no target fits both, the result is **conflicting**, and
   both facts are named.
3. If no target is a candidate, the result is **unknown** (unrecognized IDs are listed).
4. If exactly one target is a candidate:
   - **confirmed** when the GPU and the southbridge are identified by strong evidence;
   - **probable** otherwise.
5. If several targets are candidates, the result is **unknown**, with the evidence that would
   separate them.

Belize 2 PCI IDs are not documented, so a Belize PCI ID is treated as consistent with Belize 2. The
subsys id (0x40100) or the board name tells them apart.

## Exit codes

| Command | Codes |
|---|---|
| `orbis-hw identify` | 0 confirmed/probable, 4 unknown, 5 conflicting |
| `orbis-hw compatibility` / `qualify` | 4 when no confirmed target and no `--target` given |
| `orbis-hw qualify` | 0 PASS/INCOMPLETE, 1 FAIL, 4 BLOCKED |
| `portforge check` | 1 when a candidate does not apply |
| `portforge promote`, `recipe.sh` | 3 while candidates are unreviewed |
| `orbis-ge` building an unsupported target | 6 |
