#!/usr/bin/env bash
# NickLabs Orbis Graphics Pack — collect PS4 GPU kernel deltas for human review (run on Nobara;
# needs git and internet; ~2-4 GB of git objects in out/kernel-research/git).
#
#   kernel/research-deltas.sh [--only ID[,ID...]] [--follow]
#
# For every tree in kernel/candidates.json: fetches the pinned ref and its upstream Linux tag
# (blobless, history since 2024-12), finds where the PS4 work starts (merge-base with upstream),
# and writes, per candidate, under out/kernel-research/<id>/:
#   commits.txt    PS4 commits touching GPU/display/bridge paths (oneline, newest first)
#   files.txt      changed GPU paths with insertions/deletions
#   gpu.diff       the full diff of those paths
#   patches/       the same commits as format-patch files (for cherry-picking)
# plus out/kernel-research/summary.json. NOTHING is applied: a commit you choose after review is
# copied into kernel/patches/ and listed in kernel/patches/series; then build-ge-kernel.sh.
# --follow accepts refs that moved since candidates.json was written (default: refuse).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
R="$(cd "$here/.." && pwd)"
only=""; follow=0
while [ $# -gt 0 ]; do
  case "$1" in
    --only) only="$2"; shift 2 ;;
    --follow) follow=1; shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
out="$R/out/kernel-research"; g="$out/git"
mkdir -p "$out"
[ -d "$g" ] || git init -q --bare "$g"
cj="$here/candidates.json"
q() { python3 -c "import json,sys; d=json.load(open('$cj')); print(eval(sys.argv[1]))" "$1"; }
mirror="$(q "d['upstream_mirror']")"
mapfile -t paths < <(python3 -c "import json; print('\n'.join(json.load(open('$cj'))['gpu_paths']))")
mapfile -t ids < <(python3 -c "import json; print('\n'.join(c['id'] for c in json.load(open('$cj'))['candidates']))")
fetch() {  # fetch <repo> <ref> <localref>
  git -C "$g" fetch -q --filter=blob:none --shallow-since=2024-12-01 "$1" "+$2:$3"
}
base_repo="$(q "d['base']['repo']")"; base_ref="$(q "d['base']['ref']")"; base_commit="$(q "d['base']['commit']")"
echo "== base $base_ref"
fetch "$base_repo" "$base_ref" refs/orbis/base
[ "$(git -C "$g" rev-parse 'refs/orbis/base^{commit}')" = "$base_commit" ] || { echo "base tag moved" >&2; exit 4; }
echo '[' > "$out/summary.json.tmp"; first=1
for id in "${ids[@]}"; do
  if [ -n "$only" ] && ! printf ',%s,' "$only" | grep -q ",$id,"; then continue; fi
  c="[c for c in d['candidates'] if c['id']=='$id'][0]"
  repo="$(q "$c['repo']")"; ref="$(q "$c['ref']")"; pin="$(q "$c['commit']")"; up="$(q "$c['upstream']")"
  echo "== $id ($ref, upstream $up)"
  fetch "$repo" "$ref" "refs/orbis/cand/$id"
  have="$(git -C "$g" rev-parse "refs/orbis/cand/$id^{commit}")"
  if [ "$have" != "$pin" ]; then
    if [ "$follow" = 1 ]; then echo "   NOTE: $ref moved $pin -> $have (--follow)"; else
      echo "   SKIP: $ref moved $pin -> $have; review candidates.json or pass --follow" >&2; continue; fi
  fi
  git -C "$g" rev-parse -q --verify "refs/orbis/up/$up" >/dev/null || fetch "$mirror" "refs/tags/$up" "refs/orbis/up/$up"
  mb="$(git -C "$g" merge-base "refs/orbis/up/$up" "refs/orbis/cand/$id" || true)"
  if [ -z "$mb" ]; then echo "   SKIP: no merge base with $up inside the fetched window" >&2; continue; fi
  d="$out/$id"; rm -rf "$d"; mkdir -p "$d/patches"
  git -C "$g" log --no-merges --format='%h %ad %an: %s' --date=short "$mb..$have" -- "${paths[@]}" > "$d/commits.txt"
  git -C "$g" diff --numstat "$mb" "$have" -- "${paths[@]}" > "$d/files.txt"
  git -C "$g" diff "$mb" "$have" -- "${paths[@]}" > "$d/gpu.diff"
  git -C "$g" format-patch -q --no-merges -o "$d/patches" "$mb..$have" -- "${paths[@]}" >/dev/null || true
  # same base line as Orbis (6.15.4)? then show what the candidate has that Orbis does not
  rel=""
  if [ "$up" = v6.15.4 ]; then
    git -C "$g" log --no-merges --format='%h %s' "refs/orbis/base..$have" -- "${paths[@]}" > "$d/not-in-orbis-base.txt"
    rel="$(wc -l < "$d/not-in-orbis-base.txt")"
  fi
  n="$(wc -l < "$d/commits.txt")"; nf="$(wc -l < "$d/files.txt")"
  echo "   $n GPU commits, $nf files${rel:+, $rel not in the Orbis base}"
  [ $first = 1 ] || echo ',' >> "$out/summary.json.tmp"; first=0
  printf '{"id":"%s","ref":"%s","commit":"%s","pinned":"%s","upstream":"%s","merge_base":"%s","gpu_commits":%s,"gpu_files":%s,"not_in_orbis_base":%s}' \
    "$id" "$ref" "$have" "$pin" "$up" "$mb" "$n" "$nf" "${rel:-null}" >> "$out/summary.json.tmp"
done
echo ']' >> "$out/summary.json.tmp"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); json.dump({'schema':1,'generated':__import__('time').strftime('%Y-%m-%dT%H:%M:%SZ', __import__('time').gmtime()),'candidates':d}, open(sys.argv[2],'w'), indent=1)" \
  "$out/summary.json.tmp" "$out/summary.json"
rm -f "$out/summary.json.tmp"
cat <<EOF

Wrote $out/summary.json and one folder per candidate.
Review, then for each commit you accept:
  cp $out/<id>/patches/NNNN-*.patch kernel/patches/
  echo NNNN-....patch >> kernel/patches/series       # order matters; they apply after Orbis 0001
  kernel/build-ge-kernel.sh --orbis-src <Orbis 0.2.0 checkout>
Priorities and risks per area: docs/GRAPHICS-KERNEL-MESA-COMPATIBILITY.md.
EOF
