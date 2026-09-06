/**
 * The machining showcase: a billet becomes a part as a CAM job runs.
 *
 * The stock is a dexel heightfield — the same top-down model the product's
 * stock simulation uses (`core/cam/heightfield`), and the reason the animation
 * is honest rather than a crossfade: every frame the cutter is stamped into
 * the grid along the toolpath actually generated for the operation, so the
 * remaining material is whatever the tool has really swept. The renderer
 * raymarches `stock ∩ {y < h(x, z)}` straight from that grid, with the tool as
 * an SDF in the same field, so the cutter casts its shadow on the floor it is
 * cutting and the curvature-driven edge highlight picks out every new wall.
 *
 * Toolpaths are generated here, not stored: facing zigzags, a zigzag pocket
 * with a finishing contour at two stepdowns, three-stepdown offset contours
 * around the part, and four drilled holes. Each is fitted to its row in the
 * Operations table, so the table and the render cannot disagree.
 */
import {
  MACHINING_CYCLE, MACHINING_HOLD_END, MACHINING_OPS, type MachiningOp, type ToolKind,
} from './machining-timeline';
import { glslPrelude, STAGE_UNIFORMS } from './sdf-glsl';
import { createStage, easeInOut, type StageHandle } from './stage';

export interface MachiningOptions {
  /** Called whenever the active operation index changes; `cutting` false during the hold and restore. */
  onOp?: (index: number, cutting: boolean) => void;
  /** Render a single frame and stop. Used for prefers-reduced-motion. */
  still?: boolean;
  /** The driver took the context away; see `StageOptions.onLost`. */
  onLost?: () => void;
}

export type MachiningHandle = StageHandle;

// ---------------------------------------------------------------------------
// Scene constants, shared between the CPU stock model and the shader.
// ---------------------------------------------------------------------------

/** Stock half extents; the shader has the same numbers as `S`. */
const SX = 1.25;
const SY = 0.25;
const SZ = 0.85;
/** Heightfield resolution. ~0.0065 units per cell, square to within 1%. */
const W = 384;
const H = 262;
const CELL_X = (2 * SX) / W;
const CELL_Z = (2 * SZ) / H;
const CELL = Math.min(CELL_X, CELL_Z);
const SAFE_Y = SY + 0.3;
const FACE_Y = SY - 0.03;
/** Where the spindle waits before the first cut and after the last. */
const PARK = { x: SX + 0.5, y: SY + 0.12, z: 0 };

// ---------------------------------------------------------------------------
// Toolpath generation
// ---------------------------------------------------------------------------

interface Pt { x: number; y: number; z: number; cut: boolean }

interface Seg {
  x0: number; y0: number; z0: number;
  x1: number; y1: number; z1: number;
  /** Absolute cycle time this move begins and ends. */
  t0: number; t1: number;
  cut: boolean;
}

interface OpPath {
  readonly op: MachiningOp;
  readonly segs: readonly Seg[];
}

/** Closed polyline around a rounded rectangle, counter-clockwise from the +x side. */
function roundedRect(cx: number, cz: number, hx: number, hz: number, c: number, y: number): Pt[] {
  const pts: Pt[] = [];
  const corners = [
    [hx - c, hz - c, 0], [-(hx - c), hz - c, Math.PI / 2],
    [-(hx - c), -(hz - c), Math.PI], [hx - c, -(hz - c), 1.5 * Math.PI],
  ] as const;
  const ARC = 8;
  for (const [ox, oz, a0] of corners) {
    for (let k = 0; k <= ARC; k++) {
      const a = a0 + (k / ARC) * (Math.PI / 2);
      pts.push({ x: cx + ox + Math.cos(a) * c, z: cz + oz + Math.sin(a) * c, y, cut: true });
    }
  }
  pts.push({ ...pts[0] });
  return pts;
}

/** Half-width of a rounded rectangle's chord at height z from its centre. */
function chordHalfWidth(z: number, hx: number, hz: number, c: number): number {
  const dz = Math.abs(z) - (hz - c);
  if (dz <= 0) return hx;
  return hx - c + Math.sqrt(Math.max(0, c * c - dz * dz));
}

