//! orbis-core: shared, dependency-free building blocks for the NickLabs Orbis native tools.

pub mod admit;
pub mod fsx;
pub mod json;
pub mod procs;
pub mod restore;
pub mod sys;

pub use json::Value;

/// Result states shared by every Orbis report (same vocabulary as the Python tools).
pub const STATES: [&str; 6] = ["PASS", "FAIL", "DEGRADED", "BLOCKED", "UNSUPPORTED", "NOT_RUN"];

/// Release status labels for features (engineering directive): what has actually been shown.
pub const FEATURE_STATUS: [&str; 5] = ["BUILT", "HOST-VERIFIED", "PS4-VERIFIED", "BLOCKED", "UNVERIFIED"];

/// Tiny argument helper: `--key value` / `--flag` / positionals.
pub struct Args {
    pub pos: Vec<String>,
    pub opts: Vec<(String, Option<String>)>,
}

impl Args {
    /// `flags` lists options that take no value.
    pub fn parse(argv: &[String], flags: &[&str]) -> Args {
        let mut pos = Vec::new();
        let mut opts = Vec::new();
        let mut i = 0;
        while i < argv.len() {
            let a = &argv[i];
            if let Some(k) = a.strip_prefix("--") {
                if let Some((k, v)) = k.split_once('=') {
                    opts.push((k.to_string(), Some(v.to_string())));
                } else if flags.contains(&k) || i + 1 >= argv.len() || argv[i + 1].starts_with("--") {
                    opts.push((k.to_string(), None));
                } else {
                    opts.push((k.to_string(), Some(argv[i + 1].clone())));
                    i += 1;
                }
            } else {
                pos.push(a.clone());
            }
            i += 1;
        }
        Args { pos, opts }
    }
    pub fn flag(&self, k: &str) -> bool {
        self.opts.iter().any(|(n, _)| n == k)
    }
    pub fn get(&self, k: &str) -> Option<&str> {
        self.opts.iter().rev().find(|(n, _)| n == k).and_then(|(_, v)| v.as_deref())
    }
    pub fn all(&self, k: &str) -> Vec<String> {
        self.opts.iter().filter(|(n, _)| n == k).filter_map(|(_, v)| v.clone()).collect()
    }
}

/// Print a report as JSON (`--json`) or as indented text.
pub fn emit(v: &Value, as_json: bool) {
    if as_json {
        print!("{}", v.to_string_pretty());
    } else {
        print!("{}", render(v, 0));
    }
}

pub fn render(v: &Value, level: usize) -> String {
    let pad = "  ".repeat(level);
    let mut out = String::new();
    match v {
        Value::Obj(m) => {
            for (k, x) in m {
                match x {
                    Value::Obj(mm) if !mm.is_empty() => {
                        out.push_str(&format!("{pad}{k}:\n{}", render(x, level + 1)));
                    }
                    Value::Arr(a) if !a.is_empty() => {
                        out.push_str(&format!("{pad}{k}:\n{}", render(x, level + 1)));
                    }
                    _ => out.push_str(&format!("{pad}{k}: {}\n", scalar(x))),
                }
            }
        }
        Value::Arr(a) => {
            for x in a {
                match x {
                    Value::Obj(_) | Value::Arr(_) => out.push_str(&format!("{pad}-\n{}", render(x, level + 1))),
                    _ => out.push_str(&format!("{pad}- {}\n", scalar(x))),
                }
            }
        }
        _ => out.push_str(&format!("{pad}{}\n", scalar(v))),
    }
    out
}

fn scalar(v: &Value) -> String {
    match v {
        Value::Null => "-".into(),
        Value::Str(s) => s.clone(),
        Value::Obj(_) => "{}".into(),
        Value::Arr(_) => "[]".into(),
        other => other.to_string_compact(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn args() {
        let v: Vec<String> = ["status", "--json", "--server", "10.0.0.2", "--x=1", "pos2"].iter().map(|s| s.to_string()).collect();
        let a = Args::parse(&v, &["json"]);
        assert!(a.flag("json"));
        assert_eq!(a.get("server"), Some("10.0.0.2"));
        assert_eq!(a.get("x"), Some("1"));
        assert_eq!(a.pos, vec!["status", "pos2"]);
    }
}
