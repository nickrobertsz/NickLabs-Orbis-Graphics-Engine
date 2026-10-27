//! Filesystem helpers. Every system path goes through `p()`, which re-roots it under
//! $ORBIS_ROOT (or $ORBIS_GFX_ROOT, the convention of the Python tools) so tests run on fixture
//! trees. Writes that must survive power loss use write-temp + fsync + rename.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

pub fn root() -> PathBuf {
    std::env::var_os("ORBIS_ROOT")
        .or_else(|| std::env::var_os("ORBIS_GFX_ROOT"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"))
}

/// Absolute system path re-rooted for tests: p("/sys/class/rtc") -> $ORBIS_ROOT/sys/class/rtc
pub fn p(path: &str) -> PathBuf {
    root().join(path.trim_start_matches('/'))
}

pub fn read(path: &Path) -> Option<String> {
    let mut s = String::new();
    File::open(path).ok()?.read_to_string(&mut s).ok()?;
    Some(s.trim().to_string())
}

pub fn read_raw(path: &Path) -> Option<String> {
    fs::read(path).ok().map(|b| String::from_utf8_lossy(&b).into_owned())
}

pub fn read_i64(path: &Path) -> Option<i64> {
    read(path)?.split_whitespace().next()?.parse().ok()
}

pub fn read_u64(path: &Path) -> Option<u64> {
    read(path)?.split_whitespace().next()?.parse().ok()
}

/// Write one value to a sysfs/procfs/cgroupfs file; returns the kernel's error unchanged.
pub fn write_value(path: &Path, value: &str) -> io::Result<()> {
    let mut f = OpenOptions::new().write(true).truncate(true).open(path)?;
    f.write_all(value.as_bytes())
}

pub fn atomic_write(path: &Path, data: &[u8]) -> io::Result<()> {
    if let Some(d) = path.parent() {
        fs::create_dir_all(d)?;
    }
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("f");
    let tmp = path.with_file_name(format!(".{name}.tmp{}", std::process::id()));
    {
        let mut f = File::create(&tmp)?;
        f.write_all(data)?;
        f.sync_all()?;
    }
    fs::rename(&tmp, path)?;
    if let Some(d) = path.parent() {
        if let Ok(df) = File::open(d) {
            let _ = df.sync_all();
        }
    }
    Ok(())
}

pub fn append_line(path: &Path, line: &str) -> io::Result<()> {
    if let Some(d) = path.parent() {
        fs::create_dir_all(d)?;
    }
    let mut f = OpenOptions::new().create(true).append(true).open(path)?;
    f.write_all(line.as_bytes())?;
    if !line.ends_with('\n') {
        f.write_all(b"\n")?;
    }
    f.sync_data()
}

pub fn exists(path: &Path) -> bool {
    path.exists()
}

pub fn list_dir(path: &Path) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = fs::read_dir(path).map(|rd| rd.flatten().map(|e| e.path()).collect()).unwrap_or_default();
    v.sort();
    v
}

/// /proc/cmdline as (key, value) pairs; flags get an empty value.
pub fn cmdline() -> Vec<(String, String)> {
    read(&p("/proc/cmdline"))
        .unwrap_or_default()
        .split_whitespace()
        .map(|t| match t.split_once('=') {
            Some((k, v)) => (k.to_string(), v.to_string()),
            None => (t.to_string(), String::new()),
        })
        .collect()
}

pub fn cmdline_get(key: &str) -> Option<String> {
    cmdline().into_iter().find(|(k, _)| k == key).map(|(_, v)| v)
}

/// Parse "a=1 b=0x2 c=x" (Graphics Engine sysfs format).
pub fn kv(text: &str) -> Vec<(String, String)> {
    text.split_whitespace()
        .filter_map(|t| t.split_once('=').map(|(k, v)| (k.to_string(), v.to_string())))
        .collect()
}

pub fn kv_get(text: &str, key: &str) -> Option<String> {
    kv(text).into_iter().find(|(k, _)| k == key).map(|(_, v)| v)
}

pub fn parse_int(s: &str) -> Option<i64> {
    let s = s.trim();
    if let Some(h) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        i64::from_str_radix(h, 16).ok()
    } else {
        s.parse().ok()
    }
}

/// The PS4 boot partition (bootargs.txt, vram.txt, ...): $ORBIS_BOOT_DIR or /boot/ps4.
pub fn boot_dir() -> PathBuf {
    std::env::var_os("ORBIS_BOOT_DIR").map(PathBuf::from).unwrap_or_else(|| p("/boot/ps4"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kv_and_int() {
        assert_eq!(kv_get("state=max did=1 x", "did").as_deref(), Some("1"));
        assert_eq!(parse_int("0x1f"), Some(31));
        assert_eq!(parse_int(" 42 "), Some(42));
    }

    #[test]
    fn atomic_roundtrip() {
        let d = std::env::temp_dir().join(format!("orbis-fsx-{}", std::process::id()));
        let f = d.join("a/b.txt");
        atomic_write(&f, b"hello").unwrap();
        assert_eq!(read(&f).as_deref(), Some("hello"));
        append_line(&d.join("log"), "x").unwrap();
        append_line(&d.join("log"), "y").unwrap();
        assert_eq!(read_raw(&d.join("log")).unwrap(), "x\ny\n");
        fs::remove_dir_all(d).unwrap();
    }
}
