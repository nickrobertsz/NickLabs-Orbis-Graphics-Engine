//! Pre-launch admission server (GE-A03): applies a game's dmem limits to its own scope before
//! orbis-run lets the game start. Protocol and trust model: orbis_core::admit.
//!
//! Nothing in a request is trusted but the limits: the cgroup comes from /proc, the requester is
//! the owner of the request file (fstat of the opened file), the PID must still be the process
//! that wrote the request (start time), and only an orbis-game scope of that user's systemd
//! manager, owned by that user and holding nothing but the requesting process, is ever limited.
//! The identity checks are repeated after the limits are written. Writes are not journalled: the
//! scope exists only for the game and its limits disappear with it.

use crate::hw;
use orbis_core::admit::{self, Request};
use orbis_core::fsx::{self, p};
use orbis_core::{jobj, json, procs, Value};
use std::collections::BTreeMap;
use std::os::unix::fs::MetadataExt;

/// The cgroup a user's `orbis-run` scope must have: inside that user's systemd manager, in
/// orbis-game.slice, named orbis-game-<pid>.scope (root may use any scope).
pub fn scope_allowed(cg: &str, owner: u32) -> Result<(), String> {
    if !cg.ends_with(".scope") {
        return Err(format!("{cg} is not a scope: only a dedicated game scope is limited"));
    }
    if owner == 0 {
        return Ok(());
    }
    let prefix = format!("/user.slice/user-{owner}.slice/user@{owner}.service/");
    if !cg.starts_with(&prefix) {
        return Err(format!("{cg} is not in uid {owner}'s systemd user manager ({prefix})"));
    }
    let mut parts = cg.rsplit('/');
    let scope = parts.next().unwrap_or("");
    let slice = parts.next().unwrap_or("");
    let num = scope.strip_prefix("orbis-game-").and_then(|x| x.strip_suffix(".scope"));
    if slice != "orbis-game.slice" || !num.map(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit())).unwrap_or(false) {
        return Err(format!("{cg} is not an orbis-run game scope (orbis-game.slice/orbis-game-<n>.scope)"));
    }
    Ok(())
}
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// cgroup -> reply of every admission granted while orbis-perf runs (read by the controller)
pub type Admitted = Arc<Mutex<BTreeMap<String, Value>>>;

const POLL: Duration = Duration::from_millis(100);
const LOG: &str = "/run/orbis/perf/admissions.jsonl";

