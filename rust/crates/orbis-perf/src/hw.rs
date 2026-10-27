//! Hardware/kernel knobs orbis-perf reads and writes. Every write goes through the restore
//! journal; a knob the running kernel does not offer is reported, never emulated.

use orbis_core::fsx::{self, p};
use orbis_core::restore::{active_choice, choices, Journal};
use orbis_core::{jobj, Value};
use std::path::{Path, PathBuf};
use std::process::Command;

pub fn act(knob: &str, state: &str, detail: impl Into<Value>) -> Value {
    jobj! {"knob" => knob, "state" => state, "detail" => detail.into()}
}

fn set(j: &mut Journal, knob: &str, path: &Path, value: &str) -> Value {
    if !path.exists() {
        return act(knob, "UNSUPPORTED", format!("{} not present", path.display()));
    }
    let offered = fsx::read(path).map(|c| choices(&c)).unwrap_or_default();
    let is_selector = fsx::read(path).map(|c| c.contains('[')).unwrap_or(false);
    if is_selector && !offered.iter().any(|c| c == value) {
        return act(knob, "UNSUPPORTED", format!("{value} not offered ({})", offered.join(" ")));
    }
    match j.set_file(path, value) {
        Ok(true) => act(knob, "APPLIED", value),
        Ok(false) => act(knob, "UNCHANGED", value),
        Err(e) => act(knob, "FAILED", format!("{}: {e}", path.display())),
    }
}

// ------------------------------------------------------------------ CPU
pub fn cpufreq_policies() -> Vec<PathBuf> {
    fsx::list_dir(&p("/sys/devices/system/cpu/cpufreq"))
        .into_iter()
        .filter(|d| d.file_name().map(|n| n.to_string_lossy().starts_with("policy")).unwrap_or(false))
        .collect()
}

pub fn set_governor(j: &mut Journal, gov: &str) -> Value {
    let pols = cpufreq_policies();
    if pols.is_empty() {
        return act("cpu_governor", "UNSUPPORTED", "no cpufreq policy (kernel without ps4-cpufreq)");
    }
    let avail = fsx::read(&pols[0].join("scaling_available_governors")).unwrap_or_default();
    if !avail.split_whitespace().any(|g| g == gov) {
        return act("cpu_governor", "UNSUPPORTED", format!("{gov} not offered ({avail})"));
    }
    let mut changed = 0;
    for pol in &pols {
        if let Ok(true) = j.set_file(&pol.join("scaling_governor"), gov) {
            changed += 1;
        }
    }
    act("cpu_governor", if changed > 0 { "APPLIED" } else { "UNCHANGED" }, format!("{gov} on {} policies", pols.len()))
}

/// (cpu, cur_khz) for every CPU with cpufreq
pub fn cpu_freqs() -> Vec<(String, Option<i64>)> {
    fsx::list_dir(&p("/sys/devices/system/cpu"))
        .into_iter()
        .filter(|d| {
            let n = d.file_name().unwrap().to_string_lossy().into_owned();
            n.starts_with("cpu") && n[3..].chars().all(|c| c.is_ascii_digit()) && n.len() > 3
        })
        .map(|d| (d.file_name().unwrap().to_string_lossy().into_owned(), fsx::read_i64(&d.join("cpufreq/scaling_cur_freq"))))
        .collect()
}

/// Jaguar modules: CPUs sharing an L2 (two 4-core modules on the PS4)
pub fn l2_groups() -> Vec<String> {
    let mut g: Vec<String> = fsx::list_dir(&p("/sys/devices/system/cpu"))
        .into_iter()
        .filter_map(|d| fsx::read(&d.join("cache/index2/shared_cpu_list")))
        .collect();
    g.sort();
    g.dedup();
    g
}

