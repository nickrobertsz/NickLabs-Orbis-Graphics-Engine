//! orbis-run side of pre-launch admission (GE-A03); protocol in orbis_core::admit.
//!
//! Order guaranteed for a game whose profile sets `memory.dmem_max_mb` / `memory.gtt_max_mb`:
//!   scope created (systemd-run --user --scope, holding only orbis-run stage 2)
//!   -> request to orbis-perf -> dmem controller enabled down to the scope -> dmem.max written
//!   -> dmem.max read back by stage 2 -> exec of the game (Steam reaper, Proton, Wine,
//!      Gamescope or a native binary: whatever argv[0] is, it inherits the scope).
//! With `memory.require_limits: true` any missing limit stops the launch (exit 75).
//! Without a scope (no user manager, --no-scope) required limits stop the launch too; optional
//! ones are left to orbis-perf, which applies them retroactively (later allocations only).

use orbis_core::admit::{self, Request};
use orbis_core::fsx;
use orbis_core::json::{self, Value};
use orbis_core::jobj;
use std::path::Path;
use std::time::{Duration, Instant};

pub const ENV_VRAM: &str = "ORBIS_ADMIT_VRAM";
pub const ENV_GTT: &str = "ORBIS_ADMIT_GTT";
pub const ENV_REQUIRE: &str = "ORBIS_ADMIT_REQUIRE";
pub const ENV_RESULT: &str = "ORBIS_ADMIT_RESULT";
pub const ENV_TITLE: &str = "ORBIS_ADMIT_TITLE";
pub const STAGE2_FLAG: &str = "--admit-exec";

/// Limits a profile asks for: (vram bytes, gtt bytes, required)
pub fn profile_limits(doc: &Value) -> (Option<u64>, Option<u64>, bool) {
    let mb = |k: &str| doc.at(k).and_then(Value::as_i64).filter(|v| *v > 0).map(|v| (v as u64) << 20);
    (mb("memory.dmem_max_mb"), mb("memory.gtt_max_mb"), doc.at("memory.require_limits").and_then(Value::as_bool).unwrap_or(false))
}

/// Outcome of stage 2 before exec: whether to start the game and the record kept in the log dir.
pub fn decide(reply: Option<&Value>, checks: &[(String, bool, String)], require: bool) -> (bool, Value) {
    let verified = !checks.is_empty() && checks.iter().all(|(_, ok, _)| *ok);
    let failed: Vec<Value> = checks.iter().filter(|(_, ok, _)| !ok).map(|(k, _, d)| jobj! {"kind" => k.clone(), "detail" => d.clone()}).collect();
    let state = if verified {
        "ADMITTED"
    } else if reply.is_none() {
        "NO_REPLY"
    } else if reply.and_then(|r| r.get("rejected")).is_some() {
        "REJECTED"
    } else {
        "INCOMPLETE"
    };
    let start = verified || !require;
    let rec = jobj! {"schema" => "orbis-run-admission/1", "state" => state, "required" => require,
        "game_started" => start, "failed" => Value::Arr(failed),
        "verified" => Value::Arr(checks.iter().map(|(k, ok, d)| jobj! {"kind" => k.clone(), "ok" => *ok, "detail" => d.clone()}).collect()),
        "reply" => reply.cloned(),
        "note" => if verified { "limits in force before the game's first instruction" }
                  else if start { "game started without (all) limits; orbis-perf applies them retroactively if it can" }
                  else { "required limits could not be applied: the game was not started" }};
    (start, rec)
}

