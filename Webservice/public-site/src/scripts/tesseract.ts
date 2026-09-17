/**
 * The brand mark, live: a tesseract (4-cube) turning in 4-space, projected
 * 4D -> 3D -> 2D in a vertex shader, drawn as additive glow ribbons.
 *
 * This is the same object as the in-product mark and the OS icon, not a
 * lookalike: the vertices, edges, rotation planes, rates, camera pose and
 * both projection eye distances are the ones in host/src/ui/tesseract.cpp
 * (DoubleRotation style — xy and zw planes off independent phases at 1:0.618,
 * the golden-ratio conjugate, so the figure never repeats) and the fit
 * constants below are the output of the same fitStyle() sweeps, ported and
 * verified numerically before being baked in.
 *
 * Why analytic glow ribbons instead of a bloom pass: 32 edges and 16 vertices
 * are the whole figure. A ribbon whose fragment alpha is a bright core plus a
 * gaussian shoulder IS a one-pass bloom for line art — no FBO, no blur
 * kernel, no dependency, and it stays sharp at any DPR. The armillary rings
 * and dust are the same pipeline fed different instances, so the whole stage
 * is two programs and three draws.
 *
 * Verified invariants (Python port, 60k-sample trajectory sweep with full
 * camera wobble + pointer range): d4 - max|w| = 0.891 (projection can never
 * reach the pole), worst screen radius 0.9652 < 1.0 (never clips its box).
 */

export interface TesseractOptions {
  /** Live zw phase angle in degrees, for the HUD readout. */
  onAngle?: (zwDeg: number) => void;
  /** Render one parked frame at the brand pose (prefers-reduced-motion). */
  still?: boolean;
  /**
   * The driver took the context away. Mobile browsers do this routinely — a
   * backgrounded tab, memory pressure, or simply opening a second WebGL
   * context on the same page is enough — so this is a normal event, not an
   * error path. The handle is dead by the time this fires; the caller shows
   * its static fallback and waits for `webglcontextrestored` on the canvas
   * before building a new one.
   */
  onLost?: () => void;
}

export interface TesseractHandle {
  destroy(): void;
}

// ---- baked brand constants (fitStyle, DoubleRotation) ----------------------
const K_SPIN = 0.62; // rad/s, matches tesseract.cpp kSpin
const K_DOUBLE_B = 0.618034; // golden-ratio conjugate: the two phases never realign
const D4 = 2.30517; // 4D eye distance (maxW * 1.63)
const D3 = 10.97699; // 3D eye distance (maxR3 * 3)
const DEPTH = 2.6631; // depth-ramp normalizer
const SCALE = 0.2487; // unit-box fill, 3% headroom over fit.scale for camera wobble
const CAM_YAW = 0.35; // brand pose: just off head-on so the inside-out reads
const CAM_PITCH = 0.22;

// ---- figure ----------------------------------------------------------------
// 16 vertices (+-1)^4; 32 edges = pairs differing in exactly one coordinate.
const VERTS: number[] = [];
for (let i = 0; i < 16; i++) {
  VERTS.push(
    i & 1 ? 1 : -1,
    i & 2 ? 1 : -1,
    i & 4 ? 1 : -1,
    i & 8 ? 1 : -1,
  );
}
const EDGES: Array<readonly [number, number]> = [];
for (let i = 0; i < 16; i++) {
  for (const b of [1, 2, 4, 8]) if ((i ^ b) > i) EDGES.push([i, i ^ b] as const);
}

// Three armillary rings around the figure, each 96 segments. Generated in the
// xy plane at a radius a touch wider than the figure's 3D reach; the shader
// gives each ring its own fixed tilt and slow independent spin.
const RING_R = 4.15;
const RING_SEGMENTS = 96;
const RING_COUNT = 3;

const RIBBON_VERTS = 4; // triangle strip per instance
const EDGE_INSTANCE_COUNT = EDGES.length + RING_COUNT * RING_SEGMENTS;

// ---- shaders ---------------------------------------------------------------

