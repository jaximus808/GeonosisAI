// TDM-59: connection quality thresholds and plain-language advice for remote operators.
//
// One place for the numbers so the teleop page indicator, the practice-mode readiness check and
// any later surface agree. Video latency is TDM-55's capture -> screen delay (worst camera); the
// TDM-56 console pauses driving above 400 ms, so "bad" starts before that.

export type Grade = 'good' | 'ok' | 'bad' | 'unknown';

export const TDM59_VIDEO_MS = { good: 150, ok: 300 } as const; // > 300 ms: too laggy to drive well
export const TDM59_RTT_MS = { good: 150, ok: 300 } as const; // control link ack round trip
export const TDM59_CMD_HZ = { good: 25, ok: 20 } as const; // contract rate is ~30 Hz while the clutch is held
export const TDM59_JITTER_MS = { good: 40, ok: 100 } as const;

export function gradeMs(ms: number | null | undefined, t: { good: number; ok: number }): Grade {
  if (ms == null || !Number.isFinite(ms)) return 'unknown';
  if (ms <= t.good) return 'good';
  if (ms <= t.ok) return 'ok';
  return 'bad';
}

export function gradeHz(hz: number | null | undefined): Grade {
  if (hz == null || !Number.isFinite(hz)) return 'unknown';
  if (hz >= TDM59_CMD_HZ.good) return 'good';
  if (hz >= TDM59_CMD_HZ.ok) return 'ok';
  return 'bad';
}

export function worst(...g: Grade[]): Grade {
  if (g.includes('bad')) return 'bad';
  if (g.includes('ok')) return 'ok';
  if (g.includes('good')) return 'good';
  return 'unknown';
}

/** What the browser can tell about the network (Chrome/Android only; Safari returns nothing). */
export interface NetInfo {
  type?: string; // wifi | cellular | ethernet | ...
  effectiveType?: string; // slow-2g | 2g | 3g | 4g
  rttMs?: number;
  downlinkMbps?: number;
  saveData?: boolean;
}

export function readNetInfo(): NetInfo | null {
  if (typeof navigator === 'undefined') return null;
  const c = (navigator as Navigator & { connection?: Record<string, unknown> }).connection;
  if (!c) return null;
  return {
    type: typeof c.type === 'string' ? c.type : undefined,
    effectiveType: typeof c.effectiveType === 'string' ? c.effectiveType : undefined,
    rttMs: typeof c.rtt === 'number' ? c.rtt : undefined,
    downlinkMbps: typeof c.downlink === 'number' ? c.downlink : undefined,
    saveData: c.saveData === true,
  };
}

export const onCellular = (n: NetInfo | null) => n?.type === 'cellular' || n?.effectiveType === '2g' || n?.effectiveType === '3g' || n?.effectiveType === 'slow-2g';

export interface Advice {
  grade: Grade;
  headline: string;
  tips: string[];
}

export interface QualityInput {
  videoMs?: number | null;
  videoStale?: boolean;
  /** Camera feed not connected: 'denied' | 'error' | 'closed'. */
  videoDown?: string;
  rttMs?: number | null;
  /** Command rate; only meaningful while driving. */
  cmdHz?: number | null;
  driving?: boolean;
  jitterMs?: number | null;
  net?: NetInfo | null;
}

