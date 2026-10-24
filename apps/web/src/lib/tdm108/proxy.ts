// TDM-108: allow-list for /api/tdm108 (app/api/tdm108/[...path]/route.ts). Pure, so it is unit-tested.

export type Tdm108Route = { target: string; operator: boolean; download?: string };

const ID = /^\d{1,19}$/;
const DROP = /^[a-z0-9-]{1,64}$/;
const CHANNEL = /^(discord|bluesky|x|telegram)\.png$/;

/** The orchestrator path for an allowed call, else null. */
export function tdm108Route(method: 'GET' | 'POST' | 'PUT', parts: string[], search: URLSearchParams): Tdm108Route | null {
  // per-ticket channel choice: GET / PUT drops/tickets/{id}/channels (operator)
  if ((method === 'GET' || method === 'PUT') && parts.length === 4 && parts[0] === 'drops' && parts[1] === 'tickets' &&
    ID.test(parts[2]) && parts[3] === 'channels') {
    return { target: parts.join('/'), operator: true };
  }
  if (method === 'PUT') return null;
  if (method === 'GET') {
    if (parts.length === 1 && parts[0] === 'drops') return { target: 'drops', operator: true };
    if (parts.length === 4 && parts[0] === 'drops' && DROP.test(parts[1]) && parts[2] === 'card' && CHANNEL.test(parts[3])) {
      return { target: parts.join('/'), operator: true };
    }
    if (parts.length === 2 && parts[0] === 'share' && parts[1].endsWith('.png') && ID.test(parts[1].slice(0, -4))) {
      const task = parts[1].slice(0, -4);
      const size = search.get('size') === 'wide' ? 'wide' : 'square';
      return {
        target: `drops/share/${task}.png?size=${size}`,
        operator: false,
        download: search.get('download') === '1' ? `geonosisai-task-${task}-${size}.png` : undefined,
      };
    }
    return null;
  }
  if (parts.length === 3 && parts[0] === 'drops' && DROP.test(parts[1]) && parts[2] === 'post') {
    return { target: parts.join('/'), operator: true };
  }
  if (parts.length === 2 && parts[0] === 'drops' && parts[1] === 'preview') return { target: 'drops/preview', operator: true };
  if (parts.length === 1 && parts[0] === 'claim-src') return { target: 'drops/claim-src', operator: false };
  return null;
}

/** /j/{task}/{src} short links: the marketplace deep link they redirect to, else null. */
export function tdm108ShortTarget(task: string, src: string): string | null {
  if (!ID.test(task) || task === '0' || !/^[a-z0-9_-]{1,24}$/.test(src)) return null;
  return `/marketplace?task=${task}&src=${src}`;
}
