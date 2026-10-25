// Teleop / practice 3D view quality: "Quality: Smooth / Sharp".
//
// Smooth (the default) keeps a laptop cool while someone drives: while the user holds control only the big
// view and ONE small view draw; the big one at min(devicePixelRatio, 1.25) with a 1024 shadow map, the small
// one at 1x, no shadows, 30 fps. Every other view is paused (unmounted, its WebGL context freed). Sharp
// restores everything: all views live, up to 2x pixel ratio, 2048 shadow maps (the twin's own defaults).
// The choice is remembered per browser in localStorage (every access wrapped: private windows throw).
//
// Pure: no React, no DOM beyond the optional storage argument (apps/web/test/teleop-view-quality.test.ts).

export type ViewQuality = 'smooth' | 'sharp';
export type ViewRole = 'main' | 'inset';

export const VIEW_QUALITY_KEY = 'rdf.teleop.view-quality.v1';
export const DEFAULT_VIEW_QUALITY: ViewQuality = 'smooth';
/** Smooth: the big view's pixel-ratio cap. */
export const SMOOTH_MAIN_DPR = 1.25;
/** Smooth: the small view's pixel-ratio cap and frame rate. */
export const SMOOTH_INSET_DPR = 1;
export const SMOOTH_INSET_FPS = 30;

/** Render settings RobotTwin3D takes (`perf`); null = the twin's own defaults (Sharp). */
export interface TwinPerf {
  /** Upper bound on the canvas pixel ratio (the twin uses min(this, devicePixelRatio)). */
  maxDpr: number;
  shadows: boolean;
  shadowMapSize: number;
  blurSamples: number;
  /** Cap on frames per second (null = the display's rate). */
  maxFps: number | null;
}

const MAIN: TwinPerf = { maxDpr: SMOOTH_MAIN_DPR, shadows: true, shadowMapSize: 1024, blurSamples: 8, maxFps: null };
const INSET: TwinPerf = { maxDpr: SMOOTH_INSET_DPR, shadows: false, shadowMapSize: 512, blurSamples: 0, maxFps: SMOOTH_INSET_FPS };

/** The twin's render settings for a view of this role. Constant objects, so they are safe as memo deps. */
export function twinPerf(quality: ViewQuality | null | undefined, role: ViewRole): TwinPerf | null {
  if (quality !== 'smooth') return null;
  return role === 'main' ? MAIN : INSET;
}

/** The pixel ratio a view draws at: min(devicePixelRatio, cap), never below 0.5. */
export function effectiveDpr(devicePixelRatio: number | undefined, maxDpr: number): number {
  const d = devicePixelRatio && Number.isFinite(devicePixelRatio) && devicePixelRatio > 0 ? devicePixelRatio : 1;
  return Math.max(0.5, Math.min(d, maxDpr));
}

export function parseViewQuality(v: unknown): ViewQuality | null {
  return v === 'smooth' || v === 'sharp' ? v : null;
}

type Store = Pick<Storage, 'getItem' | 'setItem'>;

function storage(s?: Store | null): Store | null {
  if (s) return s;
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function readViewQuality(s?: Store | null): ViewQuality {
  try {
    return parseViewQuality(storage(s)?.getItem(VIEW_QUALITY_KEY)) ?? DEFAULT_VIEW_QUALITY;
  } catch {
    return DEFAULT_VIEW_QUALITY;
  }
}

export function writeViewQuality(q: ViewQuality, s?: Store | null): boolean {
  try {
    storage(s)?.setItem(VIEW_QUALITY_KEY, q);
    return true;
  } catch {
    return false;
  }
}

export interface PaneLayout {
  /** The big view. */
  main: string;
  /** Small views that draw. */
  live: string[];
  /** Small views that are paused (not mounted). */
  paused: string[];
}

/**
 * Which panes draw. `ids[0]` is the big view, the rest are small. In Smooth while the user holds control,
 * only `inset` (default: the first small one) stays live next to the big view; otherwise all of them do.
 */
export function paneLayout(ids: readonly string[], opts: { quality: ViewQuality | null | undefined; driving: boolean; inset?: string | null }): PaneLayout {
  const [main, ...rest] = ids;
  if (opts.quality !== 'smooth' || !opts.driving || rest.length <= 1) return { main, live: rest, paused: [] };
  const inset = opts.inset && rest.includes(opts.inset) ? opts.inset : rest[0];
  return { main, live: [inset], paused: rest.filter((id) => id !== inset) };
}
