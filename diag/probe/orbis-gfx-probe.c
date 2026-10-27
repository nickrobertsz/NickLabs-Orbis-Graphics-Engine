/*
 * orbis-gfx-probe — NickLabs Orbis Graphics Pack 1.0.0
 * (derived from orbis-gpu-probe 0.1.0 of NickLabs PS4 Orbis Linux 0.2.0; that tool is unchanged)
 *
 * Answers, with measurements rather than names:
 *   --gl       Which OpenGL ES renderer actually runs, and does a draw land
 *              the expected pixels? (EGL surfaceless; no window, no modeset)
 *   --vk       Which Vulkan physical devices exist (type, driver, heaps,
 *              budgets, queue families)?
 *   --compute  Is Vulkan compute *correct* on the chosen device and queue?
 *              fill-buffer boundaries, copy round-trips, large dispatch
 *              geometry, exact integer SAXPY, shared-memory reduction with
 *              atomics, and repeated submissions with fence timeouts.
 *   --gl-bench GLSL arithmetic correctness (exact integer pattern), 1080p frame
 *              timing (median/p95 over N frames, glFinish per frame) and
 *              draw-call overhead (many small draws).
 *   --vk-raster Vulkan rasterisation: per-pixel gradient check and a depth-test
 *              scene (nearer surface must win; HTILE/depth path on GFX7).
 *   --pipeline-cache  compute pipeline creation time without/with a VkPipelineCache.
 *   --opencl   OpenCL (e.g. Mesa RustiCL with RUSTICL_ENABLE=radeonsi): platforms,
 *              devices and an integer vector kernel checked against the CPU.
 *   --membench Shared-GDDR5 characterisation (NickLabs Orbis unified-memory U6/U27):
 *              GPU write/read/read-write bandwidth per Vulkan memory type (VRAM
 *              carve-out, GTT write-combined, GTT cached/snooped), CPU access to the
 *              mapped types, CPU->GPU and GPU->CPU copies, and an end-to-end zero-copy
 *              check with VK_EXT_external_memory_host (the GPU writes and reads a plain
 *              malloc'ed buffer; the CPU checks it in place, no staging copy).
 *              --membench-mb N sets the buffer size (default 128).
 *
 * Every GPU wait is bounded (--timeout-ms). A timeout or VK_ERROR_DEVICE_LOST
 * stops the suite and is reported as such; the caller (orbisd / the
 * inference harness) runs this in a child process with its own deadline.
 *
 * No link-time dependency on libEGL/libGLESv2/libvulkan: they are dlopen()ed,
 * so the probe also reports *missing* libraries instead of failing to start.
 *
 * SPDX-License-Identifier: MIT
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>

#include "shaders_spv.h"
#if defined(__has_include)
#if __has_include("shaders_raster_spv.h")
#include "shaders_raster_spv.h"
#endif
#endif

#define PROBE_VERSION "1.0.0"
static bool opt_gl_bench, opt_vk_raster, opt_pcache, opt_opencl, opt_membench;
static unsigned opt_membench_mb = 128;
static int opt_frames = 120;

/* ------------------------------------------------------------------ JSON */
static FILE *J;
static int j_need_comma[64];
static int j_depth;

static void j_raw(const char *s) { fputs(s, J); }
static void j_comma(void) {
    if (j_need_comma[j_depth]) fputc(',', J);
    j_need_comma[j_depth] = 1;
}
static void j_str_val(const char *s) {
    fputc('"', J);
    for (const unsigned char *p = (const unsigned char *)(s ? s : ""); *p; p++) {
        if (*p == '"' || *p == '\\') { fputc('\\', J); fputc(*p, J); }
        else if (*p < 0x20) fprintf(J, "\\u%04x", *p);
        else fputc(*p, J);
    }
    fputc('"', J);
}
static void j_key(const char *k) { j_comma(); j_str_val(k); fputc(':', J); }
static void j_open(const char *k, char c) {
    if (k) j_key(k); else j_comma();
    fputc(c, J);
    j_need_comma[++j_depth] = 0;
}
static void j_close(char c) { fputc(c, J); j_depth--; }
static void j_kstr(const char *k, const char *v) { j_key(k); if (v) j_str_val(v); else j_raw("null"); }
static void j_kint(const char *k, long long v) { j_key(k); fprintf(J, "%lld", v); }
static void j_kuint(const char *k, unsigned long long v) { j_key(k); fprintf(J, "%llu", v); }
static void j_kdbl(const char *k, double v) { j_key(k); fprintf(J, "%.6f", v); }
static void j_kbool(const char *k, bool v) { j_key(k); j_raw(v ? "true" : "false"); }

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

/* ------------------------------------------------------------------ GL */
typedef void *EGLDisplay, *EGLConfig, *EGLContext, *EGLSurface;
typedef int32_t EGLint;
typedef unsigned int EGLBoolean, EGLenum;
#define EGL_NONE 0x3038
#define EGL_RENDERABLE_TYPE 0x3040
#define EGL_OPENGL_ES2_BIT 0x0004
#define EGL_OPENGL_ES_API 0x30A0
#define EGL_CONTEXT_CLIENT_VERSION 0x3098
#define EGL_VENDOR 0x3053
#define EGL_VERSION 0x3054
#define EGL_EXTENSIONS 0x3055
#define EGL_PLATFORM_SURFACELESS_MESA 0x31DD
#define EGL_SURFACE_TYPE 0x3033
#define GL_VENDOR 0x1F00
#define GL_RENDERER 0x1F01
#define GL_VERSION 0x1F02
#define GL_SHADING_LANGUAGE_VERSION 0x8B8C
#define GL_FRAMEBUFFER 0x8D40
#define GL_RENDERBUFFER 0x8D41
#define GL_RGBA8 0x8058
#define GL_COLOR_ATTACHMENT0 0x8CE0
#define GL_FRAMEBUFFER_COMPLETE 0x8CD5
#define GL_COLOR_BUFFER_BIT 0x4000
#define GL_RGBA 0x1908
#define GL_UNSIGNED_BYTE 0x1401
#define GL_TRIANGLES 0x0004
#define GL_VERTEX_SHADER 0x8B31
#define GL_FRAGMENT_SHADER 0x8B30
#define GL_COMPILE_STATUS 0x8B81
#define GL_LINK_STATUS 0x8B82
#define GL_FLOAT 0x1406

static int probe_gl(void) {
    void *egl = dlopen("libEGL.so.1", RTLD_NOW | RTLD_LOCAL);
    j_open("gl", '{');
    if (!egl) { j_kstr("error", "libEGL.so.1 not found"); j_close('}'); return 1; }
#define EGLF(ret, name, ...) ret (*name)(__VA_ARGS__) = (ret (*)(__VA_ARGS__))dlsym(egl, #name)
    EGLF(void *, eglGetProcAddress, const char *);
    EGLF(EGLDisplay, eglGetDisplay, void *);
    EGLF(EGLBoolean, eglInitialize, EGLDisplay, EGLint *, EGLint *);
    EGLF(EGLBoolean, eglBindAPI, EGLenum);
    EGLF(EGLBoolean, eglChooseConfig, EGLDisplay, const EGLint *, EGLConfig *, EGLint, EGLint *);
    EGLF(EGLContext, eglCreateContext, EGLDisplay, EGLConfig, EGLContext, const EGLint *);
    EGLF(EGLBoolean, eglMakeCurrent, EGLDisplay, EGLSurface, EGLSurface, EGLContext);
    EGLF(const char *, eglQueryString, EGLDisplay, EGLint);
    EGLF(EGLBoolean, eglTerminate, EGLDisplay);
    EGLF(EGLint, eglGetError, void);
    if (!eglGetProcAddress || !eglInitialize || !eglCreateContext) {
        j_kstr("error", "libEGL lacks core entry points"); j_close('}'); return 1;
    }
    EGLDisplay (*getPlat)(EGLenum, void *, const intptr_t *) =
        (EGLDisplay (*)(EGLenum, void *, const intptr_t *))eglGetProcAddress("eglGetPlatformDisplay");
    EGLDisplay dpy = NULL;
    const char *how = "surfaceless";
    if (getPlat) dpy = getPlat(EGL_PLATFORM_SURFACELESS_MESA, NULL, NULL);
    if (!dpy && eglGetDisplay) { dpy = eglGetDisplay(NULL); how = "default"; }
    EGLint maj = 0, min = 0;
    if (!dpy || !eglInitialize(dpy, &maj, &min)) {
        j_kstr("error", "eglInitialize failed");
        j_kint("egl_error", eglGetError ? eglGetError() : 0);
        j_close('}'); return 1;
    }
    j_kstr("egl_platform", how);
    j_kstr("egl_vendor", eglQueryString(dpy, EGL_VENDOR));
    j_kstr("egl_version", eglQueryString(dpy, EGL_VERSION));
    eglBindAPI(EGL_OPENGL_ES_API);
    EGLint cattr[] = {EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT, EGL_SURFACE_TYPE, 0, EGL_NONE};
    EGLConfig cfg = NULL; EGLint n = 0;
    if (!eglChooseConfig(dpy, cattr, &cfg, 1, &n) || n < 1) {
        /* surfaceless contexts may be created without a config (EGL_KHR_no_config_context) */
        cfg = NULL;
    }
    EGLint ctxattr[] = {EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
    EGLContext ctx = eglCreateContext(dpy, cfg, NULL, ctxattr);
    if (!ctx || !eglMakeCurrent(dpy, NULL, NULL, ctx)) {
        j_kstr("error", "could not create/bind a surfaceless GLES2 context");
        j_kint("egl_error", eglGetError ? eglGetError() : 0);
        eglTerminate(dpy); j_close('}'); return 1;
    }
#define GLF(ret, name, ...) ret (*name)(__VA_ARGS__) = (ret (*)(__VA_ARGS__))eglGetProcAddress(#name)
    GLF(const unsigned char *, glGetString, unsigned);
    GLF(void, glGenFramebuffers, int, unsigned *);
    GLF(void, glBindFramebuffer, unsigned, unsigned);
    GLF(void, glGenRenderbuffers, int, unsigned *);
    GLF(void, glBindRenderbuffer, unsigned, unsigned);
    GLF(void, glRenderbufferStorage, unsigned, unsigned, int, int);
    GLF(void, glFramebufferRenderbuffer, unsigned, unsigned, unsigned, unsigned);
    GLF(unsigned, glCheckFramebufferStatus, unsigned);
    GLF(void, glViewport, int, int, int, int);
    GLF(void, glClearColor, float, float, float, float);
    GLF(void, glClear, unsigned);
    GLF(void, glReadPixels, int, int, int, int, unsigned, unsigned, void *);
    GLF(unsigned, glCreateShader, unsigned);
    GLF(void, glShaderSource, unsigned, int, const char *const *, const int *);
    GLF(void, glCompileShader, unsigned);
    GLF(void, glGetShaderiv, unsigned, unsigned, int *);
    GLF(unsigned, glCreateProgram, void);
    GLF(void, glAttachShader, unsigned, unsigned);
    GLF(void, glBindAttribLocation, unsigned, unsigned, const char *);
    GLF(void, glLinkProgram, unsigned);
    GLF(void, glGetProgramiv, unsigned, unsigned, int *);
    GLF(void, glUseProgram, unsigned);
    GLF(void, glVertexAttribPointer, unsigned, int, unsigned, unsigned char, int, const void *);
    GLF(void, glEnableVertexAttribArray, unsigned);
    GLF(void, glDrawArrays, unsigned, int, int);
    GLF(void, glFinish, void);
    if (!glGetString) { j_kstr("error", "glGetString unavailable"); j_close('}'); return 1; }
    j_kstr("vendor", (const char *)glGetString(GL_VENDOR));
    j_kstr("renderer", (const char *)glGetString(GL_RENDERER));
    j_kstr("version", (const char *)glGetString(GL_VERSION));
    j_kstr("glsl", (const char *)glGetString(GL_SHADING_LANGUAGE_VERSION));

    /* Draw test: clear to blue, draw a triangle covering the left half red,
     * read back and check both colours. Proves the renderer executes work,
     * not just that it reports a name. */
    bool draw_ok = false;
    const char *draw_err = NULL;
    if (glGenFramebuffers && glReadPixels && glDrawArrays && glCreateShader) {
        enum { W = 64, H = 64 };
        unsigned fbo, rb;
        glGenRenderbuffers(1, &rb); glBindRenderbuffer(GL_RENDERBUFFER, rb);
        glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA8, W, H);
        glGenFramebuffers(1, &fbo); glBindFramebuffer(GL_FRAMEBUFFER, fbo);
        glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, rb);
        if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
            draw_err = "framebuffer incomplete";
        } else {
            static const char *vs = "attribute vec2 p; void main(){ gl_Position = vec4(p,0.0,1.0); }";
            static const char *fs = "precision mediump float; void main(){ gl_FragColor = vec4(1.0,0.0,0.0,1.0); }";
            unsigned v = glCreateShader(GL_VERTEX_SHADER), f = glCreateShader(GL_FRAGMENT_SHADER);
            glShaderSource(v, 1, &vs, NULL); glCompileShader(v);
            glShaderSource(f, 1, &fs, NULL); glCompileShader(f);
            int okv = 0, okf = 0, okp = 0;
            glGetShaderiv(v, GL_COMPILE_STATUS, &okv); glGetShaderiv(f, GL_COMPILE_STATUS, &okf);
            unsigned prog = glCreateProgram();
            glAttachShader(prog, v); glAttachShader(prog, f);
            glBindAttribLocation(prog, 0, "p"); glLinkProgram(prog);
            glGetProgramiv(prog, GL_LINK_STATUS, &okp);
            if (!okv || !okf || !okp) {
                draw_err = "shader compile/link failed";
            } else {
                /* left half: x in [-1,0] */
                static const float tri[] = {-1.f, -1.f, 0.f, -1.f, -1.f, 3.f,  0.f, -1.f, 0.f, 3.f, -1.f, 3.f};
                glViewport(0, 0, W, H);
                glClearColor(0.f, 0.f, 1.f, 1.f); glClear(GL_COLOR_BUFFER_BIT);
                glUseProgram(prog);
                glVertexAttribPointer(0, 2, GL_FLOAT, 0, 0, tri); glEnableVertexAttribArray(0);
                glDrawArrays(GL_TRIANGLES, 0, 6); glFinish();
                static unsigned char px[W * H * 4];
                glReadPixels(0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, px);
                int red = 0, blue = 0, other = 0;
                for (int y = 0; y < H; y++)
                    for (int x = 0; x < W; x++) {
                        unsigned char *q = &px[(y * W + x) * 4];
                        bool left = x < W / 2;
                        if (left && q[0] > 240 && q[1] < 16 && q[2] < 16) red++;
                        else if (!left && q[0] < 16 && q[1] < 16 && q[2] > 240) blue++;
                        else other++;
                    }
                j_kint("draw_red_px", red); j_kint("draw_blue_px", blue); j_kint("draw_wrong_px", other);
                draw_ok = (other == 0 && red == W * H / 2 && blue == W * H / 2);
                if (!draw_ok) draw_err = "pixel mismatch";
            }
        }
    } else {
        draw_err = "GL entry points missing";
    }
    j_kbool("draw_ok", draw_ok);
    if (draw_err) j_kstr("draw_error", draw_err);
    if (opt_gl_bench && glGenFramebuffers && glReadPixels && glDrawArrays && glCreateShader) {
        GLF(int, glGetUniformLocation, unsigned, const char *);
        GLF(void, glUniform1f, int, float);
        j_open("bench", '{');
        /* 1. arithmetic correctness: integer pattern from the fragment position, exact in fp32 */
        {
            enum { W = 256, H = 256 };
            unsigned fbo, rb;
            glGenRenderbuffers(1, &rb); glBindRenderbuffer(GL_RENDERBUFFER, rb);
            glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA8, W, H);
            glGenFramebuffers(1, &fbo); glBindFramebuffer(GL_FRAMEBUFFER, fbo);
            glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, rb);
            static const char *vs = "attribute vec2 p; void main(){ gl_Position = vec4(p,0.0,1.0); }";
            static const char *fs =
                "precision highp float;\n"
                "void main(){ float x = floor(gl_FragCoord.x); float y = floor(gl_FragCoord.y);\n"
                "  float r = mod(x * 7.0 + y * 3.0, 256.0); float g = mod(x * y, 251.0);\n"
                "  float b = mod(x + 2.0 * y, 256.0);\n"
                "  gl_FragColor = vec4(r / 255.0, g / 255.0, b / 255.0, 1.0); }";
            unsigned v = glCreateShader(GL_VERTEX_SHADER), f = glCreateShader(GL_FRAGMENT_SHADER);
            glShaderSource(v, 1, &vs, NULL); glCompileShader(v);
            glShaderSource(f, 1, &fs, NULL); glCompileShader(f);
            int okv = 0, okf = 0, okp = 0;
            glGetShaderiv(v, GL_COMPILE_STATUS, &okv); glGetShaderiv(f, GL_COMPILE_STATUS, &okf);
            unsigned prog = glCreateProgram();
            glAttachShader(prog, v); glAttachShader(prog, f);
            glBindAttribLocation(prog, 0, "p"); glLinkProgram(prog);
            glGetProgramiv(prog, GL_LINK_STATUS, &okp);
            long wrong = -1, maxdiff = -1;
            if (okv && okf && okp) {
                static const float full[] = {-1.f, -1.f, 3.f, -1.f, -1.f, 3.f};
                glViewport(0, 0, W, H); glUseProgram(prog);
                glVertexAttribPointer(0, 2, GL_FLOAT, 0, 0, full); glEnableVertexAttribArray(0);
                glDrawArrays(GL_TRIANGLES, 0, 3); glFinish();
                static unsigned char px[W * H * 4];
                glReadPixels(0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, px);
                wrong = 0; maxdiff = 0;
                for (int y = 0; y < H; y++)
                    for (int x = 0; x < W; x++) {
                        unsigned char *q = &px[(y * W + x) * 4];
                        int e[3] = {(x * 7 + y * 3) % 256, (x * y) % 251, (x + 2 * y) % 256};
                        int bad = 0;
                        for (int k = 0; k < 3; k++) {
                            int d = abs((int)q[k] - e[k]);
                            if (d > maxdiff) maxdiff = d;
                            if (d > 1) bad = 1;
                        }
                        wrong += bad;
                    }
            }
            j_kbool("shader_ok", wrong == 0);
            j_kint("shader_wrong_px", wrong);
            j_kint("shader_max_channel_diff", maxdiff);
        }
        /* 2. frame timing at 1920x1080: fullscreen draw + glFinish per frame */
        {
            enum { W = 1920, H = 1080 };
            unsigned fbo, rb;
            glGenRenderbuffers(1, &rb); glBindRenderbuffer(GL_RENDERBUFFER, rb);
            glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA8, W, H);
            glGenFramebuffers(1, &fbo); glBindFramebuffer(GL_FRAMEBUFFER, fbo);
            glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, rb);
            bool complete = glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE;
            j_kbool("fb1080_complete", complete);
            if (complete) {
                static const char *vs = "attribute vec2 p; void main(){ gl_Position = vec4(p,0.0,1.0); }";
                static const char *fs =
                    "precision highp float; uniform float t;\n"
                    "void main(){ vec2 u = gl_FragCoord.xy / vec2(1920.0, 1080.0); float a = 0.0;\n"
                    "  for (int i = 0; i < 16; i++) a += sin(u.x * float(i) + t) * cos(u.y * float(i) - t);\n"
                    "  gl_FragColor = vec4(fract(a), u, 1.0); }";
                unsigned v = glCreateShader(GL_VERTEX_SHADER), f = glCreateShader(GL_FRAGMENT_SHADER);
                glShaderSource(v, 1, &vs, NULL); glCompileShader(v);
                glShaderSource(f, 1, &fs, NULL); glCompileShader(f);
                unsigned prog = glCreateProgram();
                glAttachShader(prog, v); glAttachShader(prog, f);
                glBindAttribLocation(prog, 0, "p"); glLinkProgram(prog);
                int okp = 0; glGetProgramiv(prog, GL_LINK_STATUS, &okp);
                if (okp && glGetUniformLocation && glUniform1f) {
                    static const float full[] = {-1.f, -1.f, 3.f, -1.f, -1.f, 3.f};
                    glViewport(0, 0, W, H); glUseProgram(prog);
                    glVertexAttribPointer(0, 2, GL_FLOAT, 0, 0, full); glEnableVertexAttribArray(0);
                    int ut = glGetUniformLocation(prog, "t");
                    int n = opt_frames > 0 && opt_frames <= 10000 ? opt_frames : 120;
                    double *ft = calloc((size_t)n, sizeof *ft);
                    glUniform1f(ut, 0.f); glDrawArrays(GL_TRIANGLES, 0, 3); glFinish();   /* warm-up */
                    for (int i = 0; i < n; i++) {
                        double t0 = now_s();
                        glUniform1f(ut, (float)i * 0.01f);
                        glDrawArrays(GL_TRIANGLES, 0, 3); glFinish();
                        ft[i] = (now_s() - t0) * 1000.0;
                    }
                    for (int i = 1; i < n; i++) { double x = ft[i]; int k = i - 1; while (k >= 0 && ft[k] > x) { ft[k + 1] = ft[k]; k--; } ft[k + 1] = x; }
                    j_open("frame_ms_1080p", '{');
                    j_kint("frames", n);
                    j_kdbl("min", ft[0]); j_kdbl("median", ft[n / 2]);
                    j_kdbl("p95", ft[(int)(n * 0.95) < n ? (int)(n * 0.95) : n - 1]); j_kdbl("max", ft[n - 1]);
                    j_close('}');
                    free(ft);
                    /* 3. draw-call overhead: 5000 tiny draws, one glFinish */
                    static const float tiny[] = {-1.f, -1.f, -0.999f, -1.f, -1.f, -0.999f};
                    glVertexAttribPointer(0, 2, GL_FLOAT, 0, 0, tiny);
                    glFinish();
                    double t0 = now_s();
                    for (int i = 0; i < 5000; i++) glDrawArrays(GL_TRIANGLES, 0, 3);
                    glFinish();
                    double el = now_s() - t0;
                    j_open("draw_calls", '{');
                    j_kint("count", 5000); j_kdbl("total_ms", el * 1000.0); j_kdbl("us_per_draw", el * 1e6 / 5000.0);
                    j_close('}');
                } else {
                    j_kstr("frame_error", "timing shader failed to link");
                }
            }
        }
        j_close('}');
    }
    eglMakeCurrent(dpy, NULL, NULL, NULL);
    eglTerminate(dpy);
    j_close('}');
    return draw_ok ? 0 : 1;
}