/// Send the request for `pid` and wait for orbis-perf's reply (None after the timeout).
pub fn request(req: &Request, timeout: Duration) -> Option<Value> {
    let rp = admit::reply_path(req.pid);
    fsx::atomic_write(&admit::req_path(req.pid), req.to_json().to_string_compact().as_bytes()).ok()?;
    let t0 = Instant::now();
    while t0.elapsed() < timeout {
        // only the reply to this request: a leftover reply for a recycled PID has another nonce
        if let Some(v) = fsx::read_raw(&rp).and_then(|t| json::parse(&t).ok()) {
            if v.get("nonce").and_then(Value::as_str) == Some(req.nonce.as_str()) {
                return Some(v);
            }
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    // nobody answered: withdraw the request so a late daemon does not act on a dead PID
    let _ = std::fs::remove_file(admit::req_path(req.pid));
    None
}

/// Stage 2: runs inside the fresh scope. Admits, verifies, then calls `exec` (which replaces
/// the process with the game). Returns the exit code when the game is not started.
pub fn stage2(exec: impl FnOnce() -> i32, timeout: Duration) -> i32 {
    let env_u = |k: &str| std::env::var(k).ok().and_then(|v| v.parse::<u64>().ok()).filter(|v| *v > 0);
    let req = Request {
        pid: std::process::id(),
        vram_max: env_u(ENV_VRAM),
        gtt_max: env_u(ENV_GTT),
        require: std::env::var(ENV_REQUIRE).map(|v| v == "1").unwrap_or(false),
        title: std::env::var(ENV_TITLE).unwrap_or_default(),
        nonce: admit::new_nonce(),
    };
    let result_file = std::env::var(ENV_RESULT).ok();
    for k in [ENV_VRAM, ENV_GTT, ENV_REQUIRE, ENV_RESULT, ENV_TITLE] {
        std::env::remove_var(k);
    }
    let (start, rec) = if req.limits().is_empty() {
        (true, jobj! {"schema" => "orbis-run-admission/1", "state" => "NOT_REQUESTED", "game_started" => true})
    } else {
        let reply = request(&req, timeout);
        let checks = match admit::cgroup_of(req.pid) {
            Some(cg) => admit::verify(&cg, &req),
            None => req.limits().iter().map(|(k, _)| (k.to_string(), false, "own cgroup unknown".to_string())).collect(),
        };
        decide(reply.as_ref(), &checks, req.require)
    };
    if let Some(f) = &result_file {
        let _ = fsx::atomic_write(Path::new(f), rec.to_string_pretty().as_bytes());
    }
    for f in rec.get("failed").and_then(Value::as_arr).into_iter().flatten() {
        eprintln!("orbis-run: admission: {} limit not in force: {}", f.get("kind").and_then(Value::as_str).unwrap_or(""),
                  f.get("detail").and_then(Value::as_str).unwrap_or(""));
    }
    if !start {
        eprintln!("orbis-run: required memory limits could not be applied; the game was not started (exit {})", admit::EXIT_ADMISSION);
        return admit::EXIT_ADMISSION;
    }
    exec()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    const VR: &str = "drm/0000:00:01.0/vram";

    fn fixture(tag: &str) -> std::path::PathBuf {
        let r = std::env::temp_dir().join(format!("orbis-run-admit-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&r);
        let me = std::process::id();
        let w = |rel: &str, c: &str| {
            let f = r.join(rel);
            fs::create_dir_all(f.parent().unwrap()).unwrap();
            fs::write(f, c).unwrap();
        };
        w("sys/fs/cgroup/dmem.capacity", &format!("{VR} 1073741824\n"));
        w(&format!("proc/{me}/cgroup"), "0::/user.slice/user-1000.slice/orbis-game-1.scope\n");
        w("sys/fs/cgroup/user.slice/user-1000.slice/orbis-game-1.scope/dmem.max", &format!("{VR} max\n"));
        fs::create_dir_all(r.join("run/orbis/admit")).unwrap();
        r
    }

    #[test]
    fn decisions() {
        let ok = vec![("vram".to_string(), true, "x".to_string())];
        let bad = vec![("gtt".to_string(), false, "kernel reports: unavailable".to_string())];
        assert!(decide(Some(&jobj! {"admitted" => true}), &ok, true).0);
        assert!(!decide(None, &bad, true).0);
        assert_eq!(decide(None, &bad, true).1.get("state").and_then(Value::as_str), Some("NO_REPLY"));
        let (start, rec) = decide(Some(&jobj! {"rejected" => "x"}), &bad, false);
        assert!(start && rec.get("state").and_then(Value::as_str) == Some("REJECTED"));
        let prof = json::parse(r#"{"memory":{"dmem_max_mb":512,"gtt_max_mb":0,"require_limits":true}}"#).unwrap();
        assert_eq!(profile_limits(&prof), (Some(512 << 20), None, true));
    }

    /// The game starts only after its limit is in force (a stand-in for orbis-perf answers).
    #[test]
    fn game_starts_only_after_limits_are_in_force() {
        let _g = LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let r = fixture("order");
        std::env::set_var("ORBIS_ROOT", &r);
        std::env::set_var(ENV_VRAM, (512u64 << 20).to_string());
        std::env::set_var(ENV_REQUIRE, "1");
        let me = std::process::id();
        let r2 = r.clone();
        let server = std::thread::spawn(move || {
            let req = r2.join(format!("run/orbis/admit/{me}.req"));
            for _ in 0..400 {
                if req.exists() {
                    std::thread::sleep(Duration::from_millis(30)); // a slow daemon
                    fs::write(r2.join("sys/fs/cgroup/user.slice/user-1000.slice/orbis-game-1.scope/dmem.max"), format!("{VR} {}\n", 512u64 << 20)).unwrap();
                    let n = orbis_core::json::parse(&fs::read_to_string(&req).unwrap()).unwrap().get("nonce").and_then(Value::as_str).unwrap().to_string();
                    fs::create_dir_all(r2.join("run/orbis/admit-replies")).unwrap();
                    // a stale reply for this PID first: must be ignored
                    fs::write(r2.join(format!("run/orbis/admit-replies/{me}.reply")), r#"{"admitted":true,"nonce":"00"}"#).unwrap();
                    std::thread::sleep(Duration::from_millis(60));
                    fs::write(r2.join(format!("run/orbis/admit-replies/{me}.reply")), format!(r#"{{"admitted":true,"nonce":"{n}"}}"#)).unwrap();
                    let _ = fs::remove_file(&req);
                    return;
                }
                std::thread::sleep(Duration::from_millis(5));
            }
        });
        let r3 = r.clone();
        let code = stage2(move || {
            // at exec time the limit must already be there
            let m = fs::read_to_string(r3.join("sys/fs/cgroup/user.slice/user-1000.slice/orbis-game-1.scope/dmem.max")).unwrap();
            assert!(m.contains(&(512u64 << 20).to_string()), "exec before admission: {m}");
            0
        }, Duration::from_secs(5));
        server.join().unwrap();
        assert_eq!(code, 0);
        assert!(std::env::var(ENV_VRAM).is_err(), "admission variables never reach the game");
        std::env::remove_var("ORBIS_ROOT");
    }

    /// Required limits and no daemon: the game is never started.
    #[test]
    fn required_limits_without_daemon_refuse_launch() {
        let _g = LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let r = fixture("nodaemon");
        std::env::set_var("ORBIS_ROOT", &r);
        std::env::set_var(ENV_VRAM, (512u64 << 20).to_string());
        std::env::set_var(ENV_REQUIRE, "1");
        let res = r.join("admission.json");
        std::env::set_var(ENV_RESULT, &res);
        let code = stage2(|| panic!("the game must not start"), Duration::from_millis(200));
        assert_eq!(code, admit::EXIT_ADMISSION);
        let rec = json::parse(&fs::read_to_string(&res).unwrap()).unwrap();
        assert_eq!(rec.get("state").and_then(Value::as_str), Some("NO_REPLY"));
        assert!(!r.join(format!("run/orbis/admit/{}.req", std::process::id())).exists(), "request withdrawn");
        // optional limits: the game starts, the record says so
        std::env::set_var(ENV_VRAM, (512u64 << 20).to_string());
        let code = stage2(|| 7, Duration::from_millis(100));
        assert_eq!(code, 7);
        std::env::remove_var("ORBIS_ROOT");
    }
}
