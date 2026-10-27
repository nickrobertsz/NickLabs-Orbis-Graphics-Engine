//! orbis-run: launch a game with its Orbis profile.
//!
//!   Steam:   launch options  `orbis-run %command%`
//!   Lutris:  command prefix  `orbis-run --profile ~/.config/orbis/games.d/x.json --`
//!   direct:  `orbis-run --name "Witcher 3" -- ~/Games/witcher3/bin/x64/witcher3.exe`
//!
//! What it does: picks the profile (file, name, or Steam AppId), resolves the runner and the
//! D3D translation layer from the measured Vulkan capabilities (never "newest is best"), sets the
//! environment (DXVK/VKD3D/WineD3D, shader caches, sync primitives, audio, controller mapping,
//! logging), runs the game in its own systemd scope inside orbis-game.slice so orbis-perf can
//! protect its GPU memory, records a marker the performance controller recognises, keeps a log,
//! and on exit classifies what went wrong (driver / Vulkan requirements / Wine / Proton runtime /
//! launcher / DRM / anti-cheat / network / game) instead of failing silently.

use crate::caps::Caps;
use crate::{catalog, classify, rules};
use orbis_core::fsx::{self, p};
use orbis_core::json::{self, Value};
use orbis_core::{jobj, sys};
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub fn slug(s: &str) -> String {
    let mut out: String = s.chars().map(|c| if c.is_ascii_alphanumeric() { c.to_ascii_lowercase() } else { '-' }).collect();
    while out.contains("--") {
        out = out.replace("--", "-");
    }
    out.trim_matches('-').chars().take(48).collect::<String>().if_empty("game")
}

trait IfEmpty {
    fn if_empty(self, d: &str) -> String;
}
impl IfEmpty for String {
    fn if_empty(self, d: &str) -> String {
        if self.is_empty() { d.to_string() } else { self }
    }
}

pub fn profile_dirs() -> Vec<PathBuf> {
    let mut v = vec![p("/usr/share/orbis/games.d"), p("/etc/orbis/games.d")];
    v.push(catalog::home().join(".config/orbis/games.d"));
    v
}

pub struct Profile {
    pub doc: Value,
    pub file: Option<String>,
}

impl Profile {
    pub fn s(&self, k: &str) -> Option<String> {
        self.doc.at(k).and_then(Value::as_str).map(str::to_string)
    }
}

pub fn find_profile(file: Option<&str>, name: Option<&str>, appid: Option<&str>) -> Profile {
    if let Some(f) = file {
        let doc = fsx::read_raw(Path::new(f)).and_then(|t| json::parse(&t).ok()).unwrap_or_else(Value::obj);
        return Profile { doc, file: Some(f.to_string()) };
    }
    let mut found: Option<Profile> = None;
    for d in profile_dirs() {
        for f in fsx::list_dir(&d) {
            let Some(doc) = fsx::read_raw(&f).and_then(|t| json::parse(&t).ok()) else { continue };
            let ids: Vec<String> = match doc.at("match.steam_appid") {
                Some(Value::Arr(a)) => a.iter().filter_map(|x| x.as_str().map(str::to_string).or_else(|| x.as_i64().map(|i| i.to_string()))).collect(),
                Some(Value::Str(s)) => vec![s.clone()],
                Some(Value::Int(i)) => vec![i.to_string()],
                _ => vec![],
            };
            let nm = doc.get("name").and_then(Value::as_str).map(str::to_lowercase);
            let stem = f.file_stem().map(|s| s.to_string_lossy().to_lowercase());
            let hit = appid.map(|a| ids.iter().any(|x| x == a)).unwrap_or(false)
                || name.map(|n| Some(n.to_lowercase()) == nm || Some(slug(n)) == stem).unwrap_or(false);
            if hit {
                found = Some(Profile { doc, file: Some(f.display().to_string()) }); // later dirs win
            }
        }
    }
    found.unwrap_or(Profile { doc: Value::obj(), file: None })
}

pub fn load_caps() -> Option<(Caps, Value)> {
    let t = fsx::read_raw(&p("/var/lib/orbis/games/vulkan-caps.json"))?;
    let v = json::parse(&t).ok()?;
    let raw = v.get("raw").and_then(Value::as_str).map(str::to_string)?;
    let c = crate::caps::from_json(&raw).ok().unwrap_or_else(|| crate::caps::from_text(&raw));
    Some((c, v))
}