/**
 * Zigzag over the region whose row extents `extent(z)` returns, from
 * `zStart` to `zEnd` in `rows` passes, at depth `y`. Alternating direction so
 * the stepover link is a short cutting move, as a real pocket cycle does it.
 */
function zigzag(
  zStart: number, zEnd: number, rows: number, y: number,
  extent: (z: number) => readonly [number, number], out: Pt[],
) {
  for (let j = 0; j < rows; j++) {
    const z = zStart + ((zEnd - zStart) * j) / Math.max(1, rows - 1);
    const [xa, xb] = extent(z);
    const forward = j % 2 === 0;
    if (j === 0) {
      out.push({ x: forward ? xa : xb, y: SAFE_Y, z, cut: false });
      out.push({ x: forward ? xa : xb, y, z, cut: true });
    } else {
      out.push({ x: forward ? xa : xb, y, z, cut: true });
    }
    out.push({ x: forward ? xb : xa, y, z, cut: true });
  }
}

function facePath(r: number): Pt[] {
  const pts: Pt[] = [];
  const over = r + 0.06;
  const zStart = -(SZ + 0.25 * r);
  const zEnd = SZ + 0.25 * r;
  zigzag(zStart, zEnd, 7, FACE_Y, () => [-(SX + over), SX + over], pts);
  const last = pts[pts.length - 1];
  pts.push({ x: last.x, y: SAFE_Y, z: last.z, cut: false });
  return pts;
}

/** Pocket geometry, shared by the toolpath and the drill placement below. */
const POCKET = { cx: 0.15, cz: 0, hx: 0.42, hz: 0.24, c: 0.14 };
const OUTLINE = { hx: 0.78, hz: 0.5, c: 0.2 };

function pocketPath(r: number): Pt[] {
  const pts: Pt[] = [];
  const hx = POCKET.hx - r;
  const hz = POCKET.hz - r;
  const c = POCKET.c - r;
  const depths = [FACE_Y - 0.1, FACE_Y - 0.2];
  for (const y of depths) {
    if (pts.length) {
      const last = pts[pts.length - 1];
      pts.push({ x: last.x, y: SAFE_Y, z: last.z, cut: false });
    }
    const inset = 0.02;
    zigzag(-(hz - inset), hz - inset, 7, y, (z) => {
      const half = chordHalfWidth(z - POCKET.cz, hx, hz, c) - inset;
      return [POCKET.cx - half, POCKET.cx + half];
    }, pts);
    // Finishing contour around the wall at this depth.
    for (const p of roundedRect(POCKET.cx, POCKET.cz, hx, hz, c, y)) pts.push(p);
  }
  const last = pts[pts.length - 1];
  pts.push({ x: last.x, y: SAFE_Y, z: last.z, cut: false });
  return pts;
}

function contourPath(r: number): Pt[] {
  const pts: Pt[] = [];
  const floor = -SY + 0.03;
  const depths = [FACE_Y - 0.147, FACE_Y - 0.294, floor];
  const offsets = [r + 0.09, r];
  let first = true;
  for (const y of depths) {
    for (const o of offsets) {
      const loop = roundedRect(0, 0, OUTLINE.hx + o, OUTLINE.hz + o, OUTLINE.c + o, y);
      if (first) {
        pts.push({ x: loop[0].x, y: SAFE_Y, z: loop[0].z, cut: false });
        first = false;
      }
      // Plunge (or step over) to the loop start, then run it.
      pts.push({ ...loop[0] });
      for (let i = 1; i < loop.length; i++) pts.push(loop[i]);
    }
  }
  const last = pts[pts.length - 1];
  pts.push({ x: last.x, y: SAFE_Y, z: last.z, cut: false });
  return pts;
}

function drillPath(): Pt[] {
  const pts: Pt[] = [];
  const holes = [[0.58, 0.36], [-0.58, 0.36], [-0.58, -0.36], [0.58, -0.36]] as const;
  for (const [x, z] of holes) {
    pts.push({ x, y: SAFE_Y, z, cut: false });
    pts.push({ x, y: -SY - 0.02, z, cut: true });
    pts.push({ x, y: SAFE_Y, z, cut: false });
  }
  return pts;
}

