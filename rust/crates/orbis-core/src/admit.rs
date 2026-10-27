//! Pre-launch resource admission (GE-A03): the protocol between `orbis-run` and `orbis-perf`.
//!
//! A game started through `orbis-run` with memory limits in its profile is admitted *before*
//! its first instruction runs:
//!
//! 1. `orbis-run` starts `systemd-run --user --scope` around a second copy of itself (stage 2).
//!    The scope is a cgroup that holds stage 2 only.
//! 2. Stage 2 writes `/run/orbis/admit/<pid>.<nonce>.req` (its PID, its process start time and a
//!    random nonce) and waits for `/run/orbis/admit-replies/<pid>.<nonce>.reply`.
//! 3. `orbis-perf` trusts nothing in the request except the limits. It takes the cgroup from
//!    `/proc/<pid>/cgroup`, checks the request file's owner against the process owner, checks
//!    that the cgroup is a scope of that user holding nothing but that process, enables the
//!    dmem controller down to it, writes `dmem.max` for each region and reads it back.
//!
//! File handling (r3.1 hardening):
//! * the client creates its request with O_CREAT|O_EXCL|O_NOFOLLOW, mode 0600, under a random
//!   temporary name in the sticky directory, then renames it; a planted symlink or file is never
//!   followed or truncated, and the name carries the nonce, so another user cannot pre-empt it;
//! * the server opens a request with O_NOFOLLOW|O_NONBLOCK and decides on the open file (fstat:
//!   regular file, one link, size, owner), never by re-opening the path; it unlinks the path only
//!   when it still names the file it read;
//! * the PID is bound to the process start time (/proc/<pid>/stat field 22): a recycled PID is
//!   rejected, before and after the limits are written;
//! * replies are created by root in a directory only root can write (0711: no listing), mode 0600
//!   and owned by the requesting user, so only that user (and root) can read them.
//! 4. Stage 2 verifies `dmem.max` itself (the reply is advisory) and only then `exec`s the
//!    game, so every GPU allocation of the game and its children is charged and limited.
//!
//! If a profile marks the limits as required (`memory.require_limits`) and any of them cannot
//! be applied, stage 2 exits with [`EXIT_ADMISSION`] and the game never starts.

use crate::fsx::{self, p};
use crate::json::Value;
use crate::jobj;
use std::path::PathBuf;

pub const DIR: &str = "/run/orbis/admit";
/// root-owned, 0711: root never creates files where users can write; users cannot list replies
pub const REPLY_DIR: &str = "/run/orbis/admit-replies";
/// largest request file the server reads
pub const MAX_REQUEST: u64 = 4096;

#[cfg(any(target_arch = "x86_64", target_arch = "x86"))]
const O_NOFOLLOW: i32 = 0o400000;
#[cfg(not(any(target_arch = "x86_64", target_arch = "x86")))]
const O_NOFOLLOW: i32 = 0o100000;
const O_NONBLOCK: i32 = 0o4000;
/// Exit code of orbis-run when required limits could not be applied (EX_TEMPFAIL)
pub const EXIT_ADMISSION: i32 = 75;
/// How long stage 2 waits for orbis-perf
pub const REPLY_TIMEOUT_MS: u64 = 5000;

#[derive(Clone, Debug, PartialEq)]
pub struct Request {
    pub pid: u32,
    pub vram_max: Option<u64>,
    pub gtt_max: Option<u64>,
    pub require: bool,
    pub title: String,
    /// echoed in the reply, so a stale reply for a recycled PID is never taken for this request
    pub nonce: String,
    /// /proc/<pid>/stat start time (clock ticks since boot): binds the PID to one process
    pub starttime: u64,
}

impl Request {
    pub fn to_json(&self) -> Value {
        jobj! {"schema" => "orbis-admit-request/1", "pid" => self.pid, "vram_max" => self.vram_max,
               "gtt_max" => self.gtt_max, "require" => self.require, "title" => self.title.clone(),
               "nonce" => self.nonce.clone(), "starttime" => self.starttime}
    }
    pub fn from_json(v: &Value) -> Option<Request> {
        let num = |k: &str| v.get(k).and_then(Value::as_i64).filter(|n| *n > 0).map(|n| n as u64);
        Some(Request {
            pid: v.get("pid").and_then(Value::as_i64).filter(|n| *n > 0 && *n <= u32::MAX as i64)? as u32,
            vram_max: num("vram_max"),
            gtt_max: num("gtt_max"),
            require: v.get("require").and_then(Value::as_bool).unwrap_or(false),
            title: v.get("title").and_then(Value::as_str).unwrap_or("").chars().take(200).collect(),
            nonce: v.get("nonce").and_then(Value::as_str).unwrap_or("").chars().filter(|c| c.is_ascii_hexdigit()).take(64).collect(),
            starttime: v.get("starttime").and_then(Value::as_i64).filter(|n| *n > 0).map(|n| n as u64).unwrap_or(0),
        })
    }
    /// (kind, bytes) for every limit asked for
    pub fn limits(&self) -> Vec<(&'static str, u64)> {
        let mut v = vec![];
        if let Some(b) = self.vram_max {
            v.push(("vram", b));
        }
        if let Some(b) = self.gtt_max {
            v.push(("gtt", b));
        }
        v
    }
}

