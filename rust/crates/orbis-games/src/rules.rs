//! Translation-layer requirements and automatic selection. Requirements come from the upstream
//! documentation (pinned in manifest/sources.json -> gaming):
//!   DXVK 3.x  (wiki "Driver support"): VK_EXT_depth_clip_enable, VK_KHR_maintenance5,
//!             VK_KHR_maintenance6, VK_KHR_load_store_op_none, VK_EXT_robustness2,
//!             VK_EXT_transform_feedback; >= 256 bytes of push constants; descriptor indexing,
//!             8/16/64-bit integers, scalarBlockLayout. Vulkan 1.4 "should" be supported, but
//!             the wiki notes older GCN cards keep working without advertising 1.4, so the
//!             extension/feature list decides, not the API number.
//!   DXVK 2.x  (2.0 release notes): Vulkan 1.3 with dynamic rendering, extended dynamic state and
//!             null descriptors (VK_EXT_robustness2), plus transform feedback / depth clip.
//!   DXVK 1.10.3 (legacy): a Vulkan 1.1 driver.
//!   VKD3D-Proton (README): Vulkan 1.3, descriptor indexing with >= 1,000,000 update-after-bind
//!             descriptors, samplerMirrorClampToEdge, shaderDrawParameters, VK_EXT_robustness2,
//!             VK_KHR_push_descriptor (VK_EXT_mutable_descriptor_type / image_view_min_lod recommended).
//! Newest is not assumed best: the newest version whose requirements are all met is chosen, and
//! every missing item is listed. WineD3D (OpenGL) is the D3D9-11 fallback.

use crate::caps::Caps;
use orbis_core::{jobj, Value};

pub struct Check {
    pub ok: bool,
    pub missing: Vec<String>,
}

fn need(c: &Caps, exts: &[&str], feats: &[&str], promoted_in_14: &[&str]) -> Vec<String> {
    let mut m = vec![];
    for e in exts {
        let promoted = promoted_in_14.contains(e) && c.api_at_least(1, 4);
        if !c.has_ext(e) && !promoted {
            m.push(e.to_string());
        }
    }
    for f in feats {
        if !c.feature(f) {
            m.push(format!("feature {f}"));
        }
    }
    m
}

pub fn dxvk3(c: &Caps) -> Check {
    let mut m = need(
        c,
        &["VK_EXT_depth_clip_enable", "VK_KHR_maintenance5", "VK_KHR_maintenance6", "VK_KHR_load_store_op_none",
          "VK_EXT_robustness2", "VK_EXT_transform_feedback"],
        &["robustBufferAccess2", "nullDescriptor", "descriptorIndexing", "shaderInt8", "shaderInt16", "shaderInt64",
          "scalarBlockLayout"],
        &["VK_KHR_maintenance5", "VK_KHR_maintenance6", "VK_KHR_load_store_op_none"],
    );
    if c.prop("maxPushConstantsSize").unwrap_or(0.0) < 256.0 {
        m.push("maxPushConstantsSize >= 256".into());
    }
    Check { ok: m.is_empty() && !c.software, missing: m }
}

pub fn dxvk2(c: &Caps) -> Check {
    let mut m = need(
        c,
        &["VK_EXT_robustness2", "VK_EXT_transform_feedback", "VK_EXT_depth_clip_enable"],
        &["robustBufferAccess2", "nullDescriptor", "geometryShader", "dynamicRendering", "extendedDynamicState"],
        &[],
    );
    if !c.api_at_least(1, 3) {
        m.insert(0, format!("Vulkan 1.3 (driver reports {})", c.api_str()));
        // dynamicRendering / extendedDynamicState may be reported only through 1.3 core structs
    }
    Check { ok: m.is_empty() && !c.software, missing: m }
}

pub fn dxvk1(c: &Caps) -> Check {
    let mut m = vec![];
    if !c.api_at_least(1, 1) {
        m.push(format!("Vulkan 1.1 (driver reports {})", c.api_str()));
    }
    Check { ok: m.is_empty() && !c.software, missing: m }
}

pub fn vkd3d(c: &Caps) -> Check {
    let mut m = need(
        c,
        &["VK_EXT_robustness2", "VK_KHR_push_descriptor"],
        &["samplerMirrorClampToEdge", "shaderDrawParameters", "runtimeDescriptorArray", "descriptorBindingPartiallyBound",
          "descriptorBindingVariableDescriptorCount", "descriptorBindingSampledImageUpdateAfterBind",
          "descriptorBindingStorageImageUpdateAfterBind", "descriptorBindingStorageBufferUpdateAfterBind",
          "shaderSampledImageArrayNonUniformIndexing", "shaderStorageImageArrayNonUniformIndexing"],
        &["VK_KHR_push_descriptor"],
    );
    if !c.api_at_least(1, 3) {
        m.insert(0, format!("Vulkan 1.3 (driver reports {})", c.api_str()));
    }
    for p in ["maxDescriptorSetUpdateAfterBindSampledImages", "maxDescriptorSetUpdateAfterBindStorageImages",
              "maxDescriptorSetUpdateAfterBindStorageBuffers"] {
        if c.prop(p).unwrap_or(0.0) < 1_000_000.0 {
            m.push(format!("{p} >= 1000000"));
        }
    }
    Check { ok: m.is_empty() && !c.software, missing: m }
}

