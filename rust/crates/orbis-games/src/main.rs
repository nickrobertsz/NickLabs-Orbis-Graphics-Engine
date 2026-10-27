//! orbis-games: game-compatibility management for NickLabs Orbis.
//!
//!   orbis-games caps [--save] [--json]      probe Vulkan (vulkaninfo) and select DXVK/VKD3D/WineD3D
//!   orbis-games runners [--json]            runner catalog with installed state and the automatic pick
//!   orbis-games fetch --dest DIR [--only id,..] [--lock FILE]    (build host) download + verify an offline bundle
//!   orbis-games install --bundle DIR [--only id,..]              (console) install runners from a bundle
//!   orbis-games profile new <name> [--appid N] [--exe x.exe]     write ~/.config/orbis/games.d/<name>.json
//!   orbis-games last <name>                 the last orbis-run diagnosis for a game
//!   orbis-games network                     what the LAN-only policy means for Steam and downloads

use orbis_core::fsx::{self, p};
use orbis_core::json::{self, Value};
use orbis_core::{emit, jobj, Args};
use orbis_games::{caps, catalog, rules, run};
use std::path::PathBuf;

fn cmd_caps(save: bool) -> Result<Value, String> {
    let c = caps::probe()?;
    let sel = rules::select(&c);
    let doc = jobj! {
        "schema" => "orbis-vulkan-caps/1", "time" => orbis_core::sys::now_unix().0,
        "device" => jobj! {"name" => c.device_name.clone(), "driver" => c.driver_name.clone(), "api_version" => c.api_str(),
                           "software" => c.software, "extensions" => c.extensions.len(), "source" => c.source.clone()},
        "selection" => sel
    };
    if save {
        // keep the raw probe so orbis-run (unprivileged) can re-evaluate without running vulkaninfo
        let tmp = std::env::temp_dir().join(format!("orbis-vkcaps-save-{}.json", std::process::id()));
        let raw = std::process::Command::new("vulkaninfo").args(["--json=0", "-o"]).arg(&tmp).output().ok()
            .and_then(|_| std::fs::read_to_string(&tmp).ok()).unwrap_or_default();
        let _ = std::fs::remove_file(&tmp);
        let mut d = doc.clone();
        d.set("raw", raw);
        fsx::atomic_write(&p("/var/lib/orbis/games/vulkan-caps.json"), d.to_string_pretty().as_bytes()).map_err(|e| e.to_string())?;
    }
    Ok(doc)
}

fn cmd_runners() -> Result<Value, String> {
    let rs = catalog::load(&catalog::catalog_path())?;
    let line = run::load_caps().map(|(c, _)| rules::select(&c)).and_then(|s| s.at("dxvk.line").and_then(Value::as_str).map(str::to_string));
    let pick = line.as_deref().and_then(|l| catalog::pick_proton(&rs, l)).map(|r| r.id.clone());
    Ok(jobj! {
        "supported_dxvk_line" => line.clone(),
        "automatic_proton" => pick,
        "runners" => Value::Arr(rs.iter().map(|r| jobj! {"id" => r.id.clone(), "kind" => r.kind.clone(), "version" => r.version.clone(),
            "bundled_dxvk_line" => r.dxvk_line.clone(), "installed" => catalog::installed(r),
            "install" => catalog::expand(&r.install).display().to_string(), "note" => r.note.clone()}).collect())
    })
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let a = Args::parse(&argv, &["json", "save"]);
    let only: Vec<String> = a.get("only").map(|s| s.split(',').map(str::to_string).collect()).unwrap_or_default();
    let res: Result<Value, String> = match a.pos.first().map(String::as_str).unwrap_or("caps") {
        "caps" => cmd_caps(a.flag("save")),
        "runners" => cmd_runners(),
        "fetch" => (|| {
            let dest = PathBuf::from(a.get("dest").ok_or("--dest DIR required")?);
            let lock = a.get("lock").map(PathBuf::from).unwrap_or_else(|| dest.join("runners.lock.json"));
            let rs = catalog::load(&catalog::catalog_path())?;
            Ok(catalog::fetch(&rs, &dest, &lock, &only))
        })(),
        "install" => (|| {
            let b = PathBuf::from(a.get("bundle").ok_or("--bundle DIR required")?);
            let rs = catalog::load(&catalog::catalog_path())?;
            Ok(catalog::install_bundle(&rs, &b, &only))
        })(),
        "profile" => (|| {
            if a.pos.get(1).map(String::as_str) != Some("new") {
                return Err("usage: orbis-games profile new <name> [--appid N] [--exe x.exe]".to_string());
            }
            let name = a.pos.get(2).ok_or("name required")?;
            let mut m = Value::obj();
            if let Some(id) = a.get("appid") {
                m.set("steam_appid", vec![id.to_string()]);
            }
            if let Some(e) = a.get("exe") {
                m.set("exe", vec![e.to_lowercase()]);
            }
            let doc = jobj! {"name" => name.clone(), "match" => m, "runner" => "auto", "dxvk" => "auto", "renderer" => "auto",
                "scheduler" => "auto", "governor" => "performance", "gpu_sclk" => "max", "nice" => -4i64,
                "memory" => jobj! {"dmem_low_mb" => Value::Null}, "shader_cache" => jobj! {"max_mb" => 2048i64},
                "audio" => jobj! {"pulse_latency_msec" => Value::Null}, "controller" => jobj! {"sdl_mapping" => Value::Null},
                "env" => Value::obj(), "log" => jobj! {"dxvk" => "warn", "proton" => false}};
            let f = catalog::home().join(".config/orbis/games.d").join(format!("{}.json", run::slug(name)));
            if f.exists() {
                return Err(format!("{} exists", f.display()));
            }
            fsx::atomic_write(&f, doc.to_string_pretty().as_bytes()).map_err(|e| e.to_string())?;
            Ok(jobj! {"written" => f.display().to_string()})
        })(),
        "last" => (|| {
            let name = a.pos.get(1).ok_or("usage: orbis-games last <name>")?;
            let f = catalog::home().join(".local/state/orbis/games").join(run::slug(name)).join("last.json");
            fsx::read_raw(&f).and_then(|t| json::parse(&t).ok()).ok_or_else(|| format!("no report at {}", f.display()))
        })(),
        "network" => Ok(jobj! {
            "policy" => "The PS4 stays off the public internet (NickLabs rule). Nothing in Orbis opens it.",
            "steam" => "Steam needs the internet to sign in, verify licences and download. Options: (1) sign in once on a temporarily \
                        allowed connection, then use Steam offline mode; (2) install games from the Nobara's Steam over the LAN \
                        (Steam local network game transfer, still needs a signed-in client); (3) explicit, logged internet access \
                        through the Nobara, decided by Nick.",
            "offline" => "Lutris/Wine with local installers (e.g. GOG offline installers), runners from the offline bundle \
                          (orbis-games fetch on the Nobara, orbis-games install on the console), Kodi/ES-DE.",
            "diagnosis" => "orbis-run reports download/online failures as category 'network' (HTTP 404, DNS, unreachable), \
                            separately from driver or Wine problems; 'Noob404' (the PS4 Linux developer) is not an HTTP error."
        }),
        other => Err(format!("unknown command {other}")),
    };
    match res {
        Ok(v) => emit(&v, a.flag("json")),
        Err(e) => {
            eprintln!("orbis-games: {e}");
            std::process::exit(2);
        }
    }
}
