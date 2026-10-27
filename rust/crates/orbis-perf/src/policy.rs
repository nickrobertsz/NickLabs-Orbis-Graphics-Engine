//! Mode policies (PC / GAMING / AI), per-game profiles and the ownership rules that keep
//! orbis-perf from fighting the other Orbis components:
//!
//! * Orbis 0.2.0's mode engine (orbisd) owns: systemd targets/services, the CPU governor preset
//!   at a mode switch, the sysctls listed in its profile ([sysctl], e.g. vm.swappiness), VRAM
//!   planning and zram (zram-generator). orbis-perf never writes those at mode level.
//! * orbis-memd (Graphics Engine) owns the GTT budget.
//! * orbis-perf owns: the CPU scheduler (EEVDF / sched_ext / BORE toggle), THP, MGLRU, le9
//!   working-set protection, I/O schedulers, IRQ affinity, cgroup CPU sets of the background
//!   slice, GPU engine clock and fan threshold (replacing the 1.0 profile units), dmem
//!   protection and process priorities of game sessions, and - only for the duration of a game
//!   session - the CPU governor.
//! Manual changes (`orbis-hw set`, `orbis-gpu profile`) are overrides: while one is recorded,
//! orbis-perf leaves that knob alone.

use orbis_core::fsx::{self, p};
use orbis_core::json::{self, Value};
use std::path::PathBuf;

#[derive(Clone, Debug, PartialEq)]
pub enum GpuPolicy {
    /// full clock when the GPU is busy, startup divider when idle (PC desktop)
    Adaptive,
    Max,
    /// do not touch
    Leave,
}

#[derive(Clone, Debug)]
pub struct ModePolicy {
    pub name: String,
    /// "eevdf" (kernel default), "bore", a sched_ext scheduler ("scx_lavd", ...) or "auto"
    pub scheduler: String,
    pub scx_args: Vec<String>,
    pub gpu: GpuPolicy,
    pub fan_threshold_c: Option<i64>,
    pub thp_enabled: String,
    pub thp_defrag: String,
    pub mglru_min_ttl_ms: i64,
    pub workingset_protection: bool,
    pub io_scheduler: Vec<String>,
    pub background_cpus: Option<String>,
    pub irq_gpu_cpu: Option<u32>,
    pub max_map_count: i64,
    /// vm.compaction_proactiveness: background compaction keeps huge pages available (THP, GPU
    /// buffers) but costs CPU time; 0 in GAMING (no compaction stalls mid-frame)
    pub compaction_proactiveness: i64,
    /// KSM (/sys/kernel/mm/ksm/run): None = leave as the system has it (off on Orbis). Scanning
    /// costs Jaguar CPU time and games rarely share identical anonymous pages, so it is only
    /// switched on where a measurement shows a gain (perf.json "ksm": true, e.g. several Wine
    /// prefixes or AI processes with duplicated buffers).
    pub ksm: Option<bool>,
    /// governor for game sessions (mode-level governor stays with orbisd)
    pub session_governor: String,
}

pub fn builtin(mode: &str) -> ModePolicy {
    let base = ModePolicy {
        name: mode.to_string(),
        scheduler: "eevdf".into(),
        scx_args: vec![],
        gpu: GpuPolicy::Adaptive,
        fan_threshold_c: None,
        thp_enabled: "madvise".into(),
        thp_defrag: "defer+madvise".into(),
        mglru_min_ttl_ms: 1000,
        workingset_protection: true,
        io_scheduler: vec!["bfq".into(), "mq-deadline".into()],
        background_cpus: None,
        irq_gpu_cpu: None,
        // games and DXVK map many regions; SteamOS/Fedora use this value
        max_map_count: 2147483642,
        compaction_proactiveness: 20,
        ksm: None,
        session_governor: "performance".into(),
    };
    match mode {
        "gaming" => ModePolicy {
            gpu: GpuPolicy::Max,
            fan_threshold_c: Some(70),
            thp_defrag: "defer".into(),
            io_scheduler: vec!["adios".into(), "mq-deadline".into(), "bfq".into()],
            background_cpus: Some("0-1".into()),
            irq_gpu_cpu: Some(0),
            compaction_proactiveness: 0,
            ..base
        },
        "ai" => ModePolicy {
            gpu: GpuPolicy::Max,
            fan_threshold_c: Some(72),
            thp_enabled: "always".into(),
            // THP=always for llama.cpp: keep large contiguous blocks available
            compaction_proactiveness: 40,
            io_scheduler: vec!["mq-deadline".into(), "none".into()],
            background_cpus: Some("0-1".into()),
            ..base
        },
        _ => base,
    }
}

