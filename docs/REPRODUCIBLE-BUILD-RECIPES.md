# Reproducible build recipes (2.0.0-r3)

Everything is built on the Nobara machine with `./orbis-ge`. This page lists:
- the inputs that are pinned;
- how each output records where it came from;
- the exact command per target.

## Pinned inputs

| Input | Pin | Where |
|---|---|---|
| Kernel base | feeRnt/ps4-linux-12xx 108ef9764583 (6.15.4 crashnt-4.7) + Orbis 0.2.0 0001 | the 0.2.0 checkout, `manifest/sources.json` → orbis_base |
| Stable backports | stable-6.15/0001 (upstream b2ed01e7ad), 0002 (upstream 1d59f36e95f7, CVE-2026-52949) | `kernel/patches/stable-6.15/`, provenance in each patch header |
| GE kernel series | ge-6.15/0001-0016 | `kernel/patches/series` (applied in that order after the backports) |
| Kernel config | `kernel/config/*.fragment` | hashed into every kernel provenance record |
| Loader | ps4-linux-loader d0f4d4c + GE patch | `manifest/sources.json` → loader |
| Mesa | 26.2.5 (archive sha256 794b3d17…a0a5) + `mesa/stable/patches/series` | `manifest/sources.json` → mesa |
| Arch packages | Arch Linux Archive 2026/10/20 (same snapshot as 0.2.0) | `manifest/sources.json` → arch_snapshot |
| Rust | `rust/Cargo.lock` (std only), crates built `--locked --offline` | |
| Kernel Rust toolchain | `RUST_KERNEL_TOOLCHAIN` (default 1.87.0) | kernel fingerprint |
| Runners | `gaming/runners.lock.json` (SHA-256 per file) | `./orbis-ge runners` |
| Targets | `targets/` + `targets/generated/registry.json` | hashed into every receipt |
| Build containers | `orbis-build:0.2.0` (from the 0.2.0 checkout, built from a private copy), `orbis-gfx-build:2.0.0` | image ids in every provenance record |

Not reproducible bit-for-bit:
- container image ids (they depend on when the base layers were pulled);
- timestamps inside packages and images.

Each receipt records the image ids, so two builds can be compared input by input.

## What each output records

- `<output>.provenance.json` sits next to every kernel, loader, pack and image. It records:
  - artifact name and target (or kernel flavor);
  - source revision and whether the tree was dirty;
  - patch-set hash: the series and every listed patch;
  - configuration hash: config fragments and `sources.json`;
  - toolchain image ids and options;
  - the step's input fingerprint and the sha256 of the file.
- `out/artifact-selection.json` lists what the pack may contain:
  - the GE kernel is required;
  - the BORE kernel only with `--bore`;
  - the loader when built;
  - research kernels never.
  An artifact is selected only when its provenance matches the current inputs and its file still
  has the recorded checksum.
- `out/targets/<id>/ORBIS-GE-RELEASE-RECEIPT.json` (schema 3) is the per-target receipt. It holds:
  - the target as resolved from the registry, and how it was chosen;
  - the selected artifacts, and those left out with the reason;
  - all input hashes;
  - the image and stage-0 hashes with their provenance;
  - the rollback steps.
- `out/RELEASE-MANIFEST.json` lists every target of the run with its receipt and image hashes.

## Guarantees checked by the build

| Guarantee | Mechanism |
|---|---|
| The 0.2.0 checkout is not modified | `build/orbis_src_guard.py` records it before the kernel and pack steps and compares it after (exit 9 on any change). It covers: git HEAD and status; every file under platform/, kernel/, packaging/, out/repo, out/keys and out/rootfs/packages.txt, by size, mode, mtime and inode; and the sha256 of every file up to 64 MiB |
| No writes into it | `make container` runs on a private copy (`out/.work-orbis-src`). gpg signs from a private copy of the keys (mode 0700, removed after). Every other mount of the checkout is read-only |
| No stale artifact in a release | explicit selection with provenance (above). `./orbis-ge verify` hashes only this run's images |
| No unsupported target is built | `./orbis-ge` stops with exit 6 and the registry's reason |

## Recipes

Set these once:

```sh
ORBIS=~/orbis-release/nicklabs-ps4-orbis-linux
GE=~/orbis-release/nicklabs-orbis-graphics-engine
cd $GE && ./orbis-ge check            # Python + target registry + Rust (when cargo is installed)
```

Reference console (CUH-1215A):

```sh
./orbis-ge all --orbis-src $ORBIS --target cuh-1215a \
  --base-image $ORBIS/out/release/orbis-0.2.0-cuh-1215a.img \
  [--base-stage0-image $ORBIS/out/release/orbis-0.2.0-stage0.img]
```

Any other buildable target. Use that console's own 0.2.0 image, built with a matching 0.2.0 profile:

```sh
./orbis-ge all --orbis-src $ORBIS --target cuh-12xx --base-image /path/to/that-console-0.2.0.img
```

Several targets in one run. They must share the kernel flavor, and the pack and kernel are built
once:

```sh
./orbis-ge all --orbis-src $ORBIS --target cuh-1215a,cuh-12xx \
  --base-image cuh-1215a=/path/a.img --base-image cuh-12xx=/path/b.img
```

Unsupported targets, for example any Baikal board, are refused:

```sh
./orbis-ge kernel --orbis-src $ORBIS --target cuh-22xx-baikal    # exit 6 + reason; see TARGET-PORTING-GUIDE.md
```

To compare two builds:

```sh
jq '.inputs, .toolchain, .kernels' out/targets/cuh-1215a/ORBIS-GE-RELEASE-RECEIPT.json
```

Run this on both machines and compare the output. Equal inputs and toolchain ids mean the same
build recipe was used.
