//! orbis-time: PS4 timekeeping for NickLabs Orbis.
//!
//! Four different things are often confused; this tool keeps them apart:
//!   1. hardware RTC      the southbridge's battery-backed counter, exposed by the GE kernel's
//!                        Aeolia RTC driver as /dev/rtc0 (base "hardware" once the counter
//!                        register is known, "loader" otherwise)
//!   2. Linux system clock CLOCK_REALTIME, set at boot by the kernel from the loader's time
//!                        (orbis.time= / time=) or the RTC, and here by the policy below
//!   3. PS4 secure clock   Sony's licence/trophy clock inside OrbisOS: not reachable from Linux and
//!                        never changed by anything Orbis does
//!   4. CPU/GPU clocks     operating frequencies (orbis-perf / orbis-hw), unrelated to time
//!
//! Time-source policy at boot (first valid wins, every step recorded):
//!   1. valid native RTC (hardware counter + saved/calibrated offset)
//!   2. loader-provided time (already applied by the kernel)
//!   3. persisted last-known-good timestamp (never let the clock run backwards past it)
//!   4. optional NTP from a LAN server (systemd-timesyncd, off by default: the console is LAN-only)
//!
//!   orbis-time status [--json]
//!   orbis-time boot                  (orbis-time.service, early boot)
//!   orbis-time save                  (timer + shutdown: last-known-good, RTC offset)
//!   orbis-time set <YYYY-MM-DD HH:MM[:SS]|@unix>
//!   orbis-time ntp enable --server <LAN IP/host> [--server ...] [--allow-public] | disable | status
//!   orbis-time rtc-probe [--interval 2.5] [--apply]   find the RTC counter register (boot with ps4_rtc.hw_probe=1)

use orbis_core::fsx::{self, p};
use orbis_core::json::{self, Value};
use orbis_core::{emit, jobj, sys, Args};
use std::path::PathBuf;
use std::process::Command;

const MIN_VALID: i64 = 1_577_836_800; // 2020-01-01
const MAX_VALID: i64 = 4_102_444_800; // 2100-01-01

fn state_dir() -> PathBuf {
    p("/var/lib/orbis/time")
}

fn valid(t: i64) -> bool {
    (MIN_VALID..MAX_VALID).contains(&t)
}

#[derive(Debug, Default)]
struct Rtc {
    present: bool,
    name: Option<String>,
    since_epoch: Option<i64>,
    hctosys: bool,
    base: Option<String>,    // hardware | loader | none (Orbis driver only)
    offset: Option<i64>,
    offset_from: Option<String>,
    counter_reg: Option<i64>,
    counter_hz: Option<i64>,
    hw_counter: Option<u64>,
}

fn rtc_dir() -> PathBuf {
    p("/sys/class/rtc/rtc0")
}

fn read_rtc() -> Rtc {
    let d = rtc_dir();
    if !d.exists() {
        return Rtc::default();
    }
    let mut r = Rtc {
        present: true,
        name: fsx::read(&d.join("name")),
        since_epoch: fsx::read_i64(&d.join("since_epoch")),
        hctosys: fsx::read(&d.join("hctosys")).as_deref() == Some("1"),
        ..Default::default()
    };
    if let Some(src) = fsx::read(&d.join("source")) {
        r.base = fsx::kv_get(&src, "base");
        r.offset_from = fsx::kv_get(&src, "calibrated_from").filter(|s| s != "none");
        r.counter_reg = fsx::kv_get(&src, "counter_reg").and_then(|v| v.parse().ok()).filter(|v| *v >= 0);
        r.counter_hz = fsx::kv_get(&src, "counter_hz").and_then(|v| v.parse().ok());
    }
    r.offset = fsx::read(&d.join("offset")).and_then(|v| v.parse().ok());
    r.hw_counter = fsx::read(&d.join("hw_counter")).and_then(|v| v.parse().ok());
    r
}