fn s(v: &Value, k: &str) -> Option<String> {
    v.get(k).and_then(Value::as_str).map(str::to_string)
}

/// Built-in policy merged with /etc/orbis/perf/perf.json {"modes": {"gaming": {...}}}.
pub fn mode_policy(mode: &str) -> ModePolicy {
    let mut pol = builtin(mode);
    let cfg = config();
    if let Some(m) = cfg.at(&format!("modes.{mode}")) {
        if let Some(x) = s(m, "scheduler") {
            pol.scheduler = x;
        }
        if let Some(a) = m.get("scx_args").and_then(Value::as_arr) {
            pol.scx_args = a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect();
        }
        if let Some(x) = s(m, "gpu") {
            pol.gpu = match x.as_str() {
                "max" => GpuPolicy::Max,
                "leave" => GpuPolicy::Leave,
                _ => GpuPolicy::Adaptive,
            };
        }
        if let Some(v) = m.get("fan_threshold_c") {
            pol.fan_threshold_c = v.as_i64();
        }
        if let Some(x) = s(m, "thp_enabled") {
            pol.thp_enabled = x;
        }
        if let Some(x) = s(m, "thp_defrag") {
            pol.thp_defrag = x;
        }
        if let Some(x) = m.get("mglru_min_ttl_ms").and_then(Value::as_i64) {
            pol.mglru_min_ttl_ms = x;
        }
        if let Some(a) = m.get("io_scheduler").and_then(Value::as_arr) {
            pol.io_scheduler = a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect();
        }
        if let Some(v) = m.get("background_cpus") {
            pol.background_cpus = v.as_str().map(str::to_string);
        }
        if let Some(v) = m.get("irq_gpu_cpu") {
            pol.irq_gpu_cpu = v.as_i64().map(|x| x as u32);
        }
        if let Some(v) = m.get("ksm") {
            pol.ksm = v.as_bool();
        }
        if let Some(x) = m.get("compaction_proactiveness").and_then(Value::as_i64) {
            pol.compaction_proactiveness = x.clamp(0, 100);
        }
    }
    pol
}

pub fn config() -> Value {
    fsx::read_raw(&p("/etc/orbis/perf/perf.json")).and_then(|t| json::parse(&t).ok()).unwrap_or_else(Value::obj)
}

pub fn current_mode() -> String {
    match fsx::read(&p("/var/lib/orbis/desired-mode")).as_deref() {
        Some(m @ ("pc" | "gaming" | "ai")) => m.to_string(),
        _ => "pc".into(),
    }
}

/// sysctl keys Orbis 0.2.0's active profile sets (those stay with orbisd).
pub fn orbisd_sysctls(mode: &str) -> Vec<String> {
    for f in [format!("/etc/orbis/profiles/{mode}.toml"), format!("/usr/share/orbis/profiles/{mode}.toml"),
              format!("/usr/lib/orbis/profiles/{mode}.toml")] {
        if let Some(t) = fsx::read_raw(&p(&f)) {
            let mut in_sys = false;
            let mut out = vec![];
            for line in t.lines() {
                let l = line.trim();
                if l.starts_with('[') {
                    in_sys = l == "[sysctl]";
                } else if in_sys {
                    if let Some((k, _)) = l.split_once('=') {
                        out.push(k.trim().trim_matches('"').to_string());
                    }
                }
            }
            return out;
        }
    }
    vec!["vm.swappiness".into(), "vm.vfs_cache_pressure".into()]
}

/// Knobs a user changed by hand (Graphics Engine controls / 1.0 profiles): left alone.
pub fn manual_overrides() -> Vec<String> {
    let mut out = vec![];
    if let Some(v) = fsx::read_raw(&p("/var/lib/orbis-graphics/controls-state.json")).and_then(|t| json::parse(&t).ok()) {
        if let Some(m) = v.get("original").and_then(Value::as_obj) {
            for (path, info) in m {
                let knob = info.get("knob").and_then(Value::as_str).unwrap_or("");
                out.push(knob.to_string());
                out.push(path.clone());
            }
        }
    }
    if let Some(v) = fsx::read_raw(&p("/var/lib/orbis-graphics/profile-state.json")).and_then(|t| json::parse(&t).ok()) {
        // orbis-gpu profile <name> keeps the values it replaced in "original" until `profile reset`
        if let Some(m) = v.get("original").and_then(Value::as_obj) {
            if !m.is_empty() {
                out.extend(m.keys().cloned());
                if let Some(acts) = v.get("actions").and_then(Value::as_arr) {
                    out.extend(acts.iter().filter_map(|a| a.get("knob").and_then(Value::as_str).map(str::to_string)));
                }
            }
        }
    }
    out
}