pub fn cpu_times() -> Option<(u64, u64)> {
    let s = fsx::read_raw(&p("/proc/stat"))?;
    let l = s.lines().next()?;
    let f: Vec<u64> = l.split_whitespace().skip(1).filter_map(|x| x.parse().ok()).collect();
    if f.len() < 5 {
        return None;
    }
    Some((f.iter().sum(), f[3] + f[4]))
}

// ------------------------------------------------------------------ GPU (Graphics Engine kernel)
pub fn gpu_dir() -> Option<PathBuf> {
    for c in fsx::list_dir(&p("/sys/class/drm")) {
        let n = c.file_name().unwrap().to_string_lossy().into_owned();
        if !n.starts_with("card") || n.contains('-') {
            continue;
        }
        let d = c.join("device");
        if fsx::read(&d.join("vendor")).as_deref() == Some("0x1002") {
            return Some(d);
        }
    }
    None
}

pub fn sclk_state() -> Option<String> {
    fsx::read(&gpu_dir()?.join("ps4_sclk")).and_then(|t| fsx::kv_get(&t, "state"))
}

pub fn set_sclk(j: &mut Journal, state: &str) -> Value {
    let Some(g) = gpu_dir() else { return act("gpu_sclk", "UNSUPPORTED", "no AMD GPU") };
    let f = g.join("ps4_sclk");
    if !f.exists() {
        return act("gpu_sclk", "UNSUPPORTED", "kernel without ge-6.15/0002 (ps4_sclk)");
    }
    let cur = sclk_state().unwrap_or_default();
    if cur == state {
        return act("gpu_sclk", "UNCHANGED", state);
    }
    let _ = j.remember(&f.display().to_string(), &cur);
    match fsx::write_value(&f, state) {
        Ok(()) => act("gpu_sclk", "APPLIED", format!("{cur} -> {state}")),
        Err(e) => act("gpu_sclk", "FAILED", e.to_string()),
    }
}

/// gfx busy % from orbis_engine_busy (costs ~20 ms of kernel sampling: call sparingly)
pub fn gpu_busy() -> Option<i64> {
    fsx::read(&gpu_dir()?.join("orbis_engine_busy")).and_then(|t| fsx::kv_get(&t, "gfx")).and_then(|v| v.parse().ok())
}

pub fn fan_hwmon() -> Option<PathBuf> {
    fsx::list_dir(&p("/sys/class/hwmon")).into_iter().find(|h| fsx::read(&h.join("name")).as_deref() == Some("ps4_fan"))
}

pub fn apu_temp_mc() -> Option<i64> {
    fsx::read_i64(&fan_hwmon()?.join("temp1_input"))
}

pub fn set_fan_threshold(j: &mut Journal, c: i64) -> Value {
    if !(45..=85).contains(&c) {
        return act("fan_threshold_c", "REJECTED", "outside 45..85 C");
    }
    match fan_hwmon() {
        Some(h) => set(j, "fan_threshold_c", &h.join("temp1_crit"), &(c * 1000).to_string()),
        None => act("fan_threshold_c", "UNSUPPORTED", "kernel without ge-6.15/0001 (ps4_fan)"),
    }
}

// ------------------------------------------------------------------ memory
pub fn set_thp(j: &mut Journal, enabled: &str, defrag: &str) -> Vec<Value> {
    vec![
        set(j, "thp_enabled", &p("/sys/kernel/mm/transparent_hugepage/enabled"), enabled),
        set(j, "thp_defrag", &p("/sys/kernel/mm/transparent_hugepage/defrag"), defrag),
    ]
}

pub fn set_mglru(j: &mut Journal, min_ttl_ms: i64) -> Vec<Value> {
    let en = p("/sys/kernel/mm/lru_gen/enabled");
    if !en.exists() {
        return vec![act("mglru", "UNSUPPORTED", "kernel without CONFIG_LRU_GEN")];
    }
    vec![set(j, "mglru_min_ttl_ms", &p("/sys/kernel/mm/lru_gen/min_ttl_ms"), &min_ttl_ms.to_string())]
}

