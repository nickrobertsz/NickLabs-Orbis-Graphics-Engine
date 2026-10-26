# Native PS4 reverse-engineering workflow (documentation only)

The GameOS (Orbis OS) does not run under Linux, and nothing in the Graphics Engine tries to run it.
This page describes how to study how the original system drives the hardware: GPU clocks, UVD,
the HDMI bridge and the EMC/ICC protocol. The findings then feed Linux drivers, which are written
from your own notes, not from copied code.

## Ground rules

* Work only on dumps from **your own console**, made with tools you are entitled to use on it
  (GoldHEN payloads, FTP). Never download or share Sony binaries, keys or firmware.
* **Clean room.** Whoever reads disassembly writes a plain-language specification: register,
  value, order, timing. Driver code is written from that specification and cites it. No
  decompiled code goes into Orbis or upstream patches.
* Do not touch the PS4's internal drive from Linux. Dumps travel over FTP to the ASUS laptop.
* Keep the console on the LAN only. Ghidra runs offline on the ASUS.

## Tools (ASUS laptop)

| Tool | Use |
|---|---|
| Ghidra 11.x (offline) | disassembly and data-flow of dumped modules |
| a PS4 ELF/SELF loader extension for Ghidra (community; check its licence) | maps decrypted SPRX/ELF modules and resolves NIDs |
| the console's own decrypted modules (GoldHEN FTP: `/system/priv/lib`, `/system/common/lib`) | the code under study |
| a text spec template (below) | the clean-room output |

## Method

1. **Choose one question.** Examples: "How does GameOS program the SCLK divider after a GFX
   reset?", "Which ICC message sets the fan curve?", "What order does the MN864729 un-mute
   follow?"
2. **Find the entry points.** Search the GPU or bridge driver modules for the MMIO offsets Linux
   already knows (`gpuregs.REGS` in `diag/orbis_engine`, and the `cik`/`gmc`/`uvd` headers).
   Register writes appear as stores to the mapped MMIO base plus the dword offset × 4.
3. **Annotate in Ghidra.** Rename functions by behaviour, record constants and call order, and
   mark delays and polling loops with their timeouts.
4. **Write the spec.** For each step record the register, mask, value, condition, delay, the
   source module and version (firmware 13.50), and how sure you are of it.
5. **Cross-check on Linux.** Read the same registers with `sudo orbis-debug gpu` (read-only)
   before and after the Linux driver acts. Differences become the test case for a kernel patch.
6. **Write the patch from the spec.** Put a new behaviour behind a module parameter that defaults
   to the current one, as all GE patches do. Qualify it on the CUH-1215A with `orbis-gpu qualify`
   and the Black Box.

## Spec template

```
Topic:            e.g. Liverpool SCLK restore after soft reset
Firmware:         13.50, module <name> <sha256 of the dump>
Sequence:
  1. read  CG_SCLK_CNTL (0x...)        -> keep bits ...
  2. write CG_SCLK_CNTL = ... | DIRECT_CTRL | DID(1)
  3. poll  ... until ... (timeout ... us)
Conditions:       only when ...
Confidence:       observed / inferred / guessed
Linux mapping:    drivers/gpu/drm/amd/amdgpu/liverpool_clk.c (ge-0002)
Verified on HW:   date, console, orbis-debug bundle id
```

## Where the community already is

* SCLK divider, fan/ICC, cpufreq P-states and the SMU mailbox messages are already understood well
  enough to drive (ge-0001/0002/0007/0009).
* The open questions most worth Ghidra time:
  * the UVD firmware load and session setup (to fix the research kernel's VM faults);
  * VCE bring-up;
  * the MN864729 mode-change sequence (the black-screen class);
  * memory P-state usage by GameOS.
