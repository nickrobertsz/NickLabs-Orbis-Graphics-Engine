//! Runner catalog: Proton / GE-Proton / Wine / DXVK releases with pinned versions and sources.
//! The shipped catalog is /usr/share/orbis/games/runners.json (from gaming/runners.json in the
//! source tree). Integrity: GE-Proton publishes a .sha512sum per release (verified); for assets
//! without upstream checksums the SHA-256 is recorded in the lock file at first download on the
//! build host (gaming/runners.lock.json, committed) and verified on every later download and on
//! install from an offline bundle.

use orbis_core::fsx::{self, p};
use orbis_core::json::{self, Value};
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Clone, Debug)]
pub struct Runner {
    pub id: String,
    pub kind: String,    // proton | wine | dxvk | vkd3d
    pub version: String,
    pub dxvk_line: Option<String>,
    pub urls: Vec<String>,
    pub sha512_url: Option<String>,
    pub file: String,
    pub install: String, // where it goes on the console
    pub note: Option<String>,
}

pub fn catalog_path() -> PathBuf {
    std::env::var_os("ORBIS_GAMES_CATALOG").map(PathBuf::from).unwrap_or_else(|| p("/usr/share/orbis/games/runners.json"))
}

pub fn load(path: &Path) -> Result<Vec<Runner>, String> {
    let t = fsx::read_raw(path).ok_or_else(|| format!("no runner catalog at {}", path.display()))?;
    let v = json::parse(&t).map_err(|e| e.to_string())?;
    let arr = v.get("runners").and_then(Value::as_arr).ok_or("catalog has no runners[]")?;
    let s = |r: &Value, k: &str| r.get(k).and_then(Value::as_str).map(str::to_string);
    Ok(arr
        .iter()
        .filter_map(|r| {
            Some(Runner {
                id: s(r, "id")?,
                kind: s(r, "kind")?,
                version: s(r, "version").unwrap_or_default(),
                dxvk_line: s(r, "dxvk_line"),
                urls: r.get("urls").and_then(Value::as_arr).map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect()).unwrap_or_default(),
                sha512_url: s(r, "sha512_url"),
                file: s(r, "file")?,
                install: s(r, "install").unwrap_or_default(),
                note: s(r, "note"),
            })
        })
        .collect())
}

pub fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/root"))
}

/// Expand "~" and "$STEAM_COMPAT" in install paths.
pub fn expand(path: &str) -> PathBuf {
    let h = home();
    let s = path.replace("$STEAM_COMPAT", &h.join(".steam/root/compatibilitytools.d").display().to_string());
    if let Some(r) = s.strip_prefix("~/") {
        h.join(r)
    } else {
        PathBuf::from(s)
    }
}

pub fn installed(r: &Runner) -> bool {
    let dir = expand(&r.install);
    match r.kind.as_str() {
        "proton" => dir.join("proton").exists(),
        "dxvk" => dir.join("x64/d3d11.dll").exists(),
        "vkd3d" => dir.join("x64/d3d12.dll").exists(),
        "wine" => Command::new("wine").arg("--version").output().map(|o| o.status.success()).unwrap_or(false),
        _ => false,
    }
}

/// Newest Proton runner whose bundled DXVK line the driver supports (per rules::select).
pub fn pick_proton<'a>(rs: &'a [Runner], dxvk_line: &str) -> Option<&'a Runner> {
    let rank = |l: &str| match l {
        "3.x" => 3,
        "2.x" => 2,
        "1.10" => 1,
        _ => 0,
    };
    let max = rank(dxvk_line);
    rs.iter()
        .filter(|r| r.kind == "proton" && r.dxvk_line.as_deref().map(rank).unwrap_or(0) <= max && max > 0)
        .max_by_key(|r| (r.dxvk_line.as_deref().map(rank).unwrap_or(0), installed(r) as u8, r.version.clone()))
}

pub fn sha256_file(path: &Path) -> Result<String, String> {
    let o = Command::new("sha256sum").arg(path).output().map_err(|e| e.to_string())?;
    String::from_utf8_lossy(&o.stdout).split_whitespace().next().map(str::to_string).ok_or_else(|| "sha256sum failed".into())
}

pub fn sha512_file(path: &Path) -> Result<String, String> {
    let o = Command::new("sha512sum").arg(path).output().map_err(|e| e.to_string())?;
    String::from_utf8_lossy(&o.stdout).split_whitespace().next().map(str::to_string).ok_or_else(|| "sha512sum failed".into())
}

fn curl(url: &str, out: &Path) -> Result<(), String> {
    let st = Command::new("curl").args(["-fL", "--retry", "3", "--connect-timeout", "20", "-o"]).arg(out).arg(url).status()
        .map_err(|e| format!("curl: {e}"))?;
    if st.success() { Ok(()) } else { Err(format!("curl {url}: {st}")) }
}