const RIBBON_VERT = `#version 300 es
layout(location = 0) in vec2 aCorner; // x: along 0..1, y: side -1..1
layout(location = 1) in vec4 aA;
layout(location = 2) in vec4 aB;
layout(location = 3) in float aKind;  // 0 = tesseract edge, 1..3 = ring index

uniform vec2 uPhase;   // (xy phase, zw phase)
uniform vec2 uCam;     // yaw, pitch
uniform vec2 uRes;
uniform vec2 uShift;   // px: where the figure's centre sits on stage
uniform float uRadius; // px: model unit box -> pixels
uniform float uWidth;  // px: core stroke width at mid depth
uniform float uTime;

out float vSide;
out float vAlong;
out float vN01;
out float vKind;

vec4 rotate4(vec4 p, float xy, float zw) {
  float c = cos(xy), s = sin(xy);
  p = vec4(p.x * c - p.y * s, p.x * s + p.y * c, p.z, p.w);
  c = cos(zw); s = sin(zw);
  return vec4(p.x, p.y, p.z * c - p.w * s, p.z * s + p.w * c);
}

// Per-ring pose: a fixed tilt plus a slow spin in one plane, each ring its own
// rate and axis mix so the cage never looks mechanical.
vec3 ringPose(vec3 p, float kind, float t) {
  float c, s;
  if (kind < 1.5) {
    c = cos(t * 0.10); s = sin(t * 0.10);
    p = vec3(p.x * c - p.y * s, p.x * s + p.y * c, p.z);
  } else if (kind < 2.5) {
    c = cos(1.08); s = sin(1.08); // fixed tilt about x
    p = vec3(p.x, p.y * c - p.z * s, p.y * s + p.z * c);
    c = cos(t * 0.07); s = sin(t * 0.07);
    p = vec3(p.x * c + p.z * s, p.y, -p.x * s + p.z * c);
  } else {
    c = cos(-0.84); s = sin(-0.84);
    p = vec3(p.x, p.y * c - p.z * s, p.y * s + p.z * c);
    c = cos(0.61); s = sin(0.61);
    p = vec3(p.x * c + p.z * s, p.y, -p.x * s + p.z * c);
    c = cos(t * -0.085); s = sin(t * -0.085);
    p = vec3(p.x * c - p.y * s, p.x * s + p.y * c, p.z);
  }
  return p;
}

void main() {
  vec4 a = aA;
  vec4 b = aB;
  if (aKind < 0.5) {
    a = rotate4(a, uPhase.x, uPhase.y);
    b = rotate4(b, uPhase.x, uPhase.y);
  }
  // 4D -> 3D perspective. Rings sit at w = 0 with k = 1 by construction.
  float ka = ${D4.toFixed(5)} / (${D4.toFixed(5)} - a.w);
  float kb = ${D4.toFixed(5)} / (${D4.toFixed(5)} - b.w);
  vec3 pa = a.xyz * ka;
  vec3 pb = b.xyz * kb;
  if (aKind > 0.5) {
    pa = ringPose(pa, aKind, uTime);
    pb = ringPose(pb, aKind, uTime);
  }
  // camera: yaw about +y, then pitch about +x; +z points into the screen
  float cy = cos(uCam.x), sy = sin(uCam.x);
  float cp = cos(uCam.y), sp = sin(uCam.y);
  vec3 ca = vec3(pa.x * cy + pa.z * sy, 0.0, -pa.x * sy + pa.z * cy);
  ca = vec3(ca.x, pa.y * cp - ca.z * sp, pa.y * sp + ca.z * cp);
  vec3 cb = vec3(pb.x * cy + pb.z * sy, 0.0, -pb.x * sy + pb.z * cy);
  cb = vec3(cb.x, pb.y * cp - cb.z * sp, pb.y * sp + cb.z * cp);

  float n01a = clamp(0.5 - ca.z / (2.0 * ${DEPTH.toFixed(5)}), 0.0, 1.0);
  float n01b = clamp(0.5 - cb.z / (2.0 * ${DEPTH.toFixed(5)}), 0.0, 1.0);

  // 3D -> 2D perspective into the unit box, then to pixels
  float qa = ${D3.toFixed(5)} / (${D3.toFixed(5)} + ca.z);
  float qb = ${D3.toFixed(5)} / (${D3.toFixed(5)} + cb.z);
  vec2 sa = ca.xy * qa * ${SCALE.toFixed(5)} * uRadius;
  vec2 sb = cb.xy * qb * ${SCALE.toFixed(5)} * uRadius;

  vec2 along = mix(sa, sb, aCorner.x);
  float n01 = mix(n01a, n01b, aCorner.x);

  vec2 delta = sb - sa;
  float len = max(length(delta), 1e-4);
  vec2 normal = vec2(-delta.y, delta.x) / len;

  // Near edges draw heavier, far edges hairline — the logo's own ramp.
  float ring = step(0.5, aKind);
  float core = uWidth * mix(0.78 + 0.5 * n01, 0.62, ring);
  float half_ = core * mix(4.6, 2.6, ring) * 0.5; // halo shoulder extent
  vec2 px = along + normal * aCorner.y * half_ + uShift;

  vSide = aCorner.y;
  vAlong = aCorner.x;
  vN01 = n01;
  vKind = aKind;
  vec2 clip = vec2(px.x / uRes.x, -px.y / uRes.y) * 2.0;
  gl_Position = vec4(clip, 0.0, 1.0);
}`;