/**
 * Time the moves so the operation fills its window exactly. Rapids run at 3.5×
 * the cutting feed and plunges at 0.6×, which is roughly the ratio a real
 * post would produce; the absolute feed is whatever makes the sum fit.
 */
function schedule(op: MachiningOp, from: Pt, pts: readonly Pt[]): Seg[] {
  const window = op.end - op.start;
  const moves: { a: Pt; b: Pt; len: number; k: number }[] = [];
  let weighted = 0;
  let prev = from;
  for (const p of pts) {
    const dx = p.x - prev.x, dy = p.y - prev.y, dz = p.z - prev.z;
    const len = Math.hypot(dx, dy, dz);
    const plunge = Math.hypot(dx, dz) < 1e-6;
    const k = !p.cut ? 3.5 : plunge ? 0.6 : 1;
    moves.push({ a: prev, b: p, len, k });
    weighted += len / k;
    prev = p;
  }
  const feed = weighted / window;
  const segs: Seg[] = [];
  let t = op.start;
  for (const m of moves) {
    const dur = m.len / (m.k * feed);
    segs.push({
      x0: m.a.x, y0: m.a.y, z0: m.a.z, x1: m.b.x, y1: m.b.y, z1: m.b.z,
      t0: t, t1: t + dur, cut: m.b.cut,
    });
    t += dur;
  }
  return segs;
}

function buildJob(): (OpPath | null)[] {
  const paths: (OpPath | null)[] = [];
  let from: Pt = { ...PARK, cut: false };
  for (const op of MACHINING_OPS) {
    if (op.radius === 0) { paths.push(null); continue; }
    const pts = op.tool === 'face' ? facePath(op.radius)
      : op.tool === 'drill' ? drillPath()
      : op.label.startsWith('Pocket') ? pocketPath(op.radius)
      : contourPath(op.radius);
    paths.push({ op, segs: schedule(op, from, pts) });
    from = pts[pts.length - 1];
  }
  return paths;
}

// ---------------------------------------------------------------------------
// Stock model
// ---------------------------------------------------------------------------

/** Cut levels a cell remembers; deeper than this and the nearest two merge. */
const LEVELS = 4;

/**
 * RGBA per cell: R = surface height, G = programmed path of the running
 * operation, B = time the cutter centre last passed (or -1000), A unused.
 *
 * The surface height is not stamped directly. A cutter edge crossing a cell
 * grid produces toothed walls, and the teeth read as a comb of key-lit and
 * fill-lit facets down every curved wall. So each cell keeps the depths it has
 * been cut to and, per depth, the largest fraction of its area the cutter has
 * covered there; the surface is the mean height those nested cuts imply,
 * \`top − Σ cover·Δdepth\`. A wall is then a one-cell ramp whose position
 * carries the sub-cell edge, which bilinear filtering turns into a smooth
 * face — and because the levels are a set, not a history, the result does not
 * depend on which pass reached the cell first.
 */
class Stock {
  readonly data = new Float32Array(W * H * 4);
  private readonly depth = new Float32Array(W * H * LEVELS);
  private readonly cover = new Float32Array(W * H * LEVELS);
  private readonly count = new Uint8Array(W * H);
  dirtyMin = H;
  dirtyMax = -1;
  private simTime = 0;
  private programmed = -1;

  constructor(private readonly job: readonly (OpPath | null)[]) {
    this.reset();
  }

  reset() {
    const d = this.data;
    for (let i = 0; i < W * H; i++) {
      d[i * 4] = SY;
      d[i * 4 + 1] = 0;
      d[i * 4 + 2] = -1000;
      d[i * 4 + 3] = 0;
    }
    this.count.fill(0);
    this.simTime = 0;
    this.programmed = -1;
    this.dirtyMin = 0;
    this.dirtyMax = H - 1;
  }

  private touch(j0: number, j1: number) {
    if (j0 < this.dirtyMin) this.dirtyMin = j0;
    if (j1 > this.dirtyMax) this.dirtyMax = j1;
  }