/* ------------------------------------------------------------------ Vulkan */
static void *vklib;
static PFN_vkGetInstanceProcAddr gipa;
static VkInstance inst;
#define IF(name) PFN_##name name = (PFN_##name)gipa(inst, #name)

static const char *vkres(VkResult r) {
    switch (r) {
    case VK_SUCCESS: return "VK_SUCCESS";
    case VK_TIMEOUT: return "VK_TIMEOUT";
    case VK_NOT_READY: return "VK_NOT_READY";
    case VK_ERROR_OUT_OF_HOST_MEMORY: return "VK_ERROR_OUT_OF_HOST_MEMORY";
    case VK_ERROR_OUT_OF_DEVICE_MEMORY: return "VK_ERROR_OUT_OF_DEVICE_MEMORY";
    case VK_ERROR_INITIALIZATION_FAILED: return "VK_ERROR_INITIALIZATION_FAILED";
    case VK_ERROR_DEVICE_LOST: return "VK_ERROR_DEVICE_LOST";
    case VK_ERROR_INCOMPATIBLE_DRIVER: return "VK_ERROR_INCOMPATIBLE_DRIVER";
    case VK_ERROR_EXTENSION_NOT_PRESENT: return "VK_ERROR_EXTENSION_NOT_PRESENT";
    case VK_ERROR_FEATURE_NOT_PRESENT: return "VK_ERROR_FEATURE_NOT_PRESENT";
    default: return "VK_ERROR_OTHER";
    }
}

static int vk_init(void) {
    vklib = dlopen("libvulkan.so.1", RTLD_NOW | RTLD_LOCAL);
    if (!vklib) return -1;
    gipa = (PFN_vkGetInstanceProcAddr)dlsym(vklib, "vkGetInstanceProcAddr");
    if (!gipa) return -2;
    PFN_vkEnumerateInstanceVersion eiv = (PFN_vkEnumerateInstanceVersion)gipa(NULL, "vkEnumerateInstanceVersion");
    uint32_t api = VK_API_VERSION_1_0;
    if (eiv) eiv(&api);
    PFN_vkCreateInstance ci = (PFN_vkCreateInstance)gipa(NULL, "vkCreateInstance");
    VkApplicationInfo ai = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .pApplicationName = "orbis-gpu-probe",
                            .apiVersion = api >= VK_API_VERSION_1_2 ? VK_API_VERSION_1_2 : VK_API_VERSION_1_1};
    VkInstanceCreateInfo ici = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &ai};
    VkResult r = ci(&ici, NULL, &inst);
    if (r != VK_SUCCESS) return (int)r;
    return 0;
}

static bool has_dev_ext(VkPhysicalDevice pd, const char *name) {
    IF(vkEnumerateDeviceExtensionProperties);
    uint32_t n = 0;
    vkEnumerateDeviceExtensionProperties(pd, NULL, &n, NULL);
    VkExtensionProperties *e = calloc(n ? n : 1, sizeof(*e));
    vkEnumerateDeviceExtensionProperties(pd, NULL, &n, e);
    bool found = false;
    for (uint32_t i = 0; i < n; i++)
        if (!strcmp(e[i].extensionName, name)) { found = true; break; }
    free(e);
    return found;
}

static void describe_device(VkPhysicalDevice pd, uint32_t idx) {
    IF(vkGetPhysicalDeviceProperties2);
    IF(vkGetPhysicalDeviceMemoryProperties2);
    IF(vkGetPhysicalDeviceQueueFamilyProperties);
    VkPhysicalDeviceDriverProperties drv = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES};
    VkPhysicalDeviceProperties2 p2 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &drv};
    vkGetPhysicalDeviceProperties2(pd, &p2);
    VkPhysicalDeviceProperties *p = &p2.properties;
    j_open(NULL, '{');
    j_kint("index", idx);
    j_kstr("name", p->deviceName);
    j_kint("device_type", p->deviceType);
    j_kint("vendor_id", p->vendorID);
    j_kint("device_id", p->deviceID);
    char buf[64];
    snprintf(buf, sizeof buf, "%u.%u.%u", VK_API_VERSION_MAJOR(p->apiVersion),
             VK_API_VERSION_MINOR(p->apiVersion), VK_API_VERSION_PATCH(p->apiVersion));
    j_kstr("api_version", buf);
    j_kint("driver_id", drv.driverID);
    j_kstr("driver_name", drv.driverName);
    j_kstr("driver_info", drv.driverInfo);
    j_kuint("max_compute_workgroup_count_x", p->limits.maxComputeWorkGroupCount[0]);
    j_kuint("max_storage_buffer_range", p->limits.maxStorageBufferRange);
    bool budget = has_dev_ext(pd, VK_EXT_MEMORY_BUDGET_EXTENSION_NAME);
    VkPhysicalDeviceMemoryBudgetPropertiesEXT mb = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_BUDGET_PROPERTIES_EXT};
    VkPhysicalDeviceMemoryProperties2 m2 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_PROPERTIES_2,
                                            .pNext = budget ? &mb : NULL};
    vkGetPhysicalDeviceMemoryProperties2(pd, &m2);
    j_kbool("memory_budget_ext", budget);
    j_open("heaps", '[');
    for (uint32_t h = 0; h < m2.memoryProperties.memoryHeapCount; h++) {
        j_open(NULL, '{');
        j_kuint("size", m2.memoryProperties.memoryHeaps[h].size);
        j_kbool("device_local", m2.memoryProperties.memoryHeaps[h].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT);
        if (budget) { j_kuint("budget", mb.heapBudget[h]); j_kuint("usage", mb.heapUsage[h]); }
        j_close('}');
    }
    j_close(']');
    uint32_t qn = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, NULL);
    VkQueueFamilyProperties *q = calloc(qn ? qn : 1, sizeof(*q));
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, q);
    j_open("queue_families", '[');
    for (uint32_t i = 0; i < qn; i++) {
        j_open(NULL, '{');
        j_kint("index", i);
        j_kint("count", q[i].queueCount);
        j_kbool("graphics", q[i].queueFlags & VK_QUEUE_GRAPHICS_BIT);
        j_kbool("compute", q[i].queueFlags & VK_QUEUE_COMPUTE_BIT);
        j_kbool("transfer", q[i].queueFlags & VK_QUEUE_TRANSFER_BIT);
        j_close('}');
    }
    j_close(']');
    free(q);
    j_close('}');
}

/* ----- compute suite */
struct ctx {
    VkPhysicalDevice pd;
    VkDevice dev;
    VkQueue queue;
    uint32_t qfam;
    VkCommandPool pool;
    VkPhysicalDeviceMemoryProperties mem;
    uint64_t timeout_ns;
    bool fatal;          /* device lost / timeout: stop everything */
    const char *fatal_why;
    /* device fns */
    PFN_vkCreateBuffer CreateBuffer; PFN_vkDestroyBuffer DestroyBuffer;
    PFN_vkGetBufferMemoryRequirements GetBufMemReq; PFN_vkAllocateMemory AllocateMemory;
    PFN_vkFreeMemory FreeMemory; PFN_vkBindBufferMemory BindBufferMemory;
    PFN_vkMapMemory MapMemory; PFN_vkUnmapMemory UnmapMemory;
    PFN_vkAllocateCommandBuffers AllocCB; PFN_vkFreeCommandBuffers FreeCB;
    PFN_vkBeginCommandBuffer BeginCB; PFN_vkEndCommandBuffer EndCB;
    PFN_vkCmdFillBuffer CmdFill; PFN_vkCmdCopyBuffer CmdCopy;
    PFN_vkCmdPipelineBarrier CmdBarrier; PFN_vkCmdBindPipeline CmdBindPipe;
    PFN_vkCmdBindDescriptorSets CmdBindDS; PFN_vkCmdPushConstants CmdPush;
    PFN_vkCmdDispatch CmdDispatch; PFN_vkQueueSubmit QueueSubmit;
    PFN_vkCreateFence CreateFence; PFN_vkDestroyFence DestroyFence;
    PFN_vkWaitForFences WaitFences; PFN_vkCreateShaderModule CreateSM;
    PFN_vkDestroyShaderModule DestroySM; PFN_vkCreateDescriptorSetLayout CreateDSL;
    PFN_vkDestroyDescriptorSetLayout DestroyDSL; PFN_vkCreatePipelineLayout CreatePL;
    PFN_vkDestroyPipelineLayout DestroyPL; PFN_vkCreateComputePipelines CreateCP;
    PFN_vkDestroyPipeline DestroyPipe; PFN_vkCreateDescriptorPool CreateDP;
    PFN_vkDestroyDescriptorPool DestroyDP; PFN_vkAllocateDescriptorSets AllocDS;
    PFN_vkUpdateDescriptorSets UpdateDS; PFN_vkDeviceWaitIdle DeviceWaitIdle;
    PFN_vkFlushMappedMemoryRanges Flush; PFN_vkInvalidateMappedMemoryRanges Invalidate;
};

struct buf { VkBuffer b; VkDeviceMemory m; VkDeviceSize size; void *map; };

static int find_mem(struct ctx *c, uint32_t bits, VkMemoryPropertyFlags want, VkMemoryPropertyFlags avoid) {
    for (uint32_t i = 0; i < c->mem.memoryTypeCount; i++)
        if ((bits & (1u << i)) && (c->mem.memoryTypes[i].propertyFlags & want) == want &&
            !(c->mem.memoryTypes[i].propertyFlags & avoid))
            return (int)i;
    for (uint32_t i = 0; i < c->mem.memoryTypeCount; i++)
        if ((bits & (1u << i)) && (c->mem.memoryTypes[i].propertyFlags & want) == want) return (int)i;
    return -1;
}