/// Decide on one request. `owner` is the uid owning the (opened) request file.
pub fn decide(req: &Request, owner: u32) -> Value {
    let mut results = vec![];
    let reject = |why: String| jobj! {"pid" => req.pid, "nonce" => req.nonce.clone(), "admitted" => false, "rejected" => why, "results" => Value::Arr(vec![])};
    if req.nonce.len() < 16 {
        return reject("request without a nonce".into());
    }
    let Some(pr) = procs::read_proc(req.pid, false) else {
        return reject(format!("process {} not found", req.pid));
    };
    if owner != 0 && pr.uid != owner {
        return reject(format!("request owned by uid {owner}, process {} by uid {}", req.pid, pr.uid));
    }
    // the PID must still be the process that wrote the request (not a recycled PID)
    let start = admit::proc_starttime(req.pid);
    if req.starttime == 0 || start != Some(req.starttime) {
        return reject(format!("process {} start time {:?} does not match the request's {}: not the requesting process", req.pid, start, req.starttime));
    }
    let Some(cg) = admit::cgroup_of(req.pid) else {
        return reject(format!("no cgroup v2 path for process {}", req.pid));
    };
    if let Err(e) = scope_allowed(&cg, owner) {
        return reject(e);
    }
    // the scope directory itself must belong to the requester (systemd delegates user scopes)
    let cg_dir = p(&format!("/sys/fs/cgroup/{}", cg.trim_matches('/')));
    match std::fs::symlink_metadata(&cg_dir) {
        Ok(m) if m.is_dir() && (owner == 0 || m.uid() == owner) => {}
        Ok(m) => return reject(format!("{cg} belongs to uid {}, not the requester", m.uid())),
        Err(e) => return reject(format!("{cg}: {e}")),
    }
    let procs_in = hw::cgroup_procs(&cg);
    if !procs_in.contains(&req.pid) {
        return reject(format!("process {} is not a member of {cg}", req.pid));
    }
    let others: Vec<u32> = procs_in.into_iter().filter(|x| *x != req.pid).collect();
    if !others.is_empty() {
        return reject(format!("{cg} also holds {others:?}: limits would hit other programs"));
    }
    // controllers first: dmem must be enabled on every ancestor before dmem.max exists
    let path = match hw::enable_dmem_path(&cg) {
        Ok(changed) => jobj! {"state" => "OK", "enabled_in" => changed},
        Err(e) => jobj! {"state" => "FAILED", "detail" => e},
    };
    let mut j = orbis_core::restore::Journal::open(p("/run/orbis/perf/admit-scratch.json"));
    for (kind, bytes) in req.limits() {
        let mut a = hw::limit_dmem(&mut j, &cg, kind, bytes);
        a.set("kind", kind);
        results.push(a);
    }
    // the scratch journal only satisfies limit_dmem's interface: scopes are not restored
    for (k, _) in j.entries() {
        let _ = j.forget(&k);
    }
    // identity again: the process must not have exited, been replaced or moved meanwhile
    let still = admit::proc_starttime(req.pid) == Some(req.starttime) && admit::cgroup_of(req.pid).as_deref() == Some(cg.as_str());
    let ok = still && results.iter().all(|a| a.get("state").and_then(Value::as_str) == Some("APPLIED"));
    let mut rep = jobj! {"pid" => req.pid, "nonce" => req.nonce.clone(), "cgroup" => cg, "admitted" => ok, "require" => req.require,
           "title" => req.title.clone(), "dmem_controller" => path, "results" => Value::Arr(results)};
    if !still {
        rep.set("rejected", "the requesting process exited or changed cgroup while it was being admitted");
    }
    rep
}

/// Handle every pending request once; returns the replies written.
pub fn serve_once(admitted: &Admitted) -> Vec<Value> {
    let mut out = vec![];
    for f in fsx::list_dir(&p(admit::DIR)) {
        let name = f.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let Some((pid, nonce)) = admit::parse_req_name(&name) else { continue };
        let (reply, owner) = match admit::read_request(&f) {
            Ok(o) => {
                let rep = match json::parse(&o.text) {
                    Ok(v) => match Request::from_json(&v) {
                        Some(r) if r.pid == pid && r.nonce == nonce => decide(&r, o.owner),
                        Some(_) => jobj! {"pid" => pid, "nonce" => nonce.clone(), "admitted" => false, "rejected" => format!("file {name} does not match its content")},
                        None => jobj! {"pid" => pid, "nonce" => nonce.clone(), "admitted" => false, "rejected" => "malformed request"},
                    },
                    Err(_) => jobj! {"pid" => pid, "nonce" => nonce.clone(), "admitted" => false, "rejected" => "request is not JSON"},
                };
                (rep, Some(o.owner))
            }
            Err(e) => (jobj! {"pid" => pid, "nonce" => nonce.clone(), "admitted" => false, "rejected" => e}, None),
        };
        let mut rep = reply.clone();
        rep.set("schema", "orbis-admit-reply/2").set("time", orbis_core::sys::now_unix().0);
        // a reply only for a request we could attribute to a user: that user (and root) can read it
        if let Some(uid) = owner {
            if let Err(e) = admit::write_reply(pid, &nonce, uid, rep.to_string_compact().as_bytes()) {
                rep.set("reply_error", e.to_string());
            }
        }
        if rep.get("admitted").and_then(Value::as_bool) == Some(true) {
            if let (Some(cg), Ok(mut m)) = (rep.get("cgroup").and_then(Value::as_str).map(str::to_string), admitted.lock()) {
                m.insert(cg, rep.clone());
            }
        }
        out.push(rep);
    }
    out
}

