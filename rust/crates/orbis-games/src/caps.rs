//! What the GPU driver really offers, from `vulkaninfo --json` (Vulkan Profiles JSON) with a
//! text fallback, flattened into extension / feature / property sets. Nothing is assumed from
//! the GPU's name: a capability is present only when the driver reports it.

use orbis_core::json::{self, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::process::Command;

#[derive(Clone, Debug, Default)]
pub struct Caps {
    pub device_name: String,
    pub driver_name: String,
    pub api: (u32, u32, u32),
    pub software: bool,
    pub extensions: BTreeSet<String>,
    /// featureName -> enabled (all feature structs merged)
    pub features: BTreeMap<String, bool>,
    /// propertyName -> numeric value
    pub props: BTreeMap<String, f64>,
    pub source: String,
}

pub fn parse_api(v: &Value) -> Option<(u32, u32, u32)> {
    match v {
        Value::Int(i) => {
            let i = *i as u32;
            Some(((i >> 22) & 0x7f, (i >> 12) & 0x3ff, i & 0xfff))
        }
        Value::Str(s) => {
            let mut it = s.split(['.', ' ']).filter_map(|x| x.parse::<u32>().ok());
            Some((it.next()?, it.next()?, it.next().unwrap_or(0)))
        }
        _ => None,
    }
}

fn walk(v: &Value, ctx: &str, c: &mut Caps) {
    match v {
        Value::Obj(m) => {
            for (k, x) in m {
                match k.as_str() {
                    "extensions" => {
                        if let Some(o) = x.as_obj() {
                            c.extensions.extend(o.keys().cloned());
                        } else if let Some(a) = x.as_arr() {
                            for e in a {
                                if let Some(n) = e.get("extensionName").and_then(Value::as_str) {
                                    c.extensions.insert(n.to_string());
                                }
                            }
                        }
                        continue;
                    }
                    "apiVersion" => {
                        if let Some(api) = parse_api(x) {
                            if api > c.api {
                                c.api = api;
                            }
                        }
                    }
                    "deviceName" => {
                        if let Some(s) = x.as_str() {
                            c.device_name = s.to_string();
                        }
                    }
                    "driverName" => {
                        if let Some(s) = x.as_str() {
                            c.driver_name = s.to_string();
                        }
                    }
                    "deviceType" => {
                        let t = x.as_str().map(str::to_string).or_else(|| x.as_i64().map(|i| i.to_string())).unwrap_or_default();
                        if t.contains("CPU") || t == "4" {
                            c.software = true;
                        }
                    }
                    _ => {}
                }
                match x {
                    Value::Bool(b) if ctx == "features" => {
                        c.features.insert(k.clone(), *b);
                    }
                    Value::Int(_) | Value::Float(_) if ctx == "properties" || k.starts_with("max") => {
                        if let Some(f) = x.as_f64() {
                            c.props.insert(k.clone(), f);
                        }
                    }
                    _ => {}
                }
                let nctx = if k == "features" { "features" } else if k == "properties" { "properties" } else { ctx };
                walk(x, nctx, c);
            }
        }
        Value::Arr(a) => {
            for x in a {
                walk(x, ctx, c);
            }
        }
        _ => {}
    }
}

pub fn from_json(text: &str) -> Result<Caps, String> {
    let v = json::parse(text).map_err(|e| e.to_string())?;
    let mut c = Caps { source: "vulkaninfo --json".into(), ..Default::default() };
    walk(&v, "", &mut c);
    finish(&mut c);
    Ok(c)
}

/// `vulkaninfo` text output: "VK_EXT_robustness2 : extension revision 1", "shaderInt64 = true",
/// "apiVersion = 1.3.296 (4206888)", "deviceName = AMD Radeon ..."
pub fn from_text(text: &str) -> Caps {
    let mut c = Caps { source: "vulkaninfo (text)".into(), ..Default::default() };
    for line in text.lines() {
        let l = line.trim();
        if let Some((name, _)) = l.split_once(':') {
            let n = name.trim();
            if n.starts_with("VK_") && l.contains("revision") {
                c.extensions.insert(n.to_string());
                continue;
            }
        }
        if let Some((k, v)) = l.split_once('=') {
            let (k, v) = (k.trim(), v.trim());
            match k {
                "apiVersion" => {
                    if let Some(api) = parse_api(&Value::Str(v.to_string())) {
                        if api > c.api {
                            c.api = api;
                        }
                    }
                }
                "deviceName" => c.device_name = v.to_string(),
                "driverName" => c.driver_name = v.to_string(),
                "deviceType" if v.contains("CPU") => c.software = true,
                _ if v == "true" || v == "false" => {
                    c.features.insert(k.to_string(), v == "true");
                }
                _ => {
                    if let Ok(f) = v.split_whitespace().next().unwrap_or("").parse::<f64>() {
                        if k.starts_with("max") {
                            c.props.insert(k.to_string(), f);
                        }
                    }
                }
            }
        }
    }
    finish(&mut c);
    c
}

fn finish(c: &mut Caps) {
    let n = format!("{} {}", c.device_name, c.driver_name).to_lowercase();
    if n.contains("llvmpipe") || n.contains("lavapipe") || n.contains("swiftshader") {
        c.software = true;
    }
}

impl Caps {
    pub fn has_ext(&self, e: &str) -> bool {
        self.extensions.contains(e)
    }
    pub fn feature(&self, f: &str) -> bool {
        self.features.get(f).copied().unwrap_or(false)
    }
    pub fn prop(&self, p: &str) -> Option<f64> {
        self.props.get(p).copied()
    }
    pub fn api_at_least(&self, maj: u32, min: u32) -> bool {
        (self.api.0, self.api.1) >= (maj, min)
    }
    pub fn api_str(&self) -> String {
        format!("{}.{}.{}", self.api.0, self.api.1, self.api.2)
    }
}

/// Run vulkaninfo on the first GPU. JSON output goes to a temporary file (-o).
pub fn probe() -> Result<Caps, String> {
    let tmp = std::env::temp_dir().join(format!("orbis-vkcaps-{}.json", std::process::id()));
    let r = Command::new("vulkaninfo").args(["--json=0", "-o"]).arg(&tmp).output();
    if let Ok(o) = &r {
        if o.status.success() {
            if let Ok(t) = std::fs::read_to_string(&tmp) {
                let _ = std::fs::remove_file(&tmp);
                return from_json(&t);
            }
        }
    }
    let _ = std::fs::remove_file(&tmp);
    let o = Command::new("vulkaninfo").output().map_err(|e| format!("vulkaninfo not runnable ({e}); install vulkan-tools"))?;
    if !o.status.success() {
        return Err(format!("vulkaninfo failed: {}", String::from_utf8_lossy(&o.stderr).lines().last().unwrap_or("")));
    }
    Ok(from_text(&String::from_utf8_lossy(&o.stdout)))
}
