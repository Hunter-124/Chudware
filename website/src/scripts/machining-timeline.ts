/**
 * The machining showcase is a CAM job, not a decorative loop.
 *
 * One table drives both surfaces so they can never drift: the Astro component
 * renders an Operations row per entry at build time, and the renderer reads
 * the same rows to schedule its toolpaths. Each operation's generated path is
 * fitted to its [`start`, `end`) window, so a longer path runs faster rather
 * than overrunning the row that describes it.
 */
export type ToolKind = 'face' | 'endmill' | 'drill';

export interface MachiningOp {
  /** Operation name, as it would read in the Manufacture workspace. */
  readonly label: string;
  /** Tool and parameter readout shown while the operation is cutting. */
  readonly detail: string;
  readonly tool: ToolKind;
  /** Cutter radius in scene units (the stock is 2.5 × 0.5 × 1.7). */
  readonly radius: number;
  readonly start: number;
  readonly end: number;
}

export const MACHINING_OPS: readonly MachiningOp[] = [
  { label: 'Stock1', detail: '6061-T6 · 125 × 85 × 25 mm', tool: 'endmill', radius: 0, start: 0.0, end: 1.2 },
  { label: 'Face1', detail: '⌀20 face mill · 1.5 mm · 7 passes', tool: 'face', radius: 0.2, start: 1.2, end: 4.4 },
  { label: 'Pocket1', detail: '⌀6 end mill · 2 × 5 mm · 50%', tool: 'endmill', radius: 0.06, start: 4.4, end: 9.2 },
  { label: 'Contour1', detail: '⌀6 end mill · 3 stepdowns · 1.5 mm floor', tool: 'endmill', radius: 0.06, start: 9.2, end: 14.0 },
  { label: 'Drill1', detail: '⌀4.2 drill · 4 × through', tool: 'drill', radius: 0.042, start: 14.0, end: 16.2 },
];

/** Showcase rotation after the last cut, before the stock is restored. */
export const MACHINING_HOLD_END = 20.0;
/** The restore. Cut material returns, the paths fade, and the cycle repeats. */
export const MACHINING_CYCLE = 21.6;
