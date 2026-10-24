'use client';

// TDM-59: operator readiness, stored per browser.
//
// A contributor passes the practice-mode readiness check once (all lessons on the simulated arm +
// a network test that is not "too laggy"); that unlocks claiming arm-control tasks
// (clean_teleop / recovery_takeover) in the marketplace. Reset tasks never need it.
//
// localStorage is a convenience gate for a first-time operator, not a security boundary: the real
// guard on the arm is still the claim_task lease plus the robot's TDM-45 safety layer.

import { useCallback, useSyncExternalStore } from 'react';

export const TDM59_READY_KEY = 'rdf.tdm59.ready.v1';
export const TDM59_INTRO_KEY = 'rdf.tdm59.intro-seen.v1';
const EVT = 'rdf-tdm59-ready';

export interface ReadinessRecord {
  v: 1;
  passedAt: number;
  practiceMs: number;
  lessons: string[];
  network: { medianMs: number | null; jitterMs: number | null; grade: string; type?: string } | null;
}

function read(): ReadinessRecord | null {
  try {
    const raw = window.localStorage.getItem(TDM59_READY_KEY);
    if (!raw) return null;
    const r = JSON.parse(raw) as ReadinessRecord;
    return r && r.v === 1 && typeof r.passedAt === 'number' ? r : null;
  } catch {
    return null;
  }
}

let cacheRaw: string | null | undefined;
let cacheVal: ReadinessRecord | null = null;
function snapshot(): ReadinessRecord | null {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(TDM59_READY_KEY);
  } catch {
    raw = null;
  }
  if (raw !== cacheRaw) {
    cacheRaw = raw;
    cacheVal = read();
  }
  return cacheVal;
}

function subscribe(cb: () => void) {
  const on = (e: Event) => {
    if (e instanceof StorageEvent && e.key && e.key !== TDM59_READY_KEY) return;
    cb();
  };
  window.addEventListener('storage', on);
  window.addEventListener(EVT, on);
  return () => {
    window.removeEventListener('storage', on);
    window.removeEventListener(EVT, on);
  };
}

export function saveReadiness(r: Omit<ReadinessRecord, 'v' | 'passedAt'>) {
  try {
    window.localStorage.setItem(TDM59_READY_KEY, JSON.stringify({ v: 1, passedAt: Date.now(), ...r }));
  } catch {
    /* private mode: readiness lasts for this page only */
    memoryReady = true;
  }
  window.dispatchEvent(new Event(EVT));
}

let memoryReady = false;

export function clearReadiness() {
  try {
    window.localStorage.removeItem(TDM59_READY_KEY);
  } catch {
    /* ignore */
  }
  memoryReady = false;
  window.dispatchEvent(new Event(EVT));
}

/** `undefined` until hydrated (server render), then the record or null. */
export function useTdm59Readiness(): { status: 'unknown' | 'ready' | 'not_ready'; record: ReadinessRecord | null; clear: () => void } {
  const rec = useSyncExternalStore(subscribe, snapshot, () => undefined as ReadinessRecord | null | undefined);
  const clear = useCallback(() => clearReadiness(), []);
  if (rec === undefined) return { status: 'unknown', record: null, clear };
  return { status: rec || memoryReady ? 'ready' : 'not_ready', record: rec ?? null, clear };
}

export function introSeen(kind: string): boolean {
  try {
    const raw = window.localStorage.getItem(TDM59_INTRO_KEY);
    return raw ? (JSON.parse(raw) as string[]).includes(kind) : false;
  } catch {
    return false;
  }
}

export function markIntroSeen(kind: string) {
  try {
    const raw = window.localStorage.getItem(TDM59_INTRO_KEY);
    const list = raw ? (JSON.parse(raw) as string[]) : [];
    if (!list.includes(kind)) window.localStorage.setItem(TDM59_INTRO_KEY, JSON.stringify([...list, kind]));
  } catch {
    /* ignore */
  }
}
