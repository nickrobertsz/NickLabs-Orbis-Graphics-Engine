# Target porting guide

This guide takes the Graphics Engine to a PS4 board it does not support yet, or confirms that a
console matches a registered target. Every step either reads data or writes a file you review.
Nothing in this workflow runs on a PS4 except the read-only probe.

## 1. Identify the console

On the console (any Linux, including a community kernel; the probe is read-only):

```sh
sudo orbis-hw probe --json --out probe.json                 # PCI IDs, CPU, GE sysfs; serials and MACs redacted
sudo orbis-hw identify --probe probe.json --model CUH-2215B # the label model is weak evidence
```

On Nobara, from files only:

```sh
./orbis-ge port probe probe.json [--log serial.log] [--model CUH-....]
```

`identify` returns one of four results. The exit code tells scripts which one.

| Result | Exit | Meaning |
|---|---|---|
| confirmed | 0 | one target, with the GPU and the southbridge identified by strong evidence |
| probable | 0 | one target, but evidence is missing (listed in `missing_evidence`) |
| unknown | 4 | no target, or several (`candidates`, plus the evidence that would separate them) |
| conflicting | 5 | the evidence describes different consoles (`conflicts` names the facts) |

The two kinds of evidence:
- **Strong:** the GPU PCI ID, the southbridge PCI IDs, `hw.sce_subsys_subid`, a board name from a log.
- **Weak:** a CUH model number, a southbridge name mentioned in a log.

Hardware evidence alone always picks the family (`cuh-12xx`). Only a model number picks a model
(`cuh-1215a`). Nothing defaults to the reference console.

Logs you can import with `--log`:
- a bootloader or serial capture;
- `dmesg` or `lspci -nn` output;
- the output of ps4-which-southbridge, run under OrbisOS.

Serial numbers, MAC addresses and 128-bit console IDs are redacted on import.

## 2. Check compatibility

```sh
orbis-hw compatibility --probe probe.json            # needs a confirmed match, or --target <id>
./orbis-ge target inspect cuh-12xx                   # the full resolved target, recovery assets included
```

The output lists each capability with its registry state. When the probe came from the console
itself, it also shows what the console has right now (`observed_now`: PRESENT, ABSENT or
NOT_CHECKED). Observations never change a state.

## 3. Build for a registered target

```sh
./orbis-ge all --orbis-src $ORBIS --target cuh-12xx --base-image $BASE
```

- An **unsupported** target stops with exit code 6 and the reason.
- Several targets that share a kernel flavor build in one run:
  `--target a,b --base-image a=A.img --base-image b=B.img`.

## 4. Port a new target with PortForge

```sh
./orbis-ge port new cuh-2215b-nicklabs --probe probe.json      # inventory, match, comparison, gap report, draft target
./orbis-ge port diff-trees cuh-2215b-nicklabs --base <6.15 tree> --other <community tree> \
      --paths drivers/ps4/ps4-bpcie.c,... --source "repo@branch (commit)"     # community candidate
./orbis-ge port candidate cuh-2215b-nicklabs --patch my.patch --origin manual --source "NickLabs"
./orbis-ge port assist cuh-2215b-nicklabs --assistant "cmd:/path/to/local-model-wrapper"   # optional
./orbis-ge port check cuh-2215b-nicklabs --kernel-tree <6.15 tree>     # git apply --check, checkpatch, register-access flag
./orbis-ge port review cuh-2215b-nicklabs c001 --approve --reviewer Nick --notes "read every hunk"
./orbis-ge port report cuh-2215b-nicklabs                      # review.md + recipe.sh
./orbis-ge port promote cuh-2215b-nicklabs                     # draft -> targets/models (all candidates approved)
./orbis-ge target generate && git add targets && git commit
```

Then add the approved patches to `kernel/patches/series`. Next, give the new flavor a build recipe
(today only `aeolia-belize-6.15` has one). Build on Nobara and qualify on the console:

```sh
sudo orbis-hw qualify --target <id>          # writes var/lib/orbis-graphics/qualification/qual-<id>-<time>.json
```

`qualify` does three things:
- it runs read-only checks;
- it reads the results of `orbis-gpu qualify` and `orbis-mem bench`;
- it proposes a `registry_patch` that marks as verified only the capabilities that passed, each
  citing the record.

The proposal is applied by a reviewed commit to the target file. It is never automatic.

## Worked example: the NickLabs CUH-2215B (Baikal)

`research/portforge/cuh-2215b-nicklabs/` holds the workspace PortForge produced in the authoring
environment.

The **inventory is simulated**: the PCI IDs come from the registry because the console was not
available. Replace it with a real probe before relying on it.

| Step | Result |
|---|---|
| match | confirmed, `cuh-22xx-baikal` (unsupported) |
| gap | no 6.15 Baikal kernel flavor |
| candidate c001 | community, from `sony-jaguar-devs/ps4-linux@5.4.247-baikal-dfaus` (7f796666a956); new files only: ps4-bpcie.c, ps4-bpcie-icc.c, ps4-bpcie-uart.c, baikal.h, aeolia-baikal.h |
| check | applies cleanly to the 6.15.4 GE tree; checkpatch 124 errors (5.4 style); touches registers (expected for a bus driver); **not compiled** |
| merge notes | the Baikal hunks in `asm/ps4.h` and `platform/ps4/ps4.c` must be merged by hand (a whole-file replacement would revert 6.15 and GE changes) |
| review | **unreviewed**: `recipe.sh` and `promote` refuse until it is approved |

Next steps:
1. Run a real `orbis-hw probe` on the CUH-2215B (or capture `lspci -nn` under the 5.4.247 kernel).
2. Review c001.
3. Merge the hunks listed in `merge-notes.md`.
4. Fix the 5.4-to-6.15 API differences.
5. Compile on Nobara.
