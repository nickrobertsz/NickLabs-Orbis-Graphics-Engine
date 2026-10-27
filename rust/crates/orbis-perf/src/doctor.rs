//! `orbis-perf doctor`: on-device checks for everything the native components depend on.
//! Complements Orbis 0.2.0's `orbis doctor` (left unchanged). Every check reports what it saw;
//! UNSUPPORTED means the running kernel/userspace lacks the feature, not that something broke.

use crate::sched;
use orbis_core::fsx::{self, p};
use orbis_core::{jobj, Value};

struct Check {
    id: &'static str,
    state: &'static str, // PASS | WARN | FAIL | UNSUPPORTED
    detail: String,
    fix: Option<&'static str>,
}

fn c(id: &'static str, state: &'static str, detail: impl Into<String>, fix: Option<&'static str>) -> Check {
    Check { id, state, detail: detail.into(), fix }
}

fn running(comms: &[&str]) -> Vec<String> {
    let mut out = vec![];
    for pid in orbis_core::procs::pids() {
        if let Some(cm) = fsx::read(&p(&format!("/proc/{pid}/comm"))) {
            if comms.contains(&cm.as_str()) && !out.contains(&cm) {
                out.push(cm);
            }
        }
    }
    out
}

pub fn cpu_flags() -> Vec<String> {
    fsx::read_raw(&p("/proc/cpuinfo"))
        .and_then(|t| t.lines().find(|l| l.starts_with("flags")).map(|l| l.split(':').nth(1).unwrap_or("").to_string()))
        .map(|f| f.split_whitespace().map(str::to_string).collect())
        .unwrap_or_default()
}