static VkResult mkbuf(struct ctx *c, struct buf *o, VkDeviceSize size, bool host) {
    memset(o, 0, sizeof *o);
    o->size = size;
    VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = size,
        .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT,
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE};
    VkResult r = c->CreateBuffer(c->dev, &bi, NULL, &o->b);
    if (r) return r;
    VkMemoryRequirements mr;
    c->GetBufMemReq(c->dev, o->b, &mr);
    int t = host ? find_mem(c, mr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, 0)
                 : find_mem(c, mr.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);
    if (t < 0) return VK_ERROR_FEATURE_NOT_PRESENT;
    VkMemoryAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = mr.size, .memoryTypeIndex = (uint32_t)t};
    r = c->AllocateMemory(c->dev, &ai, NULL, &o->m);
    if (r) return r;
    r = c->BindBufferMemory(c->dev, o->b, o->m, 0);
    if (r) return r;
    if (host) r = c->MapMemory(c->dev, o->m, 0, VK_WHOLE_SIZE, 0, &o->map);
    return r;
}

static void rmbuf(struct ctx *c, struct buf *o) {
    if (o->map) c->UnmapMemory(c->dev, o->m);
    if (o->b) c->DestroyBuffer(c->dev, o->b, NULL);
    if (o->m) c->FreeMemory(c->dev, o->m, NULL);
    memset(o, 0, sizeof *o);
}

static VkCommandBuffer cb_begin(struct ctx *c) {
    VkCommandBufferAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = c->pool,
                                      .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    VkCommandBuffer cb;
    if (c->AllocCB(c->dev, &ai, &cb)) return VK_NULL_HANDLE;
    VkCommandBufferBeginInfo bi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
                                   .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    c->BeginCB(cb, &bi);
    return cb;
}

static void barrier_all(struct ctx *c, VkCommandBuffer cb) {
    VkMemoryBarrier mb = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
                          .srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT | VK_ACCESS_TRANSFER_WRITE_BIT,
                          .dstAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT |
                                           VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_HOST_READ_BIT};
    c->CmdBarrier(cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT,
                  VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT | VK_PIPELINE_STAGE_HOST_BIT,
                  0, 1, &mb, 0, NULL, 0, NULL);
}

/* Submit, wait with timeout. Returns VkResult; marks fatal on TIMEOUT/DEVICE_LOST. */
static VkResult cb_submit(struct ctx *c, VkCommandBuffer cb, double *elapsed) {
    c->EndCB(cb);
    VkFenceCreateInfo fi = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    VkFence f;
    VkResult r = c->CreateFence(c->dev, &fi, NULL, &f);
    if (r) return r;
    VkSubmitInfo si = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &cb};
    double t0 = now_s();
    r = c->QueueSubmit(c->queue, 1, &si, f);
    if (r == VK_SUCCESS) r = c->WaitFences(c->dev, 1, &f, VK_TRUE, c->timeout_ns);
    if (elapsed) *elapsed = now_s() - t0;
    if (r == VK_TIMEOUT || r == VK_ERROR_DEVICE_LOST) {
        c->fatal = true;
        c->fatal_why = vkres(r);
        /* Do not free resources the GPU may still reference. */
        return r;
    }
    c->DestroyFence(c->dev, f, NULL);
    c->FreeCB(c->dev, c->pool, 1, &cb);
    return r;
}

struct pipe { VkShaderModule sm; VkDescriptorSetLayout dsl; VkPipelineLayout pl; VkPipeline p; VkDescriptorPool dp; VkDescriptorSet ds; };

static VkResult mkpipe(struct ctx *c, struct pipe *o, const uint32_t *code, size_t size, int nbuf, uint32_t pc_size) {
    memset(o, 0, sizeof *o);
    VkShaderModuleCreateInfo smi = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = size, .pCode = code};
    VkResult r = c->CreateSM(c->dev, &smi, NULL, &o->sm);
    if (r) return r;
    VkDescriptorSetLayoutBinding b[2];
    for (int i = 0; i < nbuf; i++)
        b[i] = (VkDescriptorSetLayoutBinding){.binding = (uint32_t)i, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
                                              .descriptorCount = 1, .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT};
    VkDescriptorSetLayoutCreateInfo dli = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO, .bindingCount = (uint32_t)nbuf, .pBindings = b};
    if ((r = c->CreateDSL(c->dev, &dli, NULL, &o->dsl))) return r;
    VkPushConstantRange pcr = {.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT, .offset = 0, .size = pc_size};
    VkPipelineLayoutCreateInfo pli = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1,
                                      .pSetLayouts = &o->dsl, .pushConstantRangeCount = 1, .pPushConstantRanges = &pcr};
    if ((r = c->CreatePL(c->dev, &pli, NULL, &o->pl))) return r;
    VkComputePipelineCreateInfo cpi = {.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage = {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_COMPUTE_BIT,
                  .module = o->sm, .pName = "main"},
        .layout = o->pl};
    if ((r = c->CreateCP(c->dev, VK_NULL_HANDLE, 1, &cpi, NULL, &o->p))) return r;
    VkDescriptorPoolSize ps = {.type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .descriptorCount = 2};
    VkDescriptorPoolCreateInfo dpi = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 1, .poolSizeCount = 1, .pPoolSizes = &ps};
    if ((r = c->CreateDP(c->dev, &dpi, NULL, &o->dp))) return r;
    VkDescriptorSetAllocateInfo dai = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO, .descriptorPool = o->dp,
                                       .descriptorSetCount = 1, .pSetLayouts = &o->dsl};
    return c->AllocDS(c->dev, &dai, &o->ds);
}

static void bindbufs(struct ctx *c, struct pipe *o, struct buf *a, struct buf *b2) {
    VkDescriptorBufferInfo bi[2] = {{a->b, 0, VK_WHOLE_SIZE}, {b2 ? b2->b : VK_NULL_HANDLE, 0, VK_WHOLE_SIZE}};
    VkWriteDescriptorSet w[2] = {
        {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = o->ds, .dstBinding = 0, .descriptorCount = 1,
         .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .pBufferInfo = &bi[0]},
        {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = o->ds, .dstBinding = 1, .descriptorCount = 1,
         .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .pBufferInfo = &bi[1]}};
    c->UpdateDS(c->dev, b2 ? 2 : 1, w, 0, NULL);
}

static void rmpipe(struct ctx *c, struct pipe *o) {
    if (o->dp) c->DestroyDP(c->dev, o->dp, NULL);
    if (o->p) c->DestroyPipe(c->dev, o->p, NULL);
    if (o->pl) c->DestroyPL(c->dev, o->pl, NULL);
    if (o->dsl) c->DestroyDSL(c->dev, o->dsl, NULL);
    if (o->sm) c->DestroySM(c->dev, o->sm, NULL);
}

static uint32_t xs32(uint32_t *s) { uint32_t x = *s; x ^= x << 13; x ^= x >> 17; x ^= x << 5; return *s = x; }

static int n_pass, n_fail;
static void result(const char *name, bool ok, double secs, const char *fmt, ...) {
    char msg[256] = "";
    if (fmt) { va_list ap; va_start(ap, fmt); vsnprintf(msg, sizeof msg, fmt, ap); va_end(ap); }
    j_open(NULL, '{');
    j_kstr("test", name);
    j_kbool("ok", ok);
    j_kdbl("seconds", secs);
    if (*msg) j_kstr("detail", msg);
    j_close('}');
    if (ok) n_pass++; else n_fail++;
    fflush(J);
}

/* T1: vkCmdFillBuffer around the 4 KiB threshold where RADV switches from
 * CP DMA to a compute fill shader (linux-psx 0005 investigated this path). */
static void t_fill(struct ctx *c, VkDeviceSize size) {
    const VkDeviceSize G = 256;
    char name[64]; snprintf(name, sizeof name, "fill_%llu", (unsigned long long)size);
    struct buf d, up, down;
    double t0 = now_s(), el = 0;
    /* Separate upload and readback staging buffers: host writes happen at
     * record time, GPU copies at submit time. */
    if (mkbuf(c, &d, size + 2 * G, false) || mkbuf(c, &up, size + 2 * G, true) || mkbuf(c, &down, size + 2 * G, true)) {
        result(name, false, 0, "allocation failed"); rmbuf(c, &d); rmbuf(c, &up); rmbuf(c, &down); return;
    }
    uint32_t *upp = up.map, *hp = down.map;
    for (VkDeviceSize i = 0; i < (size + 2 * G) / 4; i++) upp[i] = 0xA5A5A5A5u;
    memset(down.map, 0, size + 2 * G);
    VkCommandBuffer cb = cb_begin(c);
    VkBufferCopy cp = {0, 0, size + 2 * G};
    c->CmdCopy(cb, up.b, d.b, 1, &cp);
    barrier_all(c, cb);
    c->CmdFill(cb, d.b, G, size, 0x5A5A5A5Au);
    barrier_all(c, cb);
    c->CmdCopy(cb, d.b, down.b, 1, &cp);
    barrier_all(c, cb);
    VkResult r = cb_submit(c, cb, &el);
    if (r) { result(name, false, el, "%s", vkres(r)); if (!c->fatal) { rmbuf(c, &d); rmbuf(c, &up); rmbuf(c, &down); } return; }
    long bad_guard = 0, bad_fill = 0;
    for (VkDeviceSize i = 0; i < (size + 2 * G) / 4; i++) {
        bool in = i * 4 >= G && i * 4 < G + size;
        if (in && hp[i] != 0x5A5A5A5Au) bad_fill++;
        if (!in && hp[i] != 0xA5A5A5A5u) bad_guard++;
    }
    result(name, !bad_guard && !bad_fill, now_s() - t0, "bad_fill_words=%ld bad_guard_words=%ld gpu_s=%.4f", bad_fill, bad_guard, el);
    rmbuf(c, &d); rmbuf(c, &up); rmbuf(c, &down);
}

/* T2: host -> device-local -> host copy round trip */
static void t_copy(struct ctx *c, VkDeviceSize size) {
    char name[64]; snprintf(name, sizeof name, "copy_%llu", (unsigned long long)size);
    struct buf d, h, h2;
    double t0 = now_s(), el = 0;
    if (mkbuf(c, &d, size, false) || mkbuf(c, &h, size, true) || mkbuf(c, &h2, size, true)) {
        result(name, false, 0, "allocation failed"); rmbuf(c, &d); rmbuf(c, &h); rmbuf(c, &h2); return;
    }
    uint32_t s = 0x1234567u, *a = h.map, *b = h2.map;
    for (VkDeviceSize i = 0; i < size / 4; i++) a[i] = xs32(&s);
    memset(b, 0, size);
    VkCommandBuffer cb = cb_begin(c);
    VkBufferCopy cp = {0, 0, size};
    c->CmdCopy(cb, h.b, d.b, 1, &cp); barrier_all(c, cb);
    c->CmdCopy(cb, d.b, h2.b, 1, &cp); barrier_all(c, cb);
    VkResult r = cb_submit(c, cb, &el);
    if (r) { result(name, false, el, "%s", vkres(r)); return; }
    long bad = 0;
    for (VkDeviceSize i = 0; i < size / 4; i++) bad += a[i] != b[i];
    result(name, !bad, now_s() - t0, "mismatched_words=%ld gpu_s=%.4f", bad, el);
    rmbuf(c, &d); rmbuf(c, &h); rmbuf(c, &h2);
}

/* T3: dispatch geometry — every invocation writes its own linear index */
static void t_index(struct ctx *c, uint32_t gx, uint32_t gy) {
    char name[64]; snprintf(name, sizeof name, "dispatch_%ux%ux64", gx, gy);
    uint64_t count = (uint64_t)gx * gy * 64;
    VkDeviceSize size = count * 4;
    struct buf d, h; struct pipe p;
    double t0 = now_s(), el = 0;
    if (mkbuf(c, &d, size, false) || mkbuf(c, &h, size, true)) { result(name, false, 0, "allocation failed (%llu bytes)", (unsigned long long)size); rmbuf(c, &d); rmbuf(c, &h); return; }
    if (mkpipe(c, &p, spv_index_write, spv_index_write_size, 1, 4)) { result(name, false, 0, "pipeline failed"); return; }
    bindbufs(c, &p, &d, NULL);
    VkCommandBuffer cb = cb_begin(c);
    c->CmdFill(cb, d.b, 0, VK_WHOLE_SIZE, 0xFFFFFFFFu); barrier_all(c, cb);
    c->CmdBindPipe(cb, VK_PIPELINE_BIND_POINT_COMPUTE, p.p);
    c->CmdBindDS(cb, VK_PIPELINE_BIND_POINT_COMPUTE, p.pl, 0, 1, &p.ds, 0, NULL);
    uint32_t pc = (uint32_t)count;
    c->CmdPush(cb, p.pl, VK_SHADER_STAGE_COMPUTE_BIT, 0, 4, &pc);
    c->CmdDispatch(cb, gx, gy, 1); barrier_all(c, cb);
    VkBufferCopy cp = {0, 0, size};
    c->CmdCopy(cb, d.b, h.b, 1, &cp); barrier_all(c, cb);
    VkResult r = cb_submit(c, cb, &el);
    if (r) { result(name, false, el, "%s", vkres(r)); return; }
    uint32_t *o = h.map; long bad = 0; long first_bad = -1;
    for (uint64_t i = 0; i < count; i++) if (o[i] != (uint32_t)i) { if (first_bad < 0) first_bad = (long)i; bad++; }
    result(name, !bad, now_s() - t0, "invocations=%llu wrong=%ld first_wrong=%ld gpu_s=%.4f", (unsigned long long)count, bad, first_bad, el);
    rmpipe(c, &p); rmbuf(c, &d); rmbuf(c, &h);
}

/* T4: exact integer SAXPY, optionally repeated N times in separate submissions */
static void t_saxpy(struct ctx *c, uint32_t n, int repeats) {
    char name[64]; snprintf(name, sizeof name, repeats > 1 ? "saxpy_u32_%u_x%d" : "saxpy_u32_%u", n, repeats);
    VkDeviceSize size = (VkDeviceSize)n * 4;
    struct buf x, y, hx, hy; struct pipe p;
    double t0 = now_s(), el = 0, worst = 0;
    if (mkbuf(c, &x, size, false) || mkbuf(c, &y, size, false) || mkbuf(c, &hx, size, true) || mkbuf(c, &hy, size, true)) {
        result(name, false, 0, "allocation failed"); return;
    }
    if (mkpipe(c, &p, spv_saxpy_u32, spv_saxpy_u32_size, 2, 8)) { result(name, false, 0, "pipeline failed"); return; }
    bindbufs(c, &p, &x, &y);
    uint32_t s = 42, *ax = hx.map, *ay = hy.map;
    for (uint32_t i = 0; i < n; i++) { ax[i] = xs32(&s); ay[i] = xs32(&s); }
    uint32_t *y0 = malloc(size); memcpy(y0, ay, size);
    uint32_t pcs[2] = {n, 2654435761u};
    uint32_t groups = (n + 63) / 64, gx = groups > 65535 ? 65535 : groups, gy = (groups + gx - 1) / gx;
    VkCommandBuffer cb = cb_begin(c);
    VkBufferCopy cp = {0, 0, size};
    c->CmdCopy(cb, hx.b, x.b, 1, &cp); c->CmdCopy(cb, hy.b, y.b, 1, &cp); barrier_all(c, cb);
    VkResult r = cb_submit(c, cb, &el);
    for (int it = 0; it < repeats && !r; it++) {
        cb = cb_begin(c);
        c->CmdBindPipe(cb, VK_PIPELINE_BIND_POINT_COMPUTE, p.p);
        c->CmdBindDS(cb, VK_PIPELINE_BIND_POINT_COMPUTE, p.pl, 0, 1, &p.ds, 0, NULL);
        c->CmdPush(cb, p.pl, VK_SHADER_STAGE_COMPUTE_BIT, 0, 8, pcs);
        c->CmdDispatch(cb, gx, gy, 1); barrier_all(c, cb);
        r = cb_submit(c, cb, &el);
        if (el > worst) worst = el;
        if (r) { result(name, false, el, "%s at iteration %d", vkres(r), it); free(y0); return; }
    }
    cb = cb_begin(c);
    c->CmdCopy(cb, y.b, hy.b, 1, &cp); barrier_all(c, cb);
    if ((r = cb_submit(c, cb, &el))) { result(name, false, el, "%s on readback", vkres(r)); free(y0); return; }
    long bad = 0;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t e = y0[i];
        for (int it = 0; it < repeats; it++) e = pcs[1] * ax[i] + e;
        bad += ay[i] != e;
    }
    result(name, !bad, now_s() - t0, "elements=%u wrong=%ld worst_submit_s=%.4f", n, bad, worst);
    free(y0); rmpipe(c, &p); rmbuf(c, &x); rmbuf(c, &y); rmbuf(c, &hx); rmbuf(c, &hy);
}

