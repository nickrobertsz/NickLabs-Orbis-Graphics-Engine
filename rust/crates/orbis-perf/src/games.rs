//! Game detection (the falcond-equivalent part of orbis-perf).
//!
//! A process is a game when one of these holds, checked cheaply on new PIDs only:
//!   * orbis-run started it (marker /run/orbis/games/<pid>.json with the profile it used);
//!   * it is the tree under Steam's `reaper SteamLaunch AppId=<id> -- ...`;
//!   * its name matches a game profile (`match.exe`);
//!   * it is a Windows executable under Wine/Proton (argv[0] *.exe) that is not one of Wine's,
//!     Proton's or Steam's own helper processes (system list + /etc/orbis/perf/ignore.txt).
//! A session is keyed by Steam AppId, or by the first game PID; it ends when none of its
//! processes is alive.

use crate::policy::{find_profile, GameProfile};
use orbis_core::fsx::{self, p};
use orbis_core::procs::Proc;
use std::collections::{BTreeMap, BTreeSet};

pub const SYSTEM_EXE: &[&str] = &[
    "wineserver", "wine", "wine64", "wine-preloader", "wine64-preloader", "services.exe", "explorer.exe",
    "plugplay.exe", "winedevice.exe", "svchost.exe", "rpcss.exe", "conhost.exe", "start.exe", "steam.exe",
    "steamwebhelper.exe", "steamerrorreporter.exe", "steamerrorreporter64.exe", "crashhandler.exe",
    "crashhandler64.exe", "tabtip.exe", "rundll32.exe", "winecfg.exe", "wineboot.exe", "mscorsvw.exe",
    "iexplore.exe", "unitycrashhandler64.exe", "unitycrashhandler32.exe", "crashreportclient.exe",
    "cmd.exe", "regedit.exe", "regsvr32.exe", "msiexec.exe", "dxsetup.exe", "vc_redist.x64.exe",
];

/// Processes that compete with a running game and are demoted while one runs.
pub const BACKGROUND: &[&str] = &[
    "fossilize_replay", "pacman", "makepkg", "cc1", "cc1plus", "rustc", "ld", "ninja", "make", "cargo",
    "tracker-miner-f", "updatedb", "baloo_file", "xz",
];

#[derive(Clone, Debug)]
pub struct Session {
    pub key: String,
    pub appid: Option<String>,
    pub title: String,
    pub pids: BTreeSet<u32>,
    pub root_cgroup: String,
    pub profile: Option<GameProfile>,
    pub via: String,
    pub started: i64,
}

pub fn ignore_list() -> Vec<String> {
    let mut v: Vec<String> = SYSTEM_EXE.iter().map(|s| s.to_string()).collect();
    if let Some(t) = fsx::read_raw(&p("/etc/orbis/perf/ignore.txt")) {
        v.extend(t.lines().map(|l| l.trim().to_lowercase()).filter(|l| !l.is_empty() && !l.starts_with('#')));
    }
    v
}

fn steam_appid_of_reaper(pr: &Proc) -> Option<String> {
    if pr.comm != "reaper" || !pr.argv.iter().any(|a| a == "SteamLaunch") {
        return None;
    }
    pr.argv.iter().find_map(|a| a.strip_prefix("AppId=").map(str::to_string))
}

fn is_windows_game(pr: &Proc, ignore: &[String]) -> bool {
    let a0 = pr.arg0_base().to_lowercase();
    let comm = pr.comm.to_lowercase();
    let exe_like = a0.ends_with(".exe") || comm.ends_with(".exe");
    exe_like && !ignore.contains(&a0) && !ignore.contains(&comm)
}

pub fn orbis_run_marker(pid: u32) -> Option<orbis_core::Value> {
    fsx::read_raw(&p(&format!("/run/orbis/games/{pid}.json"))).and_then(|t| orbis_core::json::parse(&t).ok())
}

