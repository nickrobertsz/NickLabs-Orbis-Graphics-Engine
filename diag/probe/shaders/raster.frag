#version 450
// mode 0: gradient from the fragment position (checks rasterisation and interpolation exactly);
// mode 1: solid green (used by the depth test).
layout(push_constant) uniform PC { float depth; float x0; float x1; float mode; } pc;
layout(location = 0) out vec4 o;
void main() {
    if (pc.mode < 0.5)
        o = vec4(gl_FragCoord.x / 256.0, gl_FragCoord.y / 256.0, 0.0, 1.0);
    else
        o = vec4(0.0, 1.0, 0.0, 1.0);
}