/* T5: shared-memory reduction + atomics */
static void t_reduce(struct ctx *c, uint32_t n) {
    char name[64]; snprintf(name, sizeof name, "reduce_%u", n);
    struct buf v, acc, hv, hacc; struct pipe p;
    double t0 = now_s(), el = 0;
    VkDeviceSize size = (VkDeviceSize)n * 4;
    if (mkbuf(c, &v, size, false) || mkbuf(c, &acc, 8, false) || mkbuf(c, &hv, size, true) || mkbuf(c, &hacc, 8, true)) {
        result(name, false, 0, "allocation failed"); return;
    }
    if (mkpipe(c, &p, spv_reduce, spv_reduce_size, 2, 4)) { result(name, false, 0, "pipeline failed"); return; }
    bindbufs(c, &p, &v, &acc);
    uint32_t s = 7, *hp = hv.map, expect = 0;
    for (uint32_t i = 0; i < n; i++) { hp[i] = xs32(&s) & 0xFFFFu; expect += hp[i]; }
    uint32_t groups = (n + 255) / 256, gx = groups > 65535 ? 65535 : groups, gy = (groups + gx - 1) / gx;
    VkCommandBuffer cb = cb_begin(c);
    VkBufferCopy cp = {0, 0, size}, ca = {0, 0, 8};
    c->CmdCopy(cb, hv.b, v.b, 1, &cp);
    c->CmdFill(cb, acc.b, 0, 8, 0); barrier_all(c, cb);
    c->CmdBindPipe(cb, VK_PIPELINE_BIND_POINT_COMPUTE, p.p);
    c->CmdBindDS(cb, VK_PIPELINE_BIND_POINT_COMPUTE, p.pl, 0, 1, &p.ds, 0, NULL);
    c->CmdPush(cb, p.pl, VK_SHADER_STAGE_COMPUTE_BIT, 0, 4, &n);
    c->CmdDispatch(cb, gx, gy, 1); barrier_all(c, cb);
    c->CmdCopy(cb, acc.b, hacc.b, 1, &ca); barrier_all(c, cb);
    VkResult r = cb_submit(c, cb, &el);
    if (r) { result(name, false, el, "%s", vkres(r)); return; }
    uint32_t *o = hacc.map;
    bool ok = o[0] == expect && o[1] == gx * gy;
    result(name, ok, now_s() - t0, "sum=%u expected=%u groups_done=%u expected_groups=%u", o[0], expect, o[1], gx * gy);
    rmpipe(c, &p); rmbuf(c, &v); rmbuf(c, &acc); rmbuf(c, &hv); rmbuf(c, &hacc);
}

static void t_pipeline_cache(struct ctx *c);

static void load_dev_fns(struct ctx *cp, PFN_vkGetDeviceProcAddr vkGetDeviceProcAddr) {
    struct ctx *c_ = cp;
#define DF(field, name) c_->field = (PFN_##name)vkGetDeviceProcAddr(c_->dev, #name)
    DF(CreateBuffer, vkCreateBuffer); DF(DestroyBuffer, vkDestroyBuffer); DF(GetBufMemReq, vkGetBufferMemoryRequirements);
    DF(AllocateMemory, vkAllocateMemory); DF(FreeMemory, vkFreeMemory); DF(BindBufferMemory, vkBindBufferMemory);
    DF(MapMemory, vkMapMemory); DF(UnmapMemory, vkUnmapMemory); DF(AllocCB, vkAllocateCommandBuffers);
    DF(FreeCB, vkFreeCommandBuffers); DF(BeginCB, vkBeginCommandBuffer); DF(EndCB, vkEndCommandBuffer);
    DF(CmdFill, vkCmdFillBuffer); DF(CmdCopy, vkCmdCopyBuffer); DF(CmdBarrier, vkCmdPipelineBarrier);
    DF(CmdBindPipe, vkCmdBindPipeline); DF(CmdBindDS, vkCmdBindDescriptorSets); DF(CmdPush, vkCmdPushConstants);
    DF(CmdDispatch, vkCmdDispatch); DF(QueueSubmit, vkQueueSubmit); DF(CreateFence, vkCreateFence);
    DF(DestroyFence, vkDestroyFence); DF(WaitFences, vkWaitForFences); DF(CreateSM, vkCreateShaderModule);
    DF(DestroySM, vkDestroyShaderModule); DF(CreateDSL, vkCreateDescriptorSetLayout); DF(DestroyDSL, vkDestroyDescriptorSetLayout);
    DF(CreatePL, vkCreatePipelineLayout); DF(DestroyPL, vkDestroyPipelineLayout); DF(CreateCP, vkCreateComputePipelines);
    DF(DestroyPipe, vkDestroyPipeline); DF(CreateDP, vkCreateDescriptorPool); DF(DestroyDP, vkDestroyDescriptorPool);
    DF(AllocDS, vkAllocateDescriptorSets); DF(UpdateDS, vkUpdateDescriptorSets); DF(DeviceWaitIdle, vkDeviceWaitIdle);
    DF(Flush, vkFlushMappedMemoryRanges); DF(Invalidate, vkInvalidateMappedMemoryRanges);
#undef DF
}

static int run_compute(VkPhysicalDevice pd, uint32_t devidx, int want_qfam, uint64_t timeout_ms, int level) {
    struct ctx c = {.pd = pd, .timeout_ns = timeout_ms * 1000000ull};
    n_pass = n_fail = 0;
    IF(vkGetPhysicalDeviceQueueFamilyProperties);
    IF(vkGetPhysicalDeviceMemoryProperties);
    IF(vkCreateDevice);
    IF(vkGetDeviceProcAddr);
    IF(vkGetPhysicalDeviceProperties);
    vkGetPhysicalDeviceMemoryProperties(pd, &c.mem);
    uint32_t qn = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, NULL);
    VkQueueFamilyProperties q[16];
    if (qn > 16) qn = 16;
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, q);
    int fam = -1;
    if (want_qfam >= 0 && (uint32_t)want_qfam < qn && (q[want_qfam].queueFlags & VK_QUEUE_COMPUTE_BIT)) fam = want_qfam;
    for (uint32_t i = 0; fam < 0 && i < qn; i++) if (q[i].queueFlags & VK_QUEUE_COMPUTE_BIT) fam = (int)i;
    VkPhysicalDeviceProperties props;
    vkGetPhysicalDeviceProperties(pd, &props);
    j_open("compute", '{');
    j_open("device", '{');
    j_kint("index", devidx); j_kstr("name", props.deviceName);
    j_kint("device_type", props.deviceType); j_kint("vendor_id", props.vendorID);
    j_close('}');
    if (fam < 0) { j_kstr("error", "no compute queue family"); j_close('}'); return 1; }
    c.qfam = (uint32_t)fam;
    j_kint("queue_family", fam);
    j_kbool("queue_family_has_graphics", q[fam].queueFlags & VK_QUEUE_GRAPHICS_BIT);
    float prio = 1.0f;
    VkDeviceQueueCreateInfo qci = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = c.qfam, .queueCount = 1, .pQueuePriorities = &prio};
    VkDeviceCreateInfo dci = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1, .pQueueCreateInfos = &qci};
    VkResult r = vkCreateDevice(pd, &dci, NULL, &c.dev);
    if (r) { j_kstr("error", vkres(r)); j_close('}'); return 1; }
    load_dev_fns(&c, vkGetDeviceProcAddr);
    PFN_vkGetDeviceQueue gdq = (PFN_vkGetDeviceQueue)vkGetDeviceProcAddr(c.dev, "vkGetDeviceQueue");
    PFN_vkCreateCommandPool ccp = (PFN_vkCreateCommandPool)vkGetDeviceProcAddr(c.dev, "vkCreateCommandPool");
    gdq(c.dev, c.qfam, 0, &c.queue);
    VkCommandPoolCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, .queueFamilyIndex = c.qfam,
                                   .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT};
    ccp(c.dev, &pci, NULL, &c.pool);

    j_open("tests", '[');
    static const VkDeviceSize fills[] = {4092, 4096, 4100, 65536, 1u << 20, 16u << 20};
    for (unsigned i = 0; i < sizeof fills / sizeof *fills && !c.fatal; i++)
        if (level >= 2 || fills[i] <= (1u << 20)) t_fill(&c, fills[i]);
    if (!c.fatal) t_copy(&c, 1u << 20);
    if (!c.fatal && level >= 2) t_copy(&c, 64u << 20);
    if (!c.fatal) t_index(&c, 1, 1);
    if (!c.fatal) t_index(&c, 1024, 64);
    if (!c.fatal && level >= 2) t_index(&c, 65535, 1);
    if (!c.fatal) t_saxpy(&c, 1u << 20, 1);
    if (!c.fatal && level >= 2) t_saxpy(&c, 16u << 20, 1);
    if (!c.fatal) t_reduce(&c, 1u << 20);
    if (!c.fatal && level >= 2) t_saxpy(&c, 1u << 18, 200);
    if (!c.fatal && opt_pcache) t_pipeline_cache(&c);
    j_close(']');
    j_kint("passed_count", n_pass);
    j_kint("failed_count", n_fail);
    j_kbool("fatal", c.fatal);
    if (c.fatal) j_kstr("fatal_reason", c.fatal_why);
    j_close('}');
    if (!c.fatal) {
        c.DeviceWaitIdle(c.dev);
        PFN_vkDestroyCommandPool dcp = (PFN_vkDestroyCommandPool)vkGetDeviceProcAddr(c.dev, "vkDestroyCommandPool");
        PFN_vkDestroyDevice dd = (PFN_vkDestroyDevice)vkGetDeviceProcAddr(c.dev, "vkDestroyDevice");
        dcp(c.dev, c.pool, NULL);
        dd(c.dev, NULL);
    }
    if (c.fatal) return 3;
    return n_fail ? 1 : 0;
}


/* ------------------------------------------------------------------ shared-memory bench (U6/U27) */
/*
 * Result vocabulary of the zero-copy check and of --membench as a whole (GE-A04/A08). The probe's
 * exit code for --membench is mb_exit[result]; orbis-mem bench maps the JSON "result" to the same
 * numbers, so JSON, exit code and wrapper always agree.
 */
enum mb_res { MB_PASS, MB_FAIL, MB_UNSUPPORTED_EXTENSION, MB_INCOMPATIBLE_MEMORY_TYPE, MB_ALLOCATION_FAILED,
              MB_SYNC_FAILED, MB_DEVICE_LOST, MB_TIMEOUT, MB_NOT_RUN, MB_N };
static const char *const mb_name[MB_N] = {"PASS", "FAIL", "UNSUPPORTED_EXTENSION", "INCOMPATIBLE_MEMORY_TYPE",
                                          "ALLOCATION_FAILED", "SYNC_FAILED", "DEVICE_LOST", "TIMEOUT", "NOT_RUN"};
/* 0 pass, 1 wrong data, 2 could not run, 3 device lost/timeout (stop), 4 not supported here, 5 allocation/sync */
static const int mb_exit[MB_N] = {0, 1, 4, 4, 5, 5, 3, 3, 2};

static enum mb_res mb_from_vk(VkResult r) {
    switch (r) {
    case VK_SUCCESS: return MB_PASS;
    case VK_ERROR_DEVICE_LOST: return MB_DEVICE_LOST;
    case VK_TIMEOUT: return MB_TIMEOUT;
    case VK_ERROR_OUT_OF_HOST_MEMORY: case VK_ERROR_OUT_OF_DEVICE_MEMORY: case VK_ERROR_TOO_MANY_OBJECTS:
        return MB_ALLOCATION_FAILED;
    case VK_ERROR_INVALID_EXTERNAL_HANDLE: case VK_ERROR_FEATURE_NOT_PRESENT: case VK_ERROR_FORMAT_NOT_SUPPORTED:
        return MB_INCOMPATIBLE_MEMORY_TYPE;
    case VK_ERROR_EXTENSION_NOT_PRESENT: return MB_UNSUPPORTED_EXTENSION;
    default: return MB_FAIL;
    }
}

static bool type_coherent(struct ctx *c, int type) {
    return c->mem.memoryTypes[type].propertyFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
}

/* make CPU writes visible to the device (non-coherent memory only; the buffer must be mapped) */
static VkResult host_flush(struct ctx *c, struct buf *o, int type) {
    if (type < 0 || type_coherent(c, type) || !o->map) return VK_SUCCESS;
    VkMappedMemoryRange mr = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE, .memory = o->m, .offset = 0, .size = VK_WHOLE_SIZE};
    return c->Flush(c->dev, 1, &mr);
}

/* make device writes visible to the CPU (non-coherent memory only) */
static VkResult host_invalidate(struct ctx *c, struct buf *o, int type) {
    if (type < 0 || type_coherent(c, type) || !o->map) return VK_SUCCESS;
    VkMappedMemoryRange mr = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE, .memory = o->m, .offset = 0, .size = VK_WHOLE_SIZE};
    return c->Invalidate(c->dev, 1, &mr);
}

/*
 * Buffer in one specific memory type, or imported host memory when hostp != NULL (GE-A10).
 * Every failure releases what was created so far. For an import:
 *   - the pointer must be aligned to, and the size a multiple of, minImportedHostPointerAlignment;
 *   - the memory type comes from vkGetMemoryHostPointerPropertiesEXT AND the buffer's own
 *     VkMemoryRequirements.memoryTypeBits (a host-coherent type is preferred);
 *   - the buffer's required size must fit in the imported range, at offset 0 (alignment holds);
 *   - non-coherent types are mapped so flush/invalidate can be used.
 * *why is set to a short reason on failure.
 */
