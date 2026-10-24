'use client';

import { useEffect, useState } from 'react';
import QRCode from 'qrcode';

// Phones can't open "localhost", so the QR uses NEXT_PUBLIC_WEB_PUBLIC_URL when it
// is set (LAN address like http://192.168.1.20:3737, a tunnel, or the deployed
// site), else the public URL the make demo-public edge reports (GET /_rdf/access,
// which also gives the access code, but only to this laptop), else the address
// this dashboard was opened on.
const PUBLIC_URL = process.env.NEXT_PUBLIC_WEB_PUBLIC_URL?.replace(/\/+$/, '') || '';

function isLoopback(origin: string): boolean {
  try {
    const h = new URL(origin).hostname;
    return h === 'localhost' || h === '127.0.0.1' || h === '[::1]' || h.endsWith('.localhost');
  } catch {
    return false;
  }
}

/** TDM-60: QR code that opens /join (optionally for a ticket) on a phone. */
export function JoinQr({ ticketId, size = 176 }: { ticketId?: string | bigint; size?: number }) {
  const [origin, setOrigin] = useState(PUBLIC_URL);
  const [svg, setSvg] = useState('');
  const [code, setCode] = useState('');

  useEffect(() => {
    let live = true;
    // make demo-public edge (scripts/demo/public_guard.py); a plain dev server answers 404 and nothing changes.
    fetch('/_rdf/access', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : null))
      .then((b: { public_url?: string | null; code?: string } | null) => {
        if (!live) return;
        if (!PUBLIC_URL) setOrigin(b?.public_url?.replace(/\/+$/, '') || window.location.origin);
        if (b?.code) setCode(b.code);
      })
      .catch(() => live && !PUBLIC_URL && setOrigin(window.location.origin));
    return () => {
      live = false;
    };
  }, []);

  const url = origin ? `${origin}/join${ticketId != null ? `?ticket=${String(ticketId)}` : ''}` : '';

  useEffect(() => {
    if (!url) return;
    let cancelled = false;
    QRCode.toString(url, { type: 'svg', margin: 1, errorCorrectionLevel: 'M', width: size })
      .then((s) => {
        if (!cancelled) setSvg(s);
      })
      .catch(() => setSvg(''));
    return () => {
      cancelled = true;
    };
  }, [url, size]);

  return (
    <div className="inline-flex flex-col items-center gap-2 rounded-lg border border-zinc-200 bg-white p-3 text-center dark:border-zinc-800">
      {svg ? (
        // SVG markup generated locally by the qrcode library from our own URL.
        <div style={{ width: size, height: size }} dangerouslySetInnerHTML={{ __html: svg }} aria-label={`QR code for ${url}`} role="img" />
      ) : (
        <div style={{ width: size, height: size }} className="animate-pulse rounded bg-zinc-100" />
      )}
      <p className="text-xs font-medium text-zinc-700">Scan to join and get paid tasks</p>
      {code && (
        <p className="text-xs text-zinc-600">
          Access code <span className="ml-1 rounded bg-zinc-900 px-2 py-0.5 font-mono text-lg font-semibold tracking-[0.2em] text-white">{code}</span>
        </p>
      )}
      {url && <a href={url} className="max-w-[16rem] break-all font-mono text-[11px] text-blue-600 hover:underline">{url}</a>}
      {origin && isLoopback(origin) && (
        <p className="max-w-[16rem] text-[11px] text-amber-700">
          This is a localhost address, so phones can&apos;t open it. Set NEXT_PUBLIC_WEB_PUBLIC_URL to the laptop&apos;s LAN or tunnel URL.
        </p>
      )}
    </div>
  );
}