fn loader_time() -> Option<(String, String)> {
    for key in ["orbis.time", "time"] {
        if let Some(v) = fsx::cmdline_get(key) {
            let sec = v.split('.').next().unwrap_or("").parse::<i64>().ok();
            if sec.map(valid).unwrap_or(false) {
                return Some((key.to_string(), v));
            }
        }
    }
    None
}

fn last_good() -> Option<i64> {
    fsx::read_i64(&state_dir().join("last-good"))
}

fn saved_offset() -> Option<Value> {
    fsx::read_raw(&state_dir().join("rtc-offset.json")).and_then(|s| json::parse(&s).ok())
}

fn config() -> Value {
    fsx::read_raw(&p("/etc/orbis/time.json")).and_then(|s| json::parse(&s).ok()).unwrap_or_else(Value::obj)
}

fn timezone() -> Option<String> {
    std::fs::read_link(p("/etc/localtime"))
        .ok()
        .map(|t| t.to_string_lossy().split("zoneinfo/").last().unwrap_or("").to_string())
}

fn ntp_status() -> Value {
    let conf = fsx::read_raw(&p("/etc/systemd/timesyncd.conf.d/90-orbis.conf"));
    let servers: Vec<String> = conf
        .as_deref()
        .and_then(|c| c.lines().find_map(|l| l.strip_prefix("NTP=")))
        .map(|s| s.split_whitespace().map(str::to_string).collect())
        .unwrap_or_default();
    let synced = Command::new("timedatectl")
        .args(["show", "-p", "NTPSynchronized", "--value"])
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim() == "yes");
    jobj! {"configured_servers" => servers, "synchronized" => synced,
           "policy" => "LAN servers only unless --allow-public was given; no public fallback servers"}
}

fn source_report() -> Option<Value> {
    fsx::read_raw(&p("/run/orbis/time-source.json")).and_then(|s| json::parse(&s).ok())
}

fn status() -> Value {
    let (now, _) = sys::now_unix();
    let rtc = read_rtc();
    let rtc_v = if rtc.present {
        let rv = rtc.since_epoch.filter(|t| valid(*t));
        jobj! {
            "device" => "/dev/rtc0", "name" => rtc.name.clone(), "reads" => rtc.since_epoch,
            "valid" => rv.is_some(), "utc" => rv.map(sys::iso_utc),
            "set_system_clock_at_boot" => rtc.hctosys,
            "time_base" => rtc.base.clone().unwrap_or_else(|| "unknown (not the Orbis Aeolia driver)".into()),
            "offset" => rtc.offset, "calibrated_from" => rtc.offset_from.clone(),
            "hardware_counter" => rtc.hw_counter, "counter_reg" => rtc.counter_reg,
            "battery_backed" => rtc.base.as_deref() == Some("hardware"),
            "difference_to_system_s" => rv.map(|t| t - now)
        }
    } else {
        jobj! {"device" => Value::Null, "valid" => false,
               "why" => "no /dev/rtc0: kernel without CONFIG_RTC_CLASS / the Aeolia RTC driver (GE kernel patch 0011), or no time base"}
    };
    let lg = last_good();
    jobj! {
        "schema" => "orbis-time-status/1",
        "system_clock" => jobj! {"unix" => now, "utc" => sys::iso_utc(now), "valid" => valid(now),
                                  "timezone" => timezone(), "rtc_in_utc" => true},
        "hardware_rtc" => rtc_v,
        "loader_time" => loader_time().map(|(k, v)| format!("{k}={v}")),
        "last_known_good" => jobj! {"unix" => lg, "utc" => lg.map(sys::iso_utc)},
        "saved_rtc_offset" => saved_offset(),
        "boot_decision" => source_report(),
        "ntp" => ntp_status(),
        "ps4_secure_clock" => "OrbisOS licence/trophy clock: not accessible from Linux; unaffected by Linux time changes",
        "cpu_gpu_clocks" => "operating frequencies, not time: see orbis-perf status / orbis-hw status"
    }
}

