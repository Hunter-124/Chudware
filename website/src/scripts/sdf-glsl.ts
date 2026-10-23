/**
 * GLSL shared by the raymarched showcases: brand palette, primitives, the
 * tetrahedron normal with its curvature estimate, soft shadow, ambient
 * occlusion, the grid floor, and the filmic tonemap.
 *
 * Every chunk that samples the scene calls `map(vec3)`, which the including
 * shader defines after this prelude. GLSL ES 3.0 allows the forward
 * declaration, so the shared shading never needs to know the shape.
 */

export interface PreludeOptions {
  /**
   * Longest step the shadow march may take. An exact SDF can take 0.3 safely;
   * a heightfield stock cannot, because `y - h(x, z)` is the gap to the floor
   * below and says nothing about a wall beside the ray. A shadow step longer
   * than the thinnest wall can tunnel straight through it, and the shadow it
   * should have cast leaks.
   */
  readonly shadowStep: number;
  /** Shadow march iterations; more steps are needed once each is shorter. */
  readonly shadowSteps: number;
  /**
   * Normal sampling offset. Set by the finest feature the field resolves:
   * below a heightfield's cell size the taps land inside one bilinear cell
   * and the normal reads the interpolation, not the surface.
   */
  readonly normalEpsilon: number;
}

