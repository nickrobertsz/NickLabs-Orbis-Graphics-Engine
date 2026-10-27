//! The controller: mode-level policy, game sessions, adaptive GPU clock, thermal guard,
//! scheduler supervision and restoration. One instance runs as the orbis-perf daemon; the CLI
//! uses the same code for one-shot actions.

use crate::games::{self, Session};
use crate::hw::{self, act};
use crate::policy::{self, GameProfile, GpuPolicy, ModePolicy};
use crate::sched::{self, Failures};
use crate::workload::{self, Bound};
use orbis_core::fsx::{self, p};
use orbis_core::procs::{self, Proc};
use orbis_core::restore::Journal;
use orbis_core::{jobj, sys, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::time::{Duration, Instant};

pub const HOT_MC: i64 = 85_000;
pub const COOL_MC: i64 = 78_000;
const GPU_BUSY_HI: i64 = 15;
const GPU_BUSY_LO: i64 = 5;
const GPU_SAMPLE_EVERY: u32 = 5; // ticks (10 s at 2 s)
const WORKLOAD_EVERY: u32 = 3; // ticks (6 s at 2 s) while a game runs

pub fn log_path() -> std::path::PathBuf {
    p("/var/log/orbis/perf.jsonl")
}

pub fn log(rec: &Value) {
    let _ = fsx::append_line(&log_path(), &rec.to_string_compact());
}

fn gamemode_active() -> bool {
    procs::pids().into_iter().any(|pid| fsx::read(&p(&format!("/proc/{pid}/comm"))).as_deref() == Some("gamemoded"))
}

pub struct Controller {
    pub mode_j: Journal,
    pub sess_j: Journal,
    pub mode: String,
    pub pol: ModePolicy,
    pub fails: Failures,
    pub sessions: BTreeMap<String, Session>,
    pub desired_sched: String,
    profiles: Vec<GameProfile>,
    cache: HashMap<u32, Proc>,
    tweaked: BTreeMap<u32, (i32, i32)>, // pid -> (nice, policy) before we changed it
    demoted: BTreeMap<u32, (i32, i32)>,
    gpu_idle: u32,
    tick_n: u32,
    pub hot: bool,
    last_cpu: Option<(u64, u64)>,
    pub cpu_util: Option<f64>,
    pub gpu_busy: Option<i64>,
    pub recent: Vec<Value>,
    pub mode_applied: bool,
    pub wl: workload::Sampler,
    /// background slice narrowed to CPU 0 because the game is CPU-bound
    bg_tight: bool,
    /// scopes admitted before launch by the admission server (crate::admit)
    pub admitted: crate::admit::Admitted,
}

impl Controller {
    pub fn new() -> Controller {
        let mode = policy::current_mode();
        Controller {
            mode_j: Journal::open(p("/var/lib/orbis/perf/restore-mode.json")),
            sess_j: Journal::open(p("/var/lib/orbis/perf/restore-session.json")),
            pol: policy::mode_policy(&mode),
            mode,
            fails: Failures::load(),
            sessions: BTreeMap::new(),
            desired_sched: "eevdf".into(),
            profiles: policy::load_profiles(&policy::profile_dirs()[..2]),
            cache: HashMap::new(),
            tweaked: BTreeMap::new(),
            demoted: BTreeMap::new(),
            gpu_idle: 0,
            tick_n: 0,
            hot: false,
            last_cpu: None,
            cpu_util: None,
            gpu_busy: None,
            recent: vec![],
            mode_applied: false,
            wl: workload::Sampler::default(),
            bg_tight: false,
            admitted: Default::default(),
        }
    }

    /// Admission reply for a session, if orbis-run had its scope limited before launch
    fn admission_of(&self, s: &Session) -> Option<Value> {
        let m = self.admitted.lock().ok()?;
        if let Some(r) = m.get(&s.root_cgroup) {
            return Some(r.clone());
        }
        s.pids.iter().find_map(|pid| orbis_core::admit::cgroup_of(*pid).and_then(|cg| m.get(&cg).cloned()))
    }

    /// Limits for a game that was not admitted before launch (started outside orbis-run, or
    /// orbis-run without a scope): applied to its cgroup now, so only later allocations are
    /// held to them. Never applied to a cgroup that also holds programs outside the game.
    fn retroactive_limit(&mut self, s: &Session, kind: &str, bytes: u64) -> Value {
        let knob = format!("dmem_max_{kind}");
        if s.root_cgroup.is_empty() {
            return act(&knob, "SKIPPED", "no cgroup for this game");
        }
        let all = procs::all(false);
        let mut game: BTreeSet<u32> = BTreeSet::new();
        for pid in &s.pids {
            game.extend(procs::tree(&all, *pid));
        }
        let outside: Vec<u32> = hw::cgroup_procs(&s.root_cgroup).into_iter().filter(|x| !game.contains(x)).collect();
        if !outside.is_empty() {
            return act(&knob, "SKIPPED", format!("retroactive: {} is shared with {} other process(es); start the game with orbis-run for pre-launch limits",
                                                 s.root_cgroup, outside.len()));
        }
        let mut a = hw::limit_dmem(&mut self.sess_j, &s.root_cgroup, kind, bytes);
        a.set("admission", "retroactive");
        a
    }

    fn note(&mut self, v: Value) {
        log(&jobj! {"t" => sys::now_unix().0, "mode" => self.mode.clone(), "action" => v.clone()});
        self.recent.push(v);
        if self.recent.len() > 40 {
            self.recent.remove(0);
        }
    }

    fn scheduler_for(&self, pol: &ModePolicy, workload: &str) -> String {
        if pol.scheduler == "auto" { sched::resolve_auto(workload) } else { pol.scheduler.clone() }
    }

    /// Apply the mode-level policy (boot, mode switch, `orbis-perf apply`).
    pub fn apply_mode(&mut self) -> Vec<Value> {
        let pol = self.pol.clone();
        let ov = policy::manual_overrides();
        let owned = policy::orbisd_sysctls(&self.mode);
        let j = &mut self.mode_j;
        let mut out = vec![];
        out.extend(hw::set_thp(j, &pol.thp_enabled, &pol.thp_defrag));
        out.extend(hw::set_mglru(j, pol.mglru_min_ttl_ms));
        if hw::sysctl_path("vm.workingset_protection").exists() {
            out.push(hw::set_sysctl(j, "vm.workingset_protection", if pol.workingset_protection { "1" } else { "0" }, &owned));
        }
        out.push(hw::set_sysctl(j, "vm.max_map_count", &pol.max_map_count.to_string(), &owned));
        if let Some(on) = pol.ksm {
            let f = p("/sys/kernel/mm/ksm/run");
            if f.exists() {
                out.push(match j.set_file(&f, if on { "1" } else { "0" }) {
                    Ok(_) => act("ksm", "APPLIED", if on { "on" } else { "off" }),
                    Err(e) => act("ksm", "FAILED", e.to_string()),
                });
            } else {
                out.push(act("ksm", "UNSUPPORTED", "kernel without CONFIG_KSM"));
            }
        }
        if hw::sysctl_path("vm.compaction_proactiveness").exists() {
            out.push(hw::set_sysctl(j, "vm.compaction_proactiveness", &pol.compaction_proactiveness.to_string(), &owned));
        }
        out.extend(hw::set_io_scheduler(j, &pol.io_scheduler));
        out.push(hw::set_background_cpus(j, pol.background_cpus.as_deref()));
        if let Some(cpu) = pol.irq_gpu_cpu {
            out.extend(hw::set_gpu_irq(j, cpu));
        }
        if let Some(c) = pol.fan_threshold_c {
            if ov.iter().any(|k| k == "fan_threshold_c") {
                out.push(act("fan_threshold_c", "SKIPPED", "manual override recorded (orbis-hw reset / orbis-gpu profile reset to release)"));
            } else {
                out.push(hw::set_fan_threshold(j, c));
            }
        }
        match pol.gpu {
            GpuPolicy::Max if !ov.iter().any(|k| k == "gpu_sclk") => out.push(hw::set_sclk(j, "max")),
            GpuPolicy::Adaptive if !ov.iter().any(|k| k == "gpu_sclk") => {
                out.push(act("gpu_sclk", "ADAPTIVE", "full clock while the GPU is busy, startup divider when idle"))
            }
            GpuPolicy::Leave => out.push(act("gpu_sclk", "SKIPPED", "policy: leave")),
            _ => out.push(act("gpu_sclk", "SKIPPED", "manual override recorded")),
        }
        self.desired_sched = self.scheduler_for(&pol, &self.mode.clone());
        if self.sessions.is_empty() {
            let want = self.desired_sched.clone();
            out.push(sched::switch(&want, &pol.scx_args, &mut self.fails));
        }
        out.push(act("cpu_governor", "SKIPPED", "mode-level governor belongs to the Orbis 0.2.0 mode engine (power preset)"));
        self.mode_applied = true;
        for a in &out {
            if a.get("state").and_then(Value::as_str) != Some("UNCHANGED") {
                self.note(a.clone());
            }
        }
        out
    }

    /// VRAM ceiling for one game (profile memory.dmem_max_mb): above it TTM places the game's
    /// buffers in GTT (same GDDR5, through GART) instead of evicting other clients' VRAM.
    fn session_vram_max(s: &Session) -> Option<u64> {
        s.profile.as_ref().and_then(|p| p.int("memory.dmem_max_mb")).filter(|mb| *mb > 0).map(|mb| (mb as u64) << 20)
    }

    /// GTT ceiling for one game (profile memory.gtt_max_mb, GE kernel ge-0015).
    fn session_gtt_max(s: &Session) -> Option<u64> {
        s.profile.as_ref().and_then(|p| p.int("memory.gtt_max_mb")).filter(|mb| *mb > 0).map(|mb| (mb as u64) << 20)
    }

    fn session_policy(&self, s: &Session) -> (String, Vec<String>, String, String, Option<i64>, i32, Option<u64>) {
        let g = policy::mode_policy("gaming");
        let prof = s.profile.as_ref();
        let sched = prof.and_then(|p| p.str("scheduler")).filter(|x| x != "auto").unwrap_or_else(|| self.scheduler_for(&g, "gaming"));
        let args = prof
            .and_then(|p| p.doc.get("scx_args").and_then(Value::as_arr).cloned())
            .map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
            .unwrap_or_else(|| g.scx_args.clone());
        let gov = prof.and_then(|p| p.str("governor")).unwrap_or(g.session_governor.clone());
        let sclk = prof.and_then(|p| p.str("gpu_sclk")).unwrap_or_else(|| "max".into());
        let fan = prof.and_then(|p| p.int("fan_threshold_c")).or(g.fan_threshold_c);
        let nice = prof.and_then(|p| p.int("nice")).unwrap_or(-4).clamp(-10, 0) as i32;
        let dmem = prof
            .and_then(|p| p.int("memory.dmem_low_mb"))
            .map(|mb| (mb as u64) << 20)
            .or_else(|| hw::vram_total().map(|v| v / 4 * 3));
        (sched, args, gov, sclk, fan, nice, dmem)
    }

    fn start_session(&mut self, s: &Session) {
        let (sch, args, gov, sclk, fan, _nice, dmem) = self.session_policy(s);
        let ov = policy::manual_overrides();
        let mut acts = vec![];
        let first = self.sessions.is_empty();
        if first {
            if !ov.iter().any(|k| k == "cpu_governor") {
                acts.push(hw::set_governor(&mut self.sess_j, &gov));
            }
            if sclk == "max" && !ov.iter().any(|k| k == "gpu_sclk") && !self.hot {
                acts.push(hw::set_sclk(&mut self.sess_j, "max"));
            }
            if let Some(c) = fan {
                if !ov.iter().any(|k| k == "fan_threshold_c") {
                    acts.push(hw::set_fan_threshold(&mut self.sess_j, c));
                }
            }
            acts.push(sched::switch(&sch, &args, &mut self.fails));
            self.desired_sched = sch;
        }
        if let Some(b) = dmem {
            if !s.root_cgroup.is_empty() {
                acts.extend(hw::protect_vram(&mut self.sess_j, &s.root_cgroup, b));
            }
        }
        let admission = self.admission_of(s);
        for (kind, lim) in [("vram", Self::session_vram_max(s)), ("gtt", Self::session_gtt_max(s))] {
            let Some(b) = lim else { continue };
            match &admission {
                // orbis-run's scope got its limits before the game started (GE-A03)
                Some(r) => acts.push(act(&format!("dmem_max_{kind}"), "PRE-LAUNCH",
                                         format!("{} (admitted before launch)", r.get("cgroup").and_then(Value::as_str).unwrap_or("")))),
                None => {
                    let a = self.retroactive_limit(s, kind, b);
                    acts.push(a);
                }
            }
        }
        self.note(jobj! {"event" => "game-start", "session" => s.key.clone(), "title" => s.title.clone(), "via" => s.via.clone(),
                         "profile" => s.profile.as_ref().map(|g| g.source.clone()), "actions" => Value::Arr(acts)});
    }

    fn profile_str(&self, key: &str) -> Vec<String> {
        self.sessions.values().filter_map(|s| s.profile.as_ref().and_then(|p| p.str(key))).collect()
    }

    /// React to a change of the game's workload class (see workload.rs). Only knobs that
    /// orbis-perf already owns are touched, all journalled, and manual overrides win.
    fn on_bound(&mut self, b: Bound) {
        let ov = policy::manual_overrides();
        let sclk_ok = !self.hot && !ov.iter().any(|k| k == "gpu_sclk");
        let auto_sclk = !self.sessions.is_empty() && self.profile_str("gpu_sclk").iter().any(|v| v == "auto");
        let mut acts = vec![];
        match b {
            // the GPU waits for the CPU: give the game every core but CPU 0
            Bound::Cpu => {
                if self.pol.background_cpus.as_deref().map(|bg| bg != "0").unwrap_or(false) {
                    acts.push(hw::set_background_cpus(&mut self.mode_j, Some("0")));
                    self.bg_tight = true;
                }
            }
            // the CPU waits for the GPU: the engine clock must be at full speed
            Bound::Gpu => {
                if sclk_ok && hw::sclk_state().as_deref() != Some("max") {
                    acts.push(hw::set_sclk(&mut self.sess_j, "max"));
                }
            }
            // menus / loading: with gpu_sclk "auto" the GPU drops to its startup clock (less heat)
            Bound::Light => {
                if sclk_ok && auto_sclk && hw::sclk_state().as_deref() == Some("max") {
                    acts.push(hw::set_sclk(&mut self.sess_j, "startup"));
                }
            }
            Bound::Balanced => {}
        }
        if b != Bound::Cpu && self.bg_tight {
            let bg = self.pol.background_cpus.clone();
            acts.push(hw::set_background_cpus(&mut self.mode_j, bg.as_deref()));
            self.bg_tight = false;
        }
        if b != Bound::Light && auto_sclk && sclk_ok && hw::sclk_state().as_deref() == Some("startup") {
            acts.push(hw::set_sclk(&mut self.sess_j, "max"));
        }
        let s = self.wl.last;
        self.note(jobj! {"event" => "workload", "class" => b.name(), "gpu_busy" => s.gpu_busy,
                         "max_thread_pct" => (s.max_thread_pct * 10.0).round() / 10.0,
                         "total_cpu_pct" => (s.total_pct * 10.0).round() / 10.0, "actions" => Value::Arr(acts)});
    }

    fn end_sessions_restore(&mut self) {
        self.wl.reset();
        if self.bg_tight {
            let bg = self.pol.background_cpus.clone();
            let a = hw::set_background_cpus(&mut self.mode_j, bg.as_deref());
            self.note(a);
            self.bg_tight = false;
        }
        let (ok, err) = self.sess_j.restore_files(|_| true);
        for (pid, (nice, pol)) in std::mem::take(&mut self.tweaked).into_iter().chain(std::mem::take(&mut self.demoted)) {
            if sys::process_alive(pid) {
                let _ = sys::set_nice(pid, nice);
                let _ = sys::set_policy(pid, pol);
            }
        }
        self.desired_sched = self.scheduler_for(&self.pol.clone(), &self.mode.clone());
        let want = self.desired_sched.clone();
        let sw = sched::switch(&want, &self.pol.scx_args.clone(), &mut self.fails);
        self.note(jobj! {"event" => "game-end", "restored" => ok, "errors" => err, "scheduler" => sw});
    }

    fn tweak_pid(&mut self, pid: u32, nice: i32) {
        if self.tweaked.contains_key(&pid) {
            return;
        }
        let (Ok(n), Ok(pol)) = (sys::get_nice(pid), sys::get_policy(pid)) else { return };
        if n > nice && sys::set_nice(pid, nice).is_ok() {
            let _ = sys::set_ioprio(pid, 2, 0);
            self.tweaked.insert(pid, (n, pol));
        }
    }

    fn demote_pid(&mut self, pid: u32) {
        if self.demoted.contains_key(&pid) {
            return;
        }
        let (Ok(n), Ok(pol)) = (sys::get_nice(pid), sys::get_policy(pid)) else { return };
        if sys::set_policy(pid, sys::SCHED_IDLE).is_ok() {
            let _ = sys::set_ioprio(pid, 3, 0);
            self.demoted.insert(pid, (n, pol));
        }
    }

    fn snapshot(&mut self) -> Vec<Proc> {
        let live: BTreeSet<u32> = procs::pids().into_iter().collect();
        self.cache.retain(|pid, _| live.contains(pid));
        for pid in &live {
            if !self.cache.contains_key(pid) {
                if let Some(pr) = procs::read_proc(*pid, false) {
                    self.cache.insert(*pid, pr);
                }
            }
        }
        let mut v: Vec<Proc> = self.cache.values().cloned().collect();
        v.sort_by_key(|p| p.pid);
        v
    }

    /// One controller step (the daemon calls this every 2 s).
    pub fn tick(&mut self) {
        self.tick_n = self.tick_n.wrapping_add(1);
        // mode
        let m = policy::current_mode();
        if m != self.mode || !self.mode_applied {
            self.mode = m;
            self.pol = policy::mode_policy(&self.mode);
            let acts = self.apply_mode();
            self.note(jobj! {"event" => "mode", "mode" => self.mode.clone(), "actions" => acts.len() as i64});
        }
        // cpu utilisation
        if let Some(c) = hw::cpu_times() {
            if let Some((t0, i0)) = self.last_cpu {
                let dt = c.0.saturating_sub(t0);
                if dt > 0 {
                    self.cpu_util = Some(100.0 * (1.0 - (c.1.saturating_sub(i0)) as f64 / dt as f64));
                }
            }
            self.last_cpu = Some(c);
        }
        // games
        let snap = self.snapshot();
        let now = sys::now_unix().0;
        let mut found = games::detect(&snap, &self.profiles, now);
        for (k, s) in found.iter_mut() {
            if let Some(old) = self.sessions.get(k) {
                s.started = old.started;
            }
        }
        let new_keys: Vec<String> = found.keys().filter(|k| !self.sessions.contains_key(*k)).cloned().collect();
        for k in &new_keys {
            let s = found[k].clone();
            self.start_session(&s);
            self.sessions.insert(k.clone(), s);
        }
        let gone: Vec<String> = self.sessions.keys().filter(|k| !found.contains_key(*k)).cloned().collect();
        for k in &gone {
            self.sessions.remove(k);
            self.note(jobj! {"event" => "game-exit", "session" => k.clone()});
        }
        for (k, s) in &found {
            self.sessions.insert(k.clone(), s.clone());
        }
        if !gone.is_empty() && self.sessions.is_empty() {
            self.end_sessions_restore();
        }
        if !self.sessions.is_empty() && !gamemode_active() {
            let all: BTreeSet<u32> = self.sessions.values().flat_map(|s| s.pids.iter().copied()).collect();
            let nices: Vec<(u32, i32)> = self
                .sessions
                .values()
                .flat_map(|s| {
                    let n = s.profile.as_ref().and_then(|p| p.int("nice")).unwrap_or(-4).clamp(-10, 0) as i32;
                    s.pids.iter().map(move |pid| (*pid, n))
                })
                .collect();
            for (pid, n) in nices {
                self.tweak_pid(pid, n);
            }
            for pid in games::background_pids(&snap, &all) {
                self.demote_pid(pid);
            }
        }
        // CPU/GPU workload coordination while a game runs (profile "coordination": "off" disables)
        if !self.sessions.is_empty() && self.tick_n % WORKLOAD_EVERY == 0
            && !self.profile_str("coordination").iter().any(|v| v == "off")
        {
            let pids: BTreeSet<u32> = self.sessions.values().flat_map(|s| s.pids.iter().copied()).collect();
            let (max_thread_pct, total_pct) = self.wl.cpu(&pids, Instant::now());
            self.gpu_busy = hw::gpu_busy();
            // CPUs the game may use: its cgroup's effective cpuset, else every online CPU
            let cpus = self.sessions.values().next()
                .and_then(|s| fsx::read(&p(&format!("/sys/fs/cgroup{}/cpuset.cpus.effective", s.root_cgroup))))
                .map(|l| workload::count_cpus(&l) as f64).filter(|n| *n > 0.0)
                .unwrap_or_else(|| std::thread::available_parallelism().map(|n| n.get() as f64).unwrap_or(8.0));
            let s = workload::Sample { gpu_busy: self.gpu_busy, max_thread_pct, total_pct };
            if let Some(b) = self.wl.feed(s, cpus) {
                self.on_bound(b);
            }
        }
        self.tweaked.retain(|pid, _| sys::process_alive(*pid));
        self.demoted.retain(|pid, _| sys::process_alive(*pid));
        // scheduler supervision
        if let Some(ev) = sched::check(&self.desired_sched.clone(), &mut self.fails) {
            self.note(ev);
            let want = self.desired_sched.clone();
            let r = sched::switch(&want, &[], &mut self.fails);
            self.note(r);
        }
        // thermal guard (every tick: the fan driver caches the ICC read for 1 s)
        if let Some(t) = hw::apu_temp_mc() {
            if !self.hot && t >= HOT_MC {
                self.hot = true;
                let a = hw::set_sclk(&mut self.sess_j, "startup");
                self.note(jobj! {"event" => "thermal-guard", "apu_mc" => t, "action" => a});
            } else if self.hot && t <= COOL_MC {
                self.hot = false;
                self.note(jobj! {"event" => "thermal-clear", "apu_mc" => t});
                if !self.sessions.is_empty() || self.pol.gpu == GpuPolicy::Max {
                    let a = hw::set_sclk(&mut self.sess_j, "max");
                    self.note(a);
                }
            }
        }
        // adaptive GPU clock (PC desktop without a game)
        if self.tick_n % GPU_SAMPLE_EVERY == 0 && self.sessions.is_empty() && self.pol.gpu == GpuPolicy::Adaptive && !self.hot
            && !policy::manual_overrides().iter().any(|k| k == "gpu_sclk")
        {
            self.gpu_busy = hw::gpu_busy();
            if let (Some(b), Some(cur)) = (self.gpu_busy, hw::sclk_state()) {
                if b >= GPU_BUSY_HI && cur != "max" {
                    self.gpu_idle = 0;
                    let a = hw::set_sclk(&mut self.mode_j, "max");
                    self.note(jobj! {"event" => "gpu-busy", "gfx" => b, "action" => a});
                } else if b <= GPU_BUSY_LO {
                    self.gpu_idle += 1;
                    if self.gpu_idle >= 3 && cur != "startup" {
                        let a = hw::set_sclk(&mut self.mode_j, "startup");
                        self.note(jobj! {"event" => "gpu-idle", "gfx" => b, "action" => a});
                    }
                } else {
                    self.gpu_idle = 0;
                }
            }
        }
    }

    pub fn state(&self) -> Value {
        let sessions: Vec<Value> = self
            .sessions
            .values()
            .map(|s| jobj! {"key" => s.key.clone(), "title" => s.title.clone(), "appid" => s.appid.clone(), "via" => s.via.clone(),
                            "pids" => s.pids.iter().map(|p| *p as i64).collect::<Vec<i64>>(), "cgroup" => s.root_cgroup.clone(),
                            "profile" => s.profile.as_ref().map(|g| g.source.clone()), "started" => s.started})
            .collect();
        jobj! {
            "schema" => "orbis-perf-state/1", "time" => sys::now_unix().0, "mode" => self.mode.clone(),
            "scheduler" => jobj! {"desired" => self.desired_sched.clone(), "active" => sched::active(),
                                  "failures" => Value::Obj(self.fails.counts.iter().map(|(k, v)| (k.clone(), Value::Int(*v as i64))).collect())},
            "sessions" => Value::Arr(sessions), "thermal_guard" => self.hot,
            "cpu_util_percent" => self.cpu_util, "gpu_busy_gfx_percent" => self.gpu_busy,
            "gamemode_running" => gamemode_active(),
            "workload" => jobj! {"class" => self.wl.current.map(|b| b.name()), "gpu_busy" => self.wl.last.gpu_busy,
                                 "max_thread_pct" => (self.wl.last.max_thread_pct * 10.0).round() / 10.0,
                                 "total_cpu_pct" => (self.wl.last.total_pct * 10.0).round() / 10.0,
                                 "background_narrowed" => self.bg_tight},
            "tweaked_pids" => self.tweaked.len(), "demoted_pids" => self.demoted.len(),
            "recent" => Value::Arr(self.recent.iter().rev().take(12).cloned().collect())
        }
    }

    /// Put back everything orbis-perf changed (package removal, `orbis-perf restore`).
    pub fn restore_all(&mut self) -> Value {
        let (s_ok, s_err) = self.sess_j.restore_files(|_| true);
        let (m_ok, m_err) = self.mode_j.restore_files(|_| true);
        for (k, _) in self.mode_j.entries() {
            if let Some(rest) = k.strip_prefix("systemd:") {
                if let Some((unit, prop)) = rest.split_once(':') {
                    if std::env::var_os("ORBIS_ROOT").is_none() {
                        let _ = std::process::Command::new("systemctl").args(["set-property", "--runtime", unit, &format!("{prop}=")]).status();
                    }
                }
                let _ = self.mode_j.forget(&k);
            }
        }
        let sw = sched::switch("eevdf", &[], &mut self.fails);
        jobj! {"restored" => [s_ok, m_ok].concat(), "errors" => [s_err, m_err].concat(), "scheduler" => sw}
    }
}

impl Default for Controller {
    fn default() -> Self {
        Self::new()
    }
}

pub fn run_daemon(interval: Duration) -> ! {
    let mut c = Controller::new();
    crate::admit::spawn(c.admitted.clone());
    let state_f = p("/run/orbis/perf/state.json");
    let mut last_write = Instant::now() - Duration::from_secs(60);
    let mut last_doc = String::new();
    loop {
        c.tick();
        let doc = c.state();
        let mut cmp = doc.clone();
        cmp.set("time", 0i64);
        let cs = cmp.to_string_compact();
        if cs != last_doc || last_write.elapsed() > Duration::from_secs(30) {
            let _ = fsx::atomic_write(&state_f, doc.to_string_pretty().as_bytes());
            last_doc = cs;
            last_write = Instant::now();
        }
        std::thread::sleep(interval);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::Path;

    fn w(root: &Path, rel: &str, content: &str) {
        let f = root.join(rel);
        fs::create_dir_all(f.parent().unwrap()).unwrap();
        fs::write(f, content).unwrap();
    }

    fn proc(root: &Path, pid: u32, ppid: u32, comm: &str, argv: &[&str]) {
        w(root, &format!("proc/{pid}/status"), &format!("Name:\t{comm}\nPPid:\t{ppid}\nUid:\t1000\t1000\t1000\t1000\n"));
        w(root, &format!("proc/{pid}/comm"), &format!("{comm}\n"));
        w(root, &format!("proc/{pid}/cmdline"), &(argv.join("\0") + "\0"));
        w(root, &format!("proc/{pid}/cgroup"), &format!("0::/user.slice/user-1000.slice/app-{pid}.scope\n"));
    }

    /// End to end on a fixture tree: mode policy, a Steam game session (scheduler, governor,
    /// GPU clock, fan, VRAM protection), its end and the restoration of every value.
    #[test]
    fn controller_fixture() {
        let _g = crate::TEST_ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let root = std::env::temp_dir().join(format!("orbis-perf-fixture-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let r = root.as_path();
        // GPU (GE kernel interfaces)
        let dev = "sys/devices/pci0000:00/0000:00:01.0";
        w(r, &format!("{dev}/vendor"), "0x1002\n");
        w(r, &format!("{dev}/ps4_sclk"), "state=startup did=8 startup_did=8\n");
        w(r, &format!("{dev}/orbis_engine_busy"), "gfx=40 shader=30\n");
        w(r, &format!("{dev}/mem_info_vram_total"), &(1u64 << 30).to_string());
        fs::create_dir_all(r.join("sys/class/drm/card0")).unwrap();
        std::os::unix::fs::symlink(r.join(dev), r.join("sys/class/drm/card0/device")).unwrap();
        w(r, "sys/class/hwmon/hwmon2/name", "ps4_fan\n");
        w(r, "sys/class/hwmon/hwmon2/temp1_input", "61000\n");
        w(r, "sys/class/hwmon/hwmon2/temp1_crit", "79000\n");
        // CPU
        w(r, "sys/devices/system/cpu/cpufreq/policy0/scaling_governor", "schedutil\n");
        w(r, "sys/devices/system/cpu/cpufreq/policy0/scaling_available_governors", "performance powersave schedutil\n");
        w(r, "proc/stat", "cpu  100 0 100 800 0 0 0 0 0 0\n");
        // memory / storage / cgroups
        w(r, "sys/kernel/mm/transparent_hugepage/enabled", "[always] madvise never\n");
        w(r, "sys/kernel/mm/transparent_hugepage/defrag", "always defer [defer+madvise] madvise never\n");
        w(r, "sys/kernel/mm/lru_gen/enabled", "0x0007\n");
        w(r, "sys/kernel/mm/lru_gen/min_ttl_ms", "0\n");
        w(r, "proc/sys/vm/max_map_count", "65530\n");
        w(r, "proc/sys/vm/workingset_protection", "1\n");
        w(r, "proc/sys/vm/swappiness", "60\n");
        w(r, "proc/sys/vm/compaction_proactiveness", "20\n");
        w(r, "sys/fs/cgroup/user.slice/user-1000.slice/app-22.scope/dmem.max", "drm/0000:00:01.0/vram max\n");
        w(r, "etc/orbis/games.d/elden.json", r#"{"name":"Elden Ring","match":{"steam_appid":["1245620"]},"memory":{"dmem_max_mb":512,"gtt_max_mb":2048}}"#);
        w(r, "sys/block/sda/queue/scheduler", "[mq-deadline] kyber bfq adios none\n");
        w(r, "sys/fs/cgroup/cgroup.controllers", "cpuset cpu io memory pids dmem\n");
        w(r, "sys/fs/cgroup/dmem.capacity", "drm/0000:00:01.0/vram 1073741824\ndrm/0000:00:01.0/gtt 4294967296\n");
        for cg in ["user.slice", "user.slice/user-1000.slice", "user.slice/user-1000.slice/app-22.scope"] {
            w(r, &format!("sys/fs/cgroup/{cg}/dmem.low"), "drm/0000:00:01.0/vram 0\n");
        }
        w(r, "proc/interrupts", "           CPU0       CPU1\n 40:        100          5   PCI-MSI  amdgpu\n 41:          1          1   PCI-MSI  xhci_hcd\n");
        w(r, "proc/irq/40/smp_affinity_list", "0-7\n");
        w(r, "var/lib/orbis/desired-mode", "pc\n");
        proc(r, 1, 0, "systemd", &["/sbin/init"]);
        std::env::set_var("ORBIS_ROOT", r);

        let mut c = Controller::new();
        c.tick(); // pc mode
        let rd = |rel: &str| fs::read_to_string(r.join(rel)).unwrap().trim().to_string();
        assert_eq!(rd("sys/kernel/mm/transparent_hugepage/enabled"), "madvise");
        assert_eq!(rd("sys/block/sda/queue/scheduler"), "bfq");
        assert_eq!(rd("proc/sys/vm/max_map_count"), "2147483642");
        assert_eq!(rd("proc/sys/vm/swappiness"), "60"); // owned by Orbis 0.2.0
        assert_eq!(rd("sys/kernel/mm/lru_gen/min_ttl_ms"), "1000");
        assert!(c.sessions.is_empty());

        // a Steam game starts
        proc(r, 20, 1, "reaper", &["reaper", "SteamLaunch", "AppId=1245620", "--", "proton"]);
        proc(r, 22, 20, "eldenring.exe", &["Z:\\ER\\eldenring.exe"]);
        c.tick();
        assert_eq!(c.sessions.len(), 1, "{:?}", c.sessions.keys());
        assert_eq!(rd("sys/devices/system/cpu/cpufreq/policy0/scaling_governor"), "performance");
        assert_eq!(rd(&format!("{dev}/ps4_sclk")), "max");
        assert_eq!(rd("sys/class/hwmon/hwmon2/temp1_crit"), "70000");
        assert_eq!(rd("sys/fs/cgroup/user.slice/user-1000.slice/app-22.scope/dmem.low"), format!("drm/0000:00:01.0/vram {}", (1u64 << 30) / 4 * 3));
        // a fixture file keeps only the last write (the kernel keeps one line per region): GTT came last
        assert_eq!(rd("sys/fs/cgroup/user.slice/user-1000.slice/app-22.scope/dmem.max"), format!("drm/0000:00:01.0/gtt {}", 2048u64 << 20));
        let j = fs::read_to_string(r.join("var/lib/orbis/perf/restore-session.json")).unwrap();
        assert!(j.contains("dmem.max#vram") && j.contains("dmem.max#gtt"), "{j}");
        let st = c.state();
        assert_eq!(st.at("sessions").and_then(|s| s.as_arr()).map(|a| a.len()), Some(1));

        // thermal guard drops the clock and recovers
        w(r, &format!("{dev}/ps4_sclk"), "state=max did=1 startup_did=8\n");
        w(r, "sys/class/hwmon/hwmon2/temp1_input", "86000\n");
        c.tick();
        assert!(c.hot);
        assert_eq!(rd(&format!("{dev}/ps4_sclk")), "startup");
        w(r, &format!("{dev}/ps4_sclk"), "state=startup did=8 startup_did=8\n");
        w(r, "sys/class/hwmon/hwmon2/temp1_input", "70000\n");
        c.tick();
        assert!(!c.hot);
        assert_eq!(rd(&format!("{dev}/ps4_sclk")), "max");

        // the game exits: session values restored
        fs::remove_dir_all(r.join("proc/22")).unwrap();
        fs::remove_dir_all(r.join("proc/20")).unwrap();
        w(r, "sys/devices/system/cpu/cpufreq/policy0/scaling_governor", "performance\n");
        c.tick();
        assert!(c.sessions.is_empty());
        assert_eq!(rd("sys/devices/system/cpu/cpufreq/policy0/scaling_governor"), "schedutil");
        assert_eq!(rd("sys/class/hwmon/hwmon2/temp1_crit"), "79000");
        assert_eq!(rd("sys/fs/cgroup/user.slice/user-1000.slice/app-22.scope/dmem.low"), "drm/0000:00:01.0/vram 0");
        // both regions restored (the last restore write in key order is vram)
        assert_eq!(rd("sys/fs/cgroup/user.slice/user-1000.slice/app-22.scope/dmem.max"), "drm/0000:00:01.0/vram max");
        assert_eq!(rd("proc/sys/vm/compaction_proactiveness"), "20");

        // gaming mode
        w(r, "var/lib/orbis/desired-mode", "gaming\n");
        w(r, "sys/block/sda/queue/scheduler", "mq-deadline kyber [bfq] adios none\n");
        c.tick();
        assert_eq!(rd("sys/block/sda/queue/scheduler"), "adios");
        assert_eq!(rd("proc/irq/40/smp_affinity_list"), "0");
        assert_eq!(rd("proc/sys/vm/compaction_proactiveness"), "0"); // no compaction stalls while gaming

        // full restore returns to the boot values
        w(r, "sys/block/sda/queue/scheduler", "mq-deadline kyber bfq [adios] none\n");
        let res = c.restore_all();
        assert!(res.get("errors").and_then(|e| e.as_arr()).map(|a| a.is_empty()).unwrap_or(false), "{res:?}");
        assert_eq!(rd("sys/block/sda/queue/scheduler"), "mq-deadline");
        assert_eq!(rd("sys/kernel/mm/transparent_hugepage/enabled"), "always");
        assert_eq!(rd("proc/sys/vm/max_map_count"), "65530");
        assert_eq!(rd("proc/irq/40/smp_affinity_list"), "0-7");
        std::env::remove_var("ORBIS_ROOT");
        let _ = fs::remove_dir_all(&root);
    }
}
