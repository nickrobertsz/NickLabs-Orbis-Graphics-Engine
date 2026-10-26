#version 450
// Orbis Graphics raster test: two triangles covering x in [x0, x1], full height, at a fixed depth.
layout(push_constant) uniform PC { float depth; float x0; float x1; float mode; } pc;
void main() {
    vec2 p[6] = vec2[](vec2(pc.x0, -1.0), vec2(pc.x1, -1.0), vec2(pc.x0, 1.0),
                       vec2(pc.x1, -1.0), vec2(pc.x1, 1.0), vec2(pc.x0, 1.0));
    gl_Position = vec4(p[gl_VertexIndex], pc.depth, 1.0);
}
