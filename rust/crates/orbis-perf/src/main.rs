//! orbis-perf: the unified NickLabs Orbis performance controller (Rust).
//!
//!   orbis-perf status [--json]            everything in one view (mode, scheduler, CPU/GPU clocks and load,
//!                                         memory, pressure, temperature/cooling, Vulkan, games, time source)
//!   orbis-perf apply                      apply the current mode's policy now (the daemon does this itself)
//!   orbis-perf daemon [--interval 2]      orbis-perf.service
//!   orbis-perf restore                    undo everything orbis-perf changed
//!   orbis-perf sched [list|set <name>|bench [--seconds 8] [--only a,b]]
//!   orbis-perf games [--json]             detected game sessions and profiles
//!   orbis-perf profiles [check]           per-game profiles found
//!   orbis-perf scx-exec <scheduler>       ExecStart of orbis-scx@.service (execs the scheduler with its args)
//!   orbis-perf policy [pc|gaming|ai]      show a mode's policy
//!   orbis-perf doctor [--json]            on-device checks for the native components (exit 1 on FAIL)

mod bench;
mod control;
mod doctor;
mod games;
mod hw;
mod policy;
mod sched;
mod workload;

/// Tests that point ORBIS_ROOT at a fixture tree take this lock (the variable is process-wide).
#[cfg(test)]
pub static TEST_ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

use orbis_core::fsx::{self, p};
use orbis_core::json::{self, Value};
use orbis_core::{emit, jobj, Args};
use std::os::unix::process::CommandExt;

fn meminfo() -> Value {
    let mut v = Value::obj();
    if let Some(t) = fsx::read_raw(&p("/proc/meminfo")) {
        for l in t.lines() {
            if let Some((k, rest)) = l.split_once(':') {
                if ["MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "AnonHugePages"].contains(&k) {
                    let kb: i64 = rest.split_whitespace().next().and_then(|x| x.parse().ok()).unwrap_or(0);
                    v.set(&format!("{}_mib", k.to_lowercase()), kb / 1024);
                }
            }
        }
    }
    v
}

fn psi(kind: &str) -> Option<String> {
    fsx::read(&p(&format!("/proc/pressure/{kind}"))).and_then(|t| t.lines().next().map(str::to_string))
}

fn vulkan_caps() -> Value {
    fsx::read_raw(&p("/var/lib/orbis/games/vulkan-caps.json"))
        .and_then(|t| json::parse(&t).ok())
        .map(|v| jobj! {"device" => v.at("device.name").cloned(), "api" => v.at("device.api_version").cloned(),
                         "software_renderer" => v.at("device.software").cloned(),
                         "dxvk" => v.at("selection.dxvk").cloned(), "vkd3d" => v.at("selection.vkd3d").cloned(),
                         "probed_at" => v.get("time").cloned()})
        .unwrap_or_else(|| jobj! {"probed" => false, "how" => "orbis-games caps"})
}

fn compat_layers() -> Vec<String> {
    let mut out = vec![];
    for pid in orbis_core::procs::pids() {
        if let Some(c) = fsx::read(&p(&format!("/proc/{pid}/comm"))) {
            let tag = match c.as_str() {
                "wineserver" => "wine/proton (wineserver)",
                "pressure-vessel" | "pv-bwrap" | "pressure-vessel-" => "steam runtime (pressure-vessel)",
                "gamescope" => "gamescope",
                "lutris" => "lutris",
                _ => continue,
            };
            if !out.iter().any(|x: &String| x == tag) {
                out.push(tag.to_string());
            }
        }
    }
    out
}

