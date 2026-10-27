//! `orbis-perf bench`: measure the schedulers instead of assuming one is best.
//!
//! For each available scheduler (EEVDF, BORE when the kernel has it, installed sched_ext
//! schedulers) it runs, for a bounded time:
//!   * a CPU-bound load on every core (throughput = loop iterations per second), and
//!   * wake-up probes that sleep 1 ms and record how late they wake (p50 / p99 / max).
//! Interactive/gaming workloads get the lowest p99 wake-up latency among schedulers that keep at
//! least 85 % of the best throughput; AI/CPU-heavy workloads get the best throughput among those
//! within 2x of the lowest p99. The result feeds the "auto" scheduler setting.

use crate::sched::{self, Failures};
use orbis_core::fsx::{self, p};
use orbis_core::{jobj, Value};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

pub struct Sample {
    pub sched: String,
    pub p50_us: f64,
    pub p99_us: f64,
    pub max_us: f64,
    pub throughput: f64,
}

pub fn percentile(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    let i = ((sorted.len() as f64 - 1.0) * q).round() as usize;
    sorted[i.min(sorted.len() - 1)]
}

pub fn measure(seconds: f64, load_threads: usize) -> (Vec<f64>, f64) {
    let stop = Arc::new(AtomicBool::new(false));
    let iters = Arc::new(AtomicU64::new(0));
    let mut hs = vec![];
    for _ in 0..load_threads {
        let (st, it) = (stop.clone(), iters.clone());
        hs.push(std::thread::spawn(move || {
            let mut x: u64 = 0x9E3779B97F4A7C15;
            let mut local = 0u64;
            while !st.load(Ordering::Relaxed) {
                for _ in 0..4096 {
                    x ^= x << 13;
                    x ^= x >> 7;
                    x ^= x << 17;
                }
                local += 1;
            }
            it.fetch_add(local, Ordering::Relaxed);
            std::hint::black_box(x);
        }));
    }
    let mut probes = vec![];
    for _ in 0..2 {
        let st = stop.clone();
        probes.push(std::thread::spawn(move || {
            let mut lat = Vec::with_capacity(20000);
            while !st.load(Ordering::Relaxed) {
                let t = Instant::now();
                std::thread::sleep(Duration::from_millis(1));
                let el = t.elapsed().as_secs_f64() * 1e6 - 1000.0;
                lat.push(el.max(0.0));
            }
            lat
        }));
    }
    std::thread::sleep(Duration::from_secs_f64(seconds));
    stop.store(true, Ordering::Relaxed);
    for h in hs {
        let _ = h.join();
    }
    let mut lat: Vec<f64> = probes.into_iter().flat_map(|h| h.join().unwrap_or_default()).collect();
    lat.sort_by(|a, b| a.partial_cmp(b).unwrap());
    (lat, iters.load(Ordering::Relaxed) as f64 / seconds)
}

pub fn recommend(samples: &[Sample]) -> Value {
    if samples.is_empty() {
        return Value::obj();
    }
    let best_tp = samples.iter().map(|s| s.throughput).fold(0.0, f64::max);
    let min_p99 = samples.iter().map(|s| s.p99_us).fold(f64::INFINITY, f64::min);
    let interactive = samples
        .iter()
        .filter(|s| s.throughput >= best_tp * 0.85)
        .min_by(|a, b| a.p99_us.partial_cmp(&b.p99_us).unwrap())
        .map(|s| s.sched.clone());
    let batch = samples
        .iter()
        .filter(|s| s.p99_us <= min_p99 * 2.0)
        .max_by(|a, b| a.throughput.partial_cmp(&b.throughput).unwrap())
        .map(|s| s.sched.clone());
    jobj! {"gaming" => interactive.clone(), "pc" => interactive.clone(), "shader" => batch.clone(),
           "ai" => batch.clone(), "cpu-heavy" => batch, "interactive" => interactive}
}

pub fn run(seconds: f64, only: Option<Vec<String>>) -> Value {
    let ncpu = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(8);
    let before = sched::active();
    let mut fails = Failures::load();
    let candidates: Vec<String> = sched::available()
        .into_iter()
        .filter(|s| only.as_ref().map(|o| o.contains(s)).unwrap_or(true))
        .collect();
    let mut samples = vec![];
    let mut runs = vec![];
    for s in &candidates {
        let sw = sched::switch(s, &[], &mut fails);
        let ok = matches!(sw.get("state").and_then(Value::as_str), Some("APPLIED" | "UNCHANGED"));
        if !ok {
            runs.push(jobj! {"scheduler" => s.clone(), "state" => "NOT_RUN", "why" => sw});
            continue;
        }
        std::thread::sleep(Duration::from_millis(1500));
        let (lat, tp) = measure(seconds, ncpu);
        let smp = Sample { sched: s.clone(), p50_us: percentile(&lat, 0.5), p99_us: percentile(&lat, 0.99),
                           max_us: lat.last().copied().unwrap_or(f64::NAN), throughput: tp };
        runs.push(jobj! {"scheduler" => s.clone(), "state" => "PASS", "wakeup_p50_us" => smp.p50_us, "wakeup_p99_us" => smp.p99_us,
                         "wakeup_max_us" => smp.max_us, "throughput_iter_per_s" => smp.throughput, "samples" => lat.len()});
        samples.push(smp);
    }
    let _ = sched::switch(&before, &[], &mut fails);
    let doc = jobj! {"schema" => "orbis-perf-bench/1", "time" => orbis_core::sys::now_unix().0, "seconds_each" => seconds,
                     "load_threads" => ncpu, "runs" => Value::Arr(runs), "recommendation" => recommend(&samples),
                     "note" => "synthetic: wake-up latency under full CPU load + CPU throughput; confirm with real games (frame times)"};
    let _ = fsx::atomic_write(&p("/var/lib/orbis/perf/bench.json"), doc.to_string_pretty().as_bytes());
    doc
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recommendation_rules() {
        let s = |n: &str, p99: f64, tp: f64| Sample { sched: n.into(), p50_us: 0.0, p99_us: p99, max_us: 0.0, throughput: tp };
        let r = recommend(&[s("eevdf", 900.0, 100.0), s("scx_lavd", 300.0, 110.0), s("scx_rusty", 500.0, 120.0), s("x", 100.0, 50.0)]);
        assert_eq!(r.get("gaming").unwrap().as_str(), Some("scx_lavd")); // x loses too much throughput
        assert_eq!(r.get("ai").unwrap().as_str(), Some("x")); // only x is within 2x of the lowest p99
        let r2 = recommend(&[s("eevdf", 400.0, 110.0), s("scx_rusty", 500.0, 120.0)]);
        assert_eq!(r2.get("ai").unwrap().as_str(), Some("scx_rusty"));
        assert_eq!(r2.get("gaming").unwrap().as_str(), Some("eevdf"));
    }

    #[test]
    fn measure_runs_briefly() {
        let (lat, tp) = measure(0.3, 2);
        assert!(!lat.is_empty() && tp > 0.0);
        assert!(percentile(&lat, 0.5) <= percentile(&lat, 0.99));
    }
}