  /** Record a cut of fraction `c` of cell `k` at `depth`, then refresh its height. */
  private cut(k: number, depth: number, c: number) {
    const { depth: lv, cover } = this;
    const o = k * LEVELS;
    let n = this.count[k];
    let i = 0;
    // Levels are kept shallowest first.
    while (i < n && lv[o + i] > depth + 5e-4) i++;
    if (i < n && Math.abs(lv[o + i] - depth) <= 5e-4) {
      if (c > cover[o + i]) cover[o + i] = c;
    } else if (n < LEVELS) {
      for (let j = n; j > i; j--) {
        lv[o + j] = lv[o + j - 1];
        cover[o + j] = cover[o + j - 1];
      }
      lv[o + i] = depth;
      cover[o + i] = c;
      this.count[k] = ++n;
    } else {
      // Full: fold into the nearest neighbour by depth. A ramp or plunge lands
      // here, and its steps are close enough that the merge error is
      // invisible. Taking the deeper depth keeps the list ordered either way.
      let near = i < n ? i : n - 1;
      if (i > 0 && (i >= n || lv[o + i - 1] - depth <= depth - lv[o + i])) near = i - 1;
      lv[o + near] = Math.min(lv[o + near], depth);
      cover[o + near] = Math.max(cover[o + near], c);
    }
    // Cuts nest: anything a deeper level covers, every shallower level covers
    // too, so coverage is carried upward before the heights are summed.
    let h = SY;
    let carried = 0;
    for (let j = n - 1; j >= 0; j--) carried = cover[o + j] = Math.max(cover[o + j], carried);
    let prev = SY;
    for (let j = 0; j < n; j++) {
      h -= cover[o + j] * (prev - lv[o + j]);
      prev = lv[o + j];
    }
    this.data[k * 4] = h;
  }

  /** Lower every cell under the cutter to `y`, anti-aliased at the cutter's edge. */
  private stamp(x: number, z: number, y: number, r: number) {
    const depth = Math.max(y, -SY);
    const reach = r + CELL;
    const i0 = Math.max(0, Math.floor((x - reach + SX) / CELL_X));
    const i1 = Math.min(W - 1, Math.ceil((x + reach + SX) / CELL_X));
    const j0 = Math.max(0, Math.floor((z - reach + SZ) / CELL_Z));
    const j1 = Math.min(H - 1, Math.ceil((z + reach + SZ) / CELL_Z));
    if (i0 > i1 || j0 > j1) return;
    for (let j = j0; j <= j1; j++) {
      const dz = (j + 0.5) * CELL_Z - SZ - z;
      const row = j * W;
      for (let i = i0; i <= i1; i++) {
        const dx = (i + 0.5) * CELL_X - SX - x;
        // Fraction of the cell inside the cutter, from the signed distance of
        // its centre to the edge.
        const c = Math.min(1, 0.5 + (r - Math.sqrt(dx * dx + dz * dz)) / CELL);
        if (c > 0) this.cut(row + i, depth, c);
      }
    }
    this.touch(j0, j1);
  }

  private ink(x: number, z: number, channel: 1 | 2, value: number) {
    const i = Math.floor((x + SX) / CELL_X);
    const j = Math.floor((z + SZ) / CELL_Z);
    if (i < 0 || i >= W || j < 0 || j >= H) return;
    this.data[(j * W + i) * 4 + channel] = value;
    this.touch(j, j);
  }

  /** Draw the whole programmed path of operation `index`, replacing the previous one. */
  private program(index: number) {
    const d = this.data;
    for (let i = 0; i < W * H; i++) d[i * 4 + 1] = 0;
    this.touch(0, H - 1);
    this.programmed = index;
    const path = this.job[index];
    if (!path) return;
    for (const s of path.segs) {
      if (!s.cut) continue;
      const len = Math.hypot(s.x1 - s.x0, s.z1 - s.z0);
      const n = Math.max(1, Math.ceil(len / (CELL * 0.5)));
      for (let k = 0; k <= n; k++) {
        const u = k / n;
        this.ink(s.x0 + (s.x1 - s.x0) * u, s.z0 + (s.z1 - s.z0) * u, 1, 1);
      }
    }
  }

