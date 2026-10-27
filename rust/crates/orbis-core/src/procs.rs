//! Process table from /proc (re-rooted for tests).

use crate::fsx::{p, read, read_raw};
use std::path::Path;

#[derive(Clone, Debug, Default)]
pub struct Proc {
    pub pid: u32,
    pub ppid: u32,
    pub uid: u32,
    pub comm: String,
    /// argv[0] basename and full argv (NUL-split)
    pub argv: Vec<String>,
    /// readlink /proc/pid/exe basename (empty if unreadable)
    pub exe: String,
    /// cgroup v2 path ("/user.slice/...")
    pub cgroup: String,
    pub environ_steam_appid: Option<String>,
}

impl Proc {
    pub fn arg0_base(&self) -> String {
        self.argv
            .first()
            .map(|a| a.rsplit(['/', '\\']).next().unwrap_or(a).to_string())
            .unwrap_or_default()
    }
    /// Names this process can be matched by: comm, exe basename, argv[0] basename
    /// (Windows games under Wine show up as "game.exe" in argv[0] or comm).
    pub fn names(&self) -> Vec<String> {
        let mut v = vec![self.comm.clone(), self.exe.clone(), self.arg0_base()];
        v.retain(|s| !s.is_empty());
        v.dedup();
        v
    }
}

fn status_field(text: &str, key: &str) -> Option<String> {
    text.lines().find_map(|l| l.strip_prefix(key).map(|v| v.trim().to_string()))
}

pub fn read_proc(pid: u32, with_environ: bool) -> Option<Proc> {
    let base = p(&format!("/proc/{pid}"));
    let status = read(&base.join("status"))?;
    let ppid = status_field(&status, "PPid:").and_then(|v| v.parse().ok()).unwrap_or(0);
    let uid = status_field(&status, "Uid:")
        .and_then(|v| v.split_whitespace().next().and_then(|x| x.parse().ok()))
        .unwrap_or(0);
    let comm = read(&base.join("comm")).unwrap_or_default();
    let argv = std::fs::read(base.join("cmdline"))
        .map(|b| {
            b.split(|c| *c == 0)
                .filter(|s| !s.is_empty())
                .map(|s| String::from_utf8_lossy(s).into_owned())
                .collect()
        })
        .unwrap_or_default();
    let exe = std::fs::read_link(base.join("exe"))
        .ok()
        .and_then(|t| t.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_default();
    let cgroup = read_raw(&base.join("cgroup"))
        .and_then(|c| c.lines().find_map(|l| l.strip_prefix("0::").map(str::to_string)))
        .unwrap_or_default();
    let environ_steam_appid = if with_environ {
        std::fs::read(base.join("environ")).ok().and_then(|b| {
            b.split(|c| *c == 0).find_map(|kv| {
                let s = String::from_utf8_lossy(kv);
                s.strip_prefix("SteamAppId=").or_else(|| s.strip_prefix("STEAM_COMPAT_APP_ID=")).map(str::to_string)
            })
        })
    } else {
        None
    };
    Some(Proc { pid, ppid, uid, comm, argv, exe, cgroup, environ_steam_appid })
}

pub fn pids() -> Vec<u32> {
    let mut v: Vec<u32> = std::fs::read_dir(p("/proc"))
        .map(|rd| rd.flatten().filter_map(|e| e.file_name().to_str().and_then(|s| s.parse().ok())).collect())
        .unwrap_or_default();
    v.sort_unstable();
    v
}

pub fn all(with_environ: bool) -> Vec<Proc> {
    pids().into_iter().filter_map(|pid| read_proc(pid, with_environ)).collect()
}

/// pid plus all descendants (from one snapshot)
pub fn tree(procs: &[Proc], root: u32) -> Vec<u32> {
    let mut out = vec![root];
    let mut i = 0;
    while i < out.len() {
        let cur = out[i];
        for pr in procs {
            if pr.ppid == cur && !out.contains(&pr.pid) {
                out.push(pr.pid);
            }
        }
        i += 1;
    }
    out
}

pub fn exists(pid: u32) -> bool {
    Path::new(&p(&format!("/proc/{pid}"))).exists()
}