static VkResult mkbuf_type(struct ctx *c, struct buf *o, VkDeviceSize size, int type, void *hostp, VkDeviceSize host_align,
                           PFN_vkGetMemoryHostPointerPropertiesEXT hpp, int *used_type, const char **why) {
    static const char *dummy;
    if (!why) why = &dummy;
    *why = NULL;
    memset(o, 0, sizeof *o);
    o->size = size;
    if (hostp) {
        if (!hpp) { *why = "vkGetMemoryHostPointerPropertiesEXT missing"; return VK_ERROR_EXTENSION_NOT_PRESENT; }
        if (!host_align || ((uintptr_t)hostp % host_align) || (size % host_align)) {
            *why = "host pointer or size not a multiple of minImportedHostPointerAlignment";
            return VK_ERROR_INVALID_EXTERNAL_HANDLE;
        }
    }
    VkExternalMemoryBufferCreateInfo eb = {.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO,
                                           .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT};
    VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .pNext = hostp ? &eb : NULL, .size = size,
        .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT,
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE};
    VkResult r = c->CreateBuffer(c->dev, &bi, NULL, &o->b);
    if (r) { *why = "vkCreateBuffer"; o->b = VK_NULL_HANDLE; return r; }
    VkMemoryRequirements mr;
    c->GetBufMemReq(c->dev, o->b, &mr);
    uint32_t bits = mr.memoryTypeBits;
    VkImportMemoryHostPointerInfoEXT imp = {.sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT,
                                            .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT,
                                            .pHostPointer = hostp};
    VkDeviceSize alloc = mr.size;
    if (hostp) {
        VkMemoryHostPointerPropertiesEXT hp = {.sType = VK_STRUCTURE_TYPE_MEMORY_HOST_POINTER_PROPERTIES_EXT};
        r = hpp(c->dev, VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT, hostp, &hp);
        if (r) { *why = "vkGetMemoryHostPointerPropertiesEXT refused the pointer"; goto fail; }
        bits &= hp.memoryTypeBits;
        if (!bits) { *why = "no memory type both importable for this pointer and usable by the buffer"; r = VK_ERROR_FEATURE_NOT_PRESENT; goto fail; }
        if (mr.size > size) { *why = "buffer needs more memory than the imported range"; r = VK_ERROR_FEATURE_NOT_PRESENT; goto fail; }
        type = -1;
        /* r3.1: host-visible + host-coherent first, then host-visible (synchronized through the
         * mapping, see zero_copy), then anything (the caller reports it: a non-host-visible
         * import must not be touched through the host pointer at all) */
        for (int pass = 0; pass < 3 && type < 0; pass++)
            for (uint32_t i = 0; i < c->mem.memoryTypeCount && type < 0; i++) {
                VkMemoryPropertyFlags f = c->mem.memoryTypes[i].propertyFlags;
                bool hv = f & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT, hc = f & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
                if ((bits & (1u << i)) && (pass == 0 ? (hv && hc) : pass == 1 ? hv : true))
                    type = (int)i;
            }
        alloc = size;   /* the import covers exactly the host range */
    }
    if (type < 0 || (uint32_t)type >= c->mem.memoryTypeCount || !(bits & (1u << type))) {
        *why = "memory type not allowed for this buffer";
        r = VK_ERROR_FEATURE_NOT_PRESENT;
        goto fail;
    }
    if (used_type) *used_type = type;
    VkMemoryAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .pNext = hostp ? &imp : NULL,
                               .allocationSize = alloc, .memoryTypeIndex = (uint32_t)type};
    if ((r = c->AllocateMemory(c->dev, &ai, NULL, &o->m))) { *why = hostp ? "import (vkAllocateMemory)" : "vkAllocateMemory"; o->m = VK_NULL_HANDLE; goto fail; }
    if ((r = c->BindBufferMemory(c->dev, o->b, o->m, 0))) { *why = "vkBindBufferMemory"; goto fail; }
    /* map host-visible memory: always for allocations; for imports only when flush/invalidate are needed */
    if ((c->mem.memoryTypes[type].propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT) && (!hostp || !type_coherent(c, type))) {
        if ((r = c->MapMemory(c->dev, o->m, 0, VK_WHOLE_SIZE, 0, &o->map))) { *why = "vkMapMemory"; o->map = NULL; goto fail; }
    }
    return VK_SUCCESS;
fail:
    rmbuf(c, o);
    return r;
}

static const char *mem_label(VkMemoryPropertyFlags f) {
    bool dl = f & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, hv = f & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT,
         hc = f & VK_MEMORY_PROPERTY_HOST_CACHED_BIT;
    if (dl && !hv) return "vram (device-local, not CPU-mapped)";
    if (dl && hv) return "vram, CPU-mapped (write-combined)";
    if (hv && hc) return "gtt cached (CPU-snooped)";
    if (hv) return "gtt write-combined";
    return "other";
}

/* best-of-N GPU time of one dispatch (wall clock around submit + fence; includes submit overhead) */
static double gpu_dispatch(struct ctx *c, struct pipe *p, const void *pc, uint32_t pcs, uint32_t gx, uint32_t gy, int reps) {
    double best = 1e9;
    for (int i = 0; i < reps && !c->fatal; i++) {
        VkCommandBuffer cb = cb_begin(c);
        c->CmdBindPipe(cb, VK_PIPELINE_BIND_POINT_COMPUTE, p->p);
        c->CmdBindDS(cb, VK_PIPELINE_BIND_POINT_COMPUTE, p->pl, 0, 1, &p->ds, 0, NULL);
        c->CmdPush(cb, p->pl, VK_SHADER_STAGE_COMPUTE_BIT, 0, pcs, pc);
        c->CmdDispatch(cb, gx, gy, 1);
        barrier_all(c, cb);
        double el = 0;
        if (cb_submit(c, cb, &el)) return -1;
        if (el < best) best = el;
    }
    return best;
}

static void grid(uint64_t groups, uint32_t *gx, uint32_t *gy) {
    *gx = groups > 65535 ? 65535 : (uint32_t)groups;
    *gy = (uint32_t)((groups + *gx - 1) / *gx);
}

static double gbs(double bytes, double s) { return s > 0 ? bytes / s / 1e9 : 0; }

static void bench_type(struct ctx *c, int type, VkDeviceSize size, struct pipe *pw, struct pipe *pr, struct pipe *ps) {
    VkMemoryPropertyFlags f = c->mem.memoryTypes[type].propertyFlags;
    j_open(NULL, '{');
    j_kint("type", type);
    j_kint("heap", c->mem.memoryTypes[type].heapIndex);
    j_kuint("flags", f);
    j_kstr("kind", mem_label(f));
    struct buf a = {0}, b = {0}, acc = {0};
    const char *why = NULL;
    VkResult ar = mkbuf_type(c, &a, size, type, NULL, 0, NULL, NULL, &why);
    if (!ar) ar = mkbuf_type(c, &b, size, type, NULL, 0, NULL, NULL, &why);
    if (!ar && (ar = mkbuf(c, &acc, 8, false))) why = "accumulator buffer";
    if (ar) {
        j_kstr("not_run", "allocation in this memory type failed (size or budget)");
        j_kstr("result", mb_name[mb_from_vk(ar)]);
        j_kstr("step", why ? why : "?");
        j_kstr("vk_result", vkres(ar));
        rmbuf(c, &a); rmbuf(c, &b); rmbuf(c, &acc);
        j_close('}');
        return;
    }
    uint32_t n = (uint32_t)(size / 4), gx, gy;
    uint32_t pcw = n, pcs[2] = {n, 1};
    bindbufs(c, pw, &a, NULL);
    grid((n + 63) / 64, &gx, &gy);
    double tw = gpu_dispatch(c, pw, &pcw, 4, gx, gy, 4);
    bindbufs(c, pr, &a, &acc);
    uint32_t rgx, rgy;
    grid((n + 255) / 256, &rgx, &rgy);
    double tr = gpu_dispatch(c, pr, &pcw, 4, rgx, rgy, 4);
    bindbufs(c, ps, &a, &b);
    double trw = gpu_dispatch(c, ps, pcs, 8, gx, gy, 4);
    j_kdbl("gpu_write_gbs", gbs((double)size, tw));
    j_kdbl("gpu_read_gbs", gbs((double)size, tr));
    j_kdbl("gpu_read_write_gbs", gbs(3.0 * (double)size, trw));   /* saxpy: read x, read y, write y */
    if (a.map) {
        /* CPU timings include the flush/invalidate a correct program needs on non-coherent memory */
        double t0 = now_s();
        memset(a.map, 0x5A, size);
        VkResult fr = host_flush(c, &a, type);
        double cw = now_s() - t0;
        volatile uint64_t sum = 0;
        const uint64_t *q = a.map;
        t0 = now_s();
        VkResult ir = host_invalidate(c, &a, type);
        for (VkDeviceSize i = 0; i < size / 8; i++) sum += q[i];
        double cr = now_s() - t0;
        (void)sum;
        j_kbool("host_coherent", type_coherent(c, type));
        j_kdbl("cpu_write_gbs", gbs((double)size, cw));
        j_kdbl("cpu_read_gbs", gbs((double)size, cr));
        if (fr || ir) j_kstr("sync_error", vkres(fr ? fr : ir));
    }
    if (c->fatal) j_kstr("fatal", c->fatal_why);
    j_close('}');
    if (!c->fatal) { rmbuf(c, &a); rmbuf(c, &b); rmbuf(c, &acc); }
}

/* GPU-side copies between memory kinds (transfer path) */
static void bench_copy(struct ctx *c, const char *name, int from, int to, VkDeviceSize size) {
    struct buf s = {0}, d = {0};
    j_open(NULL, '{');
    j_kstr("copy", name);
    const char *why = NULL;
    VkResult ar = (from < 0 || to < 0) ? VK_ERROR_FEATURE_NOT_PRESENT : mkbuf_type(c, &s, size, from, NULL, 0, NULL, NULL, &why);
    if (!ar) ar = mkbuf_type(c, &d, size, to, NULL, 0, NULL, NULL, &why);
    if (ar) {
        j_kstr("not_run", (from < 0 || to < 0) ? "memory type not available" : "allocation failed");
        if (why) j_kstr("step", why);
        rmbuf(c, &s); rmbuf(c, &d);
        j_close('}');
        return;
    }
    double best = 1e9;
    for (int i = 0; i < 4 && !c->fatal; i++) {
        VkCommandBuffer cb = cb_begin(c);
        VkBufferCopy cp = {0, 0, size};
        c->CmdCopy(cb, s.b, d.b, 1, &cp);
        barrier_all(c, cb);
        double el = 0;
        if (cb_submit(c, cb, &el)) break;
        if (el < best) best = el;
    }
    j_kint("from_type", from); j_kint("to_type", to);
    j_kdbl("gbs", c->fatal ? 0 : gbs((double)size, best));
    j_close('}');
    if (!c->fatal) { rmbuf(c, &s); rmbuf(c, &d); }
}

/* U6 end to end: the GPU writes into and reads from ordinary process memory, the CPU checks it in
 * place (GE-A04/A10). Returns the categorized result; the JSON object records every step. */
static enum mb_res zero_copy(struct ctx *c, VkDeviceSize size, struct pipe *pw, struct pipe *pr, bool has_ext,
                             VkDeviceSize align, PFN_vkGetMemoryHostPointerPropertiesEXT hpp, bool importable,
                             const char *import_why) {
    enum mb_res res = MB_NOT_RUN;
    void *host = NULL;
    struct buf h = {0}, acc = {0}, hacc = {0};
    const char *why = NULL, *step = NULL;
    j_open("zero_copy_host_memory", '{');
    j_kbool("extension_advertised", has_ext);
    if (!has_ext || !hpp) { res = MB_UNSUPPORTED_EXTENSION; step = "extension"; why = "VK_EXT_external_memory_host not available"; goto out; }
    if (!importable) { res = MB_INCOMPATIBLE_MEMORY_TYPE; step = "external buffer properties"; why = import_why; goto out; }
    if (align < 4096) align = 4096;
    if (align & (align - 1)) { res = MB_INCOMPATIBLE_MEMORY_TYPE; step = "alignment"; why = "minImportedHostPointerAlignment is not a power of two"; goto out; }
    size = (size + align - 1) / align * align;
    j_kuint("min_imported_host_pointer_alignment", align);
    j_kuint("bytes", size);
    if (posix_memalign(&host, (size_t)align, (size_t)size) || !host) {
        host = NULL; res = MB_ALLOCATION_FAILED; step = "host allocation"; why = "posix_memalign failed"; goto out;
    }
    int type = -1;
    VkResult r = mkbuf_type(c, &h, size, -1, host, align, hpp, &type, &why);
    if (r) { res = mb_from_vk(r); step = "import host pointer"; j_kstr("vk_result", vkres(r)); goto out; }
    VkMemoryPropertyFlags fl = c->mem.memoryTypes[type].propertyFlags;
    j_kint("imported_memory_type", type);
    j_kuint("imported_memory_flags", fl);
    j_kstr("imported_kind", mem_label(fl));
    j_kbool("host_coherent", type_coherent(c, type));
    /*
     * r3.1 synchronization of the ORIGINAL pointer (audit P1).
     * - Not host-visible: VK_EXT_external_memory_host forbids the application to access the
     *   imported range (or any other mapping of the same pages) while the memory exists, so the
     *   CPU side cannot be checked at all: INCOMPATIBLE_MEMORY_TYPE, and `host` is not touched
     *   until the memory object is destroyed.
     * - Host-coherent: CPU accesses through the original pointer need no flush/invalidate.
     * - Host-visible, not coherent: flush/invalidate are defined for host writes/reads of the
     *   memory object's ranges and need the object to be host mapped. The object is mapped
     *   (mkbuf_type) and the mapping must be the original pages: either vkMapMemory returned the
     *   original pointer, or, on x86-64, a second virtual alias of the same physical pages
     *   (x86 caches are physically tagged, so cache maintenance through the alias covers the
     *   original pointer's lines). The alias is verified both ways before use; any other case is
     *   INCOMPATIBLE_MEMORY_TYPE, never a PASS or FAIL decided by missing synchronization.
     */
    const char *sync = "host-coherent (no flush/invalidate needed)";
    if (!(fl & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)) {
        res = MB_INCOMPATIBLE_MEMORY_TYPE; step = "imported memory type";
        why = "the importable memory type is not host-visible: the host pointer must not be accessed while it is imported";
        goto out;
    }
    if (!type_coherent(c, type)) {
        if (!h.map) { res = MB_INCOMPATIBLE_MEMORY_TYPE; step = "map imported memory"; why = "non-coherent import could not be mapped for flush/invalidate"; goto out; }
        if (h.map == host) {
            sync = "flush/invalidate on the mapping, which is the original pointer";
        } else {
#if defined(__x86_64__) || defined(__i386__)
            /* alias check: CPU-only, both directions, on every page (no GPU work in flight) */
            volatile uint32_t *o = host, *m = h.map;
            bool alias = true;
            for (VkDeviceSize off = 0; off < size / 4 && alias; off += 1024) {
                o[off] = 0xA5A50000u ^ (uint32_t)off;
                if (m[off] != (0xA5A50000u ^ (uint32_t)off)) alias = false;
                m[off] = 0x5A5A0000u ^ (uint32_t)off;
                if (o[off] != (0x5A5A0000u ^ (uint32_t)off)) alias = false;
            }
            j_kbool("mapping_aliases_original_pages", alias);
            if (!alias) { res = MB_INCOMPATIBLE_MEMORY_TYPE; step = "alias check"; why = "vkMapMemory of the import does not alias the original pages"; goto out; }
            sync = "flush/invalidate on the vkMapMemory alias of the original pages (x86-64: physically tagged caches; alias verified)";
#else
            res = MB_INCOMPATIBLE_MEMORY_TYPE; step = "imported memory type";
            why = "non-coherent import mapped at another address: original-pointer synchronization not guaranteed on this architecture";
            goto out;
#endif
        }
    }
    j_kstr("synchronization", sync);
    /* initial contents written after the import, then made available to the device: dirty lines
     * left from before the import can never be written back over GPU results */
    memset(host, 0xFF, size);
    if ((r = host_flush(c, &h, type))) { res = MB_SYNC_FAILED; step = "flush initial contents"; why = vkres(r); goto out; }
    /* 1. GPU writes, CPU reads the same pages (GPU -> CPU without a copy).
     *    The dispatch ends with a compute->host barrier (HOST_READ); the fence wait orders the
     *    CPU after it; non-coherent memory is also invalidated before the CPU reads. */
    uint32_t n = (uint32_t)(size / 4), gx, gy;
    bindbufs(c, pw, &h, NULL);
    grid((n + 63) / 64, &gx, &gy);
    double tw = gpu_dispatch(c, pw, &n, 4, gx, gy, 1);
    if (c->fatal) { res = mb_from_vk(strcmp(c->fatal_why, "VK_TIMEOUT") ? VK_ERROR_DEVICE_LOST : VK_TIMEOUT); step = "gpu write"; goto out; }
    if (tw < 0) { res = MB_SYNC_FAILED; step = "gpu write submit"; goto out; }
    if ((r = host_invalidate(c, &h, type))) { res = MB_SYNC_FAILED; step = "invalidate"; why = vkres(r); goto out; }
    long bad = 0;
    const volatile uint32_t *q = host;
    for (uint32_t i = 0; i < n; i++) bad += q[i] != i;
    j_kdbl("gpu_write_into_host_memory_gbs", tw > 0 ? gbs((double)size, tw) : 0);
    j_kint("gpu_written_words_wrong", bad);
    /* 2. CPU writes, GPU reads the same pages (CPU -> GPU without a copy). Non-coherent memory
     *    is flushed; vkQueueSubmit makes completed host writes visible to the device. */
    uint32_t *w = host, sd = 99;
    uint32_t expect = 0;
    for (uint32_t i = 0; i < n; i++) { w[i] = xs32(&sd) & 0xFFFFu; expect += w[i]; }
    if ((r = host_flush(c, &h, type))) { res = MB_SYNC_FAILED; step = "flush"; why = vkres(r); goto out; }
    if ((r = mkbuf(c, &acc, 8, false)) || (r = mkbuf(c, &hacc, 8, true))) {
        res = mb_from_vk(r); step = "result buffers"; why = vkres(r); goto out;
    }
    bindbufs(c, pr, &h, &acc);
    uint32_t rgx, rgy;
    grid((n + 255) / 256, &rgx, &rgy);
    VkCommandBuffer cb = cb_begin(c);
    if (!cb) { res = MB_ALLOCATION_FAILED; step = "command buffer"; goto out; }
    c->CmdFill(cb, acc.b, 0, 8, 0);
    barrier_all(c, cb);
    c->CmdBindPipe(cb, VK_PIPELINE_BIND_POINT_COMPUTE, pr->p);
    c->CmdBindDS(cb, VK_PIPELINE_BIND_POINT_COMPUTE, pr->pl, 0, 1, &pr->ds, 0, NULL);
    c->CmdPush(cb, pr->pl, VK_SHADER_STAGE_COMPUTE_BIT, 0, 4, &n);
    c->CmdDispatch(cb, rgx, rgy, 1);
    barrier_all(c, cb);
    VkBufferCopy ca = {0, 0, 8};
    c->CmdCopy(cb, acc.b, hacc.b, 1, &ca);
    barrier_all(c, cb);
    double trd = -1;
    r = cb_submit(c, cb, &trd);
    if (r) { res = mb_from_vk(r == VK_TIMEOUT || r == VK_ERROR_DEVICE_LOST ? r : VK_ERROR_UNKNOWN); if (res == MB_FAIL) res = MB_SYNC_FAILED;
             step = "gpu read submit"; why = vkres(r); goto out; }
    /* hacc comes from mkbuf(host=true): HOST_COHERENT, no invalidate needed */
    bool ok_read = ((volatile uint32_t *)hacc.map)[0] == expect;
    j_kbool("gpu_read_of_cpu_written_data_ok", ok_read);
    res = (bad == 0 && ok_read) ? MB_PASS : MB_FAIL;
    if (res == MB_FAIL) step = bad ? "gpu write verified by cpu" : "cpu write verified by gpu";
out:
    j_kstr("result", mb_name[res]);
    if (step) j_kstr("step", step);
    if (why) j_kstr("why", why);
    if (c->fatal) j_kstr("fatal", c->fatal_why);
    j_kstr("note", "PASS means verified end to end on this device: GPU and CPU used the same pages without a staging copy");
    j_close('}');
    /* after a device loss or timeout the GPU may still reference the buffers: leak them on purpose */
    if (!c->fatal) {
        rmbuf(c, &acc); rmbuf(c, &hacc); rmbuf(c, &h);
        free(host);
    }
    return res;
}

