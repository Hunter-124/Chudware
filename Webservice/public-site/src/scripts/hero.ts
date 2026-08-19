/**
 * Animated hero: a parametric part built feature by feature, raymarched.
 *
 * Why a signed distance field rather than a mesh and a 3D library: the subject
 * is a CAD part whose whole point is boolean history. In an SDF an extrude, a
 * fillet, and four drilled holes are `min`, a rounded extrusion radius, and
 * `max(d, -cylinder)` — the booleans are exact and animating them costs a
 * uniform, not a re-tessellation. It also means no dependency on the site's
 * critical path: this file plus a WebGL2 context is the entire renderer.
 *
 * Edge lines are free. The tetrahedron normal already samples the field at four
 * offsets; the mean of those four samples relative to the surface is a
 * curvature estimate, positive on convex edges and negative in concave
 * corners. That single number draws both the machined-edge highlight and the
 * corner occlusion that make the result read as a technical render instead of
 * a shaded blob.
 */
import { HERO_CYCLE, HERO_HOLD_END, HERO_STAGES } from './hero-timeline';

export interface HeroOptions {
  /** Called whenever the active stage index changes; -1 while unwinding. */
  onStage?: (index: number, building: boolean) => void;
  /** Render a single frame and stop. Used for prefers-reduced-motion. */
  still?: boolean;
  /** Framed showcase box (no copy column) rather than the full-bleed hero. */
  centered?: boolean;
  /**
   * Only consulted with `centered`. True while the FeatureManager panel is
   * docked over the render's near corner, which is what the subject has to be
   * walked clear of; below the breakpoint the panel sits under the render and
   * the part belongs in the middle of the box. Read per frame so a resize
   * across the breakpoint reframes without a reboot.
   */
  panelDocked?: () => boolean;
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

export interface HeroHandle {
  destroy(): void;
}

const VERT = `#version 300 es
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p - 1.0, 0.0, 1.0);
}`;

const FRAG = `#version 300 es
precision highp float;

out vec4 fragColor;

uniform vec2  uRes;
uniform vec3  uCam;      // yaw, pitch, distance
uniform vec2  uShift;    // composition offset in NDC; positive pushes the subject right and up
uniform float uTime;
uniform float uSketch;
uniform float uExtrude;
uniform float uBoss;
uniform float uFillet;
uniform float uDrill;
uniform float uBore;
uniform float uPocket;

// sRGB page background. Composited after tone mapping so the canvas edge is
// invisible against the document.
const vec3  BG        = vec3(0.0667, 0.0745, 0.0627);
const vec3  ACCENT    = vec3(1.0000, 0.2543, 0.0468);  // #ff8a3d, linear
const vec3  TECHNICAL = vec3(0.0884, 0.6724, 0.4620);  // #53d6b5, linear
const vec3  TECH_HI   = vec3(0.2582, 0.7911, 0.6239);  // #8be6cf, linear

const vec2  PLATE   = vec2(1.35, 0.92);   // half extents in x, z
const float CORNER  = 0.26;               // corner radius of the sketch profile
const float GROUND  = -0.62;
const float BOSS_R  = 0.42;
const float BORE_R  = 0.24;
const float HOLE_R  = 0.11;
const vec4  HOLES   = vec4(1.10, 0.62, 0.0, 0.0);  // |x|, |z| of the four holes

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

float sdRoundRect(vec2 p, vec2 b, float r) {
  vec2 d = abs(p) - b + r;
  return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0) - r;
}

float sdCylinder(vec3 p, float h, float r) {
  vec2 d = abs(vec2(length(p.xz), p.y)) - vec2(r, h);
  return min(max(d.x, d.y), 0.0) + length(max(d, 0.0));
}

/** Extrude a 2D profile to +/- h with a fillet of radius r on both rims. */
float extrudeRound(float d2, float y, float h, float r) {
  vec2 w = vec2(d2 + r, abs(y) - h + r);
  return min(max(w.x, w.y), 0.0) + length(max(w, 0.0)) - r;
}

float smoothUnion(float a, float b, float k) {
  float h = clamp(0.5 + 0.5 * (b - a) / k, 0.0, 1.0);
  return mix(b, a, h) - k * h * (1.0 - h);
}

// ---------------------------------------------------------------------------
// The part
// ---------------------------------------------------------------------------

float plateHalfHeight() {
  return 0.006 + 0.194 * uExtrude;
}

float map(vec3 p) {
  float h = plateHalfHeight();

  // Fillet radius is clamped against the wall it rounds, so an in-progress
  // extrude can never be asked for a fillet larger than the material present.
  float fil = min(min(0.004 + 0.066 * uFillet, h * 0.75), CORNER * 0.9);
  float profile = sdRoundRect(p.xz, PLATE, CORNER);
  float solid = extrudeRound(profile, p.y, h, fil);

  // Boss, blended into the plate with a root fillet that grows with Fillet1.
  float bossH = 0.28 * uBoss;
  if (uBoss > 0.0) {
    float boss = sdCylinder(p - vec3(0.0, h + bossH * 0.5 - 0.004, 0.0), bossH * 0.5, BOSS_R);
    solid = smoothUnion(solid, boss, 0.02 + 0.05 * uFillet);
  }

  // Drilled holes. A cutter is a capped cylinder whose top stays above the
  // face while its depth grows, so the animation is the drill descending
  // rather than a radius fading in — and at depth 0 it is a zero-height disk
  // outside the material, which subtracts nothing.
  if (uDrill > 0.0) {
    float top = h + 0.12;
    float depth = uDrill * (2.0 * h + 0.34);
    vec3 q = vec3(abs(p.x), p.y, abs(p.z));
    float hole = sdCylinder(
      q - vec3(HOLES.x, top - depth * 0.5, HOLES.y), depth * 0.5, HOLE_R);
    solid = max(solid, -hole);
  }

  if (uBore > 0.0) {
    float top = h + bossH + 0.16;
    float depth = uBore * (top + h + 0.34);
    float bore = sdCylinder(p - vec3(0.0, top - depth * 0.5, 0.0), depth * 0.5, BORE_R);
    solid = max(solid, -bore);
  }

  // Mirrored side pockets. Mirroring the domain in x is exact for a distance
  // field, so one evaluation cuts both.
  if (uPocket > 0.0) {
    vec3 q = vec3(abs(p.x) - 0.72, p.y, p.z);
    float pocket2 = sdRoundRect(q.xz, vec2(0.20, 0.44), 0.16);
    float top = h + 0.05;
    float depth = uPocket * 0.19;
    vec2 w = vec2(pocket2, abs(q.y - (top - depth * 0.5)) - depth * 0.5);
    float pocket = min(max(w.x, w.y), 0.0) + length(max(w, 0.0));
    solid = max(solid, -pocket);
  }

  return solid;
}

// ---------------------------------------------------------------------------
// Marching
// ---------------------------------------------------------------------------

/** Distance to the bounding sphere, so empty space costs one test not sixty. */
float boundsEntry(vec3 ro, vec3 rd) {
  const float R = 2.1;
  float b = dot(ro, rd);
  float c = dot(ro, ro) - R * R;
  float disc = b * b - c;
  if (disc < 0.0) return -1.0;
  float t = -b - sqrt(disc);
  return max(t, 0.0);
}

float march(vec3 ro, vec3 rd, out int steps) {
  steps = 0;
  float entry = boundsEntry(ro, rd);
  if (entry < 0.0) return -1.0;
  float t = entry;
  for (int i = 0; i < 72; i++) {
    vec3 p = ro + rd * t;
    if (dot(p, p) > 5.5) return -1.0;
    float d = map(p);
    if (d < 0.0006 * t) { steps = i; return t; }
    t += d;
    if (t > 9.0) break;
  }
  return -1.0;
}

/**
 * Tetrahedron normal, with the local mean curvature that falls out of the same
 * four taps: convex edges positive, concave corners negative.
 *
 * The baseline subtraction is not optional. The march stops at d < 0.0006 * t,
 * which at the hero's viewing distance is ~0.003 — larger than the 0.0022
 * sampling offset. Without subtracting d(p) the residual hit distance divides
 * through by the offset and lands near 1.0 on *every* surface, so the edge
 * term saturates and the whole part turns milky. One extra tap buys a
 * curvature that is actually about shape.
 */
vec3 normalAndCurvature(vec3 p, out float curvature) {
  const float e = 0.0022;
  vec2 k = vec2(1.0, -1.0);
  float base = map(p);
  float a = map(p + k.xyy * e);
  float b = map(p + k.yyx * e);
  float c = map(p + k.yxy * e);
  float d = map(p + k.xxx * e);
  curvature = (a + b + c + d - 4.0 * base) / (4.0 * e);
  return normalize(k.xyy * a + k.yyx * b + k.yxy * c + k.xxx * d);
}

float softShadow(vec3 ro, vec3 rd) {
  float res = 1.0;
  float t = 0.05;
  for (int i = 0; i < 22; i++) {
    float d = map(ro + rd * t);
    res = min(res, 12.0 * d / t);
    t += clamp(d, 0.02, 0.3);
    if (res < 0.004 || t > 4.0) break;
  }
  return clamp(res, 0.0, 1.0);
}

float occlusion(vec3 p, vec3 n) {
  float occ = 0.0;
  float scale = 1.0;
  for (int i = 0; i < 5; i++) {
    float hr = 0.012 + 0.09 * float(i);
    occ += (hr - map(p + n * hr)) * scale;
    scale *= 0.72;
  }
  return clamp(1.0 - 2.4 * occ, 0.0, 1.0);
}

// ---------------------------------------------------------------------------
// Sketch plane
// ---------------------------------------------------------------------------

/** Emissive intensity of the sketch geometry at a point on the y = 0 plane. */
float sketchField(vec2 q, float px) {
  float w = max(px * 1.4, 0.0018);

  float line = abs(sdRoundRect(q, PLATE, CORNER));
  line = min(line, abs(length(abs(q) - HOLES.xy) - HOLE_R));
  line = min(line, abs(length(q) - BORE_R));
  line = min(line, abs(length(q) - BOSS_R));
  line = min(line, abs(sdRoundRect(vec2(abs(q.x) - 0.72, q.y), vec2(0.20, 0.44), 0.16)));

  float profile = 1.0 - smoothstep(0.0, w, line);

  // Construction centrelines, dashed, dimmer than the profile.
  vec2 axis = abs(q);
  float centre = min(axis.x, axis.y);
  float dash = step(0.5, fract((abs(q.x) + abs(q.y)) * 7.0));
  float construction = (1.0 - smoothstep(0.0, w * 0.8, centre)) * dash * 0.28
    * (1.0 - smoothstep(1.15, 1.75, length(q)));

  return profile + construction;
}

// ---------------------------------------------------------------------------
// Shading
// ---------------------------------------------------------------------------

vec3 shadePart(vec3 p, vec3 rd, vec3 n, float curvature) {
  vec3 key = normalize(vec3(0.58, 0.76, 0.42));
  vec3 fill = normalize(vec3(-0.62, 0.34, -0.55));

  float ao = occlusion(p, n);
  float shadow = softShadow(p + n * 0.01, key);

  // Anodised dark aluminium: low albedo, sharp specular.
  vec3 albedo = vec3(0.095, 0.102, 0.096);

  float ndl = max(dot(n, key), 0.0);
  vec3 lit = albedo * ACCENT * (2.4 * ndl * shadow);
  lit += albedo * TECHNICAL * (1.15 * max(dot(n, fill), 0.0) * ao);
  lit += albedo * vec3(0.16, 0.18, 0.17) * (0.5 + 0.5 * n.y) * ao;

  vec3 hKey = normalize(key - rd);
  lit += ACCENT * (pow(max(dot(n, hKey), 0.0), 96.0) * 0.85 * shadow);
  vec3 hFill = normalize(fill - rd);
  lit += TECHNICAL * (pow(max(dot(n, hFill), 0.0), 40.0) * 0.32);

  // Grazing rays cover most of a thin plate seen from a low angle, so a strong
  // rim term washes the whole part out. Kept low and gated by occlusion.
  float fresnel = pow(1.0 - max(dot(n, -rd), 0.0), 5.0);
  lit += TECH_HI * (fresnel * 0.26 * ao);

  // Curvature: convex edges take a machined highlight, concave corners darken.
  // The scale is set by the sampling offset, not chosen by eye — for a surface
  // of radius R the four taps give curvature ~ e/R, so the fillet at 0.07
  // lands near 0.03 and a sharp edge saturates well above it. Widening the
  // band past ~0.075 would only start shading flats.
  float edge = smoothstep(0.010, 0.075, curvature);
  float crease = smoothstep(0.010, 0.075, -curvature);
  lit += mix(TECH_HI, ACCENT, 0.35) * (edge * 0.55);
  lit *= 1.0 - crease * 0.5;

  return lit;
}

vec3 shadeGround(vec3 p, float fade) {
  vec2 g = p.xz;
  vec2 grid = abs(fract(g * 2.0 - 0.5) - 0.5) / max(fwidth(g * 2.0), 1e-5);
  float line = 1.0 - min(min(grid.x, grid.y), 1.0);

  vec2 major = abs(fract(g * 0.5 - 0.5) - 0.5) / max(fwidth(g * 0.5), 1e-5);
  float majorLine = 1.0 - min(min(major.x, major.y), 1.0);

  vec3 col = vec3(0.012, 0.015, 0.013);
  col += TECHNICAL * (line * 0.05 + majorLine * 0.12);
  col *= softShadow(p + vec3(0.0, 0.002, 0.0), normalize(vec3(0.58, 0.76, 0.42))) * 0.75 + 0.25;
  return col * fade;
}

vec3 tonemap(vec3 x) {
  // Filmic curve with a clean toe; keeps the accent highlights from clipping
  // to white on the specular rims.
  x = max(x, 0.0);
  return (x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14);
}

float hash(vec2 p) {
  return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
}

void main() {
  // Normalise by the SHORTER axis, not the height. On a landscape hero that is
  // the height and nothing changes; on a portrait one it stops the part being
  // cropped left and right by a viewport narrower than the subject.
  vec2 uv = (2.0 * gl_FragCoord.xy - uRes) / min(uRes.x, uRes.y);
  uv -= uShift;

  float cy = cos(uCam.x), sy = sin(uCam.x);
  float cp = cos(uCam.y), sp = sin(uCam.y);
  vec3 ro = vec3(sy * cp, sp, cy * cp) * uCam.z;
  vec3 ta = vec3(0.0, 0.04, 0.0);

  vec3 fwd = normalize(ta - ro);
  vec3 right = normalize(cross(fwd, vec3(0.0, 1.0, 0.0)));
  vec3 up = cross(right, fwd);
  vec3 rd = normalize(uv.x * right + uv.y * up + 1.9 * fwd);

  int steps;
  float tPart = march(ro, rd, steps);

  float tGround = (rd.y < -1e-4) ? (GROUND - ro.y) / rd.y : -1.0;

  vec3 colour = vec3(0.0);
  float coverage = 0.0;
  float tNear = 1e9;

  if (tPart > 0.0 && (tGround < 0.0 || tPart < tGround)) {
    vec3 p = ro + rd * tPart;
    float curvature;
    vec3 n = normalAndCurvature(p, curvature);
    colour = shadePart(p, rd, n, curvature);
    coverage = 1.0;
    tNear = tPart;
  } else if (tGround > 0.0) {
    vec3 p = ro + rd * tGround;
    // Two fades, not one. Radius alone ends the disc in a hard arc that the
    // low camera flattens into a diagonal seam across the frame; multiplying
    // by the grazing angle dissolves it into the background at the horizon.
    float fade = (1.0 - smoothstep(3.2, 9.0, length(p.xz)))
               * smoothstep(0.015, 0.17, -rd.y);
    if (fade > 0.001) {
      colour = shadeGround(p, fade);
      coverage = fade;
    }
    tNear = tGround;
  }

  vec3 srgb = pow(tonemap(colour), vec3(1.0 / 2.2));
  vec3 outColour = mix(BG, srgb, coverage);

  // Sketch geometry is emissive and composites in front of whatever it covers.
  if (uSketch > 0.001 && rd.y < -1e-4) {
    float tSketch = -ro.y / rd.y;
    if (tSketch > 0.0 && tSketch < tNear + 0.02) {
      vec3 p = ro + rd * tSketch;
      float px = tSketch * 2.0 / (min(uRes.x, uRes.y) * 0.95);
      float intensity = sketchField(p.xz, px);
      // A scanning wipe reveals the profile rather than fading it in flat.
      float sweep = smoothstep(-1.6, 1.6, (p.x + p.z) * 0.5 - (1.0 - uSketch) * 4.0 + 2.0);
      float glow = intensity * uSketch * sweep;
      outColour += pow(TECH_HI, vec3(1.0 / 2.2)) * glow * 0.95;
    }
  }

  // Vignette toward the page background, then dither out the banding the dark
  // gradient would otherwise show on an 8-bit display.
  float vig = 1.0 - 0.28 * dot(uv, uv) * 0.25;
  outColour = mix(BG, outColour, clamp(vig, 0.0, 1.0));
  outColour += (hash(gl_FragCoord.xy + fract(uTime)) - 0.5) / 255.0;

  fragColor = vec4(outColour, 1.0);
}`;

const UNIFORM_NAMES = [
  'uRes', 'uCam', 'uShift', 'uTime',
  'uSketch', 'uExtrude', 'uBoss', 'uFillet', 'uDrill', 'uBore', 'uPocket',
] as const;

type UniformName = (typeof UNIFORM_NAMES)[number];

function compile(gl: WebGL2RenderingContext, type: number, source: string): WebGLShader | null {
  const shader = gl.createShader(type);
  if (!shader) return null;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    console.warn('hero: shader compile failed\n' + gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

function link(gl: WebGL2RenderingContext): WebGLProgram | null {
  const vs = compile(gl, gl.VERTEX_SHADER, VERT);
  const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
  if (!vs || !fs) return null;
  const program = gl.createProgram();
  if (!program) return null;
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.warn('hero: program link failed\n' + gl.getProgramInfoLog(program));
    gl.deleteProgram(program);
    return null;
  }
  return program;
}

const easeInOut = (x: number): number =>
  x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x);

export function createHero(canvas: HTMLCanvasElement, opts: HeroOptions = {}): HeroHandle | null {
  const gl = canvas.getContext('webgl2', {
    alpha: false,
    antialias: false,
    depth: false,
    stencil: false,
    desynchronized: true,
    powerPreference: 'high-performance',
  });
  if (!gl) return null;

  const program = link(gl);
  if (!program) return null;

  const loc = {} as Record<UniformName, WebGLUniformLocation | null>;
  for (const name of UNIFORM_NAMES) loc[name] = gl.getUniformLocation(program, name);

  const vao = gl.createVertexArray();
  gl.bindVertexArray(vao);
  gl.useProgram(program);

  // Raymarching cost is per pixel, so the budget is pixels, not a scale factor:
  // a 4K display gets a lower multiplier rather than sixteen times the work.
  // The budget then adapts to what the GPU actually delivers, because "which
  // GPU is this" is not knowable up front and an integrated part running this
  // shader at native resolution would crawl.
  // A phone GPU raymarching a boolean field is the case most likely to be
  // answered by the driver reclaiming the context, so it gets a lower ceiling
  // than a workstation.
  const SMALL_VIEWPORT = window.matchMedia('(max-width: 60rem)').matches;
  const PIXEL_CEILING = SMALL_VIEWPORT ? 450_000 : 900_000;
  const PIXEL_FLOOR = SMALL_VIEWPORT ? 120_000 : 200_000;
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

  // Pointer parallax, in normalised screen space, eased toward the target so a
  // fast flick glides instead of snapping.
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
    { rootMargin: '120px' },
  );
  observer.observe(canvas);