fn status() -> Value {
    let live = fsx::read_raw(&p("/run/orbis/perf/state.json")).and_then(|t| json::parse(&t).ok());
    let g = hw::gpu_dir();
    let gpu = jobj! {
        "sclk" => g.as_ref().and_then(|d| fsx::read(&d.join("ps4_sclk"))),
        "engine_busy" => g.as_ref().and_then(|d| fsx::read(&d.join("orbis_engine_busy"))),
        "vram_total_mib" => hw::vram_total().map(|v| (v >> 20) as i64),
        "vram_used_mib" => g.as_ref().and_then(|d| fsx::read_u64(&d.join("mem_info_vram_used"))).map(|v| (v >> 20) as i64),
        "gtt_used_mib" => g.as_ref().and_then(|d| fsx::read_u64(&d.join("mem_info_gtt_used"))).map(|v| (v >> 20) as i64),
        "gtt_budget" => g.as_ref().and_then(|d| fsx::read(&d.join("orbis_gtt_budget")))
    };
    let fan = hw::fan_hwmon();
    let freqs: Vec<Value> = hw::cpu_freqs().into_iter().map(|(c, f)| jobj! {"cpu" => c, "khz" => f}).collect();
    let gov = hw::cpufreq_policies().first().and_then(|pp| fsx::read(&pp.join("scaling_governor")));
    jobj! {
        "schema" => "orbis-perf-status/1",
        "mode" => policy::current_mode(),
        "daemon" => live.clone().unwrap_or_else(|| jobj! {"running" => false}),
        "scheduler" => jobj! {"active" => sched::active(), "available" => sched::available(),
                              "sched_ext" => sched::scx_state().0, "bore_kernel" => sched::bore_available()},
        "cpu" => jobj! {"governor" => gov, "frequencies" => Value::Arr(freqs), "l2_modules" => hw::l2_groups(),
                        "psi" => psi("cpu")},
        "gpu" => gpu,
        "memory" => {
            let mut m = meminfo();
            m.set("psi", psi("memory"));
            m.set("thp", hw::active(&p("/sys/kernel/mm/transparent_hugepage/enabled")));
            m.set("mglru", fsx::read(&p("/sys/kernel/mm/lru_gen/enabled")));
            m.set("zram", fsx::read(&p("/sys/block/zram0/comp_algorithm")).map(|c| orbis_core::restore::active_choice(&c)));
            m
        },
        "thermal" => jobj! {"apu_c" => hw::apu_temp_mc().map(|t| t as f64 / 1000.0),
                            "fan_threshold_c" => fan.as_ref().and_then(|h| fsx::read_i64(&h.join("temp1_crit"))).map(|t| t / 1000),
                            "guard_c" => [control::HOT_MC / 1000, control::COOL_MC / 1000].to_vec()},
        "vulkan" => vulkan_caps(),
        "compatibility_layers_running" => compat_layers(),
        "time_source" => fsx::read_raw(&p("/run/orbis/time-source.json")).and_then(|t| json::parse(&t).ok())
            .map(|v| jobj! {"source" => v.get("source").cloned(), "valid" => v.get("valid").cloned()})
    }
}

