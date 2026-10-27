# NickLabs Orbis — gaming guide (Windows games through Steam/Proton, Wine, Lutris)

No game has been run on a PS4 with this stack yet, so nothing on this page is PS4-VERIFIED.
What follows describes how the stack decides and what it reports.

## 1. What is installed

The `orbis-gaming` package (in the Graphics Engine pack) adds Lutris, Gamescope, MangoHud
(64- and 32-bit), the sched_ext schedulers (`scx-scheds`), `vulkan-tools`, and the 32-bit
GnuTLS / PulseAudio / ALSA libraries Wine needs. These sit on top of Orbis 0.2.0's GAMING
personality, which already ships steam, wine, gamemode and kodi.

The `orbis-native` package adds:
* `orbis-games`: Vulkan capability check, runner catalog, offline bundle, profiles, last diagnosis;
* `orbis-run`: the launcher;
* the game detection in `orbis-perf`.

Proton, GE-Proton, DXVK and VKD3D-Proton are **not** taken from distribution packages. They come
from the pinned catalog `/usr/share/orbis/games/runners.json`:

| id | kind | bundles / is | for drivers that pass |
|---|---|---|---|
| GE-Proton11-11 | Proton | DXVK 3.x line | the DXVK 3.x check |
| GE-Proton10-34 | Proton | DXVK 2.x line | Vulkan 1.3 + dynamic rendering (expected pick on RADV/GFX7) |
| GE-Proton7-55 | Proton | older base | games that regressed on newer Proton |
| dxvk-3.1.1 / 2.7.1 / 1.10.3 | DXVK | d3d9/10/11 → Vulkan for plain Wine | 3.x / 2.x / Vulkan 1.1 |
| vkd3d-proton-3.0.1 | VKD3D-Proton | d3d12 → Vulkan | its own check (GFX7 is expected to fail it, and that result is reported) |

Valve's own Proton 11.0 / 10.0 come through Steam as usual. Steam downloads them, so a signed-in
Steam is needed.

## 2. Automatic selection: measured, not "newest wins"

`orbis-games caps` runs `vulkaninfo` (JSON, with a text fallback) and checks each layer's real
requirements:

* **DXVK 3.x**:
  * Vulkan 1.3;
  * extensions VK_EXT_depth_clip_enable, VK_KHR_maintenance5/6, VK_KHR_load_store_op_none,
    VK_EXT_robustness2 and VK_EXT_transform_feedback;
  * 256-byte push constants;
  * descriptor indexing;
  * int8/16/64;
  * scalar block layout.
* **DXVK 2.x**: Vulkan 1.3, null descriptors (robustness2), dynamic rendering and extended dynamic
  state.
* **DXVK 1.10.3**: Vulkan 1.1.
* **VKD3D-Proton**:
  * Vulkan 1.3;
  * descriptor indexing with 1,000,000 update-after-bind descriptors;
  * samplerMirrorClampToEdge;
  * shaderDrawParameters;
  * robustness2 and push_descriptor.

The newest line whose check passes is used. If no line passes, games use **WineD3D (OpenGL)**.
A software Vulkan device (llvmpipe/lavapipe) is reported as software and never counted as a pass.
`sudo orbis-games caps --save` (run at boot by `orbis-games-caps.service`) stores the result, so
`orbis-run` (unprivileged) can use it.

For Steam games, `orbis-run` compares the Proton you chose with the driver's DXVK line. If that
Proton bundles a DXVK line the driver cannot run, it falls back to WineD3D
(`PROTON_USE_WINED3D=1`) and names the catalog Proton that would fit.

## 3. Launching

* Steam: Properties → Launch Options: `orbis-run %command%`
* Lutris: Runner options → command prefix: `orbis-run --profile ~/.config/orbis/games.d/<game>.json --`
* Direct: `orbis-run --name "My Game" --runner wine -- ~/Games/mygame/game.exe`
  (with `--runner wine`, the matching DXVK is copied into the prefix and `WINEDLLOVERRIDES` set)