  private sweep(s: Seg, a: number, b: number, r: number) {
    const dx = s.x1 - s.x0, dy = s.y1 - s.y0, dz = s.z1 - s.z0;
    const len = Math.hypot(dx, dy, dz) * (b - a);
    // Overlapping discs spaced s apart scallop by ~s²/8r, so a big cutter can
    // be stamped far more sparsely than the one-cell trail behind it: at a
    // fifth of the radius the scallop is under a tenth of a cell.
    const stamps = Math.max(1, Math.ceil(len / Math.max(CELL * 0.4, r * 0.2)));
    for (let k = 1; k <= stamps; k++) {
      const u = a + ((b - a) * k) / stamps;
      this.stamp(s.x0 + dx * u, s.z0 + dz * u, s.y0 + dy * u, r);
    }
    const inks = Math.max(1, Math.ceil(len / (CELL * 0.4)));
    for (let k = 1; k <= inks; k++) {
      const u = a + ((b - a) * k) / inks;
      this.ink(s.x0 + dx * u, s.z0 + dz * u, 2, s.t0 + (s.t1 - s.t0) * u);
    }
  }

  /** Bring the stock up to cycle time `t`, however far that is. */
  advance(t: number) {
    if (t < this.simTime) this.reset();
    for (let i = 0; i < this.job.length; i++) {
      const path = this.job[i];
      if (!path) continue;
      if (path.op.end <= this.simTime) continue;
      if (path.op.start > t) break;
      if (this.programmed !== i) this.program(i);
      for (const s of path.segs) {
        if (s.t1 <= this.simTime) continue;
        if (s.t0 >= t) break;
        if (!s.cut) continue;
        const dur = s.t1 - s.t0;
        const a = Math.max(0, (this.simTime - s.t0) / dur);
        const b = Math.min(1, (t - s.t0) / dur);
        if (b > a) this.sweep(s, a, b, path.op.radius);
      }
    }
    this.simTime = t;
  }
}

/** Interpolated spindle position and the operation index it belongs to. */
function toolPose(
  job: readonly (OpPath | null)[], t: number, out: { x: number; y: number; z: number; op: number },
) {
  let lastEnd: Seg | null = null;
  for (let i = 0; i < job.length; i++) {
    const path = job[i];
    if (!path) continue;
    for (const s of path.segs) {
      if (t >= s.t1) { lastEnd = s; continue; }
      // Before the first move of the job the spindle is simply parked; the
      // operation has not begun.
      if (t < s.t0) { out.x = s.x0; out.y = s.y0; out.z = s.z0; out.op = -1; return; }
      const u = (t - s.t0) / (s.t1 - s.t0);
      out.x = s.x0 + (s.x1 - s.x0) * u;
      out.y = s.y0 + (s.y1 - s.y0) * u;
      out.z = s.z0 + (s.z1 - s.z0) * u;
      out.op = i;
      return;
    }
  }
  // Past the last move: glide back to the park position.
  const from = lastEnd ? { x: lastEnd.x1, y: lastEnd.y1, z: lastEnd.z1 } : PARK;
  const u = lastEnd ? easeInOut((t - lastEnd.t1) / 1.2) : 1;
  out.x = from.x + (PARK.x - from.x) * u;
  out.y = from.y + (PARK.y - from.y) * u;
  out.z = from.z + (PARK.z - from.z) * u;
  out.op = -1;
}

const TOOL_KIND: Record<ToolKind, number> = { endmill: 1, face: 2, drill: 3 };

// ---------------------------------------------------------------------------
// Shader
// ---------------------------------------------------------------------------

