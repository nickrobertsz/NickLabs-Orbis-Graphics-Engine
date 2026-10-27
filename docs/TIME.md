# NickLabs Orbis — PS4 time and RTC

Before the Graphics Engine, every Orbis boot started at 1 January 1970. The PS4 platform code
(`arch/x86/platform/ps4/ps4.c`) returned 0 for the wall clock, and no RTC driver existed. This
breaks TLS certificates, `make`, package signature dates, logs, the Black Box and Steam.

## Four different clocks (kept apart)

| | What | Who sets it |
|---|---|---|
| Hardware RTC | Aeolia southbridge battery-backed counter (BAR2 region at 0x0, size 0x1000, MSI sub-function 13) | the GE kernel driver `ps4-apcie-rtc` exposes it as `/dev/rtc0` |
| Linux system clock | `CLOCK_REALTIME` | the kernel at boot (loader time / RTC), then `orbis-time boot` |
| PS4 secure clock | Sony's licence/trophy clock inside OrbisOS | **nobody here**: not reachable from Linux and never changed by Orbis |
| CPU/GPU clocks | operating frequencies | `orbis-perf` / `orbis-hw`; unrelated to time |

## Boot policy (first valid source wins; every step is recorded)

1. **Native RTC.** This is used when the driver's base is `hardware`: the counter register is
   known and a calendar offset was calibrated or restored from
   `/var/lib/orbis/time/rtc-offset.json`.
2. **Loader time.**
   * The GE loader patch reads `clock_gettime(CLOCK_REALTIME)` and `rdtsc` in OrbisOS just before
     kexec, then appends `orbis.time=<sec.nsec> orbis.time_tsc=<tsc>` to the command line.
   * Kernel patch ge-0010 parses the value, accepting only 2020..2100. It adds the TSC-measured
     time spent in kexec (at most 900 s) and starts the clock with it at `timekeeping_init`.
   * `time=<epoch>` in `bootargs.txt` works the same way without the loader patch.
   * The RTC driver then serves this time as base `loader` (`/dev/rtc0` reads base + offset).
3. **Last-known-good.** `orbis-time save` runs every 15 minutes (timer) and at shutdown. At boot
   the clock is never left earlier than the saved value. If no better source exists, the clock is
   set to it, and the record says it may be behind.
4. **Optional NTP from a LAN server.** This is off by default.
   `sudo orbis-time ntp enable --server <Nobara IP>` writes a timesyncd drop-in with that server
   and an empty `FallbackNTP=`. Public addresses are refused unless `--allow-public` is given.

The decision goes to `/run/orbis/time-source.json` and `/var/log/orbis/time.jsonl`, and the Black
Box boot record copies it (`clock.source`). `orbis-blackbox` starts after `orbis-time`, so each
boot record has the right date.

## Finding the hardware counter (on the console, read-only)

The Aeolia RTC register layout is undocumented. The EMC "rtc" command reports a free-running
counter. Reading the region is opt-in, because `apcie_uart_init` is known to hang on some
accesses:

```
# bootargs.txt: add  ps4_rtc.hw_probe=1
sudo mount -t debugfs none /sys/kernel/debug     # if not mounted
sudo orbis-time rtc-probe --interval 2.5         # finds words that advance at ~1 Hz (or a stated rate)
sudo orbis-time rtc-probe --apply                # writes ps4_rtc.counter_reg=/counter_hz= into bootargs.txt
# reboot; with a valid loader/NTP time once, orbis-time save stores the offset; later boots use it
```

Until this has been done on a CUH-1215A, the native RTC calendar is **BLOCKED**: there is no
known register. The loader time covers every normal boot from OrbisOS.

## Commands

```
orbis-time status [--json]    # RTC, system clock, last-known-good, saved offset, boot decision, NTP
sudo orbis-time set "2026-10-26 21:09"   # UTC; also moves the RTC offset (hardware base only)
sudo orbis-time ntp enable --server 192.168.1.10 | disable | status
```

## Status

| Item | Status |
|---|---|
| Kernel wall clock from loader time (ge-0010) | UNVERIFIED (applies); epoch parser HOST-VERIFIED |
| Aeolia RTC class driver (ge-0011) | UNVERIFIED (applies); counter register unknown |
| Loader time handoff | HOST-VERIFIED (cmdline composition); payload build runs on the Nobara |
| `orbis-time` boot/save/set/ntp/probe | BUILT + HOST-VERIFIED (fixture tests, including source order) |
| Units `orbis-time.service`, `orbis-time-save.timer` | BUILT (syntax verified with systemd-analyze) |
