// Allow-list for /api/orch (see app/api/orch/[...path]/route.ts). Pure, so it is unit-tested.

/** The orchestrator path for an allowed GET, else null. */
export function orchProxyTarget(parts: string[], search: URLSearchParams): string | null {
  const p = parts.join('/');
  if (p === 'media/sign') {
    const uri = search.get('uri');
    return uri && uri.length <= 2048 ? `media/sign?uri=${encodeURIComponent(uri)}` : null;
  }
  if (parts.length === 3 && parts[0] === 'tickets' && /^\d{1,19}$/.test(parts[1]) && parts[2] === 'decision') return p;
  if (p === 'agents/replay/source') return p;
  return null;
}
