//! Turn a game's exit status and log into a meaningful diagnosis. Categories are kept apart so a
//! Wine or anti-cheat problem is never reported as a GPU problem and vice versa.

use orbis_core::{jobj, Value};

pub struct Rule {
    pub category: &'static str,
    pub needles: &'static [&'static str],
    pub hint: &'static str,
}

pub const RULES: &[Rule] = &[
    Rule { category: "graphics-driver", needles: &["VK_ERROR_DEVICE_LOST", "amdgpu: The CS has been cancelled", "GPU hang",
        "ring gfx timeout", "amdgpu_cs_query_fence_status failed", "radv/amdgpu: The CS has been rejected",
        "VK_ERROR_OUT_OF_DEVICE_MEMORY"],
        hint: "the GPU or its driver failed: check `orbis-debug last-crash` / `orbis-debug gpu`, try renderer=opengl or a lower dxvk" },
    Rule { category: "vulkan-requirements", needles: &["DXVK: No adapters found", "Required Vulkan extension", "vkCreateInstance failed",
        "VK_ERROR_INCOMPATIBLE_DRIVER", "VK_ERROR_INITIALIZATION_FAILED", "Failed to create Vulkan instance",
        "Required device feature not supported", "VK_ERROR_EXTENSION_NOT_PRESENT", "VK_ERROR_FEATURE_NOT_PRESENT"],
        hint: "the translation layer needs Vulkan features this driver lacks: `orbis-games caps` shows which DXVK/VKD3D line fits" },
    Rule { category: "software-rendering", needles: &["llvmpipe", "lavapipe", "softpipe"],
        hint: "Mesa fell back to CPU rendering: the GPU driver is not in use (orbis-gpu qualify)" },
    Rule { category: "proton-runtime", needles: &["pressure-vessel-wrap", "bwrap: setting up uid map", "bwrap: No permissions",
        "steam-runtime-launcher", "Proton: Traceback", "proton: ERROR", "Failed to initialize Steam Linux Runtime"],
        hint: "Proton/Steam Linux Runtime failed before the game: user namespaces (kernel.unprivileged_userns_clone), runtime files" },
    Rule { category: "anti-cheat", needles: &["EasyAntiCheat", "BattlEye", "BEService", "anti-cheat", "AntiCheat", "vgk.sys", "nProtect"],
        hint: "anti-cheat: works on Linux only when the developer enabled Proton support; nothing on the PS4 side can fix it" },
    Rule { category: "drm", needles: &["Denuvo", "SteamStub", "steam_api init failed", "SteamAPI_Init() failed",
        "Please launch the game through Steam", "Steam must be running", "license check failed"],
        hint: "copy protection / Steam licensing: Steam must be running and online at least once (the console is LAN-only)" },
    Rule { category: "launcher", needles: &["EpicGamesLauncher", "UbisoftConnect", "upc.exe", "EADesktop", "Battle.net", "Rockstar Launcher",
        "mscoree.dll", ".NET Framework", "launcher crashed"],
        hint: "a third-party launcher or .NET dependency failed, not the game itself: try the launcher's offline mode or a profile with protontricks" },
    Rule { category: "network", needles: &["HTTP 404", "404 Not Found", "error 404", "Failed to download", "Could not resolve host",
        "Network is unreachable", "Connection timed out", "curl: (6)", "curl: (7)", "Temporary failure in name resolution"],
        hint: "a download or online service failed; the console is LAN-only by policy (see the gaming network options)" },
    Rule { category: "wine", needles: &["err:module:import_dll", "wine: Unhandled page fault", "wine: could not load",
        "err:seh:", "Unhandled exception", "err:module:LdrInitializeThunk", "wine: failed to start"],
        hint: "a Windows API or DLL Wine does not provide: try another runner (GE-Proton), WineD3D, or a protontricks verb" },
];

/// Lines mentioning the PS4 Linux developer "Noob404" are not HTTP 404 errors.
fn is_noob404_reference(line: &str) -> bool {
    line.to_lowercase().contains("noob404")
}

pub fn classify(exit_code: Option<i32>, signal: Option<i32>, log: &str) -> Value {
    let mut hits: Vec<(&'static str, &'static str, String)> = vec![];
    for line in log.lines() {
        if is_noob404_reference(line) {
            continue;
        }
        for r in RULES {
            if r.needles.iter().any(|n| line.contains(n)) && !hits.iter().any(|(c, _, _)| *c == r.category) {
                hits.push((r.category, r.hint, line.trim().chars().take(240).collect()));
            }
        }
    }
    let ok = exit_code == Some(0) && signal.is_none();
    let primary = if let Some((c, _, _)) = hits.first() {
        c.to_string()
    } else if ok {
        "ok".into()
    } else if signal == Some(11) || exit_code == Some(139) {
        "game-crash (segmentation fault)".into()
    } else if signal == Some(4) || exit_code == Some(132) {
        "illegal-instruction (binary needs a CPU feature Jaguar lacks, e.g. AVX2)".into()
    } else if signal == Some(9) || exit_code == Some(137) {
        "killed (out of memory or by the user)".into()
    } else {
        "game (no known cause in the log)".into()
    };
    jobj! {
        "result" => if ok && hits.is_empty() { "PASS" } else if ok { "DEGRADED" } else { "FAIL" },
        "category" => primary,
        "exit_code" => exit_code, "signal" => signal,
        "findings" => Value::Arr(hits.into_iter().map(|(c, h, l)| jobj! {"category" => c, "hint" => h, "evidence" => l}).collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn categories() {
        let v = classify(Some(1), None, "info: DXVK: v3.1.1\nerr: DXVK: No adapters found\n");
        assert_eq!(v.get("category").unwrap().as_str(), Some("vulkan-requirements"));
        let v = classify(Some(1), None, "Thanks to noob404 for the kernel\nfetch: HTTP 404 on cdn\n");
        assert_eq!(v.get("category").unwrap().as_str(), Some("network"));
        let v = classify(Some(1), None, "built by Noob404, 404 Not Found? no: Noob404\n");
        assert_eq!(v.get("category").unwrap().as_str(), Some("game (no known cause in the log)"));
        let v = classify(None, Some(4), "");
        assert!(v.get("category").unwrap().as_str().unwrap().starts_with("illegal-instruction"));
        let v = classify(Some(0), None, "all good\n");
        assert_eq!(v.get("result").unwrap().as_str(), Some("PASS"));
        let v = classify(Some(1), None, "EasyAntiCheat: not installed\nwine: Unhandled page fault\n");
        assert_eq!(v.get("category").unwrap().as_str(), Some("anti-cheat"));
        assert_eq!(v.get("findings").unwrap().as_arr().unwrap().len(), 2);
    }
}
