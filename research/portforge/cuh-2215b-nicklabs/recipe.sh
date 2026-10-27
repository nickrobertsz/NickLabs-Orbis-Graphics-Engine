#!/usr/bin/env bash
# PortForge build recipe for cuh-2215b-nicklabs (Nobara build machine only; never run on a PS4).
set -euo pipefail
cd "$(dirname "$0")"
UNREVIEWED="c001"
if [ -n "$UNREVIEWED" ]; then
  echo "refusing: candidates not approved: $UNREVIEWED (portforge review cuh-2215b-nicklabs <id> --approve --reviewer NAME)" >&2
  exit 3
fi
for c in candidates/*.json; do      # every approved file must still be the reviewed one
  python3 -c 'import json,hashlib,sys; r=json.load(open(sys.argv[1])); d=open("candidates/"+r["file"],"rb").read(); sys.exit(0 if r["review"]["state"]!="approved" or hashlib.sha256(d).hexdigest()==r["review"].get("reviewed_sha256") else 1)' "$c" \
    || { echo "refusing: $c changed after review" >&2; exit 3; }
done
GE="${GE:-../../..}"          # the nicklabs-orbis-graphics-engine checkout
echo "approved patches: (none)"
echo "1. copy them into $GE/kernel/patches/port-cuh-2215b-nicklabs/ and list them in kernel/patches/series (after review in git)"
echo "2. promote the target:  python3 -m orbis_targets.portforge promote cuh-2215b-nicklabs"
echo "3. build:  (cd $GE && ./orbis-ge kernel --orbis-src <0.2.0 checkout> --target cuh-2215b-nicklabs)"