const RIBBON_FRAG = `#version 300 es
precision highp float;

in float vSide;
in float vAlong;
in float vN01;
in float vKind;

uniform vec3 uNear;   // teal hi
uniform vec3 uFar;    // teal dk
uniform vec3 uRing;   // warm accent for the cage

out vec4 outColor;

void main() {
  float s = abs(vSide);
  // Bright core plus a gaussian shoulder: a one-pass bloom for line art.
  // (1.0 - smoothstep(lo, hi, x)) rather than reversed edges: edge0 >= edge1
  // is undefined in GLSL ES and some drivers return garbage.
  float core = 1.0 - smoothstep(0.12, 0.30, s);
  float halo = exp(-s * s * 3.2) * 0.38;
  // Soften segment joints; the vertex dots cap the real edges.
  float cap = smoothstep(0.0, 0.06, vAlong) * (1.0 - smoothstep(0.94, 1.0, vAlong));
  float ring = step(0.5, vKind);
  vec3 col = mix(mix(uFar, uNear, vN01), uRing, ring);
  float energy = mix(core * 1.25 + halo, core * 0.9 + halo * 0.6, ring);
  float alpha = energy * mix(1.0, 0.22, ring) * cap;
  // Alpha carries the accumulated intensity: with additive blending into a
  // premultiplied canvas, a constant 1.0 alpha would composite as opaque
  // black wherever the glow falls to nothing.
  float a = clamp(alpha, 0.0, 1.0);
  outColor = vec4(col * alpha, a);
}`;

const SPRITE_VERT = `#version 300 es
layout(location = 0) in vec2 aCorner; // -1..1 billboard quad
layout(location = 1) in vec4 aP;      // model-space vertex anchor

uniform vec2 uPhase;
uniform vec2 uCam;
uniform vec2 uRes;
uniform vec2 uShift;
uniform float uRadius;
uniform float uWidth;

out vec2 vUv;
out float vN01;

vec4 rotate4(vec4 p, float xy, float zw) {
  float c = cos(xy), s = sin(xy);
  p = vec4(p.x * c - p.y * s, p.x * s + p.y * c, p.z, p.w);
  c = cos(zw); s = sin(zw);
  return vec4(p.x, p.y, p.z * c - p.w * s, p.z * s + p.w * c);
}

void main() {
  vec4 p = rotate4(aP, uPhase.x, uPhase.y);
  float k = ${D4.toFixed(5)} / (${D4.toFixed(5)} - p.w);
  vec3 m = p.xyz * k;
  float cy = cos(uCam.x), sy = sin(uCam.x);
  float cp = cos(uCam.y), sp = sin(uCam.y);
  vec3 c3 = vec3(m.x * cy + m.z * sy, 0.0, -m.x * sy + m.z * cy);
  c3 = vec3(c3.x, m.y * cp - c3.z * sp, m.y * sp + c3.z * cp);
  float n01 = clamp(0.5 - c3.z / (2.0 * ${DEPTH.toFixed(5)}), 0.0, 1.0);
  float q = ${D3.toFixed(5)} / (${D3.toFixed(5)} + c3.z);
  vec2 s2 = c3.xy * q * ${SCALE.toFixed(5)} * uRadius;
  float radiusPx = uWidth * (1.5 + 1.1 * n01) * 2.2;
  vec2 px = s2 + aCorner * radiusPx + uShift;
  vUv = aCorner;
  vN01 = n01;
  vec2 clip = vec2(px.x / uRes.x, -px.y / uRes.y) * 2.0;
  gl_Position = vec4(clip, 0.0, 1.0);
}`;