fn set_clock(t: i64) -> Result<(), String> {
    sys::set_realtime(t, 0).map_err(|e| format!("clock_settime: {e}"))
}

fn write_rtc_offset(off: i64) -> Result<(), String> {
    fsx::write_value(&rtc_dir().join("offset"), &off.to_string()).map_err(|e| format!("rtc offset: {e}"))
}

/// The boot policy. Returns the decision record.
fn boot(dry: bool) -> Value {
    let (now, _) = sys::now_unix();
    let rtc = read_rtc();
    let lg = last_good();
    let mut steps: Vec<Value> = Vec::new();
    let mut source = "none";
    let mut final_t = now;

    // 1. native RTC
    let mut rtc_t = rtc.since_epoch.filter(|t| valid(*t));
    if rtc.base.as_deref() == Some("hardware") && rtc.offset.is_none() {
        if let Some(so) = saved_offset() {
            let reg_ok = so.get("counter_reg").and_then(Value::as_i64) == rtc.counter_reg;
            if let (Some(off), true) = (so.get("offset").and_then(Value::as_i64), reg_ok) {
                steps.push(jobj! {"step" => "restore RTC offset saved at last shutdown", "offset" => off});
                if !dry {
                    if let Err(e) = write_rtc_offset(off) {
                        steps.push(jobj! {"error" => e});
                    }
                }
                rtc_t = fsx::read_i64(&rtc_dir().join("since_epoch")).filter(|t| valid(*t));
            } else {
                steps.push(jobj! {"step" => "saved RTC offset ignored (counter register changed)"});
            }
        }
    }
    if rtc.base.as_deref() == Some("hardware") {
        if let Some(t) = rtc_t {
            source = "rtc-hardware";
            if (t - now).abs() > 2 {
                steps.push(jobj! {"step" => "set system clock from the hardware RTC", "from" => sys::iso_utc(now), "to" => sys::iso_utc(t)});
                if !dry {
                    if let Err(e) = set_clock(t) {
                        steps.push(jobj! {"error" => e});
                    }
                }
            }
            final_t = t;
        }
    }

    // 2. loader time (the kernel already applied it at timekeeping_init)
    if source == "none" {
        if let Some((k, v)) = loader_time() {
            source = "loader";
            steps.push(jobj! {"step" => "kernel started the clock from the loader time", "param" => format!("{k}={v}")});
        } else if rtc.base.as_deref() == Some("loader") && rtc_t.is_some() {
            source = "loader";
        }
    }

    // 3. last-known-good: never earlier than the last time we knew was right
    if let Some(lg) = lg {
        if final_t < lg {
            if source == "none" {
                source = "last-known-good";
                steps.push(jobj! {"step" => "no RTC or loader time: clock set to the last known good time (may be behind)",
                                  "to" => sys::iso_utc(lg)});
                if !dry {
                    if let Err(e) = set_clock(lg) {
                        steps.push(jobj! {"error" => e});
                    }
                }
                final_t = lg;
            } else {
                steps.push(jobj! {"warning" => format!("{source} time is {} s earlier than the last shutdown; kept (higher priority) but suspicious",
                                                      lg - final_t)});
            }
        }
    }
    if source == "none" && valid(now) {
        source = "system";
        steps.push(jobj! {"step" => "clock already valid (set before orbis-time ran)"});
    }

    // 4. NTP is a separate, optional service (configured with `orbis-time ntp`)
    let ntp = ntp_status();
    let rec = jobj! {
        "schema" => "orbis-time-boot/1", "decided_at" => sys::iso_utc(final_t), "source" => source,
        "valid" => valid(final_t), "steps" => Value::Arr(steps), "ntp_configured" => ntp.get("configured_servers").cloned(),
        "rtc_base" => rtc.base.clone(), "dry_run" => dry
    };
    if !dry {
        let _ = fsx::atomic_write(&p("/run/orbis/time-source.json"), rec.to_string_pretty().as_bytes());
        let _ = fsx::append_line(&p("/var/log/orbis/time.jsonl"), &rec.to_string_compact());
    }
    rec
}