#[derive(Debug, Clone, PartialEq)]
pub enum Kind {
    SteamProton(PathBuf),
    SteamNative,
    Exe,
    Native,
}

pub fn kind_of(cmd: &[String]) -> Kind {
    if let Some(i) = cmd.iter().position(|a| a.ends_with("/proton") || a == "proton") {
        if cmd.iter().any(|a| a == "waitforexitandrun" || a == "run") {
            return Kind::SteamProton(Path::new(&cmd[i]).parent().map(Path::to_path_buf).unwrap_or_default());
        }
    }
    if cmd.iter().any(|a| a.contains("SteamLaunch") || a.ends_with("/reaper")) {
        return Kind::SteamNative;
    }
    if cmd.first().map(|a| a.to_lowercase().ends_with(".exe")).unwrap_or(false) {
        return Kind::Exe;
    }
    Kind::Native
}

/// DXVK line bundled with a Proton directory, from the runner catalog (by directory name).
pub fn proton_dxvk_line(dir: &Path, rs: &[catalog::Runner]) -> Option<String> {
    let name = dir.file_name()?.to_string_lossy().into_owned();
    rs.iter().find(|r| r.kind == "proton" && (r.id == name || r.install.ends_with(&name))).and_then(|r| r.dxvk_line.clone())
}

fn rank(l: &str) -> i32 {
    match l {
        "3.x" => 3,
        "2.x" => 2,
        "1.10" => 1,
        _ => 0,
    }
}

pub struct Plan {
    pub argv: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub notes: Vec<String>,
    pub runner: String,
    pub d3d: String,
    pub title: String,
    pub logdir: PathBuf,
}

