//! Pre-launch admission server (GE-A03): applies a game's dmem limits to its own scope before
//! orbis-run lets the game start. Protocol and trust model: orbis_core::admit.
//!
//! Nothing in a request is trusted but the limits: the cgroup comes from /proc, the requester is
//! the owner of the request file, and only a `.scope` cgroup of that user that holds nothing but
//! the requesting process is ever limited. Writes are not journalled: the scope exists only for
//! the game and its limits disappear with it.

use crate::hw;
use orbis_core::admit::{self, Request};
use orbis_core::fsx::{self, p};
use orbis_core::{jobj, json, procs, Value};
use std::collections::BTreeMap;
use std::os::unix::fs::MetadataExt;
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// cgroup -> reply of every admission granted while orbis-perf runs (read by the controller)
pub type Admitted = Arc<Mutex<BTreeMap<String, Value>>>;

const POLL: Duration = Duration::from_millis(100);
const LOG: &str = "/run/orbis/perf/admissions.jsonl";

/// Decide on one request. `owner` is the uid owning the request file.
pub fn decide(req: &Request, owner: u32) -> Value {
    let mut results = vec![];
    let reject = |why: String| jobj! {"pid" => req.pid, "nonce" => req.nonce.clone(), "admitted" => false, "rejected" => why, "results" => Value::Arr(vec![])};
    let Some(pr) = procs::read_proc(req.pid, false) else {
        return reject(format!("process {} not found", req.pid));
    };
    if owner != 0 && pr.uid != owner {
        return reject(format!("request owned by uid {owner}, process {} by uid {}", req.pid, pr.uid));
    }
    let Some(cg) = admit::cgroup_of(req.pid) else {
        return reject(format!("no cgroup v2 path for process {}", req.pid));
    };
    if owner != 0 && !cg.starts_with(&format!("/user.slice/user-{owner}.slice/")) {
        return reject(format!("{cg} is not a cgroup of uid {owner}"));
    }
    if !cg.ends_with(".scope") {
        return reject(format!("{cg} is not a scope: only a dedicated game scope is limited"));
    }
    let others: Vec<u32> = hw::cgroup_procs(&cg).into_iter().filter(|x| *x != req.pid).collect();
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
    let ok = results.iter().all(|a| a.get("state").and_then(Value::as_str) == Some("APPLIED"));
    jobj! {"pid" => req.pid, "nonce" => req.nonce.clone(), "cgroup" => cg, "admitted" => ok, "require" => req.require, "title" => req.title.clone(),
           "dmem_controller" => path, "results" => Value::Arr(results)}
}