fn save() -> Value {
    let (now, _) = sys::now_unix();
    let mut out = jobj! {"time" => sys::iso_utc(now)};
    if valid(now) {
        // never move last-good backwards
        let keep = last_good().map(|lg| lg > now).unwrap_or(false);
        if !keep {
            let ok = fsx::atomic_write(&state_dir().join("last-good"), format!("{now}\n").as_bytes()).is_ok();
            out.set("last_good_saved", ok);
        } else {
            out.set("last_good_saved", false).set("why", "system clock is behind the saved last-good time");
        }
    } else {
        out.set("last_good_saved", false).set("why", "system clock not valid");
    }
    let rtc = read_rtc();
    if rtc.base.as_deref() == Some("hardware") {
        if let (Some(off), Some(reg)) = (rtc.offset, rtc.counter_reg) {
            let doc = jobj! {"schema" => "orbis-rtc-offset/1", "offset" => off, "counter_reg" => reg,
                             "counter_hz" => rtc.counter_hz, "hw_counter" => rtc.hw_counter, "saved_at" => now};
            out.set("rtc_offset_saved", fsx::atomic_write(&state_dir().join("rtc-offset.json"), doc.to_string_pretty().as_bytes()).is_ok());
        }
    }
    out
}

fn is_private(host: &str) -> Option<bool> {
    let ip: std::net::IpAddr = host.parse().ok()?;
    Some(match ip {
        std::net::IpAddr::V4(v) => v.is_private() || v.is_loopback() || v.is_link_local(),
        std::net::IpAddr::V6(v) => v.is_loopback() || (v.segments()[0] & 0xfe00) == 0xfc00 || (v.segments()[0] & 0xffc0) == 0xfe80,
    })
}

fn ntp(a: &Args) -> Result<Value, String> {
    let action = a.pos.get(1).map(String::as_str).unwrap_or("status");
    let dropin = p("/etc/systemd/timesyncd.conf.d/90-orbis.conf");
    match action {
        "status" => Ok(ntp_status()),
        "enable" => {
            let servers = a.all("server");
            if servers.is_empty() {
                return Err("--server <LAN NTP server> is required (e.g. the Nobara running chronyd)".into());
            }
            for s in &servers {
                match is_private(s) {
                    Some(false) if !a.flag("allow-public") => {
                        return Err(format!("{s} is a public address; the console is LAN-only (use --allow-public to override)"))
                    }
                    None if !a.flag("allow-public") => eprintln!("orbis-time: {s} is a name; make sure it resolves to a LAN host"),
                    _ => {}
                }
            }
            let body = format!(
                "# NickLabs Orbis (orbis-time ntp): LAN time server only, no public fallback\n[Time]\nNTP={}\nFallbackNTP=\n",
                servers.join(" ")
            );
            fsx::atomic_write(&dropin, body.as_bytes()).map_err(|e| e.to_string())?;
            let r = Command::new("systemctl").args(["enable", "--now", "systemd-timesyncd.service"]).status();
            Ok(jobj! {"enabled" => true, "servers" => servers, "file" => dropin.display().to_string(),
                      "systemctl" => r.map(|s| s.success()).unwrap_or(false)})
        }
        "disable" => {
            let _ = std::fs::remove_file(&dropin);
            let r = Command::new("systemctl").args(["disable", "--now", "systemd-timesyncd.service"]).status();
            Ok(jobj! {"enabled" => false, "systemctl" => r.map(|s| s.success()).unwrap_or(false)})
        }
        _ => Err(format!("unknown ntp action {action}")),
    }
}