  const onVisibility = () => { if (!document.hidden) last = performance.now(); };
  document.addEventListener('visibilitychange', onVisibility);

  const resizeObserver = new ResizeObserver(() => { resize(); });
  resizeObserver.observe(canvas);

  // Context loss. `preventDefault()` here is not defensive tidiness — it is the
  // only thing that makes the loss recoverable at all: without it the browser
  // never fires `webglcontextrestored` and the canvas stays a blank rectangle
  // for the life of the page. Every GL object above is already gone, so this
  // handle is finished; the caller reinstates the product screenshot and builds
  // a fresh renderer if and when the context comes back.
  let contextAlive = true;
  const onLost = (event: Event) => {
    event.preventDefault();
    if (!contextAlive) return;
    contextAlive = false;
    cancelAnimationFrame(frame);
    opts.onLost?.();
  };
  canvas.addEventListener('webglcontextlost', onLost);

  let clock = 0;
  let last = performance.now();
  let frame = 0;
  let reportedStage = -2;

  function draw(elapsed: number) {
    const gl2 = gl!;
    // The unwind runs the history backwards, so a cycle ends where it began.
    const unwind = easeInOut((elapsed - HERO_HOLD_END) / (HERO_CYCLE - HERO_HOLD_END));
    const alive = 1 - unwind;

    let active = -1;
    const values: Record<string, number> = {
      Sketch: 0, Extrude: 0, Boss: 0, Fillet: 0, Drill: 0, Bore: 0, Pocket: 0,
    };
    for (let i = 0; i < HERO_STAGES.length; i++) {
      const stage = HERO_STAGES[i];
      const raw = (elapsed - stage.start) / (stage.end - stage.start);
      values[stage.uniform] = easeInOut(raw) * (stage.uniform === 'Sketch' ? 1 : alive);
      if (raw > 0 && raw < 1) active = i;
    }
    // The sketch stays visible while the part is unwinding back to it.
    values.Sketch = Math.max(values.Sketch * (1 - easeInOut((elapsed - 2.2) / 1.2)), unwind);

    if (unwind > 0) active = -1;
    const building = active >= 0;
    const reported = building ? active : HERO_STAGES.length - 1;
    if (opts.onStage && reported !== reportedStage) {
      reportedStage = reported;
      opts.onStage(reported, building);
    }

    pointerX += (targetX - pointerX) * 0.06;
    pointerY += (targetY - pointerY) * 0.06;

    // Slow during the build so the geometry reads; opens up for the showcase.
    const showcase = easeInOut((elapsed - HERO_HOLD_END + 4.2) / 4.2);
    const yaw = -0.55 + elapsed * (0.1 + 0.14 * showcase) + pointerX * 0.42;
    // High enough to look into the pockets and down the holes: the drilled
    // features are the subject, and a low camera hides all of them.
    const pitch = 0.62 + Math.sin(elapsed * 0.21) * 0.06 - pointerY * 0.15;

    // Wide viewports centre the part just right of the copy, so the
    // FeatureManager panel floats over its far half like a HUD; narrow ones
    // centre it outright. Subtracting from uv walks the view, so a positive
    // offset pushes the subject the other way. uv is normalised by height, so
    // targeting fraction f of the width means an offset of (f - 0.5) * 2 * aspect.
    const aspect = width / Math.max(1, height);
    const wide = !opts.centered && aspect > 1.15;
    // In the framed box the part is only walked aside while the panel is
    // actually docked over it. Once the panel drops below the render there is
    // nothing to clear, so the subject sits centred and a little closer.
    const docked = opts.centered && (opts.panelDocked?.() ?? true);

    gl2.uniform2f(loc.uRes, width, height);
    // The framed showcase box is much shorter than the old full-bleed hero,
    // and uv is normalised by the shorter axis: the same part at the same
    // distance would crop. uShift signs follow the FRAG (uv -= uShift):
    // negative x walks the subject left, clear of the docked panel.
    const distance = opts.centered ? (docked ? 7.2 : 6.1) - 0.3 * showcase : 6.6 - 0.4 * showcase;
    gl2.uniform3f(loc.uCam, yaw, pitch, distance);
    gl2.uniform2f(
      loc.uShift,
      wide ? 0.16 * aspect : docked ? -0.62 : 0.0,
      wide ? -0.08 : opts.centered ? -0.10 : 0.06,
    );
    gl2.uniform1f(loc.uTime, elapsed);
    gl2.uniform1f(loc.uSketch, values.Sketch);
    gl2.uniform1f(loc.uExtrude, values.Extrude);
    gl2.uniform1f(loc.uBoss, values.Boss);
    gl2.uniform1f(loc.uFillet, values.Fillet);
    gl2.uniform1f(loc.uDrill, values.Drill);
    gl2.uniform1f(loc.uBore, values.Bore);
    gl2.uniform1f(loc.uPocket, values.Pocket);

    gl2.drawArrays(gl2.TRIANGLES, 0, 3);
  }

  if (opts.still) {
    // One frame, parked on the finished part: reduced motion should still get
    // the subject, just not the movement.
    resize();
    draw(HERO_HOLD_END - 0.6);
  } else {
    // Frame-time EMA drives the pixel budget. Sampling in blocks rather than
    // every frame keeps a single hitch — a GC pause, a tab switch — from
    // permanently downgrading the render, and the deadband stops the two
    // thresholds oscillating against each other.
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

      clock = (clock + Math.min(delta / 1000, 0.05)) % HERO_CYCLE;
      resize();
      draw(clock);
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
      gl.deleteProgram(program);
      gl.deleteVertexArray(vao);
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