/// Download every runner into `dest` (build host / Nobara), verifying and updating the lock.
pub fn fetch(rs: &[Runner], dest: &Path, lock_path: &Path, only: &[String]) -> Value {
    let mut lock = fsx::read_raw(lock_path).and_then(|t| json::parse(&t).ok()).unwrap_or_else(Value::obj);
    if lock.get("sha256").is_none() {
        lock.set("sha256", Value::obj());
    }
    let mut results = vec![];
    let _ = std::fs::create_dir_all(dest);
    for r in rs.iter().filter(|r| only.is_empty() || only.contains(&r.id)) {
        let out = dest.join(&r.file);
        let mut errs = vec![];
        let mut ok = out.exists();
        if !ok {
            for u in &r.urls {
                match curl(u, &out) {
                    Ok(()) => {
                        ok = true;
                        break;
                    }
                    Err(e) => errs.push(e), // next mirror
                }
            }
        }
        if !ok {
            results.push(orbis_core::jobj! {"id" => r.id.clone(), "state" => "FAIL", "errors" => errs});
            continue;
        }
        // upstream checksum when published
        if let Some(su) = &r.sha512_url {
            let sf = dest.join(format!("{}.sha512sum", r.file));
            if curl(su, &sf).is_ok() {
                let want = fsx::read(&sf).and_then(|t| t.split_whitespace().next().map(str::to_string)).unwrap_or_default();
                let got = sha512_file(&out).unwrap_or_default();
                if want != got {
                    let _ = std::fs::remove_file(&out);
                    results.push(orbis_core::jobj! {"id" => r.id.clone(), "state" => "FAIL", "why" => "SHA-512 differs from upstream .sha512sum; file deleted"});
                    continue;
                }
            } else {
                errs.push("upstream .sha512sum not reachable".into());
            }
        }
        let got = sha256_file(&out).unwrap_or_default();
        let known = lock.at(&format!("sha256.{}", r.file)).and_then(Value::as_str).map(str::to_string);
        let state = match known {
            Some(k) if k == got => "PASS",
            Some(_) => {
                let _ = std::fs::remove_file(&out);
                results.push(orbis_core::jobj! {"id" => r.id.clone(), "state" => "FAIL", "why" => "SHA-256 differs from runners.lock.json; file deleted"});
                continue;
            }
            None => {
                if let Value::Obj(top) = &mut lock {
                    if let Some(Value::Obj(sh)) = top.get_mut("sha256") {
                        sh.insert(r.file.clone(), Value::Str(got.clone()));
                    }
                }
                "PASS (first download: SHA-256 recorded in the lock file; commit it)"
            }
        };
        results.push(orbis_core::jobj! {"id" => r.id.clone(), "state" => state, "file" => out.display().to_string(), "sha256" => got, "notes" => errs});
    }
    let _ = fsx::atomic_write(lock_path, lock.to_string_pretty().as_bytes());
    // bundle checksum list for offline install
    let mut sums = String::new();
    for f in fsx::list_dir(dest) {
        if f.is_file() && !f.to_string_lossy().ends_with("SHA256SUMS") {
            if let Ok(h) = sha256_file(&f) {
                sums.push_str(&format!("{h}  {}\n", f.file_name().unwrap().to_string_lossy()));
            }
        }
    }
    let _ = fsx::atomic_write(&dest.join("SHA256SUMS"), sums.as_bytes());
    orbis_core::jobj! {"bundle" => dest.display().to_string(), "results" => Value::Arr(results)}
}

/// Install runners from an offline bundle directory (console): verify SHA256SUMS, extract.
pub fn install_bundle(rs: &[Runner], bundle: &Path, only: &[String]) -> Value {
    let sums = fsx::read_raw(&bundle.join("SHA256SUMS")).unwrap_or_default();
    let want: std::collections::BTreeMap<String, String> = sums
        .lines()
        .filter_map(|l| l.split_once("  ").map(|(h, f)| (f.trim().to_string(), h.to_string())))
        .collect();
    let mut out = vec![];
    for r in rs.iter().filter(|r| only.is_empty() || only.contains(&r.id)) {
        let f = bundle.join(&r.file);
        if !f.exists() {
            out.push(orbis_core::jobj! {"id" => r.id.clone(), "state" => "NOT_RUN", "why" => "not in bundle"});
            continue;
        }
        let got = sha256_file(&f).unwrap_or_default();
        if want.get(&r.file) != Some(&got) {
            out.push(orbis_core::jobj! {"id" => r.id.clone(), "state" => "FAIL", "why" => "checksum mismatch or not listed in SHA256SUMS"});
            continue;
        }
        let dst = expand(&r.install);
        let parent = dst.parent().map(Path::to_path_buf).unwrap_or_else(|| dst.clone());
        let _ = std::fs::create_dir_all(&parent);
        // tarballs contain one top directory named like the install directory
        let st = Command::new("tar").arg("-xf").arg(&f).arg("-C").arg(&parent).status();
        let ok = st.map(|s| s.success()).unwrap_or(false) && installed(r);
        out.push(orbis_core::jobj! {"id" => r.id.clone(), "state" => if ok { "PASS" } else { "FAIL" }, "installed_to" => dst.display().to_string()});
    }
    Value::Arr(out)
}
