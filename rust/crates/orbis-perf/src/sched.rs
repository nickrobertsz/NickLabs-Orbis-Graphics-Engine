//! CPU scheduler management: EEVDF (kernel default), the BORE toggle on a -bore kernel, and
//! sched_ext BPF schedulers (scx_lavd, scx_bpfland, scx_rusty, scx_flash, ... from scx-scheds,
//! themselves written in Rust). A sched_ext scheduler runs as `orbis-scx@<name>.service`; if it
//! fails (the kernel ejects a misbehaving BPF scheduler and falls back to EEVDF by itself) the
//! controller records the failure, stops retrying after two attempts this boot and stays on
//! EEVDF. The Jaguar topology (two 4-core modules sharing an L2) is passed to schedulers that
//! need it via their argument files.

use orbis_core::fsx::{self, p};
use orbis_core::{jobj, Value};
use std::process::Command;
use std::time::{Duration, Instant};

pub const KNOWN: [&str; 7] = ["scx_lavd", "scx_bpfland", "scx_rusty", "scx_flash", "scx_p2dq", "scx_cosmos", "scx_simple"];

pub fn sched_ext_supported() -> bool {
    p("/sys/kernel/sched_ext").exists()
}

pub fn bore_available() -> bool {
    p("/proc/sys/kernel/sched_bore").exists()
}

/// sched_ext state: ("enabled"|"disabled"|..., active ops name)
pub fn scx_state() -> (Option<String>, Option<String>) {
    (fsx::read(&p("/sys/kernel/sched_ext/state")), fsx::read(&p("/sys/kernel/sched_ext/root/ops")).filter(|s| !s.is_empty()))
}

pub fn scx_binary(name: &str) -> Option<std::path::PathBuf> {
    [format!("/usr/bin/{name}"), format!("/usr/local/bin/{name}")]
        .into_iter()
        .map(|f| p(&f))
        .find(|f| f.exists())
}

pub fn available() -> Vec<String> {
    let mut v = vec!["eevdf".to_string()];
    if bore_available() {
        v.push("bore".into());
    }
    if sched_ext_supported() {
        v.extend(KNOWN.iter().filter(|n| scx_binary(n).is_some()).map(|n| n.to_string()));
    }
    v
}

/// What is running now: a sched_ext scheduler's name, "bore", or "eevdf".
pub fn active() -> String {
    if let (Some(st), Some(ops)) = scx_state() {
        if st == "enabled" {
            // ops names look like "lavd_1.0.x" or "bpfland"; map to the binary name
            let base = ops.split(['_', '-']).next().unwrap_or(&ops).to_string();
            return format!("scx_{base}");
        }
    }
    if fsx::read(&p("/proc/sys/kernel/sched_bore")).as_deref() == Some("1") {
        return "bore".into();
    }
    "eevdf".into()
}

/// Arguments for a scheduler: /etc/orbis/perf/scx/<name>.args (one per line) or Orbis defaults.
pub fn scx_args(name: &str, extra: &[String]) -> Vec<String> {
    if let Some(t) = fsx::read_raw(&p(&format!("/etc/orbis/perf/scx/{name}.args"))) {
        return t.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')).map(str::to_string).collect();
    }
    if !extra.is_empty() {
        return extra.to_vec();
    }
    match name {
        // latency-criticality aware; --performance pins it to the fast path (no power saving on a console)
        "scx_lavd" => vec!["--performance".into()],
        _ => vec![],
    }
}

fn systemctl(args: &[&str]) -> Result<(), String> {
    if std::env::var_os("ORBIS_ROOT").is_some() {
        return Ok(()); // fixture tree: no systemd
    }
    match Command::new("systemctl").args(args).status() {
        Ok(s) if s.success() => Ok(()),
        Ok(s) => Err(format!("systemctl {} -> {s}", args.join(" "))),
        Err(e) => Err(e.to_string()),
    }
}

fn stop_all_scx() {
    let _ = systemctl(&["stop", "orbis-scx@*.service"]);
}

pub struct Failures {
    pub counts: std::collections::BTreeMap<String, u32>,
}

impl Failures {
    pub fn load() -> Failures {
        let counts = fsx::read_raw(&p("/run/orbis/perf/scx-failures.json"))
            .and_then(|t| orbis_core::json::parse(&t).ok())
            .and_then(|v| v.as_obj().map(|m| m.iter().filter_map(|(k, v)| v.as_i64().map(|n| (k.clone(), n as u32))).collect()))
            .unwrap_or_default();
        Failures { counts }
    }
    pub fn save(&self) {
        let mut m = Value::obj();
        for (k, v) in &self.counts {
            m.set(k, *v as i64);
        }
        let _ = fsx::atomic_write(&p("/run/orbis/perf/scx-failures.json"), m.to_string_compact().as_bytes());
    }
    pub fn given_up(&self, name: &str) -> bool {
        self.counts.get(name).copied().unwrap_or(0) >= 2
    }
}