fn checks(now: i64) -> Vec<Check> {
    let mut v = vec![];

    // CPU: Jaguar has AVX but not AVX2/FMA/BMI2; anything built for x86-64-v3 dies with SIGILL
    let fl = cpu_flags();
    let has = |f: &str| fl.iter().any(|x| x == f);
    v.push(if fl.is_empty() {
        c("cpu.isa", "WARN", "no /proc/cpuinfo flags", None)
    } else if has("avx2") {
        c("cpu.isa", "PASS", "CPU has AVX2 (not a PS4 Jaguar; v3 binaries would run here but not on the console)", None)
    } else {
        c("cpu.isa", "PASS", format!("x86-64-v2 + AVX{}: Jaguar-class; only btver2/v2 binaries are safe",
            if has("avx") { "" } else { " (AVX missing?)" }), None)
    });

    // kernel features (GE kernel: orbis-perf.fragment)
    let feat = |id, path: &str, what: &str| {
        if p(path).exists() {
            c(id, "PASS", format!("{what}: {path}"), None)
        } else {
            c(id, "UNSUPPORTED", format!("{what} missing ({path})"), Some("boot the GE kernel (linux-orbis-ps4-ge)"))
        }
    };
    v.push(feat("kernel.sched_ext", "/sys/kernel/sched_ext/state", "sched_ext"));
    v.push(feat("kernel.mglru", "/sys/kernel/mm/lru_gen/enabled", "MGLRU"));
    v.push(feat("kernel.psi", "/proc/pressure/memory", "PSI"));
    v.push(feat("kernel.ntsync", "/dev/ntsync", "NTSYNC (Wine 10+/Proton fast sync)"));
    v.push(feat("kernel.rtc", "/dev/rtc0", "RTC (Aeolia RTC driver)"));
    let ctrl = fsx::read(&p("/sys/fs/cgroup/cgroup.controllers")).unwrap_or_default();
    for (id, name) in [("cgroup.cpuset", "cpuset"), ("cgroup.dmem", "dmem")] {
        v.push(if ctrl.split_whitespace().any(|x| x == name) {
            c(id, "PASS", format!("cgroup v2 controller {name}"), None)
        } else {
            c(id, "UNSUPPORTED", format!("cgroup v2 controller {name} not available"), Some("boot the GE kernel"))
        });
    }

    // schedulers
    let avail = sched::available();
    v.push(if !sched::sched_ext_supported() {
        c("sched.scx", "UNSUPPORTED", "kernel has no sched_ext: EEVDF only", None)
    } else if avail.is_empty() {
        c("sched.scx", "WARN", "sched_ext supported but no scx_* scheduler installed", Some("pacman -S scx-scheds"))
    } else {
        c("sched.scx", "PASS", format!("active {} ; installed {}", sched::active(), avail.join(", ")), None)
    });

    // one manager only
    let rivals = running(&["falcond", "scx_loader", "tuned", "auto-cpufreq", "system76-power"]);
    v.push(if rivals.is_empty() {
        c("managers.single", "PASS", "no competing performance manager running", None)
    } else {
        c("managers.single", "FAIL", format!("running alongside orbis-perf: {}", rivals.join(", ")),
          Some("disable them; orbis-perf owns scheduler/THP/IO/IRQ/clock policy"))
    });
    if !running(&["gamemoded"]).is_empty() {
        v.push(c("managers.gamemode", "WARN", "gamemoded is running: orbis-perf leaves the governor to it during its sessions", None));
    }
    let wants: Vec<String> = ["pc", "gaming", "ai"].iter()
        .filter(|m| p(&format!("/etc/systemd/system/orbis-{m}.target.wants/orbis-gpu-profile@{m}.service")).exists())
        .map(|m| m.to_string()).collect();
    if !wants.is_empty() {
        v.push(c("managers.gpu_profile_units", "WARN", format!("1.0 orbis-gpu-profile@ still wanted by: {}", wants.join(", ")),
                 Some("reinstall orbis-native (its install hook retires them)")));
    }

    // daemon
    let st = fsx::read_raw(&p("/run/orbis/perf/state.json")).and_then(|t| orbis_core::json::parse(&t).ok());
    let age = st.as_ref().and_then(|s| s.get("time").and_then(Value::as_i64)).map(|t| now - t);
    v.push(match age {
        Some(a) if a <= 15 => c("perf.daemon", "PASS", format!("orbis-perf daemon state {a} s old"), None),
        Some(a) => c("perf.daemon", "FAIL", format!("orbis-perf state is {a} s old (daemon stopped?)"), Some("systemctl status orbis-perf")),
        None => c("perf.daemon", "FAIL", "orbis-perf daemon has not written /run/orbis/perf/state.json", Some("systemctl enable --now orbis-perf")),
    });

    // time
    let ts = fsx::read_raw(&p("/run/orbis/time-source.json")).and_then(|t| orbis_core::json::parse(&t).ok());
    let src = ts.as_ref().and_then(|t| t.get("source").and_then(Value::as_str).map(str::to_string));
    v.push(if now < 1_577_836_800 {
        c("time.valid", "FAIL", format!("system clock is before 2020 ({})", orbis_core::sys::iso_utc(now)),
          Some("orbis-time set 'YYYY-MM-DD HH:MM' or orbis-time ntp enable --server <LAN host>"))
    } else {
        c("time.valid", "PASS", format!("{} (source: {})", orbis_core::sys::iso_utc(now), src.unwrap_or_else(|| "not recorded: orbis-time.service".into())), None)
    });

    // Vulkan / games
    let caps = fsx::read_raw(&p("/var/lib/orbis/games/vulkan-caps.json")).and_then(|t| orbis_core::json::parse(&t).ok());
    v.push(match caps {
        None => c("vulkan.caps", "WARN", "never probed", Some("sudo orbis-games caps --save")),
        Some(cp) if cp.at("device.software").and_then(Value::as_bool) == Some(true) =>
            c("vulkan.caps", "FAIL", format!("software Vulkan only ({}): games would run on the CPU",
              cp.at("device.name").and_then(Value::as_str).unwrap_or("?")), Some("orbis-gpu qualify")),
        Some(cp) => c("vulkan.caps", "PASS", format!("{} Vulkan {} -> DXVK line {}",
            cp.at("device.name").and_then(Value::as_str).unwrap_or("?"), cp.at("device.api_version").and_then(Value::as_str).unwrap_or("?"),
            cp.at("selection.dxvk.line").and_then(Value::as_str).unwrap_or("?")), None),
    });
    let userns = fsx::read_i64(&p("/proc/sys/user/max_user_namespaces")).unwrap_or(0) > 0
        && fsx::read_i64(&p("/proc/sys/kernel/unprivileged_userns_clone")).unwrap_or(1) == 1;
    v.push(if userns {
        c("games.userns", "PASS", "unprivileged user namespaces available (Steam Linux Runtime / pressure-vessel)", None)
    } else {
        c("games.userns", "FAIL", "unprivileged user namespaces disabled: Proton's runtime cannot start",
          Some("sysctl kernel.unprivileged_userns_clone=1"))
    });
    let mmc = fsx::read_i64(&p("/proc/sys/vm/max_map_count")).unwrap_or(0);
    v.push(if mmc >= 1_048_576 {
        c("games.max_map_count", "PASS", format!("vm.max_map_count {mmc}"), None)
    } else {
        c("games.max_map_count", "WARN", format!("vm.max_map_count {mmc} (some games need >= 1048576; orbis-perf apply sets it)"), None)
    });
    v.push(if p("/usr/share/orbis/games/runners.json").exists() {
        c("games.catalog", "PASS", "runner catalog installed", None)
    } else {
        c("games.catalog", "FAIL", "no /usr/share/orbis/games/runners.json", Some("reinstall orbis-native"))
    });
    v
}

