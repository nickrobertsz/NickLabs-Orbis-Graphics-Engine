# NickLabs Orbis Black Box

Persistent diagnostics that survive hangs, kernel panics and power loss. Nothing in it relies on
a graceful shutdown.

## What is recorded

`/var/lib/orbis-blackbox/boots/NNNNN-<bootid>/` (the last 30 boots are kept):

| File | When written | Content |
|---|---|---|
| `boot.json` | as soon as the root filesystem is writable (`orbis-blackbox.service`, before `sysinit.target`) | boot id, kernel, cmdline, loader memory contract, versions, GPU id/firmware, **verdict on the previous boot** |
| `dmesg-boot.txt` | same | kernel log up to that point |
| `flight.jsonl` | every 5 s, fsync'd | GPU busy, SCLK state, APU/CPU temperature, fan threshold, VRAM/GTT use and budget, RAM available, memory PSI |
| `kmsg-warn.log` | continuously, fsync'd | every kernel message of priority warning or higher, plus every classified message |
| `events.jsonl` | immediately | classified events: gpu_fault, ring_timeout, gpu_reset, ih_overflow, firmware, display, video, clock, lockup, oom, kernel_bug, thermal |
| `gpu-state-<t>.txt` | on gpu_fault / ring_timeout / gpu_reset / ih_overflow / kernel_bug / lockup (at most 1 per 10 s, 50 per boot) | debugfs fence/VM/SA/firmware info, liverpool clock, bridge status, GEM info, engine busy |
| `devcoredump/` | when amdgpu produces one | the GPU reset dump (copied before it expires) |
| `shutdown.json` | orderly shutdown only | its absence marks an unclean end |

The previous-boot verdict tells you how the last boot ended:
* *clean shutdown*;
* *ended without shutdown after a ring_timeout event: …*;
* *ended without shutdown and without a recorded kernel error; last telemetry at T*, which means a
  hard hang, power loss, or a panic that never reached the disk (netconsole and kdump cover that
  case);
* *ended before the Black Box started recording*: an early boot problem, so check stage-0.

journald is made persistent with a 10 s sync interval (`90-orbis-blackbox.conf`), so
`journalctl -b -1` works after an unclean end.

## Commands (`orbis-debug`, all take `--json`)

```
orbis-debug last-crash     # summary of the most recent boot that did not shut down cleanly
orbis-debug boots          # all recorded boots, clean or not
orbis-debug gpu            # live GPU telemetry + decoded registers (GRBM, VM fault status/address/client,
                           # MC FB location, CONFIG_MEMSIZE, UVD) + faults since boot
orbis-debug memory         # ownership + plan for the current mode
orbis-debug video          # decode status (hardware vs software)
sudo orbis-debug collect   # full bundle directory: report.json + report.txt, debugfs files and rings,
                           # dmesg, journal of this and the previous boot, vulkaninfo/glxinfo/eglinfo/
                           # vainfo/clinfo/drm_info, lspci, packages, receipts, last 3 Black Box boots
sudo orbis-debug export    # collect + one .tar.gz + .sha256; also copied to the USB boot
                           # partition's orbis-logs/ so it can be read on the ASUS/Nobara
```

Register reads use amdgpu's debugfs `amdgpu_regs`, the same interface AMD's UMR uses. UMR itself
has no Liverpool table, so the GFX7 offsets come from the kernel headers. All reads are read-only.

## LAN streaming to the Nobara (optional, LAN only)

On the Nobara, the receiver is shipped in the pack and in `/usr/share/orbis-graphics/tools/`:

```
sudo firewall-cmd --add-port=6666-6667/udp
./orbis-blackbox-receiver --allow <console LAN IP>
```

On the console:

```
sudo orbis-debug stream enable --to <Nobara LAN IP>
# also from early boot (needs a fixed console address):
sudo orbis-debug stream enable --to <Nobara IP> --boot --local-ip <console IP>
sudo orbis-debug stream disable [--boot]
```

* Kernel log, including panic output, travels over netconsole (UDP 6666; extended format).
* Black Box events, boot records and flight samples travel as JSON over UDP 6667.

The receiver accepts private addresses only and writes everything, fsync'd, to
`~/orbis-blackbox-received/<date>/`. No internet access is involved, so the console's LAN-only
policy holds.

## kdump (experimental, opt-in)

```
sudo orbis-debug kdump enable     # crashkernel=256M in bootargs.txt + capture initramfs
sudo systemctl enable orbis-kdump # loads the capture kernel at boot (kexec_file_load)
# reboot
orbis-debug kdump status
```

After a panic, the capture kernel boots with `nomodeset`, mounts `ORBIS-ROOT`, saves
`vmcore-dmesg` to `/var/lib/orbis-blackbox/kdump/<time>/` and reboots. x86 `kexec_file_load`
copies `hardware_subarch`, so the capture kernel still sees a PS4. Whether Aeolia/Belize USB comes
back after a panic is **untested**, and kdump costs 256 MiB of RAM while enabled.
Disable it with `orbis-debug kdump disable`.

## Design notes

* Every write goes through write-to-temp + fsync + rename, or append + fsync.
* The recorder runs with `Restart=always` and an OOM score of −900.
* `/dev/kmsg` is read as records, so a lost-record `EPIPE` is skipped rather than fatal.
* GPU-state captures are rate-limited so a VM-fault storm cannot fill the disk.
* The flight log is trimmed at 4000 lines.
