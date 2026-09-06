/**
 * The part-builder showcase: a parametric part built feature by feature,
 * raymarched.
 *
 * Why a signed distance field rather than a mesh and a 3D library: the subject
 * is a CAD part whose whole point is boolean history. In an SDF an extrude, a
 * fillet, and four drilled holes are `min`, a rounded extrusion radius, and
 * `max(d, -cylinder)` — the booleans are exact and animating them costs a
 * uniform, not a re-tessellation. It also means no dependency on the site's
 * critical path: this file, the shared stage, and a WebGL2 context are the
 * entire renderer.
 */
import { HERO_CYCLE, HERO_HOLD_END, HERO_STAGES } from './hero-timeline';
import { glslPrelude, STAGE_UNIFORMS } from './sdf-glsl';
import { createStage, easeInOut, type StageHandle } from './stage';

export interface HeroOptions {
  /** Called whenever the active stage index changes; -1 while unwinding. */
  onStage?: (index: number, building: boolean) => void;
  /** Render a single frame and stop. Used for prefers-reduced-motion. */
  still?: boolean;
  /** Framed showcase box (no copy column) rather than the full-bleed hero. */
  centered?: boolean;
  /**
   * Only consulted with `centered`. True while the FeatureManager panel is
   * docked over the render, so the part is walked aside to clear it.
   */
  panelDocked?: () => boolean;
  /** The driver took the context away; see `StageOptions.onLost`. */
  onLost?: () => void;
}

export type HeroHandle = StageHandle;

const FRAG = glslPrelude({ shadowStep: 0.3, shadowSteps: 22, normalEpsilon: 0.0022 }) + `
uniform float uSketch;
uniform float uExtrude;
uniform float uBoss;
uniform float uFillet;
uniform float uDrill;
uniform float uBore;
uniform float uPocket;

const vec2  PLATE   = vec2(1.35, 0.92);   // half extents in x, z
const float CORNER  = 0.26;               // corner radius of the sketch profile
const float GROUND  = -0.62;
const float BOSS_R  = 0.42;
const float BORE_R  = 0.24;
const float HOLE_R  = 0.11;
const vec4  HOLES   = vec4(1.10, 0.62, 0.0, 0.0);  // |x|, |z| of the four holes

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

float march(vec3 ro, vec3 rd) {
  float entry = boundsEntry(ro, rd);
  if (entry < 0.0) return -1.0;
  float t = entry;
  for (int i = 0; i < 72; i++) {
    vec3 p = ro + rd * t;
    if (dot(p, p) > 5.5) return -1.0;
    float d = map(p);
    if (d < 0.0006 * t) return t;
    t += d;
    if (t > 9.0) break;
  }
  return -1.0;
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

void main() {
  vec3 ro, rd;
  cameraRay(vec3(0.0, 0.04, 0.0), ro, rd);

  float tPart = march(ro, rd);
  float tGround = (rd.y < -1e-4) ? (GROUND - ro.y) / rd.y : -1.0;

  vec3 colour = vec3(0.0);
  float coverage = 0.0;
  float tNear = 1e9;

  if (tPart > 0.0 && (tGround < 0.0 || tPart < tGround)) {
    vec3 p = ro + rd * tPart;
    float curvature;
    vec3 n = normalAndCurvature(p, curvature);
    // Anodised dark aluminium: low albedo, sharp specular.
    colour = shadeMetal(p, rd, n, curvature, vec3(0.095, 0.102, 0.096), ACCENT);
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

  fragColor = vec4(finish(outColour), 1.0);
}`;

const UNIFORMS = [
  ...STAGE_UNIFORMS,
  'uSketch', 'uExtrude', 'uBoss', 'uFillet', 'uDrill', 'uBore', 'uPocket',
] as const;

export function createHero(canvas: HTMLCanvasElement, opts: HeroOptions = {}): HeroHandle | null {
  let reportedStage = -2;

  return createStage(canvas, {
    frag: FRAG,
    uniforms: UNIFORMS,
    cycle: HERO_CYCLE,
    still: opts.still,
    // Parked on the finished part: reduced motion should still get the
    // subject, just not the movement.
    stillAt: HERO_HOLD_END - 0.6,
    onLost: opts.onLost,
    frame({ gl, loc, elapsed, width, height, cssWidth, pointerX, pointerY }) {
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

      gl.uniform2f(loc.uRes, width, height);
      // The framed showcase box is much shorter than the old full-bleed hero,
      // and uv is normalised by the shorter axis: the same part at the same
      // distance would crop. uShift signs follow the FRAG (uv -= uShift):
      // negative x walks the subject left, clear of the docked panel.
      // On a phone the framed box is a third the size, so the same distance
      // leaves the part a thumbnail; step in and let the pitch do the rest.
      const compact = cssWidth < 520 ? 0.82 : 1;
      const distance = (opts.centered ? (docked ? 7.2 : 6.1) - 0.3 * showcase : 6.6 - 0.4 * showcase) * compact;
      gl.uniform3f(loc.uCam, yaw, pitch, distance);
      gl.uniform2f(
        loc.uShift,
        wide ? 0.16 * aspect : docked ? -0.62 : 0.0,
        wide ? -0.08 : opts.centered ? -0.10 : 0.06,
      );
      gl.uniform1f(loc.uTime, elapsed);
      gl.uniform1f(loc.uSketch, values.Sketch);
      gl.uniform1f(loc.uExtrude, values.Extrude);
      gl.uniform1f(loc.uBoss, values.Boss);
      gl.uniform1f(loc.uFillet, values.Fillet);
      gl.uniform1f(loc.uDrill, values.Drill);
      gl.uniform1f(loc.uBore, values.Bore);
      gl.uniform1f(loc.uPocket, values.Pocket);
    },
  });
}