pub fn plan(cmd: &[String], prof: &Profile, title_opt: Option<&str>, runner_opt: Option<&str>, caps: Option<&Caps>) -> Result<Plan, String> {
    if cmd.is_empty() {
        return Err("no command given (Steam: set launch options to `orbis-run %command%`)".into());
    }
    let rs = catalog::load(&catalog::catalog_path()).unwrap_or_default();
    let title = title_opt.map(str::to_string).or_else(|| prof.s("name")).unwrap_or_else(|| {
        Path::new(cmd.last().unwrap()).file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or("game".into())
    });
    let sl = slug(&title);
    let home = catalog::home();
    let state = home.join(".local/state/orbis/games").join(&sl);
    let cache = home.join(".cache/orbis/shaders").join(&sl);
    let mut env: BTreeMap<String, String> = BTreeMap::new();
    let mut notes = vec![];
    let sel = caps.map(rules::select);
    let supported = sel.as_ref().and_then(|s| s.at("dxvk.line").and_then(Value::as_str).map(str::to_string)).unwrap_or_else(|| "unknown".into());
    let renderer = prof.s("renderer").unwrap_or_else(|| "auto".into());
    let want_dxvk = prof.s("dxvk").unwrap_or_else(|| "auto".into());
    let d3d: String;
    let mut argv = cmd.to_vec();
    let runner: String;

    // shader caches, logging, audio, controller
    env.insert("DXVK_STATE_CACHE_PATH".into(), cache.join("dxvk").display().to_string());
    env.insert("VKD3D_SHADER_CACHE_PATH".into(), cache.join("vkd3d").display().to_string());
    env.insert("MESA_SHADER_CACHE_DIR".into(), cache.join("mesa").display().to_string());
    let max_mb = prof.doc.at("shader_cache.max_mb").and_then(Value::as_i64).unwrap_or(2048);
    env.insert("MESA_SHADER_CACHE_MAX_SIZE".into(), format!("{max_mb}M"));
    env.insert("DXVK_LOG_LEVEL".into(), prof.s("log.dxvk").unwrap_or_else(|| "warn".into()));
    env.insert("DXVK_LOG_PATH".into(), state.display().to_string());
    if prof.doc.at("log.proton").and_then(Value::as_bool).unwrap_or(false) {
        env.insert("PROTON_LOG".into(), "1".into());
        env.insert("PROTON_LOG_DIR".into(), state.display().to_string());
    }
    if let Some(ms) = prof.doc.at("audio.pulse_latency_msec").and_then(Value::as_i64) {
        env.insert("PULSE_LATENCY_MSEC".into(), ms.to_string());
    }
    if let Some(m) = prof.s("controller.sdl_mapping") {
        env.insert("SDL_GAMECONTROLLERCONFIG".into(), m);
    }
    // Windows synchronisation: NTSYNC (kernel driver, Proton 10+/Wine 10.15+ use it by themselves
    // when /dev/ntsync is accessible), else fsync (futex_waitv, Proton default), else esync.
    let ntsync = Path::new("/dev/ntsync").exists();
    notes.push(if ntsync { "sync: /dev/ntsync present (NTSYNC)".into() } else { "sync: no /dev/ntsync; Proton uses fsync (futex_waitv)".to_string() });
    if let Ok((old, new)) = sys::raise_nofile() {
        if new > old {
            notes.push(format!("open-file limit {old} -> {new} (esync fallback)"));
        }
    }

    match kind_of(cmd) {
        Kind::SteamProton(dir) => {
            runner = format!("steam proton ({})", dir.file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default());
            let bundled = proton_dxvk_line(&dir, &rs);
            let use_wined3d = renderer == "opengl" || want_dxvk == "wined3d" || supported == "none"
                || bundled.as_deref().map(|b| supported != "unknown" && rank(b) > rank(&supported)).unwrap_or(false);
            if use_wined3d {
                env.insert("PROTON_USE_WINED3D".into(), "1".into());
                d3d = "wined3d (OpenGL)".into();
                if let (Some(b), true) = (&bundled, renderer != "opengl") {
                    let better = catalog::pick_proton(&rs, &supported).map(|r| r.id.clone());
                    notes.push(format!("this Proton bundles DXVK {b} but the driver supports DXVK {supported}: using WineD3D; \
                                        pick {} as the compatibility tool for DXVK", better.unwrap_or_else(|| "an older Proton".into())));
                }
            } else {
                d3d = format!("dxvk (bundled{})", bundled.map(|b| format!(" {b}")).unwrap_or_default());
            }
            if prof.s("vkd3d").as_deref() == Some("off") || sel.as_ref().and_then(|s| s.at("vkd3d.ok").and_then(Value::as_bool)) == Some(false) {
                notes.push("VKD3D-Proton requirements not met on this driver: Direct3D 12 games will not run".into());
            }
        }
        Kind::SteamNative => {
            runner = "steam (native Linux game)".into();
            d3d = "native (OpenGL/Vulkan)".into();
        }
        Kind::Exe => {
            let want = runner_opt.map(str::to_string).or_else(|| prof.s("runner")).unwrap_or_else(|| "auto".into());
            let prefix = home.join(".local/share/orbis/prefixes").join(&sl);
            let proton = if want == "wine" {
                None
            } else if let Some(id) = want.strip_prefix("proton:") {
                rs.iter().find(|r| r.id == id).cloned()
            } else {
                catalog::pick_proton(&rs, &supported).filter(|r| catalog::installed(r)).cloned()
            };
            if let Some(pr) = proton.filter(|r| catalog::installed(r)) {
                runner = pr.id.clone();
                let pdir = catalog::expand(&pr.install);
                env.insert("STEAM_COMPAT_DATA_PATH".into(), prefix.display().to_string());
                env.insert("STEAM_COMPAT_CLIENT_INSTALL_PATH".into(), home.join(".steam/root").display().to_string());
                let mut a = vec![pdir.join("proton").display().to_string(), "run".into()];
                a.extend(cmd.iter().cloned());
                argv = a;
                if renderer == "opengl" || supported == "none" || want_dxvk == "wined3d" {
                    env.insert("PROTON_USE_WINED3D".into(), "1".into());
                    d3d = "wined3d (OpenGL)".into();
                } else {
                    d3d = format!("dxvk (bundled {})", pr.dxvk_line.unwrap_or_default());
                }
            } else {
                runner = "wine".into();
                env.insert("WINEPREFIX".into(), prefix.display().to_string());
                let mut a = vec!["wine".to_string()];
                a.extend(cmd.iter().cloned());
                argv = a;
                let line = if want_dxvk == "auto" { supported.clone() } else { want_dxvk.clone() };
                let dxvk = rs.iter().find(|r| r.kind == "dxvk" && (r.version == line || r.dxvk_line.as_deref() == Some(line.as_str())));
                match dxvk.filter(|r| catalog::installed(r) && renderer != "opengl") {
                    Some(dx) => {
                        env.insert("WINEDLLOVERRIDES".into(), "d3d9,d3d10core,d3d11,dxgi=n,b".into());
                        env.insert("ORBIS_DXVK_DIR".into(), catalog::expand(&dx.install).display().to_string());
                        d3d = format!("dxvk {}", dx.version);
                    }
                    None => {
                        d3d = "wined3d (OpenGL)".into();
                        if renderer != "opengl" {
                            notes.push(format!("no installed DXVK for line {line}: using WineD3D (orbis-games install)"));
                        }
                    }
                }
            }
        }
        Kind::Native => {
            runner = "native".into();
            d3d = "native".into();
        }
    }
    if let Some(e) = prof.doc.get("env").and_then(Value::as_obj) {
        for (k, v) in e {
            if let Some(s) = v.as_str() {
                env.insert(k.clone(), s.to_string());
            }
        }
    }
    Ok(Plan { argv, env, notes, runner, d3d, title, logdir: state })
}

