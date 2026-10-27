//! Restore journal: every knob Orbis changes is recorded with its original value first, in an
//! fsync'd JSON file, so `restore` puts the system back exactly - also after a crash or reboot
//! of the controller in the middle of a game.

use crate::fsx::{atomic_write, read_raw, write_value};
use crate::json::{self, Value};
use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};

pub struct Journal {
    file: PathBuf,
    /// key (path, "path#tag" for several values written to one file such as per-region
    /// cgroup dmem files, or "nice:<pid>" etc.) -> original value
    orig: BTreeMap<String, String>,
}

impl Journal {
    pub fn open(file: PathBuf) -> Journal {
        let orig = read_raw(&file)
            .and_then(|s| json::parse(&s).ok())
            .and_then(|v| {
                v.get("original").and_then(|o| o.as_obj()).map(|m| {
                    m.iter().filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_string()))).collect()
                })
            })
            .unwrap_or_default();
        Journal { file, orig }
    }

    fn save(&self) -> io::Result<()> {
        let mut m = BTreeMap::new();
        for (k, v) in &self.orig {
            m.insert(k.clone(), Value::Str(v.clone()));
        }
        let doc = crate::jobj! {"schema" => "orbis-restore/1", "original" => Value::Obj(m)};
        atomic_write(&self.file, doc.to_string_pretty().as_bytes())
    }

    /// Remember the original value of `key` once (first change wins).
    pub fn remember(&mut self, key: &str, original: &str) -> io::Result<()> {
        if !self.orig.contains_key(key) {
            self.orig.insert(key.to_string(), original.to_string());
            self.save()?;
        }
        Ok(())
    }

    /// Write `value` to a sysfs/procfs file, journaling the old value first. Selector files
    /// such as "[mq-deadline] bfq" or governors are stored as the active choice.
    pub fn set_file(&mut self, path: &Path, value: &str) -> io::Result<bool> {
        let cur = read_raw(path).ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, path.display().to_string()))?;
        let cur = active_choice(cur.trim());
        if cur == value {
            return Ok(false);
        }
        self.remember(&path.display().to_string(), &cur)?;
        write_value(path, value)?;
        Ok(true)
    }

    pub fn forget(&mut self, key: &str) -> io::Result<()> {
        if self.orig.remove(key).is_some() {
            self.save()?;
        }
        Ok(())
    }

    pub fn entries(&self) -> Vec<(String, String)> {
        self.orig.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
    }

    /// Restore every recorded file value (keys that are paths). Returns (restored, errors).
    pub fn restore_files(&mut self, filter: impl Fn(&str) -> bool) -> (Vec<String>, Vec<String>) {
        let mut ok = Vec::new();
        let mut err = Vec::new();
        let keys: Vec<String> = self.orig.keys().filter(|k| k.starts_with('/') && filter(k)).cloned().collect();
        for k in keys {
            let v = self.orig[&k].clone();
            let path = k.split('#').next().unwrap_or(&k);
            match write_value(Path::new(path), &v) {
                Ok(()) => {
                    ok.push(format!("{k} = {v}"));
                    self.orig.remove(&k);
                }
                Err(e) if e.kind() == io::ErrorKind::NotFound => {
                    self.orig.remove(&k); // the object is gone (process exited, device removed)
                }
                Err(e) => err.push(format!("{k}: {e}")),
            }
        }
        let _ = self.save();
        (ok, err)
    }
}

/// "[mq-deadline] kyber bfq none" -> "mq-deadline"; "always [madvise] never" -> "madvise";
/// plain values are returned unchanged.
pub fn active_choice(s: &str) -> String {
    if let (Some(a), Some(b)) = (s.find('['), s.find(']')) {
        if a < b {
            return s[a + 1..b].to_string();
        }
    }
    s.to_string()
}

/// Choices offered by a selector file.
pub fn choices(s: &str) -> Vec<String> {
    s.split_whitespace().map(|t| t.trim_matches(|c| c == '[' || c == ']').to_string()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn journal_restores() {
        let d = std::env::temp_dir().join(format!("orbis-journal-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let knob = d.join("scheduler");
        std::fs::write(&knob, "[mq-deadline] kyber bfq none\n").unwrap();
        let mut j = Journal::open(d.join("j.json"));
        assert!(j.set_file(&knob, "bfq").unwrap());
        std::fs::write(&knob, "mq-deadline kyber [bfq] none\n").unwrap(); // what the kernel shows
        assert!(!j.set_file(&knob, "bfq").unwrap());
        let j2 = Journal::open(d.join("j.json")); // survives a restart
        assert_eq!(j2.entries().len(), 1);
        let mut j2 = j2;
        let (ok, err) = j2.restore_files(|_| true);
        assert_eq!((ok.len(), err.len()), (1, 0));
        assert_eq!(std::fs::read_to_string(&knob).unwrap(), "mq-deadline");
        assert!(Journal::open(d.join("j.json")).entries().is_empty());
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn selectors() {
        assert_eq!(active_choice("always [madvise] never"), "madvise");
        assert_eq!(active_choice("schedutil"), "schedutil");
        assert_eq!(choices("[a] b c"), vec!["a", "b", "c"]);
    }
}
