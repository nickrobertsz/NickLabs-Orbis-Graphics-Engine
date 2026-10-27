//! CPU/GPU workload classification for game sessions.
//!
//! Inputs are first-hand counters only: the GPU's sampled engine activity (GE kernel
//! `orbis_engine_busy`, gfx %) and per-thread CPU time of the game's processes
//! (/proc/<pid>/task/<tid>/stat, utime + stime). A game is
//!   GPU-bound  when the graphics engine is (nearly) saturated,
//!   CPU-bound  when the GPU waits while a game thread (usually the render/main thread) or the
//!              game as a whole saturates the CPUs it may use,
//!   light      in menus, loading screens or paused,
//!   balanced   otherwise.
//! The class must hold for HOLD consecutive samples before it changes (no flapping).

use orbis_core::fsx::{self, p};
use std::collections::{BTreeSet, HashMap};
use std::time::Instant;

pub const HOLD: u32 = 3;
const USER_HZ: f64 = 100.0; // Linux x86 USER_HZ (clock ticks in /proc/*/stat)

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Bound {
    Gpu,
    Cpu,
    Balanced,
    Light,
}

impl Bound {
    pub fn name(self) -> &'static str {
        match self {
            Bound::Gpu => "gpu-bound",
            Bound::Cpu => "cpu-bound",
            Bound::Balanced => "balanced",
            Bound::Light => "light",
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Sample {
    pub gpu_busy: Option<i64>,
    /// busiest single thread, % of one CPU
    pub max_thread_pct: f64,
    /// all threads, % of one CPU (800 = all eight Jaguar cores)
    pub total_pct: f64,
}

/// `cpus` = CPUs the game may run on.
pub fn classify(s: &Sample, cpus: f64) -> Bound {
    let gpu = s.gpu_busy.unwrap_or(-1);
    if gpu >= 90 {
        return Bound::Gpu;
    }
    let cpu_sat = s.max_thread_pct >= 85.0 || s.total_pct >= 85.0 * cpus.max(1.0);
    if gpu >= 0 && gpu < 70 && cpu_sat {
        return Bound::Cpu;
    }
    if gpu >= 0 && gpu < 25 && s.max_thread_pct < 30.0 {
        return Bound::Light;
    }
    Bound::Balanced
}

fn thread_ticks(pid: u32, tid: u32) -> Option<u64> {
    let t = fsx::read_raw(&p(&format!("/proc/{pid}/task/{tid}/stat")))?;
    // fields after "(comm)": state is field 3, utime 14, stime 15
    let rest = &t[t.rfind(')')? + 2..];
    let f: Vec<&str> = rest.split_whitespace().collect();
    Some(f.get(11)?.parse::<u64>().ok()? + f.get(12)?.parse::<u64>().ok()?)
}

#[derive(Default)]
pub struct Sampler {
    prev: HashMap<(u32, u32), u64>,
    prev_t: Option<Instant>,
    candidate: Option<(Bound, u32)>,
    pub current: Option<Bound>,
    pub last: Sample,
}

impl Sampler {
    /// CPU usage of the game's threads since the previous call.
    pub fn cpu(&mut self, pids: &BTreeSet<u32>, now: Instant) -> (f64, f64) {
        let mut cur = HashMap::new();
        for pid in pids {
            for t in fsx::list_dir(&p(&format!("/proc/{pid}/task"))) {
                if let Some(tid) = t.file_name().and_then(|n| n.to_str()).and_then(|n| n.parse::<u32>().ok()) {
                    if let Some(ticks) = thread_ticks(*pid, tid) {
                        cur.insert((*pid, tid), ticks);
                    }
                }
            }
        }
        let (mut max, mut total) = (0.0f64, 0.0f64);
        if let Some(t0) = self.prev_t {
            let dt = now.duration_since(t0).as_secs_f64();
            if dt > 0.0 {
                for (k, v) in &cur {
                    if let Some(old) = self.prev.get(k) {
                        let pct = 100.0 * (v.saturating_sub(*old)) as f64 / USER_HZ / dt;
                        max = max.max(pct);
                        total += pct;
                    }
                }
            }
        }
        self.prev = cur;
        self.prev_t = Some(now);
        (max, total)
    }

    /// Feed one sample; returns Some(new class) when the stable class changes.
    pub fn feed(&mut self, s: Sample, cpus: f64) -> Option<Bound> {
        self.last = s;
        let b = classify(&s, cpus);
        let n = match self.candidate {
            Some((c, n)) if c == b => n + 1,
            _ => 1,
        };
        self.candidate = Some((b, n));
        if n >= HOLD && self.current != Some(b) {
            self.current = Some(b);
            return Some(b);
        }
        None
    }

    pub fn reset(&mut self) {
        *self = Sampler::default();
    }
}

/// Number of CPUs in a cpuset list such as "2-7" or "0,2-3".
pub fn count_cpus(list: &str) -> usize {
    list.split(',')
        .filter_map(|r| {
            let r = r.trim();
            if r.is_empty() {
                return None;
            }
            match r.split_once('-') {
                Some((a, b)) => Some(b.parse::<usize>().ok()?.saturating_sub(a.parse::<usize>().ok()?) + 1),
                None => r.parse::<usize>().ok().map(|_| 1),
            }
        })
        .sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classes_and_hysteresis() {
        let s = |g: i64, m: f64, t: f64| Sample { gpu_busy: Some(g), max_thread_pct: m, total_pct: t };
        assert_eq!(classify(&s(97, 40.0, 200.0), 8.0), Bound::Gpu);
        assert_eq!(classify(&s(55, 99.0, 180.0), 8.0), Bound::Cpu); // render thread saturated
        assert_eq!(classify(&s(10, 12.0, 30.0), 8.0), Bound::Light);
        assert_eq!(classify(&s(75, 60.0, 300.0), 8.0), Bound::Balanced);
        assert_eq!(classify(&Sample { gpu_busy: None, max_thread_pct: 99.0, total_pct: 99.0 }, 8.0), Bound::Balanced);
        let mut sm = Sampler::default();
        assert_eq!(sm.feed(s(97, 1.0, 1.0), 8.0), None);
        assert_eq!(sm.feed(s(97, 1.0, 1.0), 8.0), None);
        assert_eq!(sm.feed(s(97, 1.0, 1.0), 8.0), Some(Bound::Gpu));
        assert_eq!(sm.feed(s(50, 99.0, 99.0), 8.0), None); // one CPU-bound sample is not enough
        assert_eq!(sm.feed(s(97, 1.0, 1.0), 8.0), None);
        assert_eq!(count_cpus("2-7"), 6);
        assert_eq!(count_cpus("0,2-3"), 3);
    }

    #[test]
    fn thread_cpu_from_proc() {
        let _g = crate::TEST_ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let root = std::env::temp_dir().join(format!("orbis-wl-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let st = |tid: u32, ticks: u64| {
            let f = root.join(format!("proc/70/task/{tid}/stat"));
            std::fs::create_dir_all(f.parent().unwrap()).unwrap();
            std::fs::write(f, format!("{tid} (game (main)) R 1 70 70 0 -1 0 0 0 0 0 {ticks} 0 0 0 20 0 1 0\n")).unwrap();
        };
        st(70, 100);
        st(71, 0);
        std::env::set_var("ORBIS_ROOT", &root);
        let mut sm = Sampler::default();
        let t0 = Instant::now();
        sm.cpu(&[70u32].into_iter().collect(), t0);
        st(70, 200); // +100 ticks = 1 s of CPU
        st(71, 50);
        let (max, total) = sm.cpu(&[70u32].into_iter().collect(), t0 + std::time::Duration::from_secs(1));
        std::env::remove_var("ORBIS_ROOT");
        let _ = std::fs::remove_dir_all(&root);
        assert!((max - 100.0).abs() < 0.01, "{max}");
        assert!((total - 150.0).abs() < 0.01, "{total}");
    }
}