/** Grade a connection and say, in plain words, what to do about it. */
export function adviseConnection(q: QualityInput): Advice {
  const video = q.videoStale ? 'bad' : gradeMs(q.videoMs, TDM59_VIDEO_MS);
  const rtt = gradeMs(q.rttMs, TDM59_RTT_MS);
  const hz = q.driving ? gradeHz(q.cmdHz) : 'unknown';
  const jit = gradeMs(q.jitterMs, TDM59_JITTER_MS);
  const grade = worst(video, rtt, hz, jit);
  const cell = onCellular(q.net ?? null);
  const tips: string[] = [];

  if (q.videoDown) {
    tips.push(
      q.videoDown === 'denied'
        ? 'Only the person who claimed this task gets the video. Claim it from the task list, or check that your claim is still held.'
        : 'The robot camera is not connected. Driving stays off until it is. Reload the page if it does not come back.',
    );
    return { grade: 'bad', headline: 'No video', tips };
  }
  if (q.videoStale) {
    tips.push('The picture has frozen. Driving stays paused until it is live again.');
    tips.push(cell ? 'You are on mobile data. Switch to Wi-Fi if you can.' : 'Check your Wi-Fi, then reload the page if it does not come back.');
    return { grade: 'bad', headline: 'Picture frozen', tips };
  }
  if (video === 'bad' || rtt === 'bad' || jit === 'bad') {
    tips.push(cell ? 'Too laggy on mobile data. Try Wi-Fi.' : 'Too laggy. Move closer to your Wi-Fi router or use a wired connection.');
    tips.push('Close other tabs and apps that stream video or download.');
    tips.push('If you keep driving, use slow mode and wait for the picture to catch up after each move.');
  } else if (hz === 'bad') {
    tips.push('Your device is not sending commands fast enough, so motion will be jerky.');
    tips.push('Keep this tab in front, turn off battery saver, and close other tabs.');
  } else if (grade === 'ok') {
    tips.push('Usable. Move in short strokes and let the picture catch up before the next one.');
    if (cell) tips.push('You are on mobile data. Wi-Fi is usually steadier.');
  } else if (grade === 'good') {
    tips.push('Smooth enough to drive at normal speed.');
  } else {
    tips.push('Measuring your connection.');
  }
  const headline = grade === 'good' ? 'Connection good' : grade === 'ok' ? 'Connection OK' : grade === 'bad' ? 'Too laggy' : 'Checking connection';
  return { grade, headline, tips };
}

export function median(xs: number[]): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/** Spread of round trips: p90 - p10. */
export function jitter(xs: number[]): number | null {
  if (xs.length < 3) return null;
  const s = [...xs].sort((a, b) => a - b);
  const at = (p: number) => s[Math.min(s.length - 1, Math.max(0, Math.round(p * (s.length - 1))))];
  return at(0.9) - at(0.1);
}

export interface NetworkTest {
  medianMs: number | null;
  jitterMs: number | null;
  lost: number;
  samples: number[];
  net: NetInfo | null;
  advice: Advice;
  at: number;
}

/**
 * Times `n` small no-cache requests to this site's /api/tdm59/ping. This measures the operator's own
 * network (the hop that usually goes wrong on a phone), not the robot's tunnel; the teleop page
 * shows the robot's real video delay once a task is claimed.
 */
export async function runNetworkTest(n = 10, onSample?: (ms: number, i: number) => void): Promise<NetworkTest> {
  const samples: number[] = [];
  let lost = 0;
  for (let i = 0; i < n; i++) {
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), 2000);
    const t0 = performance.now();
    try {
      const r = await fetch(`/api/tdm59/ping?i=${i}&r=${Math.random().toString(36).slice(2)}`, { cache: 'no-store', signal: ctl.signal });
      await r.text();
      if (!r.ok) throw new Error(String(r.status));
      const ms = performance.now() - t0;
      if (i > 0) samples.push(ms); // the first one pays for DNS/TLS/route compile
      onSample?.(ms, i);
    } catch {
      lost++;
    } finally {
      clearTimeout(to);
    }
  }
  const net = readNetInfo();
  const medianMs = median(samples);
  const jitterMs = jitter(samples);
  const advice = adviseConnection({ rttMs: medianMs, jitterMs, net });
  if (lost > 1 && advice.grade !== 'bad') {
    advice.grade = 'bad';
    advice.headline = 'Requests are getting lost';
    advice.tips.unshift(`${lost} of ${n} test requests did not come back. Try Wi-Fi or move closer to the router.`);
  }
  return { medianMs, jitterMs, lost, samples, net, advice, at: Date.now() };
}

export const GRADE_STYLE: Record<Grade, { dot: string; chip: string; text: string }> = {
  good: { dot: 'bg-emerald-500', chip: 'bg-emerald-50 text-emerald-900 ring-emerald-600/25 dark:bg-emerald-950/50 dark:text-emerald-100', text: 'text-emerald-700 dark:text-emerald-300' },
  ok: { dot: 'bg-amber-500', chip: 'bg-amber-50 text-amber-950 ring-amber-600/30 dark:bg-amber-950/50 dark:text-amber-100', text: 'text-amber-700 dark:text-amber-300' },
  bad: { dot: 'bg-red-600', chip: 'bg-red-50 text-red-900 ring-red-600/30 dark:bg-red-950/50 dark:text-red-100', text: 'text-red-700 dark:text-red-300' },
  unknown: { dot: 'bg-zinc-400', chip: 'bg-zinc-100 text-zinc-700 ring-zinc-400/30 dark:bg-zinc-900 dark:text-zinc-300', text: 'text-zinc-500' },
};
