//! Shader-cache housekeeping for orbis-run games.
//!
//! Layout (set by orbis-run): ~/.cache/orbis/shaders/<game>/{dxvk,vkd3d,mesa}. Mesa limits its own
//! cache (MESA_SHADER_CACHE_MAX_SIZE from the profile); DXVK state caches and the VKD3D-Proton
//! cache are not size-limited upstream, so this module enforces:
//!   per game  the profile's shader_cache.max_mb (oldest files go first; a game that is running
//!             is never touched),
//!   in total  a cap over all games (least recently played games go first, whole directory),
//!   by age    games not played for N days.
//! Steam's own shadercache is reported, never deleted (Steam manages it).

use orbis_core::fsx;
use orbis_core::{jobj, Value};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

pub fn cache_root(home: &Path) -> PathBuf {
    home.join(".cache/orbis/shaders")
}

#[derive(Clone, Debug)]
pub struct FileEntry {
    pub path: PathBuf,
    pub size: u64,
    pub mtime: SystemTime,
}

fn walk(dir: &Path, out: &mut Vec<FileEntry>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let Ok(md) = e.metadata() else { continue };
        if md.is_dir() {
            walk(&e.path(), out);
        } else if md.is_file() {
            out.push(FileEntry { path: e.path(), size: md.len(), mtime: md.modified().unwrap_or(SystemTime::UNIX_EPOCH) });
        }
    }
}

pub fn files(dir: &Path) -> Vec<FileEntry> {
    let mut v = vec![];
    walk(dir, &mut v);
    v
}

pub fn dir_size(dir: &Path) -> u64 {
    files(dir).iter().map(|f| f.size).sum()
}

fn last_used(dir: &Path) -> SystemTime {
    files(dir).iter().map(|f| f.mtime).max().unwrap_or(SystemTime::UNIX_EPOCH)
}

/// Remove oldest files of one game's cache until it is at or below `max_bytes`.
pub fn prune_game(dir: &Path, max_bytes: u64, dry_run: bool) -> (u64, usize) {
    let mut fs = files(dir);
    let mut total: u64 = fs.iter().map(|f| f.size).sum();
    if total <= max_bytes {
        return (0, 0);
    }
    fs.sort_by_key(|f| f.mtime);
    let (mut freed, mut n) = (0u64, 0usize);
    for f in fs {
        if total <= max_bytes {
            break;
        }
        // Mesa's single-file cache (index + db) must go as a pair: drop the whole mesa dir instead
        if f.path.components().any(|c| c.as_os_str() == "mesa") {
            continue;
        }
        if dry_run || std::fs::remove_file(&f.path).is_ok() {
            total -= f.size;
            freed += f.size;
            n += 1;
        }
    }
    if total > max_bytes {
        let mesa = dir.join("mesa");
        let ms = dir_size(&mesa);
        if ms > 0 && (dry_run || std::fs::remove_dir_all(&mesa).is_ok()) {
            freed += ms;
            n += 1;
        }
    }
    (freed, n)
}

pub struct Policy {
    pub total_max_bytes: u64,
    pub max_age_days: u64,
    pub per_game_max: Box<dyn Fn(&str) -> u64>,
    pub running: Vec<String>,
}

pub fn prune(root: &Path, pol: &Policy, dry_run: bool) -> Value {
    let now = SystemTime::now();
    let mut games: Vec<(String, PathBuf, u64, SystemTime)> = fsx::list_dir(root)
        .into_iter()
        .filter(|d| d.is_dir())
        .map(|d| {
            let n = d.file_name().unwrap().to_string_lossy().into_owned();
            let s = dir_size(&d);
            let lu = last_used(&d);
            (n, d, s, lu)
        })
        .collect();
    let mut actions = vec![];
    let mut freed_total = 0u64;
    let skip = |n: &str| pol.running.iter().any(|r| r == n);
    // 1. per game
    for (n, d, s, _) in games.iter_mut() {
        if skip(n) {
            continue;
        }
        let max = (pol.per_game_max)(n);
        let (freed, files) = prune_game(d, max, dry_run);
        if freed > 0 {
            *s -= freed.min(*s);
            freed_total += freed;
            actions.push(jobj! {"game" => n.clone(), "why" => "over per-game limit", "freed_mb" => (freed >> 20) as i64, "files" => files as i64});
        }
    }
    // 2. by age
    let max_age = Duration::from_secs(pol.max_age_days * 86400);
    games.retain(|(n, d, s, lu)| {
        if pol.max_age_days > 0 && !skip(n) && now.duration_since(*lu).unwrap_or_default() > max_age {
            if dry_run || std::fs::remove_dir_all(d).is_ok() {
                freed_total += *s;
                actions.push(jobj! {"game" => n.clone(), "why" => format!("not played for over {} days", pol.max_age_days),
                                    "freed_mb" => (*s >> 20) as i64});
                return false;
            }
        }
        true
    });
    // 3. total cap: least recently played first
    let mut total: u64 = games.iter().map(|g| g.2).sum();
    games.sort_by_key(|g| g.3);
    for (n, d, s, _) in &games {
        if total <= pol.total_max_bytes {
            break;
        }
        if skip(n) {
            continue;
        }
        if dry_run || std::fs::remove_dir_all(d).is_ok() {
            total -= *s;
            freed_total += *s;
            actions.push(jobj! {"game" => n.clone(), "why" => "total shader-cache cap", "freed_mb" => (*s >> 20) as i64});
        }
    }
    jobj! {"root" => root.display().to_string(), "dry_run" => dry_run, "freed_mb" => (freed_total >> 20) as i64,
           "total_after_mb" => (total >> 20) as i64, "total_cap_mb" => (pol.total_max_bytes >> 20) as i64,
           "actions" => Value::Arr(actions)}
}