export function glslPrelude({ shadowStep, shadowSteps, normalEpsilon }: PreludeOptions): string {
  return `#version 300 es
precision highp float;

out vec4 fragColor;

uniform vec2  uRes;
uniform vec3  uCam;      // yaw, pitch, distance
uniform vec2  uShift;    // composition offset in NDC; positive pushes the subject right and up
uniform float uTime;

// sRGB page background. Composited after tone mapping so the canvas edge is
// invisible against the document.
const vec3  BG        = vec3(0.0667, 0.0745, 0.0627);
const vec3  ACCENT    = vec3(1.0000, 0.2543, 0.0468);  // #ff8a3d, linear
const vec3  TECHNICAL = vec3(0.0884, 0.6724, 0.4620);  // #53d6b5, linear
const vec3  TECH_HI   = vec3(0.2582, 0.7911, 0.6239);  // #8be6cf, linear

const vec3 KEY_LIGHT  = vec3(0.58, 0.76, 0.42);
const vec3 FILL_LIGHT = vec3(-0.62, 0.34, -0.55);

float map(vec3 p);

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

float sdRoundRect(vec2 p, vec2 b, float r) {
  vec2 d = abs(p) - b + r;
  return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0) - r;
}

float sdBox(vec3 p, vec3 b) {
  vec3 d = abs(p) - b;
  return length(max(d, 0.0)) + min(max(d.x, max(d.y, d.z)), 0.0);
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
// Sampling the field
// ---------------------------------------------------------------------------

/**
 * Tetrahedron normal, with the local mean curvature that falls out of the same
 * four taps: convex edges positive, concave corners negative.
 *
 * The baseline subtraction is not optional. The march stops short of the
 * surface by more than the sampling offset, so without subtracting d(p) the
 * residual divides through and lands near 1.0 on *every* surface: the edge
 * term saturates and the whole part turns milky. One extra tap buys a
 * curvature that is actually about shape.
 *
 * The curvature is only meaningful where the field is a distance: its second
 * difference is taken relative to the gradient magnitude, and it fades out
 * entirely where that magnitude leaves 1. An exact SDF is unaffected; on a
 * heightfield wall the field climbs thirty times faster than distance and the
 * second difference is about the bilinear cells, not the shape — left in, the
 * edge highlight stripes every wall. Such surfaces draw their own edges from
 * the field they come from.
 */
vec3 normalAndCurvature(vec3 p, out float curvature) {
  const float e = ${normalEpsilon.toFixed(4)};
  vec2 k = vec2(1.0, -1.0);
  float base = map(p);
  float a = map(p + k.xyy * e);
  float b = map(p + k.yyx * e);
  float c = map(p + k.yxy * e);
  float d = map(p + k.xxx * e);
  vec3 g = k.xyy * a + k.yyx * b + k.yxy * c + k.xxx * d;
  float slope = max(length(g) / (4.0 * e), 1e-3);
  float trust = 1.0 - smoothstep(1.3, 2.5, slope);
  curvature = (a + b + c + d - 4.0 * base) / (4.0 * e) / slope * trust;
  return g / max(length(g), 1e-9);
}

float softShadow(vec3 ro, vec3 rd) {
  float res = 1.0;
  float t = 0.05;
  for (int i = 0; i < ${shadowSteps}; i++) {
    float d = map(ro + rd * t);
    res = min(res, 12.0 * d / t);
    t += clamp(d, 0.02, ${shadowStep.toFixed(3)});
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
// Shading
// ---------------------------------------------------------------------------

/**
 * Two-light metal: a key of colour \`keyCol\` from the accent side, cool fill
 * from the technical teal, curvature-driven edge highlight and crease
 * darkening. \`albedo\` picks the material — dark anodised or bright bare
 * aluminium.
 */
vec3 shadeMetal(vec3 p, vec3 rd, vec3 n, float curvature, vec3 albedo, vec3 keyCol) {
  vec3 key = normalize(KEY_LIGHT);
  vec3 fill = normalize(FILL_LIGHT);

  float ao = occlusion(p, n);
  float shadow = softShadow(p + n * 0.01, key);

  float ndl = max(dot(n, key), 0.0);
  vec3 lit = albedo * keyCol * (2.4 * ndl * shadow);
  lit += albedo * TECHNICAL * (1.15 * max(dot(n, fill), 0.0) * ao);
  lit += albedo * vec3(0.16, 0.18, 0.17) * (0.5 + 0.5 * n.y) * ao;

  vec3 hKey = normalize(key - rd);
  lit += keyCol * (pow(max(dot(n, hKey), 0.0), 96.0) * 0.85 * shadow);
  vec3 hFill = normalize(fill - rd);
  lit += TECHNICAL * (pow(max(dot(n, hFill), 0.0), 40.0) * 0.32);

  // Grazing rays cover most of a thin plate seen from a low angle, so a strong
  // rim term washes the whole part out. Kept low and gated by occlusion.
  float fresnel = pow(1.0 - max(dot(n, -rd), 0.0), 5.0);
  lit += TECH_HI * (fresnel * 0.26 * ao);

  // Curvature: convex edges take a machined highlight, concave corners darken.
  // The scale is set by the sampling offset, not chosen by eye — for a surface
  // of radius R the four taps give curvature ~ e/R, so a 0.07 fillet lands
  // near 0.03 and a sharp edge saturates well above it. Widening the band
  // past ~0.075 would only start shading flats.
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
  col *= softShadow(p + vec3(0.0, 0.002, 0.0), normalize(KEY_LIGHT)) * 0.75 + 0.25;
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

// ---------------------------------------------------------------------------
// Camera
// ---------------------------------------------------------------------------

/** Orbit camera from uCam (yaw, pitch, distance) looking at \`target\`. */
void cameraRay(vec3 target, out vec3 ro, out vec3 rd) {
  // Normalise by the SHORTER axis, not the height. On a landscape stage that
  // is the height and nothing changes; on a portrait one it stops the subject
  // being cropped left and right by a viewport narrower than it is.
  vec2 uv = (2.0 * gl_FragCoord.xy - uRes) / min(uRes.x, uRes.y);
  uv -= uShift;

  float cy = cos(uCam.x), sy = sin(uCam.x);
  float cp = cos(uCam.y), sp = sin(uCam.y);
  ro = vec3(sy * cp, sp, cy * cp) * uCam.z + target;

  vec3 fwd = normalize(target - ro);
  vec3 right = normalize(cross(fwd, vec3(0.0, 1.0, 0.0)));
  vec3 up = cross(right, fwd);
  rd = normalize(uv.x * right + uv.y * up + 1.9 * fwd);
}

/** Vignette toward the page, then dither out 8-bit banding on the dark ramp. */
vec3 finish(vec3 outColour) {
  vec2 uv = (2.0 * gl_FragCoord.xy - uRes) / min(uRes.x, uRes.y) - uShift;
  float vig = 1.0 - 0.28 * dot(uv, uv) * 0.25;
  outColour = mix(BG, outColour, clamp(vig, 0.0, 1.0));
  outColour += (hash(gl_FragCoord.xy + fract(uTime)) - 0.5) / 255.0;
  return outColour;
}
`;
}

export const STAGE_UNIFORMS = ['uRes', 'uCam', 'uShift', 'uTime'] as const;
