'use client';

// TDM-75 episode video: storage URIs (modal-vol://, s3://) are turned into short-lived signed GET
// URLs by the orchestrator media API (TDM-4, GET /media/sign?uri=). Public, not a secret.
import { useEffect, useState } from 'react';

export const ORCHESTRATOR_URL = (process.env.NEXT_PUBLIC_ORCHESTRATOR_URL || 'http://localhost:8000').replace(/\/$/, '');

type Signed = { url: string; expiresAt: number };
const cache = new Map<string, Signed>();
const inflight = new Map<string, Promise<Signed>>();

type MediaState = { url?: string; error?: string; unplayable?: string };

function classify(uri: string): MediaState | null {
  if (/^https?:\/\//.test(uri)) return { url: uri };
  if (uri.startsWith('file://')) return { unplayable: 'Local file on the robot computer, not uploaded' };
  if (uri.startsWith('fake://')) return { unplayable: 'Fake camera, no video' };
  return null;
}

async function sign(uri: string): Promise<Signed> {
  const hit = cache.get(uri);
  if (hit && hit.expiresAt * 1000 > Date.now() + 60_000) return hit;
  let p = inflight.get(uri);
  if (!p) {
    p = fetch(`${ORCHESTRATOR_URL}/media/sign?uri=${encodeURIComponent(uri)}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`media sign ${r.status}`);
        const j = (await r.json()) as { url: string; expires_at: number };
        const s = { url: j.url, expiresAt: j.expires_at };
        cache.set(uri, s);
        return s;
      })
      .finally(() => inflight.delete(uri));
    inflight.set(uri, p);
  }
  return p;
}

export function useSignedUrl(uri: string | undefined): MediaState {
  const direct = uri ? classify(uri) : { unplayable: 'No video' };
  const [state, setState] = useState<MediaState>({});
  useEffect(() => {
    if (!uri || direct) return;
    let live = true;
    sign(uri).then(
      (s) => live && setState({ url: s.url }),
      (e: Error) => live && setState({ error: `Could not sign the video URL (${e.message}). Is the orchestrator media API running at ${ORCHESTRATOR_URL}?` }),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uri]);
  return direct ?? state;
}

export function CamVideo({ uri, name, fake, compact }: { uri: string; name: string; fake?: boolean; compact?: boolean }) {
  const { url, error, unplayable } = useSignedUrl(uri);
  const frame = `relative aspect-video w-full overflow-hidden rounded bg-zinc-900 ${fake ? 'outline outline-2 -outline-offset-2 outline-fuchsia-600' : ''}`;
  return (
    <figure className="min-w-0">
      <div className={frame}>
        {url ? (
          <video
            src={url}
            controls={!compact}
            muted
            playsInline
            preload="metadata"
            className="h-full w-full object-contain"
            onMouseEnter={compact ? (e) => void e.currentTarget.play().catch(() => {}) : undefined}
            onMouseLeave={compact ? (e) => e.currentTarget.pause() : undefined}
          />
        ) : (
          <div className="flex h-full items-center justify-center p-2 text-center text-[11px] leading-snug text-zinc-400">
            {unplayable ?? error ?? 'Signing video URL…'}
          </div>
        )}
      </div>
      {!compact && <figcaption className="mt-1 text-[11px] text-zinc-500">{name}</figcaption>}
    </figure>
  );
}
