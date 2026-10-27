//! Pre-launch resource admission (GE-A03): the protocol between `orbis-run` and `orbis-perf`.
//!
//! A game started through `orbis-run` with memory limits in its profile is admitted *before*
//! its first instruction runs:
//!
//! 1. `orbis-run` starts `systemd-run --user --scope` around a second copy of itself (stage 2).
//!    The scope is a cgroup that holds stage 2 only.
//! 2. Stage 2 writes `/run/orbis/admit/<pid>.req` (its own PID and a random nonce; the file is
//!    owned by the user) and waits for `/run/orbis/admit-replies/<pid>.reply`, which only
//!    `orbis-perf` (root) can write, carrying the same nonce.
//! 3. `orbis-perf` trusts nothing in the request except the limits. It takes the cgroup from
//!    `/proc/<pid>/cgroup`, checks the request file's owner against the process owner, checks
//!    that the cgroup is a scope of that user holding nothing but that process, enables the
//!    dmem controller down to it, writes `dmem.max` for each region and reads it back.
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
/// root-owned, not world-writable: root never creates files in a directory users can write
pub const REPLY_DIR: &str = "/run/orbis/admit-replies";
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
}

impl Request {
    pub fn to_json(&self) -> Value {
        jobj! {"schema" => "orbis-admit-request/1", "pid" => self.pid, "vram_max" => self.vram_max,
               "gtt_max" => self.gtt_max, "require" => self.require, "title" => self.title.clone(),
               "nonce" => self.nonce.clone()}
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

pub fn req_path(pid: u32) -> PathBuf {
    p(DIR).join(format!("{pid}.req"))
}

pub fn reply_path(pid: u32) -> PathBuf {
    p(REPLY_DIR).join(format!("{pid}.reply"))
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
        let r = Request { pid: 42, vram_max: Some(512 << 20), gtt_max: None, require: true, title: "G".into(), nonce: new_nonce() };
        assert_eq!(r.nonce.len(), 32);
        let back = Request::from_json(&crate::json::parse(&r.to_json().to_string_compact()).unwrap()).unwrap();
        assert_eq!(back, r);
        assert_eq!(back.limits(), vec![("vram", 512u64 << 20)]);
        // pid must be positive; non-positive limits are ignored
        assert!(Request::from_json(&crate::json::parse(r#"{"pid":0}"#).unwrap()).is_none());
        let z = Request::from_json(&crate::json::parse(r#"{"pid":7,"vram_max":-1,"gtt_max":0}"#).unwrap()).unwrap();
        assert!(z.limits().is_empty() && !z.require);
    }
}