const SPRITE_FRAG = `#version 300 es
precision highp float;

in vec2 vUv;
in float vN01;

uniform vec3 uNear;
uniform vec3 uFar;

out vec4 outColor;

void main() {
  float d = length(vUv);
  float core = 1.0 - smoothstep(0.18, 0.42, d);
  float halo = exp(-d * d * 3.4) * 0.34;
  vec3 col = mix(uFar, uNear, 0.2 + 0.8 * vN01);
  float e = core * 1.3 + halo;
  outColor = vec4(col * e, clamp(e, 0.0, 1.0));
}`;

const DUST_VERT = `#version 300 es
layout(location = 0) in vec3 aP;    // unit-ish shell position
layout(location = 1) in vec2 aSeed; // phase, size

uniform vec2 uCam;
uniform vec2 uRes;
uniform float uRadius;
uniform float uTime;

out float vTwinkle;

void main() {
  // Slow orbital drift, one shared turn so the field reads as a volume.
  float c = cos(uTime * 0.016), s = sin(uTime * 0.016);
  vec3 p = vec3(aP.x * c + aP.z * s, aP.y, -aP.x * s + aP.z * c);
  float cy = cos(uCam.x * 0.35), sy = sin(uCam.x * 0.35);
  float cp = cos(uCam.y * 0.35), sp = sin(uCam.y * 0.35);
  vec3 c3 = vec3(p.x * cy + p.z * sy, 0.0, -p.x * sy + p.z * cy);
  c3 = vec3(c3.x, p.y * cp - c3.z * sp, p.y * sp + c3.z * cp);
  float q = ${D3.toFixed(5)} / (${D3.toFixed(5)} + c3.z * 2.2);
  vec2 s2 = c3.xy * q * uRadius * 1.9;
  vec2 clip = vec2(s2.x / uRes.x, -s2.y / uRes.y) * 2.0;
  gl_Position = vec4(clip, 0.0, 1.0);
  gl_PointSize = aSeed.y;
  vTwinkle = 0.30 + 0.28 * sin(uTime * (0.5 + aSeed.x * 0.7) + aSeed.x * 41.0);
}`;

const DUST_FRAG = `#version 300 es
precision highp float;

in float vTwinkle;
uniform vec3 uDust;
out vec4 outColor;

void main() {
  vec2 d = gl_PointCoord * 2.0 - 1.0;
  float r2 = dot(d, d);
  if (r2 > 1.0) discard;
  float e = vTwinkle * (1.0 - r2);
  outColor = vec4(uDust * e, e);
}`;

// ---- palette (make_logo.py) -------------------------------------------------
const TEAL_HI: readonly [number, number, number] = [66 / 255, 236 / 255, 206 / 255];
const TEAL_DK: readonly [number, number, number] = [16 / 255, 128 / 255, 115 / 255];
// The site's warm accent, kept faint: the cage should read as instrumentation,
// not as a second brand colour.
const RING_COL: readonly [number, number, number] = [255 / 255 * 0.85, 138 / 255 * 0.85, 61 / 255 * 0.85];
const DUST_COL: readonly [number, number, number] = [0.55, 0.95, 0.85];