/// Copy DXVK DLLs into a Wine prefix (what DXVK's setup script does; overrides come from env).
pub fn install_dxvk_into_prefix(dxvk_dir: &Path, prefix: &Path) -> Result<usize, String> {
    let mut n = 0;
    for (sub, dst) in [("x64", "drive_c/windows/system32"), ("x32", "drive_c/windows/syswow64")] {
        let to = prefix.join(dst);
        if !to.is_dir() {
            continue;
        }
        for f in fsx::list_dir(&dxvk_dir.join(sub)) {
            if f.extension().map(|e| e == "dll").unwrap_or(false) {
                std::fs::copy(&f, to.join(f.file_name().unwrap())).map_err(|e| e.to_string())?;
                n += 1;
            }
        }
    }
    Ok(n)
}

pub fn main_run(argv: Vec<String>) -> i32 {
    // our options come before "--" (or before the first non-option word)
    let (ours, cmd): (Vec<String>, Vec<String>) = match argv.iter().position(|a| a == "--") {
        Some(i) => (argv[..i].to_vec(), argv[i + 1..].to_vec()),
        None => {
            let i = argv.iter().position(|a| !a.starts_with("--")).unwrap_or(argv.len());
            // options taking a value consume the next word
            let mut j = 0;
            let mut cut = i;
            while j < argv.len() {
                if ["--profile", "--name", "--title", "--appid", "--runner"].contains(&argv[j].as_str()) {
                    j += 2;
                    continue;
                }
                if argv[j].starts_with("--") {
                    j += 1;
                    continue;
                }
                cut = j;
                break;
            }
            if j >= argv.len() {
                cut = argv.len();
            }
            (argv[..cut].to_vec(), argv[cut..].to_vec())
        }
    };
    let a = orbis_core::Args::parse(&ours, &["dry-run", "no-scope", "json"]);
    let appid = a.get("appid").map(str::to_string).or_else(|| std::env::var("SteamAppId").ok()).or_else(|| std::env::var("STEAM_COMPAT_APP_ID").ok());
    let prof = find_profile(a.get("profile"), a.get("name"), appid.as_deref());
    let caps = load_caps().map(|(c, _)| c);
    let pl = match plan(&cmd, &prof, a.get("title"), a.get("runner"), caps.as_ref()) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("orbis-run: {e}");
            return 2;
        }
    };
    let summary = jobj! {"title" => pl.title.clone(), "profile" => prof.file.clone(), "steam_appid" => appid.clone(),
        "runner" => pl.runner.clone(), "d3d" => pl.d3d.clone(), "argv" => pl.argv.clone(),
        "env" => Value::Obj(pl.env.iter().map(|(k, v)| (k.clone(), Value::Str(v.clone()))).collect()),
        "notes" => pl.notes.clone(), "vulkan_caps" => caps.as_ref().map(|c| c.api_str())};
    if a.flag("dry-run") {
        print!("{}", summary.to_string_pretty());
        return 0;
    }
    let _ = std::fs::create_dir_all(&pl.logdir);
    for k in ["DXVK_STATE_CACHE_PATH", "VKD3D_SHADER_CACHE_PATH", "MESA_SHADER_CACHE_DIR"] {
        if let Some(d) = pl.env.get(k) {
            let _ = std::fs::create_dir_all(d);
        }
    }
    if let (Some(dx), Some(pre)) = (pl.env.get("ORBIS_DXVK_DIR"), pl.env.get("WINEPREFIX")) {
        let pre = PathBuf::from(pre);
        if !pre.join("drive_c").exists() {
            eprintln!("orbis-run: creating the Wine prefix {}", pre.display());
            let _ = Command::new("wineboot").arg("-i").env("WINEPREFIX", &pre).status();
        }
        match install_dxvk_into_prefix(Path::new(dx), &pre) {
            Ok(n) => eprintln!("orbis-run: DXVK DLLs in prefix: {n}"),
            Err(e) => eprintln!("orbis-run: DXVK install failed ({e}); WineD3D will be used"),
        }
    }
    // marker for orbis-perf (it recognises this PID and its descendants as the game)
    let me = std::process::id();
    let marker = p(&format!("/run/orbis/games/{me}.json"));
    let mut mk = summary.clone();
    mk.set("profile", prof.file.clone().unwrap_or_default()).set("started", sys::now_unix().0);
    let _ = fsx::atomic_write(&marker, mk.to_string_compact().as_bytes());
    for n in &pl.notes {
        eprintln!("orbis-run: {n}");
    }
    eprintln!("orbis-run: {} via {} ({})", pl.title, pl.runner, pl.d3d);

    let mut full = pl.argv.clone();
    // a user manager must be reachable, otherwise systemd-run --user fails and the game never starts
    let user_bus = std::env::var_os("XDG_RUNTIME_DIR").map(|d| std::path::Path::new(&d).join("bus").exists()).unwrap_or(false);
    let scope = !a.flag("no-scope") && user_bus
        && Command::new("systemd-run").arg("--version").stdout(Stdio::null()).status().map(|s| s.success()).unwrap_or(false);
    if scope {
        let mut s = vec!["systemd-run".to_string(), "--user".into(), "--scope".into(), "--quiet".into(),
                         "--slice=orbis-game.slice".into(), format!("--unit=orbis-game-{me}")];
        s.extend(full);
        full = s;
    }
    let logf = pl.logdir.join(format!("{}.log", sys::iso_utc(sys::now_unix().0).replace(':', "")));
    let mut child = match Command::new(&full[0]).args(&full[1..]).envs(&pl.env).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn() {
        Ok(c) => c,
        Err(e) => {
            let _ = std::fs::remove_file(&marker);
            eprintln!("orbis-run: cannot start {}: {e}", full[0]);
            return 127;
        }
    };
    let log = std::sync::Arc::new(std::sync::Mutex::new(std::fs::File::create(&logf).ok()));
    let mut hs = vec![];
    let out = child.stdout.take().unwrap();
    let err = child.stderr.take().unwrap();
    for (stream, is_err) in [(Box::new(out) as Box<dyn std::io::Read + Send>, false), (Box::new(err), true)] {
        let lg = log.clone();
        hs.push(std::thread::spawn(move || {
            for line in BufReader::new(stream).lines().map_while(Result::ok) {
                if is_err {
                    eprintln!("{line}");
                } else {
                    println!("{line}");
                }
                if let Ok(mut g) = lg.lock() {
                    if let Some(f) = g.as_mut() {
                        let _ = writeln!(f, "{line}");
                    }
                }
            }
        }));
    }
    let status = child.wait();
    for h in hs {
        let _ = h.join();
    }
    let _ = std::fs::remove_file(&marker);
    use std::os::unix::process::ExitStatusExt;
    let (code, sig) = match &status {
        Ok(s) => (s.code(), s.signal()),
        Err(_) => (None, None),
    };
    // DXVK/Proton write their own logs into the same directory
    let mut text = fsx::read_raw(&logf).unwrap_or_default();
    for f in fsx::list_dir(&pl.logdir) {
        let n = f.file_name().unwrap().to_string_lossy().into_owned();
        if (n.ends_with("_d3d11.log") || n.ends_with("_dxgi.log") || n.starts_with("steam-")) && f.is_file() {
            text.push_str(&fsx::read_raw(&f).unwrap_or_default());
        }
    }
    let mut rep = classify::classify(code, sig, &text);
    rep.set("plan", summary).set("log", logf.display().to_string());
    let _ = fsx::atomic_write(&pl.logdir.join("last.json"), rep.to_string_pretty().as_bytes());
    let cat = rep.get("category").and_then(Value::as_str).unwrap_or("").to_string();
    if cat != "ok" {
        eprintln!("orbis-run: {} -> {} ({}); details: {}", pl.title, rep.get("result").and_then(Value::as_str).unwrap_or(""), cat,
                  pl.logdir.join("last.json").display());
        if let Some(f) = rep.get("findings").and_then(Value::as_arr).and_then(|a| a.first()) {
            eprintln!("orbis-run: hint: {}", f.get("hint").and_then(Value::as_str).unwrap_or(""));
        }
    }
    code.unwrap_or(128 + sig.unwrap_or(0))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kinds_and_slug() {
        let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<_>>();
        assert!(matches!(kind_of(&s(&["/home/n/.steam/root/compatibilitytools.d/GE-Proton10-34/proton", "waitforexitandrun", "/g/x.exe"])), Kind::SteamProton(_)));
        assert_eq!(kind_of(&s(&["game.EXE"])), Kind::Exe);
        assert_eq!(kind_of(&s(&["/usr/bin/supertuxkart"])), Kind::Native);
        assert_eq!(slug("The Witcher 3: Wild Hunt"), "the-witcher-3-wild-hunt");
        assert_eq!(slug("::"), "game");
    }

    #[test]
    fn plan_steam_proton_falls_back_to_wined3d_when_dxvk_line_too_new() {
        let d = std::env::temp_dir().join(format!("orbis-run-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let cat = d.join("runners.json");
        std::fs::write(&cat, r#"{"runners":[
            {"id":"GE-Proton11-11","kind":"proton","version":"11-11","dxvk_line":"3.x","file":"GE-Proton11-11.tar.gz","install":"$STEAM_COMPAT/GE-Proton11-11"},
            {"id":"GE-Proton10-34","kind":"proton","version":"10-34","dxvk_line":"2.x","file":"GE-Proton10-34.tar.gz","install":"$STEAM_COMPAT/GE-Proton10-34"}]}"#).unwrap();
        std::env::set_var("ORBIS_GAMES_CATALOG", &cat);
        let caps = crate::caps::Caps { api: (1, 3, 0), ..Default::default() }; // supports only DXVK 1.10 here (no 1.3 extensions)
        let prof = Profile { doc: Value::obj(), file: None };
        let cmd: Vec<String> = ["/c/GE-Proton11-11/proton", "waitforexitandrun", "/g/eldenring.exe"].iter().map(|s| s.to_string()).collect();
        let pl = plan(&cmd, &prof, Some("ER"), None, Some(&caps)).unwrap();
        assert_eq!(pl.env.get("PROTON_USE_WINED3D").map(String::as_str), Some("1"));
        assert!(pl.notes.iter().any(|n| n.contains("bundles DXVK 3.x")));
        let cmd2: Vec<String> = ["/c/GE-Proton10-34/proton", "waitforexitandrun", "/g/x.exe"].iter().map(|s| s.to_string()).collect();
        let pl2 = plan(&cmd2, &prof, Some("X"), None, Some(&caps)).unwrap();
        assert!(pl2.env.get("PROTON_USE_WINED3D").is_some()); // 1.10 only: 2.x is still too new
        assert!(pl.env.contains_key("DXVK_STATE_CACHE_PATH") && pl.env.contains_key("MESA_SHADER_CACHE_MAX_SIZE"));
        std::env::remove_var("ORBIS_GAMES_CATALOG");
        std::fs::remove_dir_all(d).unwrap();
    }
}