/* Can a storage buffer be imported from host memory at all? (vkGetPhysicalDeviceExternalBufferProperties) */
static bool host_import_supported(VkPhysicalDevice pd, const char **why) {
    IF(vkGetPhysicalDeviceExternalBufferProperties);
    if (!vkGetPhysicalDeviceExternalBufferProperties) { *why = "vkGetPhysicalDeviceExternalBufferProperties missing (Vulkan 1.1)"; return false; }
    VkPhysicalDeviceExternalBufferInfo ebi = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_BUFFER_INFO,
        .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT,
        .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT};
    VkExternalBufferProperties ep = {.sType = VK_STRUCTURE_TYPE_EXTERNAL_BUFFER_PROPERTIES};
    vkGetPhysicalDeviceExternalBufferProperties(pd, &ebi, &ep);
    if (!(ep.externalMemoryProperties.externalMemoryFeatures & VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT)) {
        *why = "host allocations are not importable for storage buffers on this device";
        return false;
    }
    return true;
}

static int run_membench(VkPhysicalDevice pd, uint32_t devidx, uint64_t timeout_ms, unsigned size_mb) {
    struct ctx c = {.pd = pd, .timeout_ns = timeout_ms * 1000000ull};
    IF(vkGetPhysicalDeviceQueueFamilyProperties);
    IF(vkGetPhysicalDeviceMemoryProperties);
    IF(vkCreateDevice);
    IF(vkGetDeviceProcAddr);
    IF(vkGetPhysicalDeviceProperties2);
    vkGetPhysicalDeviceMemoryProperties(pd, &c.mem);
    uint32_t qn = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, NULL);
    VkQueueFamilyProperties q[16];
    if (qn > 16) qn = 16;
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, q);
    int fam = -1;
    for (uint32_t i = 0; fam < 0 && i < qn; i++) if (q[i].queueFlags & VK_QUEUE_COMPUTE_BIT) fam = (int)i;
    bool has_host = has_dev_ext(pd, VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME);
    VkPhysicalDeviceExternalMemoryHostPropertiesEXT hostp = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_MEMORY_HOST_PROPERTIES_EXT};
    VkPhysicalDeviceProperties2 p2 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = has_host ? &hostp : NULL};
    vkGetPhysicalDeviceProperties2(pd, &p2);
    j_open("membench", '{');
    j_kint("device_index", devidx);
    j_kstr("device", p2.properties.deviceName);
    j_kuint("buffer_bytes", (unsigned long long)size_mb << 20);
    j_kstr("timing", "best of 4, wall clock around submit+fence (includes submit overhead, a lower bound on bandwidth)");
    if (fam < 0) { j_kstr("error", "no compute queue"); j_kstr("result", "NOT_RUN"); j_close('}'); return 2; }
    c.qfam = (uint32_t)fam;
    float prio = 1.0f;
    const char *exts[1] = {VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME};
    VkDeviceQueueCreateInfo qci = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = c.qfam, .queueCount = 1, .pQueuePriorities = &prio};
    VkDeviceCreateInfo dci = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1, .pQueueCreateInfos = &qci,
                              .enabledExtensionCount = has_host ? 1 : 0, .ppEnabledExtensionNames = exts};
    VkResult r = vkCreateDevice(pd, &dci, NULL, &c.dev);
    if (r) { j_kstr("error", vkres(r)); j_kstr("result", "NOT_RUN"); j_close('}'); return 2; }
    load_dev_fns(&c, vkGetDeviceProcAddr);
    PFN_vkGetMemoryHostPointerPropertiesEXT hpp = has_host ?
        (PFN_vkGetMemoryHostPointerPropertiesEXT)vkGetDeviceProcAddr(c.dev, "vkGetMemoryHostPointerPropertiesEXT") : NULL;
    PFN_vkGetDeviceQueue gdq = (PFN_vkGetDeviceQueue)vkGetDeviceProcAddr(c.dev, "vkGetDeviceQueue");
    PFN_vkCreateCommandPool ccp = (PFN_vkCreateCommandPool)vkGetDeviceProcAddr(c.dev, "vkCreateCommandPool");
    gdq(c.dev, c.qfam, 0, &c.queue);
    VkCommandPoolCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, .queueFamilyIndex = c.qfam,
                                   .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT};
    ccp(c.dev, &pci, NULL, &c.pool);
    struct pipe pw = {0}, pr = {0}, ps = {0};
    if (mkpipe(&c, &pw, spv_index_write, spv_index_write_size, 1, 4) || mkpipe(&c, &pr, spv_reduce, spv_reduce_size, 2, 4) ||
        mkpipe(&c, &ps, spv_saxpy_u32, spv_saxpy_u32_size, 2, 8)) {
        j_kstr("error", "pipeline creation failed"); j_kstr("result", "NOT_RUN"); j_close('}');
        rmpipe(&c, &pw); rmpipe(&c, &pr); rmpipe(&c, &ps);
        PFN_vkDestroyCommandPool dcp0 = (PFN_vkDestroyCommandPool)vkGetDeviceProcAddr(c.dev, "vkDestroyCommandPool");
        PFN_vkDestroyDevice dd0 = (PFN_vkDestroyDevice)vkGetDeviceProcAddr(c.dev, "vkDestroyDevice");
        dcp0(c.dev, c.pool, NULL);
        dd0(c.dev, NULL);
        return 2;
    }
    VkDeviceSize size = (VkDeviceSize)size_mb << 20;
    /* one representative memory type per kind (the first of each) */
    int seen_vram = -1, seen_vram_vis = -1, seen_wc = -1, seen_cached = -1;
    j_open("memory_types", '[');
    for (uint32_t t = 0; t < c.mem.memoryTypeCount && !c.fatal; t++) {
        VkMemoryPropertyFlags f = c.mem.memoryTypes[t].propertyFlags;
        if (f & (VK_MEMORY_PROPERTY_LAZILY_ALLOCATED_BIT | VK_MEMORY_PROPERTY_PROTECTED_BIT)) continue;
        if (f & 0x40 /* VK_MEMORY_PROPERTY_DEVICE_COHERENT_BIT_AMD */) continue;
        int *slot = (f & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) ? ((f & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT) ? &seen_vram_vis : &seen_vram)
                  : (f & VK_MEMORY_PROPERTY_HOST_CACHED_BIT) ? &seen_cached
                  : (f & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT) ? &seen_wc : NULL;
        if (!slot || *slot >= 0) continue;
        *slot = (int)t;
        bench_type(&c, (int)t, size, &pw, &pr, &ps);
    }
    j_close(']');
    j_open("copies", '[');
    if (!c.fatal) bench_copy(&c, "cpu-visible gtt (write-combined) -> vram", seen_wc, seen_vram >= 0 ? seen_vram : seen_vram_vis, size);
    if (!c.fatal) bench_copy(&c, "vram -> gtt cached (readback)", seen_vram >= 0 ? seen_vram : seen_vram_vis, seen_cached, size);
    if (!c.fatal) bench_copy(&c, "gtt write-combined -> gtt cached", seen_wc, seen_cached, size);
    j_close(']');
    enum mb_res zc = MB_NOT_RUN;
    if (!c.fatal) {
        const char *iw = NULL;
        bool importable = has_host && host_import_supported(pd, &iw);
        zc = zero_copy(&c, size / 4, &pw, &pr, has_host, hostp.minImportedHostPointerAlignment, hpp, importable, iw);
    } else {
        zc = strcmp(c.fatal_why, "VK_TIMEOUT") ? MB_DEVICE_LOST : MB_TIMEOUT;
    }
    j_kbool("fatal", c.fatal);
    if (c.fatal) j_kstr("fatal_reason", c.fatal_why);
    /* one overall result for --membench: the zero-copy verdict (bandwidth numbers are measurements) */
    j_kstr("result", mb_name[zc]);
    j_kint("result_exit_code", mb_exit[zc]);
    j_close('}');
    if (!c.fatal) {
        c.DeviceWaitIdle(c.dev);
        rmpipe(&c, &pw); rmpipe(&c, &pr); rmpipe(&c, &ps);
        PFN_vkDestroyCommandPool dcp = (PFN_vkDestroyCommandPool)vkGetDeviceProcAddr(c.dev, "vkDestroyCommandPool");
        PFN_vkDestroyDevice dd = (PFN_vkDestroyDevice)vkGetDeviceProcAddr(c.dev, "vkDestroyDevice");
        dcp(c.dev, c.pool, NULL);
        dd(c.dev, NULL);
    }
    return mb_exit[zc];
}

/* ------------------------------------------------------------------ Vulkan raster */
#ifdef ORBIS_HAVE_RASTER_SPV
struct rctx {
    VkDevice dev; VkQueue q; VkCommandPool pool; VkPhysicalDeviceMemoryProperties mem; uint64_t timeout_ns;
    bool fatal; const char *why;
};
#define RD(name) PFN_##name name = (PFN_##name)gdpa(r->dev, #name)
static int rfind(struct rctx *r, uint32_t bits, VkMemoryPropertyFlags want) {
    for (uint32_t i = 0; i < r->mem.memoryTypeCount; i++)
        if ((bits & (1u << i)) && (r->mem.memoryTypes[i].propertyFlags & want) == want) return (int)i;
    return -1;
}

