/**
 * Mounts every live showcase on the page.
 *
 * A showcase root carries `data-show="<kind>"` and contains the product
 * screenshot (the resting state), a hidden canvas, and a hidden instrument
 * panel with one `[data-show-row]` per timeline entry. The renderer is chosen
 * by kind; everything about how it is brought up and torn down is shared:
 *
 * - The screenshot is the fallback and it only steps aside once the canvas has
 *   actually produced a context and a first frame. No JS, no WebGL, a blocked
 *   bundle, or a driver that later reclaims the context all show the product
 *   rather than an empty rectangle.
 * - A stage owns a GL context only while it is worth rendering. A phone's
 *   context ceiling is low enough that the brand hero and two showcases can
 *   exceed it between them, and the driver's answer is to take one away — so
 *   stages boot on approach and tear down once scrolled well past.
 * - Parse-time null contexts are transient on a cold GPU, so creation retries
 *   a few times before the screenshot is accepted as the final state.
 */
import { createHero } from './hero';
import { createMachining } from './machining';
import type { StageHandle } from './stage';

interface Factory {
  (canvas: HTMLCanvasElement, opts: {
    still: boolean;
    onLost(): void;
    onProgress(index: number, active: boolean): void;
  }): StageHandle | null;
}

const FACTORIES: Record<string, Factory> = {
  hero: (canvas, o) => createHero(canvas, {
    centered: true,
    panelDocked: () => false,
    still: o.still,
    onLost: o.onLost,
    onStage: o.onProgress,
  }),
  machining: (canvas, o) => createMachining(canvas, {
    still: o.still,
    onLost: o.onLost,
    onOp: o.onProgress,
  }),
};

function mount(root: HTMLElement) {
  const create = FACTORIES[root.dataset.show ?? ''];
  const canvas = root.querySelector<HTMLCanvasElement>('[data-show-canvas]');
  const panel = root.querySelector<HTMLElement>('[data-show-panel]');
  const caption = root.querySelector<HTMLElement>('[data-show-caption]');
  const status = root.querySelector<HTMLElement>('[data-show-status]');
  const rows = Array.from(root.querySelectorAll<HTMLElement>('[data-show-row]'));
  if (!create || !canvas || !panel) return;

  const activeVerb = root.dataset.showActive ?? '';
  const doneText = root.dataset.showDone ?? '';
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  let stage: StageHandle | null = null;
  let attempts = 0;
  let retry = 0;

  const showShot = () => {
    delete canvas.dataset.ready;
    canvas.hidden = true;
    // The panel duplicates the instrument already visible inside the
    // screenshot, so it belongs to the live render only.
    panel.hidden = true;
    if (caption) caption.hidden = true;
  };

  const teardown = () => {
    window.clearTimeout(retry);
    stage?.destroy();
    stage = null;
    showShot();
  };

  const boot = () => {
    if (stage) return;
    canvas.hidden = false;
    try {
      stage = create(canvas, {
        still,
        onLost() {
          stage = null;
          showShot();
        },
        onProgress(index, active) {
          for (let i = 0; i < rows.length; i++) {
            rows[i].dataset.state = !active || i < index ? 'done' : i === index ? 'building' : 'pending';
          }
          if (status) {
            status.textContent = active
              ? `${activeVerb} ${rows[index]?.dataset.showLabel ?? ''}`
              : doneText;
          }
        },
      });
    } catch {
      // Shader compilation, driver startup, and renderer construction are all
      // optional enhancement paths. The authentic product capture remains the
      // visible source of truth if any of them fails.
      stage = null;
    }

    if (stage) {
      attempts = 0;
      canvas.dataset.ready = '';
      panel.hidden = false;
      if (caption) caption.hidden = false;
    } else if (++attempts < 4) {
      showShot();
      retry = window.setTimeout(boot, 400);
    } else {
      showShot();
    }
  };

  canvas.addEventListener('webglcontextrestored', () => {
    attempts = 0;
    boot();
  });

  // Observe the framed box rather than the canvas: the canvas is hidden while
  // the render is down and a hidden element never intersects anything again.
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(
      (entries) => {
        if (entries[entries.length - 1]?.isIntersecting) boot();
        else teardown();
      },
      { rootMargin: '100% 0px' },
    ).observe(root);
  } else {
    showShot();
  }
}

for (const root of document.querySelectorAll<HTMLElement>('[data-show]')) mount(root);
