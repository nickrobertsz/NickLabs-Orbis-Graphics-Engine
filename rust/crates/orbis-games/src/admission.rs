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
    decide_state(None, reply, checks, require)
}

/// `forced`: a state decided before any check could run (NOT_IN_SCOPE, REQUEST_FAILED)
pub fn decide_state(forced: Option<(&str, String)>, reply: Option<&Value>, checks: &[(String, bool, String)], require: bool) -> (bool, Value) {
    let verified = forced.is_none() && !checks.is_empty() && checks.iter().all(|(_, ok, _)| *ok);
    let mut failed: Vec<Value> = checks.iter().filter(|(_, ok, _)| !ok).map(|(k, _, d)| jobj! {"kind" => k.clone(), "detail" => d.clone()}).collect();
    if let Some((st, why)) = &forced {
        failed.push(jobj! {"kind" => "admission", "detail" => format!("{st}: {why}")});
    }
    let state = if let Some((st, _)) = &forced {
        st
    } else if verified {
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

/// Send the request and wait for orbis-perf's reply: Ok(None) after the timeout, Err when the
/// request could not even be written (the reason is reported, never a silent NO_REPLY).
pub fn request(req: &Request, timeout: Duration) -> Result<Option<Value>, String> {
    let rp = admit::reply_path(req.pid, &req.nonce);
    let rq = admit::write_request(req).map_err(|e| format!("cannot write {}: {e}", admit::req_path(req.pid, &req.nonce).display()))?;
    let t0 = Instant::now();
    while t0.elapsed() < timeout {
        // only the reply to this request: a leftover reply for a recycled PID has another nonce
        if let Some(v) = fsx::read_raw(&rp).and_then(|t| json::parse(&t).ok()) {
            if v.get("nonce").and_then(Value::as_str) == Some(req.nonce.as_str()) {
                let _ = std::fs::remove_file(&rp);
                return Ok(Some(v));
            }
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    // nobody answered: withdraw the request so a late daemon does not act on it
    let _ = std::fs::remove_file(&rq);
    Ok(None)
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
        starttime: admit::proc_starttime(std::process::id()).unwrap_or(0),
    };
    let result_file = std::env::var(ENV_RESULT).ok();
    for k in [ENV_VRAM, ENV_GTT, ENV_REQUIRE, ENV_RESULT, ENV_TITLE] {
        std::env::remove_var(k);
    }
    let (start, rec) = if req.limits().is_empty() {
        (true, jobj! {"schema" => "orbis-run-admission/1", "state" => "NOT_REQUESTED", "game_started" => true})
    } else {
        // systemd-run must have put us in our own orbis-game scope; otherwise do not even ask
        let cg = admit::cgroup_of(req.pid);
        let in_scope = cg.as_deref().map(|c| c.ends_with(".scope") && c.contains("/orbis-game.slice/")).unwrap_or(false);
        if !in_scope {
            decide_state(Some(("NOT_IN_SCOPE", format!("own cgroup {cg:?} is not an orbis-game scope (systemd-run --user --scope)"))), None, &[], req.require)
        } else if req.starttime == 0 {
            decide_state(Some(("REQUEST_FAILED", "own process start time unreadable".into())), None, &[], req.require)
        } else {
            match request(&req, timeout) {
                Err(e) => decide_state(Some(("REQUEST_FAILED", e)), None, &[], req.require),
                Ok(reply) => {
                    let checks = admit::verify(cg.as_deref().unwrap_or("/"), &req);
                    decide(reply.as_ref(), &checks, req.require)
                }
            }
        }
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
    const SCOPE: &str = "user.slice/user-1000.slice/user@1000.service/orbis-game.slice/orbis-game-1.scope";

    fn fixture(tag: &str, cgroup: &str) -> std::path::PathBuf {
        let r = std::env::temp_dir().join(format!("orbis-run-admit-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&r);
        let me = std::process::id();
        let w = |rel: &str, c: &str| {
            let f = r.join(rel);
            fs::create_dir_all(f.parent().unwrap()).unwrap();
            fs::write(f, c).unwrap();
        };
        w("sys/fs/cgroup/dmem.capacity", &format!("{VR} 1073741824\n"));
        w(&format!("proc/{me}/cgroup"), &format!("0::/{cgroup}\n"));
        // comm with spaces and parentheses: the start time is field 22 counted after the last ')'
        w(&format!("proc/{me}/stat"), &format!("{me} (orbis run (x)) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 424242 0 0\n"));
        w(&format!("sys/fs/cgroup/{SCOPE}/dmem.max"), &format!("{VR} max\n"));
        fs::create_dir_all(r.join("run/orbis/admit")).unwrap();
        r
    }

    fn pending(r: &std::path::Path, me: u32) -> Option<std::path::PathBuf> {
        fs::read_dir(r.join("run/orbis/admit")).ok()?.flatten().map(|e| e.path()).find(|p| {
            let n = p.file_name().unwrap().to_string_lossy().into_owned();
            n.starts_with(&format!("{me}.")) && n.ends_with(".req")
        })
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
        let (start, rec) = decide_state(Some(("NOT_IN_SCOPE", "x".into())), None, &ok, true);
        assert!(!start && rec.get("state").and_then(Value::as_str) == Some("NOT_IN_SCOPE"));
        let prof = json::parse(r#"{"memory":{"dmem_max_mb":512,"gtt_max_mb":0,"require_limits":true}}"#).unwrap();
        assert_eq!(profile_limits(&prof), (Some(512 << 20), None, true));
    }

    /// The game starts only after its limit is in force (a stand-in for orbis-perf answers).
    #[test]
    fn game_starts_only_after_limits_are_in_force() {
        let _g = LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let r = fixture("order", SCOPE);
        std::env::set_var("ORBIS_ROOT", &r);
        std::env::set_var(ENV_VRAM, (512u64 << 20).to_string());
        std::env::set_var(ENV_REQUIRE, "1");
        let me = std::process::id();
        let r2 = r.clone();
        let server = std::thread::spawn(move || {
            for _ in 0..400 {
                if let Some(req) = pending(&r2, me) {
                    std::thread::sleep(Duration::from_millis(30)); // a slow daemon
                    let doc = orbis_core::json::parse(&fs::read_to_string(&req).unwrap()).unwrap();
                    assert_eq!(doc.get("starttime").and_then(Value::as_i64), Some(424242));
                    {
                        use std::os::unix::fs::PermissionsExt;
                        assert_eq!(fs::metadata(&req).unwrap().permissions().mode() & 0o777, 0o600, "request is private");
                    }
                    fs::write(r2.join(format!("sys/fs/cgroup/{SCOPE}/dmem.max")), format!("{VR} {}\n", 512u64 << 20)).unwrap();
                    let n = doc.get("nonce").and_then(Value::as_str).unwrap().to_string();
                    fs::create_dir_all(r2.join("run/orbis/admit-replies")).unwrap();
                    // a reply with another nonce at our name: ignored
                    fs::write(r2.join(format!("run/orbis/admit-replies/{me}.{n}.reply")), r#"{"admitted":true,"nonce":"00"}"#).unwrap();
                    std::thread::sleep(Duration::from_millis(60));
                    fs::write(r2.join(format!("run/orbis/admit-replies/{me}.{n}.reply")), format!(r#"{{"admitted":true,"nonce":"{n}"}}"#)).unwrap();
                    let _ = fs::remove_file(&req);
                    return;
                }
                std::thread::sleep(Duration::from_millis(5));
            }
            panic!("no request appeared");
        });
        let r3 = r.clone();
        let code = stage2(move || {
            // at exec time the limit must already be there
            let m = fs::read_to_string(r3.join(format!("sys/fs/cgroup/{SCOPE}/dmem.max"))).unwrap();
            assert!(m.contains(&(512u64 << 20).to_string()), "exec before admission: {m}");
            0
        }, Duration::from_secs(5));
        server.join().unwrap();
        assert_eq!(code, 0);
        assert!(std::env::var(ENV_VRAM).is_err(), "admission variables never reach the game");
        std::env::remove_var("ORBIS_ROOT");
    }

    /// Required limits and no daemon: the game is never started; the request is withdrawn.
    #[test]
    fn required_limits_without_daemon_refuse_launch() {
        let _g = LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let r = fixture("nodaemon", SCOPE);
        std::env::set_var("ORBIS_ROOT", &r);
        std::env::set_var(ENV_VRAM, (512u64 << 20).to_string());
        std::env::set_var(ENV_REQUIRE, "1");
        let res = r.join("admission.json");
        std::env::set_var(ENV_RESULT, &res);
        let code = stage2(|| panic!("the game must not start"), Duration::from_millis(200));
        assert_eq!(code, admit::EXIT_ADMISSION);
        let rec = json::parse(&fs::read_to_string(&res).unwrap()).unwrap();
        assert_eq!(rec.get("state").and_then(Value::as_str), Some("NO_REPLY"));
        assert!(pending(&r, std::process::id()).is_none(), "request withdrawn");
        // optional limits: the game starts, the record says so
        std::env::set_var(ENV_VRAM, (512u64 << 20).to_string());
        let code = stage2(|| 7, Duration::from_millis(100));
        assert_eq!(code, 7);
        std::env::remove_var("ORBIS_ROOT");
    }

    /// Not in an orbis-game scope (systemd-run did not create one): no request, required -> 75.
    #[test]
    fn outside_a_game_scope_is_reported_not_requested() {
        let _g = LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let r = fixture("noscope", "user.slice/user-1000.slice/session-2.scope");
        std::env::set_var("ORBIS_ROOT", &r);
        std::env::set_var(ENV_VRAM, (512u64 << 20).to_string());
        std::env::set_var(ENV_REQUIRE, "1");
        let res = r.join("admission.json");
        std::env::set_var(ENV_RESULT, &res);
        assert_eq!(stage2(|| panic!("must not start"), Duration::from_millis(100)), admit::EXIT_ADMISSION);
        let rec = json::parse(&fs::read_to_string(&res).unwrap()).unwrap();
        assert_eq!(rec.get("state").and_then(Value::as_str), Some("NOT_IN_SCOPE"));
        assert!(pending(&r, std::process::id()).is_none());
        // request directory missing and not creatable: REQUEST_FAILED, not a silent timeout
        let r = fixture("noreqdir", SCOPE);
        fs::remove_dir_all(r.join("run/orbis/admit")).unwrap();
        fs::write(r.join("run/orbis/admit"), "not a directory").unwrap();
        std::env::set_var("ORBIS_ROOT", &r);
        std::env::set_var(ENV_VRAM, (512u64 << 20).to_string());
        std::env::set_var(ENV_REQUIRE, "1");
        std::env::set_var(ENV_RESULT, r.join("a.json"));
        assert_eq!(stage2(|| panic!("must not start"), Duration::from_millis(100)), admit::EXIT_ADMISSION);
        let rec = json::parse(&fs::read_to_string(r.join("a.json")).unwrap()).unwrap();
        assert_eq!(rec.get("state").and_then(Value::as_str), Some("REQUEST_FAILED"));
        std::env::remove_var("ORBIS_ROOT");
    }
}
