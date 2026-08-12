/**
 * The hero animation is a parametric feature history, not a decorative loop.
 *
 * One table drives both surfaces so they can never drift: the Astro component
 * renders a FeatureManager row per stage at build time, and the WebGL renderer
 * reads the same rows to drive its SDF uniforms. Adding a feature here adds it
 * to both.
 *
 * `uniform` is the shader uniform the stage animates from 0 to 1 over
 * [`start`, `end`) seconds. Stages hold at 1 once complete; the whole history
 * unwinds during the wipe at the end of the cycle and the loop restarts.
 */
export interface HeroStage {
  /** Feature name, as it would read in a real feature tree. */
  readonly label: string;
  /** Parameter readout shown while the feature is building. */
  readonly detail: string;
  /** Shader uniform name, without the `u` prefix. */
  readonly uniform: 'Sketch' | 'Extrude' | 'Boss' | 'Fillet' | 'Drill' | 'Bore' | 'Pocket';
  readonly start: number;
  readonly end: number;
}

export const HERO_STAGES: readonly HeroStage[] = [
  { label: 'Sketch1', detail: '8 entities · fully defined', uniform: 'Sketch', start: 0.0, end: 2.0 },
  { label: 'Extrude1', detail: 'Blind · 20.0 mm', uniform: 'Extrude', start: 2.0, end: 3.8 },
  { label: 'Boss2', detail: 'Blind · ⌀84.0 × 28.0', uniform: 'Boss', start: 3.8, end: 5.2 },
  { label: 'Fillet1', detail: 'Constant radius · 7.0 mm', uniform: 'Fillet', start: 5.2, end: 6.5 },
  { label: 'Hole1', detail: 'Through all · ⌀22.0 × 4', uniform: 'Drill', start: 6.5, end: 8.1 },
  { label: 'Bore1', detail: 'Through all · ⌀48.0', uniform: 'Bore', start: 8.1, end: 9.4 },
  { label: 'Pocket1', detail: 'Blind · 16.0 mm · mirrored', uniform: 'Pocket', start: 9.4, end: 10.8 },
];

/** Showcase rotation after the last feature, before the history unwinds. */
export const HERO_HOLD_END = 14.6;
/** The unwind. Every uniform eases back to zero and the sketch returns. */
export const HERO_CYCLE = 16.0;