/// DXVK release used for each line (pinned tags; see the runner catalog).
pub const DXVK3: &str = "3.1.1";
pub const DXVK2: &str = "2.7.1";
pub const DXVK1: &str = "1.10.3";

pub fn select(c: &Caps) -> Value {
    let (d3, d2, d1, v) = (dxvk3(c), dxvk2(c), dxvk1(c), vkd3d(c));
    let (line, ver) = if d3.ok {
        ("3.x", Some(DXVK3))
    } else if d2.ok {
        ("2.x", Some(DXVK2))
    } else if d1.ok {
        ("1.10", Some(DXVK1))
    } else {
        ("none", None)
    };
    let d3d11 = if ver.is_some() { format!("dxvk-{line}") } else { "wined3d (OpenGL)".into() };
    let mk = |ch: &Check| jobj! {"ok" => ch.ok, "missing" => ch.missing.clone()};
    jobj! {
        "hardware_vulkan" => !c.software,
        "dxvk" => jobj! {"line" => line, "version" => ver, "checks" => jobj! {"3.x" => mk(&d3), "2.x" => mk(&d2), "1.10" => mk(&d1)}},
        "d3d9_d3d11" => d3d11,
        "vkd3d" => jobj! {"ok" => v.ok, "missing" => v.missing.clone(),
                          "recommended_missing" => ["VK_EXT_mutable_descriptor_type", "VK_EXT_image_view_min_lod"]
                              .iter().filter(|e| !c.has_ext(e)).map(|s| s.to_string()).collect::<Vec<String>>()},
        "d3d12" => if v.ok { "vkd3d-proton".to_string() } else { "UNSUPPORTED on this driver (see vkd3d.missing)".to_string() },
        "opengl_fallback" => "WineD3D renders D3D9-11 through radeonsi OpenGL when DXVK is not possible (PROTON_USE_WINED3D=1)"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn caps(api: (u32, u32, u32), exts: &[&str], feats: &[&str], props: &[(&str, f64)]) -> Caps {
        Caps {
            api,
            extensions: exts.iter().map(|s| s.to_string()).collect(),
            features: feats.iter().map(|s| (s.to_string(), true)).collect::<BTreeMap<_, _>>(),
            props: props.iter().map(|(k, v)| (k.to_string(), *v)).collect(),
            device_name: "AMD Radeon (LIVERPOOL)".into(),
            ..Default::default()
        }
    }

    #[test]
    fn picks_newest_supported_not_newest() {
        let base_ext = ["VK_EXT_robustness2", "VK_EXT_transform_feedback", "VK_EXT_depth_clip_enable"];
        let base_f = ["robustBufferAccess2", "nullDescriptor", "geometryShader", "dynamicRendering", "extendedDynamicState"];
        // GFX7-like: Vulkan 1.3, no maintenance5/6 -> DXVK 2.x
        let c = caps((1, 3, 296), &base_ext, &base_f, &[("maxPushConstantsSize", 128.0)]);
        let s = select(&c);
        assert_eq!(s.at("dxvk.line").unwrap().as_str(), Some("2.x"));
        assert!(!dxvk3(&c).ok);
        assert!(dxvk3(&c).missing.iter().any(|m| m.contains("maintenance5")));
        // 1.4 driver: promoted extensions count, 3.x if features present
        let mut e3 = base_ext.to_vec();
        e3.push("VK_KHR_push_descriptor");
        let mut f3 = base_f.to_vec();
        f3.extend(["descriptorIndexing", "shaderInt8", "shaderInt16", "shaderInt64", "scalarBlockLayout"]);
        let c4 = caps((1, 4, 300), &e3, &f3, &[("maxPushConstantsSize", 256.0)]);
        assert_eq!(select(&c4).at("dxvk.version").unwrap().as_str(), Some(DXVK3));
        // Vulkan 1.1 only -> legacy DXVK
        let c11 = caps((1, 1, 0), &[], &[], &[]);
        assert_eq!(select(&c11).at("dxvk.version").unwrap().as_str(), Some(DXVK1));
        // software renderer -> nothing hardware
        let mut sw = c4.clone();
        sw.software = true;
        assert_eq!(select(&sw).at("dxvk.line").unwrap().as_str(), Some("none"));
        assert_eq!(select(&sw).get("d3d9_d3d11").unwrap().as_str(), Some("wined3d (OpenGL)"));
    }

    #[test]
    fn vkd3d_requirements() {
        let mut c = caps((1, 3, 0), &["VK_EXT_robustness2", "VK_KHR_push_descriptor"], &[], &[]);
        assert!(!vkd3d(&c).ok);
        for f in ["samplerMirrorClampToEdge", "shaderDrawParameters", "runtimeDescriptorArray", "descriptorBindingPartiallyBound",
                  "descriptorBindingVariableDescriptorCount", "descriptorBindingSampledImageUpdateAfterBind",
                  "descriptorBindingStorageImageUpdateAfterBind", "descriptorBindingStorageBufferUpdateAfterBind",
                  "shaderSampledImageArrayNonUniformIndexing", "shaderStorageImageArrayNonUniformIndexing"] {
            c.features.insert(f.into(), true);
        }
        for p in ["maxDescriptorSetUpdateAfterBindSampledImages", "maxDescriptorSetUpdateAfterBindStorageImages",
                  "maxDescriptorSetUpdateAfterBindStorageBuffers"] {
            c.props.insert(p.into(), 1048576.0);
        }
        assert!(vkd3d(&c).ok, "{:?}", vkd3d(&c).missing);
    }
}