/// Parse "000: 0000abcd 00000000 ..." lines from debugfs ps4_rtc/window.
fn parse_window(text: &str) -> (Option<u64>, Vec<(usize, u32)>) {
    let mut ns = None;
    let mut words = Vec::new();
    for line in text.lines() {
        if line.starts_with('#') {
            ns = line.split("ns=").nth(1).and_then(|v| v.trim().parse().ok());
            continue;
        }
        if let Some((off, rest)) = line.split_once(':') {
            if let Ok(base) = usize::from_str_radix(off.trim(), 16) {
                for (i, w) in rest.split_whitespace().enumerate() {
                    if let Ok(v) = u32::from_str_radix(w, 16) {
                        words.push((base + i * 4, v));
                    }
                }
            }
        }
    }
    (ns, words)
}

/// Words whose increase matches a clock rate; returns (offset, rate_hz_estimate, value).
fn counter_candidates(a: &[(usize, u32)], b: &[(usize, u32)], secs: f64) -> Vec<(usize, f64, u32)> {
    let mut out = Vec::new();
    for ((oa, va), (ob, vb)) in a.iter().zip(b.iter()) {
        if oa != ob || vb <= va || secs <= 0.0 {
            continue;
        }
        let rate = (vb - va) as f64 / secs;
        for hz in [1.0f64, 2.0, 4.0, 128.0, 1024.0, 32768.0, 1_000_000.0] {
            if (rate - hz).abs() <= (hz * 0.15).max(0.5) {
                out.push((*oa, hz, *vb));
            }
        }
    }
    out
}