/// Drop admissions whose scope is gone, and replies nobody collected (older than a minute).
pub fn prune(admitted: &Admitted) {
    if let Ok(mut m) = admitted.lock() {
        m.retain(|cg, _| p(&format!("/sys/fs/cgroup/{}", cg.trim_matches('/'))).exists());
    }
    for f in fsx::list_dir(&p(admit::REPLY_DIR)) {
        let old = std::fs::metadata(&f).and_then(|m| m.modified()).ok().and_then(|t| t.elapsed().ok()).map(|d| d.as_secs() > 60).unwrap_or(false);
        if old {
            let _ = std::fs::remove_file(&f);
        }
    }
}

/// The two directories (normally made by tmpfiles.d/orbis-native.conf): requests 1777 (sticky),
/// replies 0711 root (users read their own 0600 reply by name, cannot list or write).
fn ensure_dirs() {
    use std::os::unix::fs::PermissionsExt;
    for (d, mode) in [(admit::DIR, 0o1777), (admit::REPLY_DIR, 0o711)] {
        let path = p(d);
        if std::fs::create_dir_all(&path).is_ok() {
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode));
        }
    }
}

pub fn spawn(admitted: Admitted) {
    ensure_dirs();
    let _ = std::thread::Builder::new().name("orbis-admit".into()).spawn(move || {
        let mut n = 0u32;
        loop {
            for r in serve_once(&admitted) {
                let _ = fsx::append_line(&p(LOG), &r.to_string_compact());
            }
            n = n.wrapping_add(1);
            if n % 600 == 0 {
                prune(&admitted);
            }
            std::thread::sleep(POLL);
        }
    });
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

    const SCOPE: &str = "user.slice/user-1000.slice/user@1000.service/app.slice/orbis-game.slice/orbis-game-77.scope";
    const VR: &str = "drm/0000:00:01.0/vram";
    const GT: &str = "drm/0000:00:01.0/gtt";
    const NONCE: &str = "0123456789abcdef0123456789abcdef";
    const START: u64 = 424242;

    fn stat(start: u64) -> String {
        format!("78 (orbis-run) S 77 77 77 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {start} 0 0\n")
    }

    fn fixture(tag: &str, with_gtt: bool) -> std::path::PathBuf {
        let r = std::env::temp_dir().join(format!("orbis-admit-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&r);
        w(&r, "sys/fs/cgroup/dmem.capacity", &format!("{VR} 1073741824\n{}", if with_gtt { format!("{GT} 4294967296\n") } else { String::new() }));
        let mut path = String::new();
        for part in SCOPE.split('/') {
            w(&r, &format!("sys/fs/cgroup/{path}cgroup.controllers"), "cpu memory pids dmem\n");
            w(&r, &format!("sys/fs/cgroup/{path}cgroup.subtree_control"), "cpu memory pids\n");
            path.push_str(part);
            path.push('/');
        }
        w(&r, &format!("sys/fs/cgroup/{SCOPE}/dmem.max"), &format!("{VR} max\n"));
        w(&r, &format!("sys/fs/cgroup/{SCOPE}/cgroup.procs"), "78\n");
        // the user's systemd owns its delegated scope (as root in tests we can make that so)
        if orbis_core::sys::is_root() {
            std::os::unix::fs::chown(r.join(format!("sys/fs/cgroup/{SCOPE}")), Some(1000), Some(1000)).unwrap();
        }
        w(&r, "proc/78/status", "Name:\torbis-run\nPPid:\t77\nUid:\t1000\t1000\t1000\t1000\n");
        w(&r, "proc/78/comm", "orbis-run\n");
        w(&r, "proc/78/stat", &stat(START));
        w(&r, "proc/78/cgroup", &format!("0::/{SCOPE}\n"));
        w(&r, "sys/devices/gpu/orbis_gtt_cgroup", "unavailable reason=no_cgroup_dmem\n");
        fs::create_dir_all(r.join("sys/class/drm/card0")).unwrap();
        std::os::unix::fs::symlink(r.join("sys/devices/gpu"), r.join("sys/class/drm/card0/device")).unwrap();
        fs::create_dir_all(r.join("run/orbis/admit")).unwrap();
        r
    }

    fn req(require: bool) -> Request {
        Request { pid: 78, vram_max: Some(512 << 20), gtt_max: Some(1 << 30), require, title: "Game".into(), nonce: NONCE.into(), starttime: START }
    }

    fn rejected(v: &Value) -> String {
        v.get("rejected").and_then(Value::as_str).unwrap_or("").to_string()
    }

    #[test]
    fn admission_limits_the_scope_and_enables_dmem_down_to_it() {
        let _g = crate::TEST_ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let r = fixture("ok", true);
        std::env::set_var("ORBIS_ROOT", &r);
        let rep = decide(&Request { gtt_max: None, ..req(true) }, 1000);
        assert_eq!(rep.get("admitted").and_then(Value::as_bool), Some(true), "{}", rep.to_string_pretty());
        assert_eq!(rep.get("cgroup").and_then(Value::as_str), Some(format!("/{SCOPE}").as_str()));
        // every ancestor got +dmem (root first), the scope itself is not touched
        assert!(fs::read_to_string(r.join("sys/fs/cgroup/cgroup.subtree_control")).unwrap().contains("dmem"));
        assert!(fs::read_to_string(r.join("sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/orbis-game.slice/cgroup.subtree_control"))
            .unwrap().contains("dmem"));
        assert_eq!(fs::read_to_string(r.join(format!("sys/fs/cgroup/{SCOPE}/dmem.max"))).unwrap(), format!("{VR} {}", 512u64 << 20));
        let v = orbis_core::admit::verify(&format!("/{SCOPE}"), &Request { gtt_max: None, ..req(true) });
        assert!(v.iter().all(|(_, ok, _)| *ok), "{v:?}");
        std::env::remove_var("ORBIS_ROOT");
    }

    #[test]
    fn admission_refuses_what_it_must_not_limit() {
        let _g = crate::TEST_ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let r = fixture("deny", false);
        std::env::set_var("ORBIS_ROOT", &r);
        // another user's process
        assert!(rejected(&decide(&req(true), 1001)).contains("uid"));
        // a recycled PID: same number, another process start time
        w(&r, "proc/78/stat", &stat(START + 1));
        assert!(rejected(&decide(&req(true), 1000)).contains("start time"));
        assert!(rejected(&decide(&Request { starttime: 0, ..req(true) }, 1000)).contains("start time"));
        w(&r, "proc/78/stat", &stat(START));
        // no nonce
        assert!(rejected(&decide(&Request { nonce: String::new(), ..req(true) }, 1000)).contains("nonce"));
        // the cgroup also holds other programs; or the process is not in it
        w(&r, &format!("sys/fs/cgroup/{SCOPE}/cgroup.procs"), "78\n79\n");
        assert!(rejected(&decide(&req(true), 1000)).contains("also holds"));
        w(&r, &format!("sys/fs/cgroup/{SCOPE}/cgroup.procs"), "79\n");
        assert!(rejected(&decide(&req(true), 1000)).contains("not a member"));
        w(&r, &format!("sys/fs/cgroup/{SCOPE}/cgroup.procs"), "78\n");
        // not a scope, a scope outside the user manager, a scope that is not orbis-run's
        for (cg, why) in [("user.slice/user-1000.slice/user@1000.service/app.slice", "not a scope"),
                          ("user.slice/user-1000.slice/session-2.scope", "systemd user manager"),
                          ("user.slice/user-1000.slice/user@1000.service/app.slice/app-steam.scope", "orbis-run game scope")] {
            w(&r, "proc/78/cgroup", &format!("0::/{cg}\n"));
            assert!(rejected(&decide(&req(true), 1000)).contains(why), "{cg}");
        }
        w(&r, "proc/78/cgroup", &format!("0::/{SCOPE}\n"));
        // a scope directory owned by someone else
        if orbis_core::sys::is_root() {
            std::os::unix::fs::chown(r.join(format!("sys/fs/cgroup/{SCOPE}")), Some(1001), Some(1001)).unwrap();
            assert!(rejected(&decide(&req(true), 1000)).contains("belongs to uid 1001"));
            std::os::unix::fs::chown(r.join(format!("sys/fs/cgroup/{SCOPE}")), Some(1000), Some(1000)).unwrap();
        }
        // no GTT region: VRAM applied, GTT reported with the kernel's reason, not admitted
        let rep = decide(&req(true), 1000);
        assert_eq!(rep.get("admitted").and_then(Value::as_bool), Some(false));
        let res = rep.get("results").and_then(Value::as_arr).unwrap();
        let gtt = res.iter().find(|a| a.get("kind").and_then(Value::as_str) == Some("gtt")).unwrap();
        assert_eq!(gtt.get("state").and_then(Value::as_str), Some("UNSUPPORTED"));
        assert!(gtt.to_string_compact().contains("no_cgroup_dmem"));
        std::env::remove_var("ORBIS_ROOT");
    }

    #[test]
    fn server_reads_safely_and_replies_privately() {
        use std::os::unix::fs::PermissionsExt;
        let _g = crate::TEST_ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let r = fixture("srv", true);
        std::env::set_var("ORBIS_ROOT", &r);
        let adm: Admitted = Default::default();
        // a proper request, written by the client routine (O_EXCL temporary, 0600, rename)
        let path = orbis_core::admit::write_request(&Request { gtt_max: None, ..req(false) }).unwrap();
        assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        // a symlink request pointing at a secret, and a hard link to it
        w(&r, "etc/secret", "{\"pid\":79}");
        let n2 = "fedcba9876543210fedcba9876543210";
        std::os::unix::fs::symlink(r.join("etc/secret"), r.join(format!("run/orbis/admit/79.{n2}.req"))).unwrap();
        fs::hard_link(r.join("etc/secret"), r.join(format!("run/orbis/admit/80.{n2}.req"))).unwrap();
        // names without a nonce are not requests at all
        w(&r, "run/orbis/admit/81.req", "{}");
        let out = serve_once(&adm);
        assert_eq!(out.len(), 3);
        assert!(!path.exists());
        let rp = orbis_core::admit::reply_path(78, NONCE);
        let rep = orbis_core::json::parse(&fs::read_to_string(&rp).unwrap()).unwrap();
        assert_eq!(rep.get("admitted").and_then(Value::as_bool), Some(true), "{}", rep.to_string_pretty());
        assert_eq!(rep.get("nonce").and_then(Value::as_str), Some(NONCE));
        let m = fs::metadata(&rp).unwrap();
        assert_eq!(m.permissions().mode() & 0o777, 0o600, "reply readable by its owner only");
        assert!(adm.lock().unwrap().contains_key(&format!("/{SCOPE}")));
        // the symlink was neither followed nor answered; the hard link was refused; the secret survives
        assert!(!r.join(format!("run/orbis/admit/79.{n2}.req")).exists());
        assert!(!orbis_core::admit::reply_path(79, n2).exists());
        let hl = out.iter().find(|v| v.get("pid").and_then(Value::as_i64) == Some(80)).unwrap();
        assert!(rejected(hl).contains("links"));
        assert_eq!(fs::read_to_string(r.join("etc/secret")).unwrap(), "{\"pid\":79}");
        assert!(r.join("run/orbis/admit/81.req").exists(), "not a request name: left alone");
        std::env::remove_var("ORBIS_ROOT");
    }
}