/* One render: 256x256 RGBA8, optional depth. Draw list: {depth, x0, x1, mode}. Returns pixels in out. */
static VkResult raster_run(struct rctx *r, PFN_vkGetDeviceProcAddr gdpa, VkFormat dfmt, const float (*draws)[4],
                           int ndraws, uint8_t *out, double *gpu_s) {
    enum { W = 256, H = 256 };
    RD(vkCreateImage); RD(vkGetImageMemoryRequirements); RD(vkAllocateMemory); RD(vkBindImageMemory);
    RD(vkCreateImageView); RD(vkCreateRenderPass); RD(vkCreateFramebuffer); RD(vkCreateShaderModule);
    RD(vkCreatePipelineLayout); RD(vkCreateGraphicsPipelines); RD(vkCreateBuffer); RD(vkGetBufferMemoryRequirements);
    RD(vkBindBufferMemory); RD(vkMapMemory); RD(vkAllocateCommandBuffers); RD(vkBeginCommandBuffer);
    RD(vkCmdBeginRenderPass); RD(vkCmdBindPipeline); RD(vkCmdPushConstants); RD(vkCmdDraw); RD(vkCmdEndRenderPass);
    RD(vkCmdCopyImageToBuffer); RD(vkEndCommandBuffer); RD(vkCreateFence); RD(vkQueueSubmit); RD(vkWaitForFences);
    RD(vkDestroyImage); RD(vkFreeMemory); RD(vkDestroyImageView); RD(vkDestroyRenderPass); RD(vkDestroyFramebuffer);
    RD(vkDestroyShaderModule); RD(vkDestroyPipelineLayout); RD(vkDestroyPipeline); RD(vkDestroyBuffer); RD(vkDestroyFence);
    RD(vkCmdPipelineBarrier);
    bool depth = dfmt != VK_FORMAT_UNDEFINED;
    VkResult res;
    VkImage img = 0, dimg = 0; VkDeviceMemory imem = 0, dmem = 0, bmem = 0; VkImageView iv = 0, dv = 0;
    VkRenderPass rp = 0; VkFramebuffer fb = 0; VkShaderModule vsm = 0, fsm = 0; VkPipelineLayout pl = 0;
    VkPipeline pipe = 0; VkBuffer buf = 0; VkFence fence = 0; void *map = NULL;
#define CK(x) do { if ((res = (x)) != VK_SUCCESS) goto done; } while (0)
    VkImageCreateInfo ici = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D,
        .format = VK_FORMAT_R8G8B8A8_UNORM, .extent = {W, H, 1}, .mipLevels = 1, .arrayLayers = 1,
        .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL,
        .usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
        .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED};
    CK(vkCreateImage(r->dev, &ici, NULL, &img));
    VkMemoryRequirements mr; vkGetImageMemoryRequirements(r->dev, img, &mr);
    VkMemoryAllocateInfo mai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = mr.size,
                                .memoryTypeIndex = (uint32_t)rfind(r, mr.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)};
    CK(vkAllocateMemory(r->dev, &mai, NULL, &imem)); CK(vkBindImageMemory(r->dev, img, imem, 0));
    VkImageViewCreateInfo vci = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = img, .viewType = VK_IMAGE_VIEW_TYPE_2D,
        .format = VK_FORMAT_R8G8B8A8_UNORM, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    CK(vkCreateImageView(r->dev, &vci, NULL, &iv));
    if (depth) {
        VkImageCreateInfo dci = ici; dci.format = dfmt; dci.usage = VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
        CK(vkCreateImage(r->dev, &dci, NULL, &dimg));
        vkGetImageMemoryRequirements(r->dev, dimg, &mr);
        VkMemoryAllocateInfo dai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = mr.size,
                                    .memoryTypeIndex = (uint32_t)rfind(r, mr.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)};
        CK(vkAllocateMemory(r->dev, &dai, NULL, &dmem)); CK(vkBindImageMemory(r->dev, dimg, dmem, 0));
        VkImageViewCreateInfo dvci = vci; dvci.image = dimg; dvci.format = dfmt;
        dvci.subresourceRange.aspectMask = VK_IMAGE_ASPECT_DEPTH_BIT;
        CK(vkCreateImageView(r->dev, &dvci, NULL, &dv));
    }
    VkAttachmentDescription att[2] = {
        {.format = VK_FORMAT_R8G8B8A8_UNORM, .samples = VK_SAMPLE_COUNT_1_BIT, .loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR,
         .storeOp = VK_ATTACHMENT_STORE_OP_STORE, .stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE,
         .stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE, .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED,
         .finalLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL},
        {.format = dfmt, .samples = VK_SAMPLE_COUNT_1_BIT, .loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR,
         .storeOp = VK_ATTACHMENT_STORE_OP_DONT_CARE, .stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE,
         .stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE, .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED,
         .finalLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL}};
    VkAttachmentReference cref = {0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkAttachmentReference dref = {1, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL};
    VkSubpassDescription sp = {.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS, .colorAttachmentCount = 1,
                               .pColorAttachments = &cref, .pDepthStencilAttachment = depth ? &dref : NULL};
    VkRenderPassCreateInfo rpi = {.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO, .attachmentCount = depth ? 2u : 1u,
                                  .pAttachments = att, .subpassCount = 1, .pSubpasses = &sp};
    CK(vkCreateRenderPass(r->dev, &rpi, NULL, &rp));
    VkImageView views[2] = {iv, dv};
    VkFramebufferCreateInfo fbi = {.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO, .renderPass = rp,
                                   .attachmentCount = depth ? 2u : 1u, .pAttachments = views, .width = W, .height = H, .layers = 1};
    CK(vkCreateFramebuffer(r->dev, &fbi, NULL, &fb));
    VkShaderModuleCreateInfo smv = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = spv_raster_vert_size, .pCode = spv_raster_vert};
    VkShaderModuleCreateInfo smf = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = spv_raster_frag_size, .pCode = spv_raster_frag};
    CK(vkCreateShaderModule(r->dev, &smv, NULL, &vsm)); CK(vkCreateShaderModule(r->dev, &smf, NULL, &fsm));
    VkPushConstantRange pcr = {VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT, 0, 16};
    VkPipelineLayoutCreateInfo pli = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .pushConstantRangeCount = 1, .pPushConstantRanges = &pcr};
    CK(vkCreatePipelineLayout(r->dev, &pli, NULL, &pl));
    VkPipelineShaderStageCreateInfo st[2] = {
        {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_VERTEX_BIT, .module = vsm, .pName = "main"},
        {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_FRAGMENT_BIT, .module = fsm, .pName = "main"}};
    VkPipelineVertexInputStateCreateInfo vi = {.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO};
    VkPipelineInputAssemblyStateCreateInfo ia = {.sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO,
                                                 .topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST};
    VkViewport vp = {0, 0, W, H, 0, 1}; VkRect2D sc = {{0, 0}, {W, H}};
    VkPipelineViewportStateCreateInfo vps = {.sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO,
                                             .viewportCount = 1, .pViewports = &vp, .scissorCount = 1, .pScissors = &sc};
    VkPipelineRasterizationStateCreateInfo rs = {.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO,
        .polygonMode = VK_POLYGON_MODE_FILL, .cullMode = VK_CULL_MODE_NONE, .frontFace = VK_FRONT_FACE_COUNTER_CLOCKWISE, .lineWidth = 1};
    VkPipelineMultisampleStateCreateInfo ms = {.sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO,
                                               .rasterizationSamples = VK_SAMPLE_COUNT_1_BIT};
    VkPipelineDepthStencilStateCreateInfo ds = {.sType = VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO,
        .depthTestEnable = depth, .depthWriteEnable = depth, .depthCompareOp = VK_COMPARE_OP_LESS};
    VkPipelineColorBlendAttachmentState cba = {.colorWriteMask = 0xF};
    VkPipelineColorBlendStateCreateInfo cb = {.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO,
                                              .attachmentCount = 1, .pAttachments = &cba};
    VkGraphicsPipelineCreateInfo gpi = {.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO, .stageCount = 2, .pStages = st,
        .pVertexInputState = &vi, .pInputAssemblyState = &ia, .pViewportState = &vps, .pRasterizationState = &rs,
        .pMultisampleState = &ms, .pDepthStencilState = depth ? &ds : NULL, .pColorBlendState = &cb, .layout = pl, .renderPass = rp};
    CK(vkCreateGraphicsPipelines(r->dev, VK_NULL_HANDLE, 1, &gpi, NULL, &pipe));
    VkBufferCreateInfo bci = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = W * H * 4, .usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT};
    CK(vkCreateBuffer(r->dev, &bci, NULL, &buf));
    VkMemoryRequirements bmr; vkGetBufferMemoryRequirements(r->dev, buf, &bmr);
    VkMemoryAllocateInfo bai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = bmr.size,
        .memoryTypeIndex = (uint32_t)rfind(r, bmr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)};
    CK(vkAllocateMemory(r->dev, &bai, NULL, &bmem)); CK(vkBindBufferMemory(r->dev, buf, bmem, 0));
    CK(vkMapMemory(r->dev, bmem, 0, VK_WHOLE_SIZE, 0, &map));
    VkCommandBufferAllocateInfo cai = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = r->pool,
                                       .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    VkCommandBuffer cmd; CK(vkAllocateCommandBuffers(r->dev, &cai, &cmd));
    VkCommandBufferBeginInfo cbi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    vkBeginCommandBuffer(cmd, &cbi);
    VkClearValue cv[2] = {{.color = {{0.f, 0.f, 1.f, 1.f}}}, {.depthStencil = {1.f, 0}}};
    VkRenderPassBeginInfo rbi = {.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO, .renderPass = rp, .framebuffer = fb,
                                 .renderArea = {{0, 0}, {W, H}}, .clearValueCount = depth ? 2u : 1u, .pClearValues = cv};
    vkCmdBeginRenderPass(cmd, &rbi, VK_SUBPASS_CONTENTS_INLINE);
    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pipe);
    for (int i = 0; i < ndraws; i++) {
        vkCmdPushConstants(cmd, pl, VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT, 0, 16, draws[i]);
        vkCmdDraw(cmd, 6, 1, 0, 0);
    }
    vkCmdEndRenderPass(cmd);
    VkBufferImageCopy bic = {.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, .imageExtent = {W, H, 1}};
    vkCmdCopyImageToBuffer(cmd, img, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, buf, 1, &bic);
    VkMemoryBarrier hb = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
                          .dstAccessMask = VK_ACCESS_HOST_READ_BIT};
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &hb, 0, NULL, 0, NULL);
    vkEndCommandBuffer(cmd);
    VkFenceCreateInfo fci = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    CK(vkCreateFence(r->dev, &fci, NULL, &fence));
    VkSubmitInfo si = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &cmd};
    double t0 = now_s();
    CK(vkQueueSubmit(r->q, 1, &si, fence));
    res = vkWaitForFences(r->dev, 1, &fence, VK_TRUE, r->timeout_ns);
    *gpu_s = now_s() - t0;
    if (res == VK_TIMEOUT || res == VK_ERROR_DEVICE_LOST) { r->fatal = true; r->why = vkres(res); return res; }
    if (res == VK_SUCCESS) memcpy(out, map, W * H * 4);
done:
    if (fence) vkDestroyFence(r->dev, fence, NULL);
    if (buf) vkDestroyBuffer(r->dev, buf, NULL);
    if (bmem) vkFreeMemory(r->dev, bmem, NULL);
    if (pipe) vkDestroyPipeline(r->dev, pipe, NULL);
    if (pl) vkDestroyPipelineLayout(r->dev, pl, NULL);
    if (vsm) vkDestroyShaderModule(r->dev, vsm, NULL);
    if (fsm) vkDestroyShaderModule(r->dev, fsm, NULL);
    if (fb) vkDestroyFramebuffer(r->dev, fb, NULL);
    if (rp) vkDestroyRenderPass(r->dev, rp, NULL);
    if (iv) vkDestroyImageView(r->dev, iv, NULL);
    if (dv) vkDestroyImageView(r->dev, dv, NULL);
    if (img) vkDestroyImage(r->dev, img, NULL);
    if (dimg) vkDestroyImage(r->dev, dimg, NULL);
    if (imem) vkFreeMemory(r->dev, imem, NULL);
    if (dmem) vkFreeMemory(r->dev, dmem, NULL);
    return res;
#undef CK
}

static int grad_ok(const uint8_t *q, int x, int y) {
    int er = (int)((x + 0.5) / 256.0 * 255.0 + 0.5), eg = (int)((y + 0.5) / 256.0 * 255.0 + 0.5);
    return abs(q[0] - er) <= 1 && abs(q[1] - eg) <= 1 && q[2] <= 1;
}

static int run_raster(VkPhysicalDevice pd, uint32_t devidx, uint64_t timeout_ms) {
    IF(vkCreateDevice); IF(vkGetDeviceProcAddr); IF(vkGetPhysicalDeviceQueueFamilyProperties);
    IF(vkGetPhysicalDeviceMemoryProperties); IF(vkGetPhysicalDeviceFormatProperties);
    struct rctx rc = {.timeout_ns = timeout_ms * 1000000ull};
    j_open("raster", '{');
    j_kint("device_index", devidx);
    uint32_t qn = 0; VkQueueFamilyProperties qf[16];
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, NULL); if (qn > 16) qn = 16;
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, qf);
    int fam = -1;
    for (uint32_t i = 0; i < qn && fam < 0; i++) if (qf[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) fam = (int)i;
    if (fam < 0) { j_kstr("error", "no graphics queue family"); j_close('}'); return 1; }
    vkGetPhysicalDeviceMemoryProperties(pd, &rc.mem);
    float prio = 1.f;
    VkDeviceQueueCreateInfo qci = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = (uint32_t)fam, .queueCount = 1, .pQueuePriorities = &prio};
    VkDeviceCreateInfo dci = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1, .pQueueCreateInfos = &qci};
    VkResult res = vkCreateDevice(pd, &dci, NULL, &rc.dev);
    if (res) { j_kstr("error", vkres(res)); j_close('}'); return 1; }
    PFN_vkGetDeviceProcAddr gdpa = vkGetDeviceProcAddr;
    PFN_vkGetDeviceQueue gdq = (PFN_vkGetDeviceQueue)gdpa(rc.dev, "vkGetDeviceQueue");
    PFN_vkCreateCommandPool ccp = (PFN_vkCreateCommandPool)gdpa(rc.dev, "vkCreateCommandPool");
    gdq(rc.dev, (uint32_t)fam, 0, &rc.q);
    VkCommandPoolCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, .queueFamilyIndex = (uint32_t)fam};
    ccp(rc.dev, &pci, NULL, &rc.pool);
    static uint8_t px[256 * 256 * 4];
    int rcode = 0;
    double gs = 0;
    j_open("tests", '[');
    /* test 1: gradient on the left half, clear colour on the right */
    {
        const float d[1][4] = {{0.5f, -1.f, 0.f, 0.f}};
        memset(px, 0, sizeof px);
        res = raster_run(&rc, gdpa, VK_FORMAT_UNDEFINED, d, 1, px, &gs);
        long wrong = 0;
        if (res == VK_SUCCESS)
            for (int y = 0; y < 256; y++) for (int x = 0; x < 256; x++) {
                const uint8_t *q = &px[(y * 256 + x) * 4];
                bool ok = x < 128 ? grad_ok(q, x, y) : (q[0] <= 1 && q[1] <= 1 && q[2] >= 254);
                wrong += !ok;
            }
        result("raster_gradient", res == VK_SUCCESS && wrong == 0, gs, "%s wrong_px=%ld", vkres(res), res == VK_SUCCESS ? wrong : -1L);
        if (res != VK_SUCCESS || wrong) rcode = 1;
    }
    /* test 2: depth. green at z=0.5 everywhere, gradient at z=0.75 on the left (hidden),
     * gradient at z=0.25 on the right (visible). */
    if (!rc.fatal) {
        VkFormat cands[] = {VK_FORMAT_D32_SFLOAT, VK_FORMAT_D16_UNORM, VK_FORMAT_D24_UNORM_S8_UINT};
        VkFormat df = VK_FORMAT_UNDEFINED;
        for (unsigned i = 0; i < 3 && df == VK_FORMAT_UNDEFINED; i++) {
            VkFormatProperties fp; vkGetPhysicalDeviceFormatProperties(pd, cands[i], &fp);
            if (fp.optimalTilingFeatures & VK_FORMAT_FEATURE_DEPTH_STENCIL_ATTACHMENT_BIT) df = cands[i];
        }
        if (df == VK_FORMAT_UNDEFINED) {
            result("raster_depth", false, 0, "no depth attachment format"); rcode = 1;
        } else {
            const float d[3][4] = {{0.5f, -1.f, 1.f, 1.f}, {0.75f, -1.f, 0.f, 0.f}, {0.25f, 0.f, 1.f, 0.f}};
            memset(px, 0, sizeof px);
            res = raster_run(&rc, gdpa, df, d, 3, px, &gs);
            long wrong = 0;
            if (res == VK_SUCCESS)
                for (int y = 0; y < 256; y++) for (int x = 0; x < 256; x++) {
                    const uint8_t *q = &px[(y * 256 + x) * 4];
                    bool ok = x < 128 ? (q[0] <= 1 && q[1] >= 254 && q[2] <= 1) : grad_ok(q, x, y);
                    wrong += !ok;
                }
            result("raster_depth", res == VK_SUCCESS && wrong == 0, gs, "%s format=%d wrong_px=%ld", vkres(res), (int)df,
                   res == VK_SUCCESS ? wrong : -1L);
            if (res != VK_SUCCESS || wrong) rcode = 1;
        }
    }
    j_close(']');
    j_kbool("fatal", rc.fatal);
    if (rc.fatal) { j_kstr("fatal_reason", rc.why); j_close('}'); return 3; }
    PFN_vkDeviceWaitIdle wi = (PFN_vkDeviceWaitIdle)gdpa(rc.dev, "vkDeviceWaitIdle");
    PFN_vkDestroyCommandPool dcp = (PFN_vkDestroyCommandPool)gdpa(rc.dev, "vkDestroyCommandPool");
    PFN_vkDestroyDevice dd = (PFN_vkDestroyDevice)gdpa(rc.dev, "vkDestroyDevice");
    wi(rc.dev); dcp(rc.dev, rc.pool, NULL); dd(rc.dev, NULL);
    j_close('}');
    return rcode;
}
#else
static int run_raster(VkPhysicalDevice pd, uint32_t devidx, uint64_t timeout_ms) {
    (void)pd; (void)timeout_ms;
    j_open("raster", '{');
    j_kint("device_index", devidx);
    j_kstr("not_run", "built without raster shaders (shaders_raster_spv.h; run gen_spv_header.py)");
    j_close('}');
    return 0;
}
#endif