fn policy_view(mode: &str) -> Value {
    let pol = policy::mode_policy(mode);
    jobj! {"mode" => pol.name.clone(), "scheduler" => pol.scheduler.clone(), "scx_args" => pol.scx_args.clone(),
           "gpu" => format!("{:?}", pol.gpu).to_lowercase(), "fan_threshold_c" => pol.fan_threshold_c,
           "thp" => format!("{} / defrag {}", pol.thp_enabled, pol.thp_defrag), "mglru_min_ttl_ms" => pol.mglru_min_ttl_ms,
           "io_scheduler_preference" => pol.io_scheduler.clone(), "background_cpus" => pol.background_cpus.clone(),
           "gpu_irq_cpu" => pol.irq_gpu_cpu.map(|c| c as i64), "session_governor" => pol.session_governor.clone(),
           "left_to_orbis_0_2_0" => policy::orbisd_sysctls(mode), "manual_overrides" => policy::manual_overrides()}
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let a = Args::parse(&argv, &["json", "dry-run"]);
    let cmd = a.pos.first().map(String::as_str).unwrap_or("status");
    let out: Result<Value, String> = match cmd {
        "status" => Ok(status()),
        "doctor" => {
            let (v, failed) = doctor::run();
            emit(&v, a.flag("json"));
            std::process::exit(if failed { 1 } else { 0 });
        }
        "policy" => Ok(policy_view(a.pos.get(1).map(String::as_str).unwrap_or(&policy::current_mode()))),
        "apply" => {
            let mut c = control::Controller::new();
            Ok(Value::Arr(c.apply_mode()))
        }
        "daemon" => {
            let iv: f64 = a.get("interval").and_then(|v| v.parse().ok()).unwrap_or(2.0);
            control::run_daemon(std::time::Duration::from_secs_f64(iv.clamp(0.5, 30.0)))
        }
        "restore" => Ok(control::Controller::new().restore_all()),
        "games" => {
            let procs = orbis_core::procs::all(false);
            let profs = policy::load_profiles(&policy::profile_dirs());
            let s = games::detect(&procs, &profs, orbis_core::sys::now_unix().0);
            Ok(Value::Arr(s.values().map(|x| jobj! {"session" => x.key.clone(), "title" => x.title.clone(), "via" => x.via.clone(),
                "pids" => x.pids.iter().map(|p| *p as i64).collect::<Vec<i64>>(),
                "profile" => x.profile.as_ref().map(|g| g.source.clone())}).collect()))
        }
        "profiles" => {
            let profs = policy::load_profiles(&policy::profile_dirs());
            Ok(Value::Arr(profs.iter().map(|g| jobj! {"name" => g.name.clone(), "file" => g.source.clone(),
                "steam_appid" => g.steam_appid.clone(), "exe" => g.exe.clone()}).collect()))
        }
        "sched" => match a.pos.get(1).map(String::as_str).unwrap_or("list") {
            "list" => Ok(jobj! {"active" => sched::active(), "available" => sched::available(),
                                "sched_ext_state" => sched::scx_state().0, "bench" => fsx::read_raw(&p("/var/lib/orbis/perf/bench.json")).and_then(|t| json::parse(&t).ok()).and_then(|v| v.get("recommendation").cloned())}),
            "set" => match a.pos.get(2) {
                Some(n) => {
                    let mut f = sched::Failures::load();
                    Ok(sched::switch(n, &[], &mut f))
                }
                None => Err("usage: orbis-perf sched set <eevdf|bore|scx_...>".into()),
            },
            "bench" => {
                let secs: f64 = a.get("seconds").and_then(|v| v.parse::<f64>().ok()).unwrap_or(8.0).clamp(2.0, 60.0);
                let only = a.get("only").map(|s| s.split(',').map(str::to_string).collect());
                Ok(bench::run(secs, only))
            }
            other => Err(format!("unknown sched action {other}")),
        },
        "scx-exec" => {
            let name = a.pos.get(1).cloned().unwrap_or_default();
            if !name.starts_with("scx_") || name.contains('/') {
                Err("scx-exec needs a scheduler name like scx_lavd".into())
            } else {
                match sched::scx_binary(&name) {
                    Some(bin) => {
                        let args = sched::scx_args(&name, &[]);
                        let e = std::process::Command::new(bin).args(&args).exec();
                        Err(format!("exec {name}: {e}"))
                    }
                    None => Err(format!("{name} is not installed (scx-scheds)")),
                }
            }
        }
        "-h" | "--help" | "help" => {
            println!("orbis-perf status|doctor|policy|apply|daemon|restore|sched|games|profiles|scx-exec  [--json]");
            return;
        }
        other => Err(format!("unknown command {other}")),
    };
    match out {
        Ok(v) => emit(&v, a.flag("json")),
        Err(e) => {
            eprintln!("orbis-perf: {e}");
            std::process::exit(2);
        }
    }
}