/// Switch to `want`. Returns an action record. Falls back to EEVDF on failure.
pub fn switch(want: &str, extra_args: &[String], fails: &mut Failures) -> Value {
    let cur = active();
    if cur == want {
        return jobj! {"knob" => "scheduler", "state" => "UNCHANGED", "detail" => want};
    }
    match want {
        "eevdf" | "bore" => {
            stop_all_scx();
            if bore_available() {
                let v = if want == "bore" { "1" } else { "0" };
                if let Err(e) = fsx::write_value(&p("/proc/sys/kernel/sched_bore"), v) {
                    return jobj! {"knob" => "scheduler", "state" => "FAILED", "detail" => format!("sched_bore: {e}")};
                }
            } else if want == "bore" {
                return jobj! {"knob" => "scheduler", "state" => "UNSUPPORTED",
                              "detail" => "kernel without BORE: install/boot linux-orbis-ps4-ge-bore"};
            }
            jobj! {"knob" => "scheduler", "state" => "APPLIED", "detail" => format!("{cur} -> {want}")}
        }
        name if name.starts_with("scx_") => {
            if !sched_ext_supported() {
                return jobj! {"knob" => "scheduler", "state" => "UNSUPPORTED",
                              "detail" => "kernel without CONFIG_SCHED_CLASS_EXT (GE perf fragment)"};
            }
            if scx_binary(name).is_none() {
                return jobj! {"knob" => "scheduler", "state" => "UNSUPPORTED", "detail" => format!("{name} not installed (scx-scheds)")};
            }
            if fails.given_up(name) {
                return jobj! {"knob" => "scheduler", "state" => "BLOCKED",
                              "detail" => format!("{name} failed twice this boot; staying on EEVDF")};
            }
            stop_all_scx();
            if let Err(e) = systemctl(&["start", &format!("orbis-scx@{name}.service")]) {
                *fails.counts.entry(name.to_string()).or_default() += 1;
                fails.save();
                return jobj! {"knob" => "scheduler", "state" => "FAILED", "detail" => e};
            }
            let _ = extra_args; // arguments are read by `orbis-perf scx-exec` from the same config
            // wait for the kernel to report the BPF scheduler as enabled
            let t0 = Instant::now();
            while t0.elapsed() < Duration::from_secs(8) {
                if active() == name || std::env::var_os("ORBIS_ROOT").is_some() {
                    return jobj! {"knob" => "scheduler", "state" => "APPLIED", "detail" => format!("{cur} -> {name}")};
                }
                std::thread::sleep(Duration::from_millis(250));
            }
            *fails.counts.entry(name.to_string()).or_default() += 1;
            fails.save();
            stop_all_scx();
            jobj! {"knob" => "scheduler", "state" => "FAILED",
                   "detail" => format!("{name} did not attach within 8 s (sched_ext state {:?}); back on EEVDF", scx_state().0)}
        }
        other => jobj! {"knob" => "scheduler", "state" => "REJECTED", "detail" => format!("unknown scheduler {other}")},
    }
}

/// Supervision tick: a scx scheduler that should run but was ejected counts as a failure.
pub fn check(desired: &str, fails: &mut Failures) -> Option<Value> {
    if !desired.starts_with("scx_") || fails.given_up(desired) || active() == desired {
        return None;
    }
    *fails.counts.entry(desired.to_string()).or_default() += 1;
    fails.save();
    Some(jobj! {"event" => "scheduler-ejected", "scheduler" => desired,
                "failures" => fails.counts.get(desired).copied().unwrap_or(0) as i64,
                "kernel_state" => scx_state().0})
}

/// "auto": the measured winner for a workload from `orbis-perf bench`, else EEVDF.
pub fn resolve_auto(workload: &str) -> String {
    fsx::read_raw(&p("/var/lib/orbis/perf/bench.json"))
        .and_then(|t| orbis_core::json::parse(&t).ok())
        .and_then(|v| v.at(&format!("recommendation.{workload}")).and_then(Value::as_str).map(str::to_string))
        .filter(|s| available().contains(s))
        .unwrap_or_else(|| "eevdf".into())
}