pub fn sysctl_path(key: &str) -> PathBuf {
    p(&format!("/proc/sys/{}", key.replace('.', "/")))
}

pub fn set_sysctl(j: &mut Journal, key: &str, value: &str, owned_by_orbisd: &[String]) -> Value {
    if owned_by_orbisd.iter().any(|k| k == key) {
        return act(key, "SKIPPED", "set by the Orbis 0.2.0 mode profile");
    }
    set(j, key, &sysctl_path(key), value)
}

// ------------------------------------------------------------------ storage
pub fn block_devices() -> Vec<PathBuf> {
    fsx::list_dir(&p("/sys/block"))
        .into_iter()
        .filter(|d| {
            let n = d.file_name().unwrap().to_string_lossy().into_owned();
            !(n.starts_with("loop") || n.starts_with("zram") || n.starts_with("ram") || n.starts_with("dm-"))
        })
        .collect()
}

pub fn set_io_scheduler(j: &mut Journal, prefs: &[String]) -> Vec<Value> {
    let mut out = vec![];
    for d in block_devices() {
        let f = d.join("queue/scheduler");
        let Some(cur) = fsx::read(&f) else { continue };
        let offered = choices(&cur);
        let name = d.file_name().unwrap().to_string_lossy().into_owned();
        match prefs.iter().find(|s| offered.contains(s)) {
            Some(s) => out.push(set(j, &format!("io_scheduler:{name}"), &f, s)),
            None => out.push(act(&format!("io_scheduler:{name}"), "UNSUPPORTED", format!("none of {prefs:?} in {cur}"))),
        }
    }
    out
}

// ------------------------------------------------------------------ IRQs
/// (irq number, action names) from /proc/interrupts
pub fn irqs() -> Vec<(u32, String)> {
    let Some(t) = fsx::read_raw(&p("/proc/interrupts")) else { return vec![] };
    t.lines()
        .skip(1)
        .filter_map(|l| {
            let (n, rest) = l.trim_start().split_once(':')?;
            let irq: u32 = n.trim().parse().ok()?;
            let name = rest.split_whitespace().filter(|w| !w.chars().all(|c| c.is_ascii_digit())).last()?.to_string();
            Some((irq, name))
        })
        .collect()
}

pub fn set_gpu_irq(j: &mut Journal, cpu: u32) -> Vec<Value> {
    let mut out = vec![];
    for (irq, name) in irqs() {
        if name.contains("amdgpu") {
            out.push(set(j, &format!("irq:{irq}:{name}"), &p(&format!("/proc/irq/{irq}/smp_affinity_list")), &cpu.to_string()));
        }
    }
    if out.is_empty() {
        out.push(act("irq_gpu", "UNSUPPORTED", "no amdgpu interrupt listed"));
    }
    out
}

// ------------------------------------------------------------------ cgroups (systemd slices)
const BG_SLICE: &str = "orbis-background.slice";

pub fn set_background_cpus(j: &mut Journal, cpus: Option<&str>) -> Value {
    let key = format!("systemd:{BG_SLICE}:AllowedCPUs");
    if !p("/sys/fs/cgroup/cgroup.controllers").exists() {
        return act("background_cpus", "UNSUPPORTED", "cgroup v2 not mounted");
    }
    let ctrl = fsx::read(&p("/sys/fs/cgroup/cgroup.controllers")).unwrap_or_default();
    if !ctrl.split_whitespace().any(|c| c == "cpuset") {
        return act("background_cpus", "UNSUPPORTED", "cpuset controller not available (kernel without CONFIG_CPUSETS)");
    }
    let val = cpus.unwrap_or("");
    let _ = j.remember(&key, "");
    if std::env::var_os("ORBIS_ROOT").is_some() {
        return act("background_cpus", "APPLIED", format!("(test root) AllowedCPUs={val}"));
    }
    let r = Command::new("systemctl").args(["set-property", "--runtime", BG_SLICE, &format!("AllowedCPUs={val}")]).status();
    match r {
        Ok(s) if s.success() => act("background_cpus", "APPLIED", if val.is_empty() { "all".to_string() } else { val.to_string() }),
        Ok(s) => act("background_cpus", "FAILED", format!("systemctl exit {s}")),
        Err(e) => act("background_cpus", "FAILED", e.to_string()),
    }
}