// ------------------------------------------------------------------ per-game profiles

#[derive(Clone, Debug, Default)]
pub struct GameProfile {
    pub name: String,
    pub source: String,
    pub steam_appid: Vec<String>,
    pub exe: Vec<String>,
    pub doc: Value,
}

impl GameProfile {
    pub fn str(&self, k: &str) -> Option<String> {
        self.doc.at(k).and_then(Value::as_str).map(str::to_string)
    }
    pub fn int(&self, k: &str) -> Option<i64> {
        self.doc.at(k).and_then(Value::as_i64)
    }
}

pub fn profile_dirs() -> Vec<PathBuf> {
    let mut v = vec![p("/usr/share/orbis/games.d"), p("/etc/orbis/games.d")];
    if let Some(h) = std::env::var_os("HOME") {
        v.push(PathBuf::from(h).join(".config/orbis/games.d"));
    }
    v
}

/// Later directories override earlier ones (same file name).
pub fn load_profiles(dirs: &[PathBuf]) -> Vec<GameProfile> {
    let mut by_file: std::collections::BTreeMap<String, GameProfile> = Default::default();
    for d in dirs {
        for f in fsx::list_dir(d) {
            if f.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            let Some(doc) = fsx::read_raw(&f).and_then(|t| json::parse(&t).ok()) else {
                eprintln!("orbis-perf: ignoring invalid profile {}", f.display());
                continue;
            };
            let list = |k: &str| -> Vec<String> {
                match doc.at(k) {
                    Some(Value::Arr(a)) => a.iter().filter_map(|x| x.as_str().map(|s| s.to_lowercase())).collect(),
                    Some(Value::Str(s)) => vec![s.to_lowercase()],
                    Some(Value::Int(i)) => vec![i.to_string()],
                    _ => vec![],
                }
            };
            let gp = GameProfile {
                name: doc.get("name").and_then(Value::as_str).unwrap_or("").to_string(),
                source: f.display().to_string(),
                steam_appid: list("match.steam_appid"),
                exe: list("match.exe"),
                doc: doc.clone(),
            };
            by_file.insert(f.file_name().unwrap().to_string_lossy().into_owned(), gp);
        }
    }
    by_file.into_values().collect()
}

pub fn find_profile<'a>(profiles: &'a [GameProfile], appid: Option<&str>, names: &[String]) -> Option<&'a GameProfile> {
    if let Some(a) = appid {
        if let Some(gp) = profiles.iter().find(|g| g.steam_appid.iter().any(|x| x == a)) {
            return Some(gp);
        }
    }
    let lower: Vec<String> = names.iter().map(|n| n.to_lowercase()).collect();
    profiles.iter().find(|g| g.exe.iter().any(|e| lower.contains(e)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn modes_differ_where_intended() {
        let g = builtin("gaming");
        let a = builtin("ai");
        let pc = builtin("pc");
        assert_eq!(pc.gpu, GpuPolicy::Adaptive);
        assert_eq!(g.gpu, GpuPolicy::Max);
        assert_eq!(g.background_cpus.as_deref(), Some("0-1"));
        assert_eq!(a.thp_enabled, "always");
        assert_eq!(pc.scheduler, "eevdf");
    }

    #[test]
    fn profiles_override_and_match() {
        let d = std::env::temp_dir().join(format!("orbis-prof-{}", std::process::id()));
        let (sys, user) = (d.join("sys"), d.join("user"));
        std::fs::create_dir_all(&sys).unwrap();
        std::fs::create_dir_all(&user).unwrap();
        std::fs::write(sys.join("er.json"), r#"{"name":"ER","match":{"steam_appid":"1245620","exe":["eldenring.exe"]},"scheduler":"scx_lavd"}"#).unwrap();
        std::fs::write(user.join("er.json"), r#"{"name":"ER user","match":{"steam_appid":["1245620"]},"scheduler":"eevdf"}"#).unwrap();
        std::fs::write(sys.join("x.json"), r#"{"name":"X","match":{"exe":"Game.EXE"}}"#).unwrap();
        std::fs::write(sys.join("bad.json"), "{").unwrap();
        let ps = load_profiles(&[sys, user]);
        assert_eq!(ps.len(), 2);
        let er = find_profile(&ps, Some("1245620"), &[]).unwrap();
        assert_eq!(er.str("scheduler").as_deref(), Some("eevdf"));
        assert_eq!(find_profile(&ps, None, &["game.exe".into()]).unwrap().name, "X");
        assert!(find_profile(&ps, Some("1"), &["other".into()]).is_none());
        std::fs::remove_dir_all(d).unwrap();
    }
}
