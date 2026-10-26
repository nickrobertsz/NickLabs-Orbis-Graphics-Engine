# Video decode on Liverpool (Graphics Engine 2.0)

## What the silicon has

Liverpool contains a UVD 4.2 decoder and a VCE 2.0 encoder from AMD's GCN 1.1 generation.
The UVD 4.2 hardware decodes H.264/AVC (up to level 4.1 at 1080p), MPEG-2, VC-1 and MPEG-4 part 2.
It has no HEVC, VP9 or AV1 support; those codecs are always decoded in software.

## What runs today

| Kernel | UVD ring | VA-API decode | Label `orbis-video` uses |
|---|---|---|---|
| Orbis 0.2.0 (`6.15.4-orbis-ps4`) | no — UVD/VCE IP blocks commented out for Liverpool in `cik.c` | no | software (CPU) |
| Graphics Engine default (`-orbis-ps4-ge`) | no (same `cik.c`) | no | software (CPU) |
| Research (`-orbis-ps4-ge-uvd`, `--research-uvd`) | UVD block enabled by feeRnt's experimental diff | possible, unproven | hardware **only after** `orbis-video test` PASS |

The GE release does **not** ship a hardware video path. That is the honest result of the research:

* **feeRnt `x_exp__6.15.4-uvd-engine2` @ 4345927** is the only 6.15 bring-up. Its own log reports
  VM faults during decode. Orbis keeps it as `kernel/patches/research/uvd-feernt-engine2-6.15.diff`,
  which applies on top of the GE series, and builds it only on request into a separately named
  kernel that is never packed.
* **linux-psx (meerzulee) @ 2138063** shows exact H.264 decode on one Baikal console (their
  experiments EXP013/014). It runs on a 7.2.11 runtime tree that is not published in git, so it
  cannot be ported line by line yet. Orbis uses its Mesa capability patch (0002, below) and its
  documented observations.
* **Firmware.** UVD needs the console's UVD microcode (`amdgpu/liverpool_uvd.bin` in the
  initramfs). The ps4-linux-loader extracts eight GFX/SDMA images but not the UVD/VCE ones
  (ledger ORB-004). The firmware is private to the console, and the pack never contains any.

## Mesa (pkgrel 201)

linux-psx 0002 (`ps4-uvd-avc-capabilities`) makes Mesa advertise video capabilities on
Liverpool/Gladius only when both of these hold:
* the kernel reports a UVD queue;
* the kernel's video-caps query succeeds with valid AVC limits.

When they hold, Mesa offers AVC decode in NV12 up to 1920×1088 and level 4.1. No other codec and
no encoding are advertised. On a kernel without UVD this build behaves exactly like pkgrel 101,
because it fails closed. The kernel already reports UVD codec caps for Liverpool in
`cik_query_video_codecs`. Orbis verified that 0002 applies with `patch -F0` after 0001 to Mesa
26.2.5 `ac_video.c`; it was not compiled here.

## The honest test (`orbis-video test`)

1. Encode a synthetic H.264 clip (testsrc2, 1280×720, 4 s) with libx264.
2. Decode it in software → per-frame MD5.
3. If the kernel has no UVD ring → **UNSUPPORTED** (and whether software decode works).
4. Otherwise decode through VA-API with frames kept in VA surfaces (`-hwaccel_output_format
   vaapi`, then `hwdownload`), so ffmpeg cannot silently fall back to software. Compare MD5 per
   frame. **PASS** only if every frame matches; otherwise **FAIL** with the mismatch count.

The result is saved in `/var/lib/orbis-graphics/video-test.json` and read by `orbis-video status`,
`orbis-debug video` and `orbis-video configure`.

## Players and frameworks

| Consumer | How it uses UVD | What Orbis does |
|---|---|---|
| mpv | `hwdec=vaapi-copy`, `hwdec-codecs=h264` | `orbis-video configure` writes the hardware config only after a PASS. Otherwise it writes the software config (8 decoder threads). Any foreign `mpv.conf` is backed up first. |
| FFmpeg | `-hwaccel vaapi` | used by the test itself |
| GStreamer | `va` plugin (`vah264dec`) from gst-plugins-bad | nothing to configure; check with `gst-inspect-1.0 va` |
| Kodi | VA-API in its video settings | enable *Allow hardware acceleration – VAAPI* only after a PASS |
| Firefox | `media.ffmpeg.vaapi.enabled` (VA-API through FFmpeg) | enable manually after a PASS. Firefox falls back to software on its own, so check `about:support` |
| Chromium | VA-API video decode flags | same; check `chrome://gpu` |

Session handling and recovery for a UVD kernel:
* UVD sessions are handles owned by the kernel. amdgpu frees them when a process closes its DRM
  file descriptor, and `orbis-debug collect` records the UVD ring and fence state.
* A ring timeout or VM fault on UVD is classified as `video` / `gpu_fault` by the Black Box, which
  also captures the GPU state. The debugfs `amdgpu_fence_info` shows whether the UVD fence still
  advances.
* Recovery is amdgpu's (`gpu_recovery`). It was not exercised on Liverpool UVD, and the research
  kernel's own log suggests it may not recover. That is why it is not the default.

## VCE (encode)

Not enabled in any Orbis kernel, and no community port exists. Investigating it would need the VCE
microcode from the console and a `vce_v2_0` bring-up for Liverpool. Out of scope for 2.0; encoding
is software (x264).

## Porting path to a real hardware decoder

1. Build the research kernel: `./orbis-ge kernel --orbis-src … --research-uvd`.
2. Put the console's `liverpool_uvd.bin` into a private initramfs. Do this only on your own
   images and never redistribute it.
3. Boot it on a test USB drive and run `sudo orbis-video test`, `sudo orbis-debug collect`.
4. If it faults, compare against the linux-psx observations (UVD.md in linux-psx) and bisect the
   research diff, starting with the fence and VM setup.
5. Only after a PASS on the CUH-1215A, move the UVD enablement into the GE series behind a
   parameter (default off), and keep `orbis-video test` as the gate.