pub fn status(root: &Path, home: &Path) -> Value {
    let games: Vec<Value> = fsx::list_dir(root)
        .into_iter()
        .filter(|d| d.is_dir())
        .map(|d| {
            let lu = last_used(&d).duration_since(SystemTime::UNIX_EPOCH).map(|x| x.as_secs() as i64).unwrap_or(0);
            jobj! {"game" => d.file_name().unwrap().to_string_lossy().into_owned(),
                   "dxvk_mb" => (dir_size(&d.join("dxvk")) >> 20) as i64, "vkd3d_mb" => (dir_size(&d.join("vkd3d")) >> 20) as i64,
                   "mesa_mb" => (dir_size(&d.join("mesa")) >> 20) as i64, "last_used" => orbis_core::sys::iso_utc(lu)}
        })
        .collect();
    let steam = home.join(".steam/root/steamapps/shadercache");
    jobj! {"root" => root.display().to_string(), "games" => Value::Arr(games),
           "total_mb" => (dir_size(root) >> 20) as i64,
           "steam_shadercache_mb" => if steam.exists() { Value::Int((dir_size(&steam) >> 20) as i64) } else { Value::Null },
           "steam_note" => "Steam's shadercache is managed by Steam (Settings → Downloads) and never pruned here"}
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mk(path: &Path, bytes: usize, age_days: u64) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, vec![0u8; bytes]).unwrap();
        let t = SystemTime::now() - Duration::from_secs(age_days * 86400);
        let f = std::fs::File::options().write(true).open(path).unwrap();
        f.set_modified(t).unwrap();
    }

    #[test]
    fn per_game_age_and_total() {
        let root = std::env::temp_dir().join(format!("orbis-cache-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        const M: usize = 1 << 20;
        mk(&root.join("witcher/dxvk/a.dxvk-cache"), 3 * M, 5);
        mk(&root.join("witcher/vkd3d/old.bin"), 2 * M, 9);
        mk(&root.join("witcher/vkd3d/new.bin"), 1 * M, 1);
        mk(&root.join("ancient/dxvk/x"), 1 * M, 200);
        mk(&root.join("running/dxvk/x"), 9 * M, 300);
        mk(&root.join("other/dxvk/x"), 4 * M, 30);
        let pol = Policy {
            total_max_bytes: 14 * M as u64,
            max_age_days: 90,
            per_game_max: Box::new(|g| if g == "witcher" { 4 * M as u64 } else { 100 * M as u64 }),
            running: vec!["running".into()],
        };
        let v = prune(&root, &pol, false);
        // witcher: 6 MiB > 4: the oldest file (old.bin, 9 days) goes first, which is enough
        assert!(!root.join("witcher/vkd3d/old.bin").exists());
        assert!(root.join("witcher/vkd3d/new.bin").exists());
        // ancient (200 days) removed; running (300 days) kept because it is running
        assert!(!root.join("ancient").exists());
        assert!(root.join("running").exists());
        // total: running 9 + other 4 + witcher <=4 = within 14 after pruning
        assert!(v.get("total_after_mb").unwrap().as_i64().unwrap() <= 14, "{}", v.to_string_pretty());
        let _ = std::fs::remove_dir_all(&root);
    }
}