function compile(gl: WebGL2RenderingContext, type: number, source: string): WebGLShader | null {
  const shader = gl.createShader(type);
  if (!shader) return null;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

function link(gl: WebGL2RenderingContext, vsSource: string, fsSource: string): WebGLProgram | null {
  const vs = compile(gl, gl.VERTEX_SHADER, vsSource);
  const fs = compile(gl, gl.FRAGMENT_SHADER, fsSource);
  if (!vs || !fs) return null;
  const program = gl.createProgram();
  if (!program) return null;
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    gl.deleteProgram(program);
    return null;
  }
  return program;
}

export function createTesseract(
  canvas: HTMLCanvasElement,
  opts: TesseractOptions = {},
): TesseractHandle | null {
  const gl = canvas.getContext('webgl2', {
    alpha: true,
    antialias: false,
    depth: false,
    stencil: false,
    premultipliedAlpha: true,
    desynchronized: true,
    powerPreference: 'high-performance',
  });
  if (!gl) return null;

  const ribbonProg = link(gl, RIBBON_VERT, RIBBON_FRAG);
  const spriteProg = link(gl, SPRITE_VERT, SPRITE_FRAG);
  const dustProg = link(gl, DUST_VERT, DUST_FRAG);
  if (!ribbonProg || !spriteProg || !dustProg) return null;

  const uniforms = (prog: WebGLProgram, names: readonly string[]) => {
    const out: Record<string, WebGLUniformLocation | null> = {};
    for (const n of names) out[n] = gl.getUniformLocation(prog, n);
    return out;
  };
  const uRib = uniforms(ribbonProg, ['uPhase', 'uCam', 'uRes', 'uShift', 'uRadius', 'uWidth', 'uTime', 'uNear', 'uFar', 'uRing'] as const);
  const uSpr = uniforms(spriteProg, ['uPhase', 'uCam', 'uRes', 'uShift', 'uRadius', 'uWidth', 'uNear', 'uFar'] as const);
  const uDst = uniforms(dustProg, ['uCam', 'uRes', 'uRadius', 'uTime', 'uDust'] as const);

  // ---- geometry -------------------------------------------------------------
  const ribbonVao = gl.createVertexArray();
  gl.bindVertexArray(ribbonVao);

  const cornerBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, cornerBuf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, -1, 1, -1, 0, 1, 1, 1]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

  // Instance table: 32 edges, then 3 x 96 ring segments. (aA, aB, aKind)
  const inst = new Float32Array(EDGE_INSTANCE_COUNT * 9);
  let o = 0;
  for (const [ia, ib] of EDGES) {
    for (let k = 0; k < 4; k++) inst[o + k] = VERTS[ia * 4 + k];
    for (let k = 0; k < 4; k++) inst[o + 4 + k] = VERTS[ib * 4 + k];
    inst[o + 8] = 0;
    o += 9;
  }
  for (let ring = 0; ring < RING_COUNT; ring++) {
    for (let seg = 0; seg < RING_SEGMENTS; seg++) {
      const a0 = (seg / RING_SEGMENTS) * Math.PI * 2;
      const a1 = ((seg + 1) / RING_SEGMENTS) * Math.PI * 2;
      inst[o] = Math.cos(a0) * RING_R;
      inst[o + 1] = Math.sin(a0) * RING_R;
      inst[o + 2] = 0;
      inst[o + 3] = 0;
      inst[o + 4] = Math.cos(a1) * RING_R;
      inst[o + 5] = Math.sin(a1) * RING_R;
      inst[o + 6] = 0;
      inst[o + 7] = 0;
      inst[o + 8] = ring + 1;
      o += 9;
    }
  }
  const instBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, instBuf);
  gl.bufferData(gl.ARRAY_BUFFER, inst, gl.STATIC_DRAW);
  gl.enableVertexAttribArray(1);
  gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 36, 0);
  gl.vertexAttribDivisor(1, 1);
  gl.enableVertexAttribArray(2);
  gl.vertexAttribPointer(2, 4, gl.FLOAT, false, 36, 16);
  gl.vertexAttribDivisor(2, 1);
  gl.enableVertexAttribArray(3);
  gl.vertexAttribPointer(3, 1, gl.FLOAT, false, 36, 32);
  gl.vertexAttribDivisor(3, 1);

  const spriteVao = gl.createVertexArray();
  gl.bindVertexArray(spriteVao);
  const spriteCorner = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, spriteCorner);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

  // The 16 cell vertices, one billboard each. Nothing sits at the origin: the
  // inner cell reads as an empty room, which is the whole point of the figure.
  const spr = new Float32Array(VERTS);
  const sprBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, sprBuf);
  gl.bufferData(gl.ARRAY_BUFFER, spr, gl.STATIC_DRAW);
  gl.enableVertexAttribArray(1);
  gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 16, 0);
  gl.vertexAttribDivisor(1, 1);

  const dustVao = gl.createVertexArray();
  gl.bindVertexArray(dustVao);
  const DUST_COUNT = 240;
  const dust = new Float32Array(DUST_COUNT * 5);
  let seed = 1337;
  const rand = () => {
    // Deterministic: the field is part of the composition, not noise per load.
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return seed / 0x7fffffff;
  };
  for (let i = 0; i < DUST_COUNT; i++) {
    const theta = rand() * Math.PI * 2;
    const z = rand() * 2 - 1;
    const rr = Math.sqrt(1 - z * z) * (0.55 + rand() * 0.75);
    dust[i * 5] = Math.cos(theta) * rr;
    dust[i * 5 + 1] = Math.sin(theta) * rr;
    dust[i * 5 + 2] = z * (0.55 + rand() * 0.75);
    dust[i * 5 + 3] = rand();
    dust[i * 5 + 4] = 1 + rand() * 2.4;
  }
  const dustBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, dustBuf);
  gl.bufferData(gl.ARRAY_BUFFER, dust, gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 20, 0);
  gl.enableVertexAttribArray(1);
  gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 20, 12);

  gl.bindVertexArray(null);

  gl.disable(gl.DEPTH_TEST);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.ONE, gl.ONE); // additive: overlaps bloom on their own

  // ---- sizing ---------------------------------------------------------------
  // This renderer now lives in a compact About-page frame. Keep its backing
  // store bounded: the analytic line work remains sharp without hero-sized
  // GPU allocations.
  const PIXEL_CEILING = 520_000;
  const PIXEL_FLOOR = 120_000;
  let pixelBudget = PIXEL_CEILING;
  let width = 0;
  let height = 0;

  function resize(): boolean {
    const rect = canvas.getBoundingClientRect();
    const cssW = Math.max(1, Math.round(rect.width));
    const cssH = Math.max(1, Math.round(rect.height));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const budgetScale = Math.sqrt(pixelBudget / Math.max(1, cssW * cssH));
    const scale = Math.max(0.4, Math.min(dpr, budgetScale));
    const w = Math.max(1, Math.round(cssW * scale));
    const h = Math.max(1, Math.round(cssH * scale));
    if (w === width && h === height) return false;
    width = w;
    height = h;
    canvas.width = w;
    canvas.height = h;
    gl!.viewport(0, 0, w, h);
    return true;
  }
  resize();

  // Pointer parallax, eased — same feel as the part hero.
  let pointerX = 0;
  let pointerY = 0;
  let targetX = 0;
  let targetY = 0;
  const onPointer = (event: PointerEvent) => {
    if (event.pointerType === 'touch') return;
    targetX = (event.clientX / window.innerWidth) * 2 - 1;
    targetY = (event.clientY / window.innerHeight) * 2 - 1;
  };
  window.addEventListener('pointermove', onPointer, { passive: true });

  let visible = true;
  const observer = new IntersectionObserver(
    (entries) => { visible = entries[0]?.isIntersecting ?? true; },
    { rootMargin: '80px' },
  );
  observer.observe(canvas);
  const resizeObserver = new ResizeObserver(() => { resize(); });
  resizeObserver.observe(canvas);
  const onVisibility = () => { if (!document.hidden) last = performance.now(); };
  document.addEventListener('visibilitychange', onVisibility);

  // Context loss. `preventDefault()` here is not defensive tidiness — it is the
  // only thing that makes the loss recoverable at all: without it the browser
  // never fires `webglcontextrestored` and the canvas stays a blank rectangle
  // for the life of the page. Every GL object above is already gone, so this
  // handle is finished; the caller reinstates its static fallback and builds a
  // fresh renderer if and when the context comes back.
  let contextAlive = true;
  const onLost = (event: Event) => {
    event.preventDefault();
    if (!contextAlive) return;
    contextAlive = false;
    cancelAnimationFrame(frame);
    observer.disconnect();
    resizeObserver.disconnect();
    window.removeEventListener('pointermove', onPointer);
    document.removeEventListener('visibilitychange', onVisibility);
    canvas.removeEventListener('webglcontextlost', onLost);
    opts.onLost?.();
  };
  canvas.addEventListener('webglcontextlost', onLost);

  let last = performance.now();
  let frame = 0;
  let lastReported = -1;

  function draw(elapsed: number) {
    const gl2 = gl!;
    pointerX += (targetX - pointerX) * 0.055;
    pointerY += (targetY - pointerY) * 0.055;

    const phXy = elapsed * K_SPIN;
    const phZw = elapsed * K_SPIN * K_DOUBLE_B;
    const yaw = CAM_YAW + Math.sin(elapsed * 0.10) * 0.28 + pointerX * 0.35;
    const pitch = CAM_PITCH + Math.cos(elapsed * 0.073) * 0.12 + pointerY * 0.22;

    if (opts.onAngle) {
      const deg = Math.round(((phZw * 180) / Math.PI) % 360);
      if (deg !== lastReported && Math.abs(deg - lastReported) > 2) {
        lastReported = deg;
        opts.onAngle(deg);
      }
    }

    // The compact frame has no copy column or HUD to clear: keep the mark
    // centered and leave enough room for the armillary rings.
    const radius = Math.min(width, height) * 0.44;
    const shiftX = 0;
    const shiftY = 0;
    const coreWidth = Math.max(1.4, Math.min(4.0, Math.min(width, height) * 0.0052));

    gl2.clearColor(0, 0, 0, 0);
    gl2.clear(gl2.COLOR_BUFFER_BIT);
    // Re-asserted per frame: state is global, and a lost race here renders as
    // opaque black sprite quads instead of additive light.
    gl2.disable(gl2.DEPTH_TEST);
    gl2.enable(gl2.BLEND);
    gl2.blendFunc(gl2.ONE, gl2.ONE);

    gl2.useProgram(dustProg);
    gl2.uniform2f(uDst.uCam, yaw, pitch);
    gl2.uniform2f(uDst.uRes, width, height);
    gl2.uniform1f(uDst.uRadius, radius);
    gl2.uniform1f(uDst.uTime, elapsed);
    gl2.uniform3f(uDst.uDust, DUST_COL[0], DUST_COL[1], DUST_COL[2]);
    gl2.bindVertexArray(dustVao);
    gl2.drawArrays(gl2.POINTS, 0, DUST_COUNT);

    gl2.useProgram(ribbonProg);
    gl2.uniform2f(uRib.uPhase, phXy, phZw);
    gl2.uniform2f(uRib.uCam, yaw, pitch);
    gl2.uniform2f(uRib.uRes, width, height);
    gl2.uniform2f(uRib.uShift, shiftX, shiftY);
    gl2.uniform1f(uRib.uRadius, radius);
    gl2.uniform1f(uRib.uWidth, coreWidth);
    gl2.uniform1f(uRib.uTime, elapsed);
    gl2.uniform3f(uRib.uNear, TEAL_HI[0], TEAL_HI[1], TEAL_HI[2]);
    gl2.uniform3f(uRib.uFar, TEAL_DK[0], TEAL_DK[1], TEAL_DK[2]);
    gl2.uniform3f(uRib.uRing, RING_COL[0], RING_COL[1], RING_COL[2]);
    gl2.bindVertexArray(ribbonVao);
    gl2.drawArraysInstanced(gl2.TRIANGLE_STRIP, 0, RIBBON_VERTS, EDGE_INSTANCE_COUNT);

    gl2.useProgram(spriteProg);
    gl2.uniform2f(uSpr.uPhase, phXy, phZw);
    gl2.uniform2f(uSpr.uCam, yaw, pitch);
    gl2.uniform2f(uSpr.uRes, width, height);
    gl2.uniform2f(uSpr.uShift, shiftX, shiftY);
    gl2.uniform1f(uSpr.uRadius, radius);
    gl2.uniform1f(uSpr.uWidth, coreWidth);
    gl2.uniform3f(uSpr.uNear, TEAL_HI[0], TEAL_HI[1], TEAL_HI[2]);
    gl2.uniform3f(uSpr.uFar, TEAL_DK[0], TEAL_DK[1], TEAL_DK[2]);
    gl2.bindVertexArray(spriteVao);
    gl2.drawArraysInstanced(gl2.TRIANGLE_STRIP, 0, 4, 16);

    gl2.bindVertexArray(null);
  }

  // Produce a frame before returning. Callers only replace the static logo
  // after this function succeeds, so a shader or draw-time failure cannot
  // reveal an empty canvas.
  resize();
  draw(opts.still ? 0 : Math.min(last / 1000, Number.MAX_SAFE_INTEGER));

  if (!opts.still) {
    let ema = 0;
    let sampled = 0;
    const loop = (now: number) => {
      frame = requestAnimationFrame(loop);
      const delta = now - last;
      last = now;
      // A loss can land between the frame request and this callback.
      if (!contextAlive) return;
      if (document.hidden || !visible) return;

      ema = ema === 0 ? delta : ema * 0.9 + delta * 0.1;
      if (++sampled >= 45) {
        sampled = 0;
        const next = ema > 24 ? pixelBudget * 0.7 : ema < 9 ? pixelBudget * 1.25 : pixelBudget;
        const clamped = Math.min(PIXEL_CEILING, Math.max(PIXEL_FLOOR, next));
        if (clamped !== pixelBudget) {
          pixelBudget = clamped;
          resize();
        }
      }

      draw(Math.min(now / 1000, Number.MAX_SAFE_INTEGER));
    };
    frame = requestAnimationFrame(loop);
  }

  return {
    destroy() {
      cancelAnimationFrame(frame);
      contextAlive = false;
      observer.disconnect();
      resizeObserver.disconnect();
      window.removeEventListener('pointermove', onPointer);
      document.removeEventListener('visibilitychange', onVisibility);
      canvas.removeEventListener('webglcontextlost', onLost);
      gl.deleteProgram(ribbonProg);
      gl.deleteProgram(spriteProg);
      gl.deleteProgram(dustProg);
      gl.deleteVertexArray(ribbonVao);
      gl.deleteVertexArray(spriteVao);
      gl.deleteVertexArray(dustVao);
      gl.deleteBuffer(cornerBuf);
      gl.deleteBuffer(instBuf);
      gl.deleteBuffer(spriteCorner);
      gl.deleteBuffer(sprBuf);
      gl.deleteBuffer(dustBuf);
      // Deleting the objects does not release the drawing buffer, which is the
      // bulk of what a suspended stage still costs the GPU — so shrink it to
      // nothing. Deliberately NOT `WEBGL_lose_context.loseContext()`: a loss
      // nobody `preventDefault()`s is permanent, and `getContext` on that
      // canvas returns the dead context forever, so a stage torn down on
      // scroll-out could never be rebuilt on scroll-in.
      canvas.width = 1;
      canvas.height = 1;
    },
  };
}