pub fn req_path(pid: u32, nonce: &str) -> PathBuf {
    p(DIR).join(format!("{pid}.{nonce}.req"))
}

pub fn reply_path(pid: u32, nonce: &str) -> PathBuf {
    p(REPLY_DIR).join(format!("{pid}.{nonce}.reply"))
}

/// (pid, nonce) from a request file name "<pid>.<hex nonce>.req"
pub fn parse_req_name(name: &str) -> Option<(u32, String)> {
    let stem = name.strip_suffix(".req")?;
    let (pid, nonce) = stem.split_once('.')?;
    let pid: u32 = pid.parse().ok().filter(|p| *p > 0)?;
    (nonce.len() >= 16 && nonce.len() <= 64 && nonce.chars().all(|c| c.is_ascii_hexdigit())).then(|| (pid, nonce.to_string()))
}

/// Process start time (field 22 of /proc/<pid>/stat, after the parenthesised comm)
pub fn proc_starttime(pid: u32) -> Option<u64> {
    let raw = fsx::read_raw(&p(&format!("/proc/{pid}/stat")))?;
    let rest = &raw[raw.rfind(')')? + 1..];
    rest.split_whitespace().nth(19)?.parse().ok()
}

/// Client side: write the request without following or truncating anything planted in the
/// shared directory (random O_EXCL temporary, mode 0600, then rename to the final name).
pub fn write_request(req: &Request) -> std::io::Result<PathBuf> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let dir = p(DIR);
    let tmp = dir.join(format!(".{}.{}.tmp", req.pid, new_nonce()));
    let mut f = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(O_NOFOLLOW).open(&tmp)?;
    let res = f.write_all(req.to_json().to_string_compact().as_bytes()).and_then(|_| f.sync_all());
    let dst = req_path(req.pid, &req.nonce);
    let res = res.and_then(|_| std::fs::rename(&tmp, &dst));
    if res.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    res.map(|_| dst)
}

/// What the server knows about a request file it opened safely
#[derive(Debug)]
pub struct OpenedRequest {
    pub owner: u32,
    pub text: String,
}

/// Server side: open without following symlinks, decide on the open file, unlink the path only
/// when it still names that file.
pub fn read_request(path: &std::path::Path) -> Result<OpenedRequest, String> {
    use std::io::Read;
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    let f = std::fs::OpenOptions::new().read(true).custom_flags(O_NOFOLLOW | O_NONBLOCK).open(path);
    let mut f = match f {
        Ok(f) => f,
        Err(e) => {
            // a symlink (ELOOP) or anything we cannot open: remove the name, never follow it
            let _ = std::fs::remove_file(path);
            return Err(format!("cannot open request without following links: {e}"));
        }
    };
    let m = f.metadata().map_err(|e| e.to_string())?;
    let same = |p: &std::path::Path| std::fs::symlink_metadata(p).map(|l| l.dev() == m.dev() && l.ino() == m.ino()).unwrap_or(false);
    if same(path) {
        let _ = std::fs::remove_file(path);
    }
    if !m.file_type().is_file() {
        return Err("request is not a regular file".into());
    }
    if m.nlink() != 1 {
        return Err(format!("request has {} links (hard links are refused)", m.nlink()));
    }
    if m.len() > MAX_REQUEST {
        return Err(format!("request larger than {MAX_REQUEST} bytes"));
    }
    let mut text = String::new();
    (&mut f).take(MAX_REQUEST + 1).read_to_string(&mut text).map_err(|e| e.to_string())?;
    Ok(OpenedRequest { owner: m.uid(), text })
}

/// Server side: reply readable only by the requesting user (0600, chowned), created in the
/// root-only reply directory through an exclusive temporary and renamed into place.
pub fn write_reply(pid: u32, nonce: &str, uid: u32, data: &[u8]) -> std::io::Result<PathBuf> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let dir = p(REPLY_DIR);
    std::fs::create_dir_all(&dir)?;
    let dst = reply_path(pid, nonce);
    let tmp = dir.join(format!(".{pid}.{}.tmp", new_nonce()));
    let mut f = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(O_NOFOLLOW).open(&tmp)?;
    let res = (|| {
        f.write_all(data)?;
        // chown can only fail when not running as root (tests); the mode keeps it private anyway
        if crate::sys::is_root() {
            std::os::unix::fs::fchown(&f, Some(uid), None)?;
        }
        f.sync_all()?;
        std::fs::rename(&tmp, &dst)
    })();
    if res.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    res.map(|_| dst)
}

