/**
 * The WebGL2 raymarch stage every live showcase runs on.
 *
 * A showcase is a fragment shader plus a per-frame uniform update. Everything
 * else — context creation, the fullscreen triangle, the pixel budget that
 * adapts to what the GPU actually delivers, pointer parallax, pausing while
 * hidden, and recoverable context loss — is the same for all of them and lives
 * here once. Adding a showcase means writing a shader and a `frame` callback,
 * not a second copy of the driver.
 */

export interface StageFrame<U extends string> {
  readonly gl: WebGL2RenderingContext;
  readonly loc: Readonly<Record<U, WebGLUniformLocation | null>>;
  /** Seconds into the current cycle, in [0, cycle). */
  readonly elapsed: number;
  /** Seconds since the previous frame; 0 for a still. */
  readonly dt: number;
  /** Drawing-buffer size in device pixels. */
  readonly width: number;
  readonly height: number;
  /** Canvas width in CSS pixels — how big the subject actually is on screen. */
  readonly cssWidth: number;
  /** Eased pointer position in [-1, 1], zero on touch devices. */
  readonly pointerX: number;
  readonly pointerY: number;
}

export interface StageOptions<U extends string> {
  readonly frag: string;
  readonly uniforms: readonly U[];
  /** Cycle length in seconds; the clock wraps here. */
  readonly cycle: number;
  /** Render one frame at this clock value and stop (prefers-reduced-motion). */
  readonly still?: boolean;
  readonly stillAt: number;
  /** Extra GL state (textures) — runs once the program is linked and bound. */
  init?(gl: WebGL2RenderingContext, program: WebGLProgram): void;
  /** Set this frame's uniforms. The stage draws after it returns. */
  frame(f: StageFrame<U>): void;
  /** Release anything `init` created. */
  dispose?(gl: WebGL2RenderingContext): void;
  /**
   * The driver took the context away. Mobile browsers do this routinely — a
   * page asking for more contexts than the GPU will fund, a background tab,
   * memory pressure. Every GL object is gone and this handle is finished; the
   * caller shows its fallback and builds a fresh stage on
   * `webglcontextrestored`.
   */
  onLost?: () => void;
}

export interface StageHandle {
  destroy(): void;
}

// One triangle that covers clip space: (-1,-1), (3,-1), (-1,3). The `* 2.0`
// is what pushes the far vertices to 3, so the hypotenuse clears the top-right
// corner. Without it the triangle ends at (1,-1)/(-1,1) and the raymarch only
// ever fills the lower-left half of the canvas.
const VERT = `#version 300 es
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

function compile(gl: WebGL2RenderingContext, type: number, source: string): WebGLShader | null {
  const shader = gl.createShader(type);
  if (!shader) return null;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    console.warn('stage: shader compile failed\n' + gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

function link(gl: WebGL2RenderingContext, frag: string): WebGLProgram | null {
  const vs = compile(gl, gl.VERTEX_SHADER, VERT);
  const fs = compile(gl, gl.FRAGMENT_SHADER, frag);
  if (!vs || !fs) return null;
  const program = gl.createProgram();
  if (!program) return null;
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.warn('stage: program link failed\n' + gl.getProgramInfoLog(program));
    gl.deleteProgram(program);
    return null;
  }
  return program;
}

export const easeInOut = (x: number): number =>
  x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x);

export function createStage<U extends string>(
  canvas: HTMLCanvasElement,
  opts: StageOptions<U>,
): StageHandle | null {
  const gl = canvas.getContext('webgl2', {
    alpha: false,
    antialias: false,
    depth: false,
    stencil: false,
    desynchronized: true,
    powerPreference: 'high-performance',
  });
  if (!gl) return null;

  const program = link(gl, opts.frag);
  if (!program) return null;

  const loc = {} as Record<U, WebGLUniformLocation | null>;
  for (const name of opts.uniforms) loc[name] = gl.getUniformLocation(program, name);

  const vao = gl.createVertexArray();
  gl.bindVertexArray(vao);
  gl.useProgram(program);
  opts.init?.(gl, program);

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
  let cssWidth = 0;

  function resize(): boolean {
    const rect = canvas.getBoundingClientRect();
    const cssW = Math.max(1, Math.round(rect.width));
    const cssH = Math.max(1, Math.round(rect.height));
    cssWidth = cssW;
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
  // for the life of the page.
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

  function draw(elapsed: number, dt: number) {
    pointerX += (targetX - pointerX) * 0.06;
    pointerY += (targetY - pointerY) * 0.06;
    opts.frame({ gl: gl!, loc, elapsed, dt, width, height, cssWidth, pointerX, pointerY });
    gl!.drawArrays(gl!.TRIANGLES, 0, 3);
  }

  if (opts.still) {
    resize();
    draw(opts.stillAt, 0);
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

      const dt = Math.min(delta / 1000, 0.05);
      clock = (clock + dt) % opts.cycle;
      resize();
      draw(clock, dt);
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
      opts.dispose?.(gl);
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