* `--dry-run` prints the plan (runner, D3D layer, environment) without starting anything.

`orbis-run`:
* sets per-game shader caches (`~/.cache/orbis/games/<game>`) and DXVK logs;
* sets the PulseAudio latency and the SDL controller mapping when the profile has them;
* raises the open-file limit (esync);
* writes a marker so `orbis-perf` applies the game profile (scheduler, SCLK, nice, background
  limits, VRAM protection);
* runs the game in its own scope in `orbis-game.slice` when a user manager is reachable;
* keeps a log;
* classifies the outcome when the game exits.

Profiles: `orbis-games profile new "My Game" --appid 292030` writes `~/.config/orbis/games.d/my-game.json`:

```json
{"name": "My Game", "match": {"steam_appid": ["292030"]}, "runner": "auto", "dxvk": "auto",
 "renderer": "auto", "scheduler": "auto", "governor": "performance", "gpu_sclk": "max", "nice": -4,
 "memory": {"dmem_low_mb": null}, "shader_cache": {"max_mb": 2048},
 "audio": {"pulse_latency_msec": null}, "controller": {"sdl_mapping": null}, "env": {},
 "log": {"dxvk": "warn", "proton": false}}
```

Profiles are read from `/usr/share/orbis/games.d`, `/etc/orbis/games.d` and
`~/.config/orbis/games.d`; later files win.

## 4. Failure classification

`orbis-games last "<game>"` shows the last diagnosis. Categories are kept apart, so a Wine
problem is never reported as a GPU problem:

| Category | Typical evidence | Meaning |
|---|---|---|
| graphics-driver | VK_ERROR_DEVICE_LOST, ring gfx timeout | GPU/driver failed: `orbis-debug last-crash` |
| vulkan-requirements | DXVK: No adapters found, VK_ERROR_EXTENSION_NOT_PRESENT | the layer needs features this driver lacks |
| software-rendering | llvmpipe / lavapipe | Mesa fell back to the CPU |
| proton-runtime | pressure-vessel, bwrap | Steam Linux Runtime failed (user namespaces) |
| anti-cheat | EasyAntiCheat, BattlEye | only fixable by the game's developer |
| drm | Denuvo, SteamAPI_Init() failed | licence/Steam must be online |
| launcher | EA/Ubisoft/Epic launcher, .NET | the launcher failed, not the game |
| network | HTTP 404, Could not resolve host | a download/online service failed; the console is LAN-only |
| wine | err:module:import_dll, Unhandled page fault | a missing Windows API/DLL |
| illegal-instruction | SIGILL (exit 132) | the binary needs a CPU feature Jaguar lacks (e.g. AVX2) |

**HTTP 404 versus "Noob404".** Noob404 is the PS4 Linux developer whose kernel work Orbis builds
on. Lines that mention Noob404 are never read as HTTP 404 errors (tested).

## 5. Offline deployment (the console stays LAN-only)

On the Nobara:

```
./orbis-ge runners                    # or: --only GE-Proton10-34,dxvk-2.7.1
```

This downloads each runner from GitHub, trying any mirrors listed after it. GE-Proton files are
checked against the upstream `.sha512sum`. Every file's SHA-256 goes into
`gaming/runners.lock.json` on first download and is checked on every later run; a mismatch
deletes the file. Review the lock file, then commit it.

On the console:

```
scp -r out/runners <user>@<ps4-ip>:~/runners
ssh <user>@<ps4-ip> orbis-games install --bundle ~/runners    # checks SHA256SUMS, then extracts
orbis-games runners                                           # installed state + automatic pick
```

## 6. Steam and the LAN-only rule

Steam needs the internet to sign in, check licences and download. Nothing in Orbis opens internet
access. `orbis-games network` lists the options:
1. sign in once on a temporarily allowed connection, then use Steam offline mode;
2. LAN game transfer from the Nobara's Steam, which still needs a signed-in client;
3. explicit, logged internet access through the Nobara, decided by Nick.

Offline alternatives: Lutris/Wine with local installers (for example GOG offline installers),
plus Kodi and ES-DE.