pub fn run() -> (Value, bool) {
    let now = orbis_core::sys::now_unix().0;
    let cs = checks(now);
    let failed = cs.iter().any(|x| x.state == "FAIL");
    let summary = if failed { "FAIL" } else if cs.iter().any(|x| x.state == "WARN" || x.state == "UNSUPPORTED") { "DEGRADED" } else { "PASS" };
    (jobj! {
        "schema" => "orbis-perf-doctor/1", "result" => summary,
        "note" => "Orbis 0.2.0 checks: `orbis doctor` (unchanged). This covers the native components.",
        "checks" => Value::Arr(cs.into_iter().map(|x| jobj! {"id" => x.id, "state" => x.state, "detail" => x.detail, "fix" => x.fix}).collect())
    }, failed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn doctor_on_fixture() {
        let _g = crate::TEST_ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let root = std::env::temp_dir().join(format!("orbis-doctor-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let w = |rel: &str, s: &str| {
            let f = root.join(rel);
            std::fs::create_dir_all(f.parent().unwrap()).unwrap();
            std::fs::write(f, s).unwrap();
        };
        w("proc/cpuinfo", "processor\t: 0\nflags\t\t: fpu sse4_2 avx f16c bmi1 movbe aes\n");
        w("sys/fs/cgroup/cgroup.controllers", "cpu io memory pids\n");
        w("proc/sys/user/max_user_namespaces", "63000\n");
        w("proc/sys/vm/max_map_count", "65530\n");
        w("var/lib/orbis/games/vulkan-caps.json", r#"{"device":{"name":"llvmpipe (LLVM 21)","software":true,"api_version":"1.4"}}"#);
        std::env::set_var("ORBIS_ROOT", &root);
        let (v, failed) = run();
        std::env::remove_var("ORBIS_ROOT");
        let _ = std::fs::remove_dir_all(&root);
        assert!(failed);
        let st = |id: &str| v.get("checks").unwrap().as_arr().unwrap().iter()
            .find(|c| c.get("id").and_then(Value::as_str) == Some(id)).and_then(|c| c.get("state").and_then(Value::as_str).map(str::to_string));
        assert_eq!(st("cpu.isa").as_deref(), Some("PASS"));
        assert_eq!(st("kernel.sched_ext").as_deref(), Some("UNSUPPORTED"));
        assert_eq!(st("cgroup.dmem").as_deref(), Some("UNSUPPORTED"));
        assert_eq!(st("vulkan.caps").as_deref(), Some("FAIL")); // software rendering is never PASS
        assert_eq!(st("perf.daemon").as_deref(), Some("FAIL"));
        assert_eq!(st("games.max_map_count").as_deref(), Some("WARN"));
        assert_eq!(st("games.userns").as_deref(), Some("PASS"));
    }
}