/// 128-bit random nonce (hex) from the kernel's CSPRNG
pub fn new_nonce() -> String {
    use std::io::Read;
    let mut b = [0u8; 16];
    if std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut b)).is_err() {
        let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
        b.copy_from_slice(&(t ^ ((std::process::id() as u128) << 64)).to_le_bytes());
    }
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// cgroup v2 path of a process ("/user.slice/...") from /proc/<pid>/cgroup
pub fn cgroup_of(pid: u32) -> Option<String> {
    let raw = fsx::read_raw(&p(&format!("/proc/{pid}/cgroup")))?;
    raw.lines().find_map(|l| l.strip_prefix("0::").map(str::to_string)).filter(|c| c.starts_with('/'))
}

/// dmem region name for one memory kind ("drm/<dev>/vram", "drm/<dev>/gtt") from dmem.capacity
pub fn region_for(kind: &str) -> Option<String> {
    let caps = fsx::read_raw(&p("/sys/fs/cgroup/dmem.capacity"))?;
    let suffix = format!("/{kind}");
    caps.lines().find_map(|l| {
        let name = l.split_whitespace().next()?;
        (name.starts_with("drm/") && name.ends_with(&suffix)).then(|| name.to_string())
    })
}

/// Current dmem.max value of one region on one cgroup ("max" or bytes); None if not readable
pub fn dmem_max_of(cgroup: &str, region: &str) -> Option<String> {
    let f = p(&format!("/sys/fs/cgroup/{}/dmem.max", cgroup.trim_matches('/')));
    let raw = fsx::read_raw(&f)?;
    Some(
        raw.lines()
            .find(|l| l.split_whitespace().next() == Some(region))
            .and_then(|l| l.split_whitespace().nth(1))
            .unwrap_or("max")
            .to_string(),
    )
}

/// Why the GTT region is missing, from the GE kernel's orbis_gtt_cgroup file (ge-0016)
pub fn gtt_region_reason() -> String {
    for d in fsx::list_dir(&p("/sys/class/drm")) {
        let f = d.join("device/orbis_gtt_cgroup");
        if let Some(s) = fsx::read(&f) {
            if s.starts_with("registered") {
                return "the kernel registered the gtt region but it is not in dmem.capacity".into();
            }
            return format!("kernel reports: {s}");
        }
    }
    "no dmem GTT region (GE kernel ge-0015 needed; kernel without ge-0016 status file)".into()
}

/// One verification line per requested limit: (kind, ok, detail)
pub fn verify(cgroup: &str, req: &Request) -> Vec<(String, bool, String)> {
    req.limits()
        .into_iter()
        .map(|(kind, bytes)| match region_for(kind) {
            None => {
                let why = if kind == "gtt" { gtt_region_reason() } else { "no dmem VRAM region (kernel without CONFIG_CGROUP_DMEM)".into() };
                (kind.to_string(), false, why)
            }
            Some(region) => match dmem_max_of(cgroup, &region) {
                Some(v) if v == bytes.to_string() => (kind.to_string(), true, format!("{region} {v}")),
                Some(v) => (kind.to_string(), false, format!("{region} is {v}, wanted {bytes}")),
                None => (kind.to_string(), false, format!("{cgroup}: no dmem.max (dmem controller not enabled)")),
            },
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_roundtrip_and_validation() {
        let r = Request { pid: 42, vram_max: Some(512 << 20), gtt_max: None, require: true, title: "G".into(), nonce: new_nonce(), starttime: 777 };
        assert_eq!(r.nonce.len(), 32);
        let back = Request::from_json(&crate::json::parse(&r.to_json().to_string_compact()).unwrap()).unwrap();
        assert_eq!(back, r);
        assert_eq!(back.limits(), vec![("vram", 512u64 << 20)]);
        // pid must be positive; non-positive limits are ignored
        assert!(Request::from_json(&crate::json::parse(r#"{"pid":0}"#).unwrap()).is_none());
        let z = Request::from_json(&crate::json::parse(r#"{"pid":7,"vram_max":-1,"gtt_max":0}"#).unwrap()).unwrap();
        assert!(z.limits().is_empty() && !z.require && z.starttime == 0);
        assert_eq!(parse_req_name(&format!("42.{}.req", r.nonce)), Some((42, r.nonce.clone())));
        for bad in ["42.req", "0.0123456789abcdef.req", "42.xyz.req", "42.0123.req", "x.0123456789abcdef.req"] {
            assert!(parse_req_name(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn starttime_of_this_process() {
        let me = std::process::id();
        if std::env::var_os("ORBIS_ROOT").is_none() && std::env::var_os("ORBIS_GFX_ROOT").is_none() {
            assert!(proc_starttime(me).unwrap() > 0);
        }
    }
}