fn rtc_probe(a: &Args) -> Result<Value, String> {
    let win = p("/sys/kernel/debug/ps4_rtc/window");
    if !win.exists() {
        return Err("debugfs ps4_rtc/window not present: boot the GE kernel with ps4_rtc.hw_probe=1 in bootargs.txt \
                    (reads the undocumented Aeolia RTC registers, read-only), mount debugfs, run as root"
            .into());
    }
    let interval: f64 = a.get("interval").and_then(|v| v.parse().ok()).unwrap_or(2.5);
    let t0 = std::time::Instant::now();
    let s1 = fsx::read_raw(&win).ok_or("cannot read window")?;
    std::thread::sleep(std::time::Duration::from_secs_f64(interval));
    let s2 = fsx::read_raw(&win).ok_or("cannot read window")?;
    let el = t0.elapsed().as_secs_f64();
    let (n1, w1) = parse_window(&s1);
    let (n2, w2) = parse_window(&s2);
    let secs = match (n1, n2) {
        (Some(a), Some(b)) if b > a => (b - a) as f64 / 1e9,
        _ => el,
    };
    let cands = counter_candidates(&w1, &w2, secs);
    let changed: Vec<Value> = w1
        .iter()
        .zip(w2.iter())
        .filter(|(x, y)| x.1 != y.1)
        .map(|(x, y)| jobj! {"offset" => format!("0x{:03x}", x.0), "before" => format!("0x{:08x}", x.1), "after" => format!("0x{:08x}", y.1)})
        .collect();
    let mut out = jobj! {"interval_s" => secs, "changed_words" => Value::Arr(changed),
        "candidates" => Value::Arr(cands.iter().map(|(o, hz, v)| jobj! {"counter_reg" => format!("0x{o:x}"), "counter_hz" => *hz as i64, "value" => *v as i64}).collect())};
    if cands.len() == 1 {
        let (o, hz, _) = cands[0];
        let args = format!("ps4_rtc.hw_probe=1 ps4_rtc.counter_reg=0x{o:x} ps4_rtc.counter_hz={}", hz as i64);
        out.set("suggested_bootargs", args.clone());
        if a.flag("apply") {
            let f = fsx::boot_dir().join("bootargs.txt");
            let cur = fsx::read(&f).unwrap_or_default();
            let mut toks: Vec<String> = cur.split_whitespace().filter(|t| !t.starts_with("ps4_rtc.")).map(str::to_string).collect();
            toks.extend(args.split_whitespace().map(str::to_string));
            fsx::atomic_write(&f.with_file_name("bootargs.txt.orbis-prev"), format!("{cur}\n").as_bytes()).map_err(|e| e.to_string())?;
            fsx::atomic_write(&f, format!("{}\n", toks.join(" ")).as_bytes()).map_err(|e| e.to_string())?;
            out.set("applied_to", f.display().to_string());
        }
    } else {
        out.set("note", if cands.is_empty() {
            "no register advanced like a clock: the counter may not be in the first probe_len bytes (try ps4_rtc.probe_len=4096)"
        } else {
            "several candidates: run again; keep the one that advances consistently"
        });
    }
    Ok(out)
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let a = Args::parse(&argv, &["json", "dry-run", "apply", "allow-public"]);
    let cmd = a.pos.first().map(String::as_str).unwrap_or("status");
    let res: Result<Value, String> = match cmd {
        "status" => Ok(status()),
        "boot" => Ok(boot(a.flag("dry-run"))),
        "save" => Ok(save()),
        "set" => (|| {
            let s = a.pos.get(1).ok_or("usage: orbis-time set <YYYY-MM-DD HH:MM[:SS]|@unix> (UTC)")?;
            let t = sys::parse_utc(s).ok_or("cannot parse the time (UTC: 2026-10-26 21:00:00 or @1793048400)")?;
            if !valid(t) {
                return Err("time outside 2020..2100".to_string());
            }
            set_clock(t)?;
            // keep the RTC in step (hwclock talks to /dev/rtc0; the Orbis driver only moves its offset)
            let hw = Command::new("hwclock").args(["--systohc", "--utc"]).status().map(|s| s.success()).unwrap_or(false);
            Ok(jobj! {"set" => sys::iso_utc(t), "rtc_updated" => hw, "saved" => save()})
        })(),
        "ntp" => ntp(&a),
        "rtc-probe" => rtc_probe(&a),
        "-h" | "--help" | "help" => {
            println!("orbis-time status|boot|save|set|ntp|rtc-probe [--json]  (see `head` of the source docs)");
            return;
        }
        other => Err(format!("unknown command {other}")),
    };
    let _ = config(); // reserved: /etc/orbis/time.json
    match res {
        Ok(v) => emit(&v, a.flag("json")),
        Err(e) => {
            eprintln!("orbis-time: {e}");
            std::process::exit(2);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn window_and_candidates() {
        let a = "# Aeolia RTC window (BAR2+0x0), 32 bytes, read-only, ns=1000000000\n000: 12f57fc1 00000000 000001fc 00000005\n010: 00000000 00010000 00000000 00000000\n";
        let b = "# Aeolia RTC window (BAR2+0x0), 32 bytes, read-only, ns=3500000000\n000: 12f57fc3 00000000 000001fc 00000006\n010: 00000000 00023fff 00000000 00000000\n";
        let (n1, w1) = parse_window(a);
        let (n2, w2) = parse_window(b);
        assert_eq!((n1, n2), (Some(1_000_000_000), Some(3_500_000_000)));
        let c = counter_candidates(&w1, &w2, 2.5);
        assert!(c.iter().any(|(o, hz, _)| *o == 0 && *hz == 1.0), "{c:?}");
        assert!(c.iter().any(|(o, hz, _)| *o == 0x14 && *hz == 32768.0), "{c:?}");
        assert!(!c.iter().any(|(o, _, _)| *o == 8 || *o == 0xc));
    }

    #[test]
    fn private_addresses() {
        assert_eq!(is_private("192.168.1.10"), Some(true));
        assert_eq!(is_private("8.8.8.8"), Some(false));
        assert_eq!(is_private("fd00::1"), Some(true));
        assert_eq!(is_private("nobara.lan"), None);
    }
}