/// Handle every pending request once; returns the replies written.
pub fn serve_once(admitted: &Admitted) -> Vec<Value> {
    let mut out = vec![];
    for f in fsx::list_dir(&p(admit::DIR)) {
        let name = f.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let Some(pid) = name.strip_suffix(".req").and_then(|x| x.parse::<u32>().ok()) else { continue };
        // never follow a symlink planted in the shared directory; ignore oversized files
        let Ok(meta) = std::fs::symlink_metadata(&f) else { continue };
        let text = if meta.file_type().is_file() && meta.len() <= 4096 { fsx::read_raw(&f) } else { None };
        let _ = std::fs::remove_file(&f);
        let reply = match text.as_deref().map(json::parse) {
            Some(Ok(v)) => match Request::from_json(&v) {
                Some(r) if r.pid == pid => decide(&r, meta.uid()),
                Some(r) => jobj! {"pid" => pid, "admitted" => false, "rejected" => format!("file {name} names pid {}", r.pid)},
                None => jobj! {"pid" => pid, "admitted" => false, "rejected" => "malformed request"},
            },
            _ => jobj! {"pid" => pid, "admitted" => false, "rejected" => "unreadable request (not a regular file, too large or not JSON)"},
        };
        let mut rep = reply.clone();
        rep.set("schema", "orbis-admit-reply/1").set("time", orbis_core::sys::now_unix().0);
        // replies go to a root-only directory (never a file root creates where users can write);
        // the client matches the nonce and verifies dmem.max itself
        let rp = admit::reply_path(pid);
        let _ = fsx::atomic_write(&rp, rep.to_string_compact().as_bytes());
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
/// replies 0755 root.
fn ensure_dirs() {
    use std::os::unix::fs::PermissionsExt;
    for (d, mode) in [(admit::DIR, 0o1777), (admit::REPLY_DIR, 0o755)] {
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
        w(&r, "proc/78/status", "Name:\torbis-run\nPPid:\t77\nUid:\t1000\t1000\t1000\t1000\n");
        w(&r, "proc/78/comm", "orbis-run\n");
        w(&r, "proc/78/cgroup", &format!("0::/{SCOPE}\n"));
        w(&r, "sys/devices/gpu/orbis_gtt_cgroup", "unavailable reason=no_cgroup_dmem\n");
        fs::create_dir_all(r.join("sys/class/drm/card0")).unwrap();
        std::os::unix::fs::symlink(r.join("sys/devices/gpu"), r.join("sys/class/drm/card0/device")).unwrap();
        r
    }

    fn req(require: bool) -> Request {
        Request { pid: 78, vram_max: Some(512 << 20), gtt_max: Some(1 << 30), require, title: "Game".into(), nonce: "ab12".into() }
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
        // and the client-side verification agrees
        let v = orbis_core::admit::verify(&format!("/{SCOPE}"), &Request { gtt_max: None, ..req(true) });
        assert!(v.iter().all(|(_, ok, _)| *ok), "{v:?}");
        std::env::remove_var("ORBIS_ROOT");
    }

    #[test]
    fn admission_refuses_what_it_must_not_limit() {
        let _g = crate::TEST_ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let r = fixture("deny", false);
        std::env::set_var("ORBIS_ROOT", &r);
        let rejected = |v: &Value| v.get("rejected").and_then(Value::as_str).map(str::to_string);
        // another user's process
        assert!(rejected(&decide(&req(true), 1001)).unwrap().contains("uid"));
        // the cgroup also holds other programs
        w(&r, &format!("sys/fs/cgroup/{SCOPE}/cgroup.procs"), "78\n79\n");
        assert!(rejected(&decide(&req(true), 1000)).unwrap().contains("also holds"));
        w(&r, &format!("sys/fs/cgroup/{SCOPE}/cgroup.procs"), "78\n");
        // not a scope (for example a whole user slice)
        w(&r, "proc/78/cgroup", "0::/user.slice/user-1000.slice/user@1000.service/app.slice\n");
        assert!(rejected(&decide(&req(true), 1000)).unwrap().contains("not a scope"));
        w(&r, "proc/78/cgroup", &format!("0::/{SCOPE}\n"));
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
    fn server_answers_requests_and_ignores_symlinks() {
        let _g = crate::TEST_ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let r = fixture("srv", true);
        std::env::set_var("ORBIS_ROOT", &r);
        let adm: Admitted = Default::default();
        w(&r, "run/orbis/admit/78.req", &Request { gtt_max: None, ..req(false) }.to_json().to_string_compact());
        w(&r, "etc/secret", "x");
        std::os::unix::fs::symlink(r.join("etc/secret"), r.join("run/orbis/admit/79.req")).unwrap();
        let out = serve_once(&adm);
        assert_eq!(out.len(), 2);
        assert!(!r.join("run/orbis/admit/78.req").exists());
        let rep = orbis_core::json::parse(&fs::read_to_string(r.join("run/orbis/admit-replies/78.reply")).unwrap()).unwrap();
        assert_eq!(rep.get("admitted").and_then(Value::as_bool), Some(true));
        assert_eq!(rep.get("nonce").and_then(Value::as_str), Some("ab12"));
        assert!(adm.lock().unwrap().contains_key(&format!("/{SCOPE}")));
        let bad = orbis_core::json::parse(&fs::read_to_string(r.join("run/orbis/admit-replies/79.reply")).unwrap()).unwrap();
        assert_eq!(bad.get("admitted").and_then(Value::as_bool), Some(false));
        assert!(r.join("etc/secret").exists(), "the symlink target is never touched");
        std::env::remove_var("ORBIS_ROOT");
    }
}
