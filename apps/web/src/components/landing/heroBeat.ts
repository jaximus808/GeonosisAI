// The hero schematic's one timeline (no three.js here: the DOM overlay and the 3D scene both read it).
// One turntable revolution is one loop. Every ROUND_S the four agents each send one packet down their conduit
// into the arm; on the last round the hand backs off its flange, the final packets (the passing eval in orange)
// land, the hand slides home, the weld seam flashes and the sheet is stamped DEPLOYED. Then it all cools.

export const LOOP_S = 48;
/** Where the loop starts on load, so the first deploy beat comes about 6 s in. */
export const START_T = 30;
/** Reduced motion: a settled three-quarter view, the hand home and cold. */
export const STILL_T = 14;

export const BEAT = {
  outStart: 36, outEnd: 37.2, // the hand backs off the flange
  slideStart: 43, slideEnd: 44.4, // and slides home once the last packets land
  coolEnd: 47.6,
  stampIn: 44.6, stampFull: 45.2, stampOut: 46.9, stampGone: 47.7,
};

export const ROUND_S = 12;
const ROUND0 = 1.2;
export const TRAVEL_S = 3.2;
const LAST_ROUND = Math.floor((BEAT.outEnd - ROUND0) / ROUND_S);

export type FeedKey = 'data' | 'train' | 'review' | 'eval';
export const FEEDS: Array<{ key: FeedKey; stagger: number }> = [
  { key: 'data', stagger: 0 },
  { key: 'review', stagger: 0.8 },
  { key: 'train', stagger: 1.6 },
  { key: 'eval', stagger: 2.4 },
];

const ease = (x: number) => (x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x));
export const ramp = (t: number, a: number, b: number) => ease((t - a) / (b - a));

/** A conduit's packet at loop time t: progress 0..1 along it (null when none is moving), hot = the passing eval. */
export function packetAt(t: number, stagger: number, key: FeedKey): { p: number | null; arrived: number; hot: boolean } {
  const s = t - ROUND0 - stagger;
  const round = Math.floor(s / ROUND_S);
  const local = s - round * ROUND_S;
  const p = s >= 0 && local < TRAVEL_S ? ease(local / TRAVEL_S) : null;
  // seconds since this conduit's last packet landed (for the port's little pulse)
  const arrived = s >= TRAVEL_S ? local - TRAVEL_S : 99;
  return { p, arrived: arrived < 0 ? 99 : arrived, hot: key === 'eval' && round === LAST_ROUND };
}

/** The hand: how far it is backed off (0..1), how hot its edges are, how bright the seam flash is. */
export function handAt(t: number): { slide: number; heat: number; flash: number } {
  const slide = ramp(t, BEAT.outStart, BEAT.outEnd) - ramp(t, BEAT.slideStart, BEAT.slideEnd);
  const flash = t >= BEAT.slideEnd ? 1 - ramp(t, BEAT.slideEnd, BEAT.coolEnd) : 0;
  const heat = t < BEAT.slideEnd ? ramp(t, BEAT.outStart, BEAT.outEnd) : flash;
  return { slide, heat, flash };
}

export function stampAt(t: number): number {
  return ramp(t, BEAT.stampIn, BEAT.stampFull) * (1 - ramp(t, BEAT.stampOut, BEAT.stampGone));
}