const FRAG = glslPrelude({ shadowStep: 0.05, shadowSteps: 40, normalEpsilon: 0.004 }) + `
uniform sampler2D uField;
uniform vec4  uTool;      // x, tip y, z, radius
uniform float uToolKind;  // 0 hidden · 1 end mill · 2 face mill · 3 drill
uniform float uWipe;      // stock materialising, 0 → 1
uniform float uRestore;   // cut material returning, 0 → 1
uniform float uSpindle;   // 1 while cutting: flutes blur

const vec3  S        = vec3(${SX}, ${SY}, ${SZ});
const float GROUND   = -0.62;
const float TOOL_LEN = 0.55;
const vec2  TEXEL    = vec2(1.0 / ${W}.0, 1.0 / ${H}.0);

// Bare 6061 wants a nearly white key; the accent key that flatters the dark
// anodised part turns bright aluminium into copper.
const vec3 KEY_ALU = mix(ACCENT, vec3(1.0), 0.72);

float fieldHeight(vec2 xz) {
  vec2 uv = (xz + S.xz) / (2.0 * S.xz);
  return mix(textureLod(uField, uv, 0.0).r, S.y, uRestore);
}

float wipeCut(vec3 p) {
  return p.x - (-S.x + (2.0 * S.x + 0.4) * uWipe);
}

/** Stock ∩ heightfield, revealed by a wipe along x while the setup loads. */
float sdStock(vec3 p) {
  return max(max(sdBox(p, S), p.y - fieldHeight(p.xz)), wipeCut(p));
}

/** Cutter, holder and spindle nose. \`part\` is 1 on the cutter, 2 on the rest. */
float sdTool(vec3 p, out int part) {
  vec3 q = p - uTool.xyz;
  float r = uTool.w;
  float cutter;
  if (uToolKind > 2.5) {
    // Drill: 118° point running into the shank. Not a true distance, so
    // halved to keep the march conservative.
    float tip = r * 0.6;
    cutter = 0.5 * max(length(q.xz) - min(q.y * (r / tip), r), max(-q.y, q.y - TOOL_LEN));
  } else if (uToolKind > 1.5) {
    // Face mill: shallow insert body on a narrower arbor.
    float body = sdCylinder(q - vec3(0.0, 0.07, 0.0), 0.07, r);
    float arbor = sdCylinder(q - vec3(0.0, 0.14 + (TOOL_LEN - 0.14) * 0.5, 0.0), (TOOL_LEN - 0.14) * 0.5, r * 0.4);
    cutter = min(body, arbor);
  } else {
    cutter = sdCylinder(q - vec3(0.0, TOOL_LEN * 0.5, 0.0), TOOL_LEN * 0.5, r);
  }
  float holder = sdCylinder(q - vec3(0.0, TOOL_LEN + 0.11, 0.0), 0.11, max(0.10, r * 0.9));
  float nose = sdCylinder(q - vec3(0.0, TOOL_LEN + 0.36, 0.0), 0.14, 0.16);
  float rest = min(holder, nose);
  part = cutter < rest ? 1 : 2;
  return min(cutter, rest);
}

float map(vec3 p) {
  float d = sdStock(p);
  if (uToolKind > 0.5) {
    int part;
    d = min(d, sdTool(p, part));
  }
  return d;
}

float boundsEntry(vec3 ro, vec3 rd) {
  const float R = 2.7;
  float b = dot(ro, rd);
  float c = dot(ro, ro) - R * R;
  float disc = b * b - c;
  if (disc < 0.0) return -1.0;
  return max(-b - sqrt(disc), 0.0);
}

/**
 * A heightfield is not a distance field: the vertical gap to the floor says
 * nothing about a wall standing beside the ray. So inside the working envelope
 * the step is capped, and a step that lands inside material bisects back to
 * the crossing instead of shading an interior point.
 */
float march(vec3 ro, vec3 rd) {
  float entry = boundsEntry(ro, rd);
  if (entry < 0.0) return -1.0;
  float t = entry;
  float tPrev = t;
  for (int i = 0; i < 110; i++) {
    vec3 p = ro + rd * t;
    if (dot(p, p) > 7.6) return -1.0;
    float d = map(p);
    if (d < 0.0) {
      float lo = tPrev, hi = t;
      for (int k = 0; k < 7; k++) {
        float mid = 0.5 * (lo + hi);
        if (map(ro + rd * mid) < 0.0) hi = mid; else lo = mid;
      }
      return lo;
    }
    if (d < 0.0007 * t) return t;
    tPrev = t;
    bool envelope = abs(p.x) < S.x + 0.1 && abs(p.z) < S.z + 0.1 && p.y < S.y + 0.3;
    t += envelope ? min(d, 0.07) : d;
    if (t > 9.0) break;
  }
  return -1.0;
}

void main() {
  vec3 ro, rd;
  cameraRay(vec3(0.0, -0.05, 0.0), ro, rd);

  float tHit = march(ro, rd);
  float tGround = (rd.y < -1e-4) ? (GROUND - ro.y) / rd.y : -1.0;

  vec3 colour = vec3(0.0);
  float coverage = 0.0;
  vec3 overlay = vec3(0.0);

  if (tHit > 0.0 && (tGround < 0.0 || tHit < tGround)) {
    vec3 p = ro + rd * tHit;
    float curvature;
    vec3 n = normalAndCurvature(p, curvature);

    int part = 0;
    float dStock = sdStock(p);
    if (uToolKind > 0.5) {
      int toolPart;
      float dTool = sdTool(p, toolPart);
      if (dTool < dStock) part = toolPart;
    }

    if (part == 0) {
      // Bare 6061: bright, neutral, with a faint brushed grain along x on the
      // as-received faces that the cutter wipes away.
      vec2 uv = (p.xz + S.xz) / (2.0 * S.xz);
      vec4 f = textureLod(uField, uv, 0.0);
      float machined = smoothstep(0.002, 0.01, S.y - f.r) * (1.0 - uRestore);
      float grain = 1.0 + 0.05 * (hash(vec2(floor(p.z * 400.0), 0.0)) - 0.5) * (1.0 - machined);
      colour = shadeMetal(p, rd, n, curvature, vec3(0.21, 0.215, 0.22) * grain, KEY_ALU);

      // Machined edges come from the field itself, since the curvature term
      // cannot be trusted on a heightfield: a convex rim is an upward face
      // whose neighbourhood drops away, a concave corner one whose
      // neighbourhood rises.
      if (n.y > 0.5) {
        vec2 t = TEXEL * 1.5;
        float hx0 = textureLod(uField, uv - vec2(t.x, 0.0), 0.0).r;
        float hx1 = textureLod(uField, uv + vec2(t.x, 0.0), 0.0).r;
        float hz0 = textureLod(uField, uv - vec2(0.0, t.y), 0.0).r;
        float hz1 = textureLod(uField, uv + vec2(0.0, t.y), 0.0).r;
        float lowest = min(min(hx0, hx1), min(hz0, hz1));
        float highest = max(max(hx0, hx1), max(hz0, hz1));
        float rim = smoothstep(0.01, 0.04, f.r - lowest) * (1.0 - uRestore);
        float corner = smoothstep(0.01, 0.04, highest - f.r) * (1.0 - uRestore);
        colour += mix(TECH_HI, KEY_ALU, 0.5) * (rim * 0.30);
        colour *= 1.0 - corner * 0.4;
      }

      // Toolpath overlay on the cut surface: the programmed path for the
      // running operation in teal, and the cutter's trail cooling from accent
      // to a persistent teal after it passes.
      float alive = 1.0 - uRestore;
      float program = f.g * alive * step(0.5, n.y);
      float age = uTime - f.b;
      float trail = f.b > -1.0 ? alive : 0.0;
      float hot = exp(-age * 1.4);
      overlay += pow(TECH_HI, vec3(1.0 / 2.2)) * program * 0.5;
      overlay += mix(pow(TECHNICAL, vec3(1.0 / 2.2)) * 0.45, vec3(1.0, 0.54, 0.24), hot)
        * trail * (0.4 + 0.6 * hot);
    } else if (part == 1) {
      // Cutter: steel, with flute shading that smears into a blur while the
      // spindle runs.
      vec3 q = p - uTool.xyz;
      float flutes = sin(atan(q.z, q.x) * 4.0 + q.y * 60.0 - uTime * 80.0);
      float band = mix(0.5 + 0.5 * flutes, 0.75, uSpindle) * 0.35 + 0.65;
      colour = shadeMetal(p, rd, n, curvature, vec3(0.30, 0.31, 0.34) * band, KEY_ALU);
    } else {
      colour = shadeMetal(p, rd, n, curvature, vec3(0.05, 0.052, 0.054), KEY_ALU);
    }
    coverage = 1.0;
  } else if (tGround > 0.0) {
    vec3 p = ro + rd * tGround;
    float fade = (1.0 - smoothstep(3.2, 9.0, length(p.xz)))
               * smoothstep(0.015, 0.17, -rd.y);
    if (fade > 0.001) {
      colour = shadeGround(p, fade);
      coverage = fade;
    }
  }

  vec3 srgb = pow(tonemap(colour), vec3(1.0 / 2.2));
  vec3 outColour = mix(BG, srgb, coverage) + overlay;
  fragColor = vec4(finish(outColour), 1.0);
}`;