/// Classify a full process snapshot into game sessions.
pub fn detect(procs: &[Proc], profiles: &[GameProfile], now: i64) -> BTreeMap<String, Session> {
    let ignore = ignore_list();
    let by_pid: BTreeMap<u32, &Proc> = procs.iter().map(|p| (p.pid, p)).collect();
    let mut sessions: BTreeMap<String, Session> = BTreeMap::new();

    // the AppId of the nearest Steam reaper ancestor
    let appid_of = |pid: u32| -> Option<String> {
        let mut cur = pid;
        for _ in 0..32 {
            let pr = by_pid.get(&cur)?;
            if let Some(a) = steam_appid_of_reaper(pr) {
                return Some(a);
            }
            if pr.ppid <= 1 {
                return None;
            }
            cur = pr.ppid;
        }
        None
    };

    // PIDs with an orbis-run marker; their descendants belong to the same game
    let markers: BTreeSet<u32> = fsx::list_dir(&p("/run/orbis/games"))
        .iter()
        .filter_map(|f| f.file_stem().and_then(|s| s.to_str()).and_then(|s| s.parse().ok()))
        .filter(|pid| by_pid.contains_key(pid))
        .collect();
    let marker_root = |pid: u32| -> Option<u32> {
        let mut cur = pid;
        for _ in 0..32 {
            if markers.contains(&cur) {
                return Some(cur);
            }
            let pr = by_pid.get(&cur)?;
            if pr.ppid <= 1 {
                return None;
            }
            cur = pr.ppid;
        }
        None
    };

    for pr in procs {
        let mroot = marker_root(pr.pid);
        let marker = mroot.and_then(orbis_run_marker);
        let reaper_app = steam_appid_of_reaper(pr);
        let names = pr.names();
        let prof = find_profile(profiles, None, &names);
        let win = is_windows_game(pr, &ignore);
        let under_reaper = appid_of(pr.pid);
        let via = if marker.is_some() {
            "orbis-run"
        } else if reaper_app.is_some() {
            "steam-reaper"
        } else if prof.is_some() {
            "profile"
        } else if win {
            "wine-exe"
        } else if under_reaper.is_some() && !names.iter().any(|n| n == "steam" || n == "pressure-vessel-wrap") {
            "steam-child"
        } else {
            continue;
        };
        let appid = marker
            .as_ref()
            .and_then(|m| m.get("steam_appid").and_then(|v| v.as_str().map(str::to_string)))
            .or(reaper_app.clone())
            .or(under_reaper.clone())
            .or(pr.environ_steam_appid.clone());
        let key = appid
            .clone()
            .map(|a| format!("steam:{a}"))
            .or_else(|| mroot.map(|m| format!("orbis-run:{m}")))
            .unwrap_or_else(|| format!("pid:{}", pr.pid));
        // a Wine helper tree under the same reaper joins the app's session instead of opening one
        let profile = find_profile(profiles, appid.as_deref(), &names).cloned().or_else(|| {
            marker.as_ref().and_then(|m| m.get("profile").and_then(|v| v.as_str())).and_then(|src| {
                profiles.iter().find(|g| g.source == src).cloned()
            })
        });
        let title = profile
            .as_ref()
            .map(|g| g.name.clone())
            .filter(|n| !n.is_empty())
            .or_else(|| marker.as_ref().and_then(|m| m.get("title").and_then(|v| v.as_str().map(str::to_string))))
            .unwrap_or_else(|| names.first().cloned().unwrap_or_default());
        let e = sessions.entry(key.clone()).or_insert_with(|| Session {
            key: key.clone(),
            appid: appid.clone(),
            title: title.clone(),
            pids: BTreeSet::new(),
            root_cgroup: pr.cgroup.clone(),
            profile: profile.clone(),
            via: via.to_string(),
            started: now,
        });
        e.pids.insert(pr.pid);
        if e.profile.is_none() && profile.is_some() {
            e.profile = profile;
            e.title = title;
        }
        // prefer the most specific evidence for display
        if via == "orbis-run" || (via == "steam-reaper" && e.via != "orbis-run") {
            e.via = via.to_string();
        }
        if via == "wine-exe" || via == "profile" {
            e.root_cgroup = pr.cgroup.clone();
        }
    }
    // Steam's reaper alone (no game process yet) is a session too: the game is starting
    sessions
}

pub fn background_pids(procs: &[Proc], exclude: &BTreeSet<u32>) -> Vec<u32> {
    procs
        .iter()
        .filter(|p| !exclude.contains(&p.pid) && BACKGROUND.contains(&p.comm.as_str()))
        .map(|p| p.pid)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pr(pid: u32, ppid: u32, comm: &str, argv: &[&str]) -> Proc {
        Proc {
            pid,
            ppid,
            comm: comm.into(),
            argv: argv.iter().map(|s| s.to_string()).collect(),
            cgroup: format!("/user.slice/app-{pid}.scope"),
            ..Default::default()
        }
    }

    #[test]
    fn steam_and_wine_sessions() {
        let ps = vec![
            pr(10, 1, "steam", &["/usr/bin/steam"]),
            pr(20, 10, "reaper", &["/home/n/.steam/reaper", "SteamLaunch", "AppId=1245620", "--", "proton"]),
            pr(21, 20, "pv-adverb", &["pv-adverb"]),
            pr(22, 21, "eldenring.exe", &["Z:\\games\\eldenring.exe"]),
            pr(23, 21, "wineserver", &["wineserver"]),
            pr(30, 1, "wine64-preload", &["C:\\GOG\\witcher3.exe"]),
            pr(31, 1, "services.exe", &["C:\\windows\\system32\\services.exe"]),
            pr(40, 1, "bash", &["bash"]),
        ];
        let s = detect(&ps, &[], 100);
        let st = &s["steam:1245620"];
        assert!(st.pids.contains(&20) && st.pids.contains(&22) && st.pids.contains(&21));
        assert_eq!(st.via, "steam-reaper");
        let w = &s["pid:30"];
        assert_eq!(w.via, "wine-exe");
        assert!(!s.values().any(|x| x.pids.contains(&31) || x.pids.contains(&40) || x.pids.contains(&10)));
        assert_eq!(background_pids(&[pr(50, 1, "fossilize_replay", &[])], &BTreeSet::new()), vec![50]);
    }

    #[test]
    fn orbis_run_descendants() {
        let _g = crate::TEST_ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let root = std::env::temp_dir().join(format!("orbis-games-mk-{}", std::process::id()));
        std::fs::create_dir_all(root.join("run/orbis/games")).unwrap();
        std::fs::write(root.join("run/orbis/games/70.json"), r#"{"title":"Witcher 3","profile":"/etc/orbis/games.d/w3.json"}"#).unwrap();
        std::env::set_var("ORBIS_ROOT", &root);
        let ps = vec![pr(70, 1, "orbis-run", &["orbis-run"]), pr(71, 70, "wine64-preload", &["witcher3.exe"]), pr(72, 71, "bash", &["bash"])];
        let s = detect(&ps, &[], 1);
        std::env::remove_var("ORBIS_ROOT");
        let w = &s["orbis-run:70"];
        assert_eq!(w.title, "Witcher 3");
        assert!(w.pids.contains(&70) && w.pids.contains(&71) && w.pids.contains(&72));
        assert_eq!(s.len(), 1);
        std::fs::remove_dir_all(root).unwrap();
    }
}
