/**
 * Pointer-reactive surfaces.
 *
 * Everything that can be done with a scroll-driven CSS animation already is
 * (see the `motion` layer in global.css). This file only covers what CSS has
 * no timeline for: where the pointer is inside a given card. It publishes that
 * as two custom properties and a small tilt, and the stylesheet decides what to
 * do with them.
 *
 * One delegated listener and one rAF for the whole document, rather than a
 * listener per card: a page with thirty cards should not install thirty
 * handlers.
 */
const SURFACES = '.panel, .access-option, .gallery-card, .stack-node, [data-lift]';

/** Peak tilt in degrees at the corner of a surface. Small on purpose. */
const TILT = 2.6;

export function mountPointerEffects(): void {
  // A coarse pointer has no hover state to track, and reduced motion means the
  // reader asked for none of this.
  if (!window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  let active: HTMLElement | null = null;
  let pendingX = 0;
  let pendingY = 0;
  let frame = 0;

  const apply = () => {
    frame = 0;
    const el = active;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    const x = (pendingX - rect.left) / rect.width;
    const y = (pendingY - rect.top) / rect.height;
    el.style.setProperty('--mx', `${(x * 100).toFixed(2)}%`);
    el.style.setProperty('--my', `${(y * 100).toFixed(2)}%`);
    el.style.setProperty('--tilt-x', `${((0.5 - y) * 2 * TILT).toFixed(3)}deg`);
    el.style.setProperty('--tilt-y', `${((x - 0.5) * 2 * TILT).toFixed(3)}deg`);
  };

  const release = (el: HTMLElement) => {
    el.removeAttribute('data-live');
    el.style.removeProperty('--tilt-x');
    el.style.removeProperty('--tilt-y');
  };

  document.addEventListener(
    'pointermove',
    (event: PointerEvent) => {
      if (event.pointerType !== 'mouse') return;
      const target = (event.target as Element | null)?.closest<HTMLElement>(SURFACES) ?? null;
      if (target !== active) {
        if (active) release(active);
        active = target;
        if (active) active.setAttribute('data-live', '');
      }
      if (!active) return;
      pendingX = event.clientX;
      pendingY = event.clientY;
      if (!frame) frame = requestAnimationFrame(apply);
    },
    { passive: true },
  );

  // Leaving the document entirely never fires a pointermove that lands outside
  // a surface, so the last card would stay lit.
  document.addEventListener(
    'pointerleave',
    () => {
      if (active) release(active);
      active = null;
    },
    { passive: true },
  );
}