/* ------------------------------------------------------------------ pipeline cache timing */
static void t_pipeline_cache(struct ctx *c) {
    PFN_vkCreatePipelineCache cpc = (PFN_vkCreatePipelineCache)gipa(inst, "vkCreatePipelineCache");
    PFN_vkDestroyPipelineCache dpc = (PFN_vkDestroyPipelineCache)gipa(inst, "vkDestroyPipelineCache");
    if (!cpc || !dpc) { result("pipeline_cache", false, 0, "vkCreatePipelineCache unavailable"); return; }
    VkPipelineCacheCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO};
    VkPipelineCache cache;
    if (cpc(c->dev, &pci, NULL, &cache)) { result("pipeline_cache", false, 0, "cache creation failed"); return; }
    enum { N = 10 };
    double cold[N], warm[N];
    VkShaderModuleCreateInfo smi = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = spv_reduce_size, .pCode = spv_reduce};
    VkDescriptorSetLayoutBinding b[2] = {{0, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL},
                                         {1, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL}};
    VkDescriptorSetLayoutCreateInfo dli = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO, .bindingCount = 2, .pBindings = b};
    VkDescriptorSetLayout dsl; c->CreateDSL(c->dev, &dli, NULL, &dsl);
    VkPushConstantRange pcr = {VK_SHADER_STAGE_COMPUTE_BIT, 0, 4};
    VkPipelineLayoutCreateInfo pli = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1, .pSetLayouts = &dsl,
                                      .pushConstantRangeCount = 1, .pPushConstantRanges = &pcr};
    VkPipelineLayout pl; c->CreatePL(c->dev, &pli, NULL, &pl);
    bool ok = true;
    for (int pass = 0; pass < 2 && ok; pass++)
        for (int i = 0; i < N && ok; i++) {
            VkShaderModule sm; c->CreateSM(c->dev, &smi, NULL, &sm);
            VkComputePipelineCreateInfo cpi = {.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
                .stage = {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_COMPUTE_BIT, .module = sm, .pName = "main"},
                .layout = pl};
            VkPipeline p;
            double t0 = now_s();
            ok = c->CreateCP(c->dev, pass ? cache : VK_NULL_HANDLE, 1, &cpi, NULL, &p) == VK_SUCCESS;
            (pass ? warm : cold)[i] = (now_s() - t0) * 1000.0;
            if (ok) c->DestroyPipe(c->dev, p, NULL);
            c->DestroySM(c->dev, sm, NULL);
        }
    double sc = 0, sw = 0;
    for (int i = 1; i < N; i++) { sc += cold[i]; sw += warm[i]; }
    result("pipeline_cache", ok, 0, "first_ms=%.3f no_cache_mean_ms=%.3f cache_mean_ms=%.3f (set MESA_SHADER_CACHE_DISABLE=true for a true cold measurement)",
           cold[0], sc / (N - 1), sw / (N - 1));
    c->DestroyPL(c->dev, pl, NULL); c->DestroyDSL(c->dev, dsl, NULL); dpc(c->dev, cache, NULL);
}

/* ------------------------------------------------------------------ OpenCL */
typedef int32_t cl_int; typedef uint32_t cl_uint; typedef uint64_t cl_ulong; typedef cl_ulong cl_bitfield;
typedef void *cl_platform_id, *cl_device_id, *cl_context, *cl_command_queue, *cl_mem, *cl_program, *cl_kernel, *cl_event;
#define CL_DEVICE_TYPE_ALL 0xFFFFFFFF
#define CL_PLATFORM_NAME 0x0902
#define CL_PLATFORM_VERSION 0x0901
#define CL_DEVICE_NAME 0x102B
#define CL_DEVICE_VERSION 0x102F
#define CL_DRIVER_VERSION 0x102D
#define CL_DEVICE_TYPE 0x1000
#define CL_MEM_READ_WRITE (1 << 0)
#define CL_MEM_COPY_HOST_PTR (1 << 5)
#define CL_TRUE 1
#define CL_PROGRAM_BUILD_LOG 0x1183

static int probe_opencl(void) {
    j_open("opencl", '{');
    void *cl = dlopen("libOpenCL.so.1", RTLD_NOW | RTLD_LOCAL);
    if (!cl) { j_kstr("error", "libOpenCL.so.1 not found (install ocl-icd)"); j_close('}'); return 2; }
#define CLF(ret, name, ...) ret (*name)(__VA_ARGS__) = (ret (*)(__VA_ARGS__))dlsym(cl, #name)
    CLF(cl_int, clGetPlatformIDs, cl_uint, cl_platform_id *, cl_uint *);
    CLF(cl_int, clGetPlatformInfo, cl_platform_id, cl_uint, size_t, void *, size_t *);
    CLF(cl_int, clGetDeviceIDs, cl_platform_id, cl_bitfield, cl_uint, cl_device_id *, cl_uint *);
    CLF(cl_int, clGetDeviceInfo, cl_device_id, cl_uint, size_t, void *, size_t *);
    CLF(cl_context, clCreateContext, const intptr_t *, cl_uint, const cl_device_id *, void *, void *, cl_int *);
    CLF(cl_command_queue, clCreateCommandQueue, cl_context, cl_device_id, cl_bitfield, cl_int *);
    CLF(cl_mem, clCreateBuffer, cl_context, cl_bitfield, size_t, void *, cl_int *);
    CLF(cl_program, clCreateProgramWithSource, cl_context, cl_uint, const char **, const size_t *, cl_int *);
    CLF(cl_int, clBuildProgram, cl_program, cl_uint, const cl_device_id *, const char *, void *, void *);
    CLF(cl_int, clGetProgramBuildInfo, cl_program, cl_device_id, cl_uint, size_t, void *, size_t *);
    CLF(cl_kernel, clCreateKernel, cl_program, const char *, cl_int *);
    CLF(cl_int, clSetKernelArg, cl_kernel, cl_uint, size_t, const void *);
    CLF(cl_int, clEnqueueNDRangeKernel, cl_command_queue, cl_kernel, cl_uint, const size_t *, const size_t *, const size_t *, cl_uint, const void *, void *);
    CLF(cl_int, clEnqueueReadBuffer, cl_command_queue, cl_mem, cl_uint, size_t, size_t, void *, cl_uint, const void *, void *);
    CLF(cl_int, clFinish, cl_command_queue);
    if (!clGetPlatformIDs || !clGetDeviceIDs || !clCreateContext || !clEnqueueNDRangeKernel) {
        j_kstr("error", "libOpenCL lacks core entry points"); j_close('}'); return 2;
    }
    cl_platform_id plats[8]; cl_uint np = 0;
    cl_int e = clGetPlatformIDs(8, plats, &np);
    j_kint("platform_count", e ? 0 : np);
    if (e || !np) { j_kint("cl_error", e); j_kstr("error", "no OpenCL platform (for RustiCL set RUSTICL_ENABLE=radeonsi)"); j_close('}'); return 2; }
    int rc = 2, tested = 0;
    j_open("platforms", '[');
    for (cl_uint i = 0; i < np && i < 8; i++) {
        char name[256] = "", ver[256] = "";
        clGetPlatformInfo(plats[i], CL_PLATFORM_NAME, sizeof name, name, NULL);
        clGetPlatformInfo(plats[i], CL_PLATFORM_VERSION, sizeof ver, ver, NULL);
        j_open(NULL, '{'); j_kstr("name", name); j_kstr("version", ver);
        cl_device_id devs[8]; cl_uint nd = 0;
        clGetDeviceIDs(plats[i], CL_DEVICE_TYPE_ALL, 8, devs, &nd);
        j_open("devices", '[');
        for (cl_uint d = 0; d < nd && d < 8; d++) {
            char dn[256] = "", dv[256] = "", drv[256] = ""; cl_ulong type = 0;
            clGetDeviceInfo(devs[d], CL_DEVICE_NAME, sizeof dn, dn, NULL);
            clGetDeviceInfo(devs[d], CL_DEVICE_VERSION, sizeof dv, dv, NULL);
            clGetDeviceInfo(devs[d], CL_DRIVER_VERSION, sizeof drv, drv, NULL);
            clGetDeviceInfo(devs[d], CL_DEVICE_TYPE, sizeof type, &type, NULL);
            j_open(NULL, '{'); j_kstr("name", dn); j_kstr("version", dv); j_kstr("driver", drv); j_kuint("type", type);
            /* integer kernel: y[i] = a*x[i] + y[i] + i, exact; checked against the CPU */
            enum { N = 1 << 20 };
            static const char *src = "__kernel void k(__global uint *x, __global uint *y, uint a) {"
                                     " uint i = get_global_id(0); y[i] = a * x[i] + y[i] + i; }";
            cl_int err = 0;
            cl_context ctx = clCreateContext(NULL, 1, &devs[d], NULL, NULL, &err);
            if (!ctx) { j_kint("cl_error", err); j_kstr("test", "context failed"); j_close('}'); continue; }
            cl_command_queue q = clCreateCommandQueue(ctx, devs[d], 0, &err);
            uint32_t *hx = malloc(N * 4), *hy = malloc(N * 4), *ho = malloc(N * 4), s = 99;
            for (uint32_t k = 0; k < N; k++) { hx[k] = xs32(&s); hy[k] = xs32(&s); }
            cl_mem bx = clCreateBuffer(ctx, CL_MEM_READ_WRITE | CL_MEM_COPY_HOST_PTR, N * 4, hx, &err);
            cl_mem by = clCreateBuffer(ctx, CL_MEM_READ_WRITE | CL_MEM_COPY_HOST_PTR, N * 4, hy, &err);
            cl_program prog = clCreateProgramWithSource(ctx, 1, &src, NULL, &err);
            double t0 = now_s();
            err = clBuildProgram(prog, 1, &devs[d], "", NULL, NULL);
            double build_s = now_s() - t0;
            if (err) {
                char log[1024] = ""; clGetProgramBuildInfo(prog, devs[d], CL_PROGRAM_BUILD_LOG, sizeof log, log, NULL);
                j_kint("cl_error", err); j_kstr("build_log", log); j_kbool("ok", false);
            } else {
                cl_kernel k = clCreateKernel(prog, "k", &err);
                uint32_t a = 2654435761u; size_t gws = N;
                clSetKernelArg(k, 0, sizeof bx, &bx); clSetKernelArg(k, 1, sizeof by, &by); clSetKernelArg(k, 2, 4, &a);
                t0 = now_s();
                err = clEnqueueNDRangeKernel(q, k, 1, NULL, &gws, NULL, 0, NULL, NULL);
                if (!err) err = clEnqueueReadBuffer(q, by, CL_TRUE, 0, N * 4, ho, 0, NULL, NULL);
                clFinish(q);
                double run_s = now_s() - t0;
                long wrong = 0;
                for (uint32_t k2 = 0; !err && k2 < N; k2++) wrong += ho[k2] != a * hx[k2] + hy[k2] + k2;
                j_kint("cl_error", err); j_kint("wrong", err ? -1 : wrong);
                j_kdbl("build_s", build_s); j_kdbl("run_s", run_s);
                j_kbool("ok", !err && !wrong);
                tested++;
                if (!err && !wrong) { if (rc) rc = 0; } else rc = 1;
            }
            free(hx); free(hy); free(ho);
            j_close('}');
        }
        j_close(']');
        j_close('}');
    }
    j_close(']');
    j_kint("devices_tested", tested);
    j_close('}');
    return rc;
}

static void usage(void) {
    fprintf(stderr,
        "orbis-gfx-probe " PROBE_VERSION "\n"
        "usage: orbis-gfx-probe [--json] [--gl] [--gl-bench] [--frames N] [--vk] [--compute] [--vk-raster]\n"
        "                       [--pipeline-cache] [--opencl] [--membench [--membench-mb N]] [--all]\n"
        "                       [--device N] [--queue-family N] [--timeout-ms MS] [--level 1|2]\n"
        "exit: 0 ok, 1 test failure, 2 setup error, 3 GPU timeout/device lost\n");
}

int main(int argc, char **argv) {
    bool gl = false, vk = false, comp = false;
    int dev = -1, qfam = -1, level = 1;
    uint64_t timeout_ms = 5000;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--json")) ;
        else if (!strcmp(argv[i], "--gl")) gl = true;
        else if (!strcmp(argv[i], "--vk")) vk = true;
        else if (!strcmp(argv[i], "--compute")) comp = vk = true;
        else if (!strcmp(argv[i], "--all")) { gl = vk = comp = true; opt_gl_bench = opt_vk_raster = opt_pcache = opt_opencl = true; }
        else if (!strcmp(argv[i], "--gl-bench")) gl = opt_gl_bench = true;
        else if (!strcmp(argv[i], "--frames") && i + 1 < argc) opt_frames = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--vk-raster")) vk = opt_vk_raster = true;
        else if (!strcmp(argv[i], "--pipeline-cache")) vk = comp = opt_pcache = true;
        else if (!strcmp(argv[i], "--opencl")) opt_opencl = true;
        else if (!strcmp(argv[i], "--membench")) vk = opt_membench = true;
        else if (!strcmp(argv[i], "--membench-mb") && i + 1 < argc) opt_membench_mb = (unsigned)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--device") && i + 1 < argc) dev = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--queue-family") && i + 1 < argc) qfam = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--timeout-ms") && i + 1 < argc) timeout_ms = strtoull(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--level") && i + 1 < argc) level = atoi(argv[++i]);
        else { usage(); return 2; }
    }
    if (!gl && !vk && !opt_opencl) gl = vk = true;
    J = stdout;
    setvbuf(stdout, NULL, _IOLBF, 0);
    int rc = 0;
    j_open(NULL, '{');
    j_kstr("probe", "orbis-gfx-probe");
    j_kstr("probe_version", PROBE_VERSION);
#ifdef ORBIS_HAVE_RASTER_SPV
    j_kbool("raster_shaders", true);
#else
    j_kbool("raster_shaders", false);
#endif
    if (gl) { if (probe_gl()) rc = rc ? rc : 1; }
    if (opt_opencl) { int orc = probe_opencl(); if (orc == 1) rc = rc ? rc : 1; }
    if (vk) {
        int ir = vk_init();
        j_open("vulkan", '{');
        if (ir == -1) { j_kstr("error", "libvulkan.so.1 not found"); j_close('}'); rc = 2; goto out; }
        if (ir) { j_kstr("error", ir == -2 ? "no vkGetInstanceProcAddr" : vkres((VkResult)ir)); j_close('}'); rc = 2; goto out; }
        IF(vkEnumeratePhysicalDevices);
        uint32_t n = 0;
        vkEnumeratePhysicalDevices(inst, &n, NULL);
        VkPhysicalDevice pds[16];
        if (n > 16) n = 16;
        vkEnumeratePhysicalDevices(inst, &n, pds);
        j_open("devices", '[');
        for (uint32_t i = 0; i < n; i++) describe_device(pds[i], i);
        j_close(']');
        j_close('}');
        int rpick = dev;
        if (rpick < 0) {
            IF(vkGetPhysicalDeviceProperties);
            for (uint32_t i = 0; i < n && rpick < 0; i++) {
                VkPhysicalDeviceProperties p; vkGetPhysicalDeviceProperties(pds[i], &p);
                if (p.deviceType != VK_PHYSICAL_DEVICE_TYPE_CPU) rpick = (int)i;
            }
            if (rpick < 0 && n) rpick = 0;
        }
        if (opt_vk_raster && rpick >= 0 && (uint32_t)rpick < n) {
            int rr = run_raster(pds[rpick], (uint32_t)rpick, timeout_ms);
            if (rr) rc = rr;
            if (rr == 3) goto out;
        }
        if (opt_membench && rpick >= 0 && (uint32_t)rpick < n) {
            if (opt_membench_mb < 4) opt_membench_mb = 4;
            int mr = run_membench(pds[rpick], (uint32_t)rpick, timeout_ms, opt_membench_mb);
            if (mr) rc = mr;
            if (mr == 3) goto out;
        }
        if (comp) {
            int pick = dev;
            if (pick < 0) {
                IF(vkGetPhysicalDeviceProperties);
                for (uint32_t i = 0; i < n && pick < 0; i++) {
                    VkPhysicalDeviceProperties p; vkGetPhysicalDeviceProperties(pds[i], &p);
                    if (p.deviceType != VK_PHYSICAL_DEVICE_TYPE_CPU) pick = (int)i;
                }
                if (pick < 0 && n) pick = 0;   /* software-only system: still test, caller flags it */
            }
            if (pick < 0 || (uint32_t)pick >= n) { j_kstr("compute_error", "no Vulkan device"); rc = 2; }
            else {
                int cr = run_compute(pds[pick], (uint32_t)pick, qfam, timeout_ms, level);
                if (cr) rc = cr;
            }
        }
    }
out:
    j_kint("exit_code", rc);
    j_close('}');
    fputc('\n', stdout);
    fflush(stdout);
    /* After a GPU timeout, skip vkDestroyInstance: the driver may block. */
    _exit(rc);
}