/// dmem.low for the GPU's VRAM region on a cgroup and its ancestors (protection is hierarchical).
pub fn dmem_region() -> Option<String> {
    let caps = fsx::read_raw(&p("/sys/fs/cgroup/dmem.capacity"))?;
    caps.lines().find_map(|l| {
        let name = l.split_whitespace().next()?;
        (name.starts_with("drm/") && name.ends_with("/vram")).then(|| name.to_string())
    })
}

pub fn protect_vram(j: &mut Journal, cgroup: &str, bytes: u64) -> Vec<Value> {
    let Some(region) = dmem_region() else {
        return vec![act("dmem_low", "UNSUPPORTED", "no dmem VRAM region (kernel without CONFIG_CGROUP_DMEM)")];
    };
    let mut out = vec![];
    let mut path = String::new();
    for part in cgroup.trim_matches('/').split('/').filter(|s| !s.is_empty()) {
        path.push('/');
        path.push_str(part);
        let f = p(&format!("/sys/fs/cgroup{path}/dmem.low"));
        if !f.exists() {
            out.push(act("dmem_low", "UNSUPPORTED", format!("{path}: dmem controller not enabled here")));
            return out;
        }
        let cur = fsx::read_raw(&f).unwrap_or_default();
        let cur_v = cur.lines().find(|l| l.starts_with(&region)).and_then(|l| l.split_whitespace().nth(1)).unwrap_or("0");
        let cur_b: u64 = cur_v.parse().unwrap_or(0);
        if cur_b >= bytes {
            continue;
        }
        let _ = j.remember(&f.display().to_string(), &format!("{region} {cur_v}"));
        match fsx::write_value(&f, &format!("{region} {bytes}")) {
            Ok(()) => out.push(act("dmem_low", "APPLIED", format!("{path} {region} {bytes}"))),
            Err(e) => out.push(act("dmem_low", "FAILED", format!("{path}: {e}"))),
        }
    }
    out
}

/// dmem.max for the GPU's VRAM region on one cgroup (the game's own scope; limits are not
/// propagated to ancestors). Journalled so the session end restores "max".
pub fn limit_vram(j: &mut Journal, cgroup: &str, bytes: u64) -> Value {
    let Some(region) = dmem_region() else {
        return act("dmem_max", "UNSUPPORTED", "no dmem VRAM region (kernel without CONFIG_CGROUP_DMEM)");
    };
    let f = p(&format!("/sys/fs/cgroup/{}/dmem.max", cgroup.trim_matches('/')));
    if !f.exists() {
        return act("dmem_max", "UNSUPPORTED", format!("{cgroup}: dmem controller not enabled here"));
    }
    let cur = fsx::read_raw(&f).unwrap_or_default();
    let cur_v = cur.lines().find(|l| l.starts_with(&region)).and_then(|l| l.split_whitespace().nth(1)).unwrap_or("max").to_string();
    let _ = j.remember(&f.display().to_string(), &format!("{region} {cur_v}"));
    match fsx::write_value(&f, &format!("{region} {bytes}")) {
        Ok(()) => act("dmem_max", "APPLIED", format!("{cgroup} {region} {bytes}")),
        Err(e) => act("dmem_max", "FAILED", format!("{cgroup}: {e}")),
    }
}

pub fn vram_total() -> Option<u64> {
    fsx::read_u64(&gpu_dir()?.join("mem_info_vram_total"))
}

pub fn active(path: &Path) -> Option<String> {
    fsx::read(path).map(|c| active_choice(&c))
}