const UNIFORMS = [
  ...STAGE_UNIFORMS, 'uField', 'uTool', 'uToolKind', 'uWipe', 'uRestore', 'uSpindle',
] as const;

export function createMachining(canvas: HTMLCanvasElement, opts: MachiningOptions = {}): MachiningHandle | null {
  const job = buildJob();
  const stock = new Stock(job);
  const pose = { x: PARK.x, y: PARK.y, z: PARK.z, op: -1 };
  let texture: WebGLTexture | null = null;
  let reportedOp = -2;
  let reportedActive = false;

  return createStage(canvas, {
    frag: FRAG,
    uniforms: UNIFORMS,
    cycle: MACHINING_CYCLE,
    still: opts.still,
    stillAt: MACHINING_HOLD_END - 0.6,
    onLost: opts.onLost,
    init(gl, program) {
      texture = gl.createTexture();
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      // Half float is the smallest format WebGL2 filters without an
      // extension, and 11 bits of mantissa is plenty for heights in ±0.25
      // and visit times under 22 s.
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, W, H, 0, gl.RGBA, gl.FLOAT, stock.data);
      gl.uniform1i(gl.getUniformLocation(program, 'uField'), 0);
    },
    dispose(gl) {
      if (texture) gl.deleteTexture(texture);
    },
    frame({ gl, loc, elapsed, width, height, cssWidth, pointerX, pointerY }) {
      stock.advance(elapsed);
      if (stock.dirtyMax >= stock.dirtyMin) {
        const rows = stock.dirtyMax - stock.dirtyMin + 1;
        gl.texSubImage2D(
          gl.TEXTURE_2D, 0, 0, stock.dirtyMin, W, rows, gl.RGBA, gl.FLOAT,
          stock.data, stock.dirtyMin * W * 4,
        );
        stock.dirtyMin = H;
        stock.dirtyMax = -1;
      }

      toolPose(job, elapsed, pose);
      const restore = easeInOut((elapsed - MACHINING_HOLD_END) / (MACHINING_CYCLE - MACHINING_HOLD_END));
      const wipe = easeInOut(elapsed / 1.1);

      // Stock1 owns the wipe; a cutting op owns its row; the hold reports the
      // last op finished.
      const cutting = pose.op >= 0;
      const reported = cutting ? pose.op : elapsed < MACHINING_OPS[0].end ? 0 : MACHINING_OPS.length - 1;
      const active = cutting || elapsed < MACHINING_OPS[0].end;
      if (opts.onOp && (reported !== reportedOp || active !== reportedActive)) {
        reportedOp = reported;
        reportedActive = active;
        opts.onOp(reported, active);
      }

      const op = pose.op >= 0 ? MACHINING_OPS[pose.op] : null;
      const kind = op ? TOOL_KIND[op.tool] : elapsed < MACHINING_OPS[0].end ? 0 : 1;
      const radius = op ? op.radius : 0.05;

      // Slow while cutting so the passes read; opens up for the showcase.
      const showcase = easeInOut((elapsed - MACHINING_HOLD_END + 3.8) / 3.8);
      const yaw = 0.75 + elapsed * (0.07 + 0.16 * showcase) + pointerX * 0.42;
      const pitch = 0.68 + Math.sin(elapsed * 0.19) * 0.05 - pointerY * 0.15;

      gl.uniform2f(loc.uRes, width, height);
      // Closer than the part showcase: a billet has no thin features to
      // protect, and the spindle above it fills the headroom the pitch leaves.
      const compact = cssWidth < 520 ? 0.84 : 1;
      gl.uniform3f(loc.uCam, yaw, pitch, (4.9 - 0.3 * showcase) * compact);
      gl.uniform2f(loc.uShift, 0.0, -0.06);
      gl.uniform1f(loc.uTime, elapsed);
      gl.uniform4f(loc.uTool, pose.x, pose.y, pose.z, radius);
      gl.uniform1f(loc.uToolKind, kind);
      gl.uniform1f(loc.uWipe, wipe);
      gl.uniform1f(loc.uRestore, restore);
      gl.uniform1f(loc.uSpindle, cutting ? 1 : 0);
    },
  });
}
