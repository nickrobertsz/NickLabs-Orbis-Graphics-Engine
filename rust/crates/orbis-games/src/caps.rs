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

/// One Vulkan memory heap as the driver reports it. On the PS4 (an APU) RADV builds its heaps from
/// the VRAM carve-out plus the GTT budget, so the heaps describe the same physical GDDR5 that
/// Linux also uses: their sizes are GPU-addressable limits, never extra memory, and are not added
/// to physical RAM anywhere in Orbis.
#[derive(Clone, Debug, PartialEq)]
pub struct Heap {
    pub index: u32,
    pub size: u64,
    pub budget: Option<u64>,
    pub usage: Option<u64>,
    pub device_local: bool,
}

/// Parse the memoryHeaps of one GPU from `vulkaninfo` text output. With several GPUs the section
/// whose deviceName matches `device` is used (the text output lists all GPUs as GPU0:, GPU1:, ...).
pub fn heaps_from_text(text: &str, device: &str) -> Vec<Heap> {
    let mut sections: Vec<String> = vec![String::new()];
    for l in text.lines() {
        let t = l.trim();
        if t.starts_with("GPU") && t.ends_with(':') && t[3..t.len() - 1].chars().all(|c| c.is_ascii_digit()) {
            sections.push(String::new());
        }
        let last = sections.last_mut().unwrap();
        last.push_str(l);
        last.push('\n');
    }
    let pick = sections
        .iter()
        .find(|s| !device.is_empty() && s.lines().any(|l| l.trim_start().starts_with("deviceName") && l.contains(device)))
        .or_else(|| sections.iter().find(|s| s.contains("memoryHeaps[")))
        .cloned()
        .unwrap_or_default();
    let num = |l: &str| l.split('=').nth(1).and_then(|v| v.split_whitespace().next()).and_then(|v| v.parse::<u64>().ok());
    let mut out: Vec<Heap> = vec![];
    let mut in_heaps = false;
    for l in pick.lines() {
        let t = l.trim();
        if t.starts_with("memoryTypes") {
            in_heaps = false;
        }
        if let Some(rest) = t.strip_prefix("memoryHeaps[") {
            if let Some(i) = rest.split(']').next().and_then(|x| x.parse().ok()) {
                out.push(Heap { index: i, size: 0, budget: None, usage: None, device_local: false });
                in_heaps = true;
                continue;
            }
        }
        if !in_heaps {
            continue;
        }
        if let Some(h) = out.last_mut() {
            if t.starts_with("size") {
                h.size = num(t).unwrap_or(0);
            } else if t.starts_with("budget") {
                h.budget = num(t);
            } else if t.starts_with("usage") {
                h.usage = num(t);
            } else if t.contains("MEMORY_HEAP_DEVICE_LOCAL_BIT") {
                h.device_local = true;
            }
        }
    }
    out
}

/// Heaps of the GPU `device` (runs plain `vulkaninfo`; empty when it is unavailable).
pub fn probe_heaps(device: &str) -> Vec<Heap> {
    Command::new("vulkaninfo")
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| heaps_from_text(&String::from_utf8_lossy(&o.stdout), device))
        .unwrap_or_default()
}

/// The memory part of the capability report.
pub fn memory_report(c: &Caps, heaps: &[Heap]) -> Value {
    let mib = |b: u64| (b >> 20) as i64;
    orbis_core::jobj! {
        "heaps" => Value::Arr(heaps.iter().map(|h| orbis_core::jobj! {
            "index" => h.index as i64, "size_mib" => mib(h.size), "budget_mib" => h.budget.map(mib),
            "usage_mib" => h.usage.map(mib), "device_local" => h.device_local}).collect()),
        "heaps_note" => "APU: heaps are views of the shared GDDR5 (VRAM carve-out + GTT budget). They overlap Linux RAM and are not added to it.",
        // zero-copy: the GPU reads/writes ordinary process memory (amdgpu userptr, HMM mirror)
        "external_memory_host" => c.has_ext("VK_EXT_external_memory_host"),
        // per-heap budget that follows the runtime GTT budget (orbis-memd)
        "memory_budget" => c.has_ext("VK_EXT_memory_budget"),
        "memory_priority" => c.has_ext("VK_EXT_memory_priority"),
        "pageable_device_local_memory" => c.has_ext("VK_EXT_pageable_device_local_memory"),
        "dma_buf" => c.has_ext("VK_EXT_external_memory_dma_buf")
    }
}

#[cfg(test)]
mod heap_tests {
    use super::*;

    #[test]
    fn heaps_parse_per_gpu() {
        let t = "GPU0:\n\tdeviceName = llvmpipe (LLVM 21.1.0, 256 bits)\nVkPhysicalDeviceMemoryProperties:\nmemoryHeaps: count = 1\n\
                 \tmemoryHeaps[0]:\n\t\tsize   = 8589934592 (0x200000000) (8.00 GiB)\n\t\tflags: count = 1\n\t\t\tMEMORY_HEAP_DEVICE_LOCAL_BIT\n\
                 GPU1:\n\tdeviceName = AMD Radeon Graphics (RADV LIVERPOOL)\nmemoryHeaps: count = 2\n\
                 \tmemoryHeaps[0]:\n\t\tsize   = 3221225472 (0xc0000000) (3.00 GiB)\n\t\tbudget = 3000000000 (0xb2d05e00) (2.79 GiB)\n\
                 \t\tusage  = 1048576 (0x00100000) (1.00 MiB)\n\t\tflags: count = 1\n\t\t\tMEMORY_HEAP_DEVICE_LOCAL_BIT\n\
                 \tmemoryHeaps[1]:\n\t\tsize   = 1610612736 (0x60000000) (1.50 GiB)\n\t\tflags:\n\t\t\tNone\n\
                 memoryTypes: count = 4\n\tmemoryTypes[0]:\n\t\theapIndex = 0\n\t\tsize = 1\n";
        let h = heaps_from_text(t, "AMD Radeon Graphics (RADV LIVERPOOL)");
        assert_eq!(h.len(), 2);
        assert_eq!(h[0], Heap { index: 0, size: 3 << 30, budget: Some(3000000000), usage: Some(1 << 20), device_local: true });
        assert_eq!((h[1].size, h[1].device_local), (1536 << 20, false));
        assert_eq!(heaps_from_text(t, "llvmpipe").len(), 1);
    }
}
