'use client';

// TDM-79: report page actions (hidden when printing). Refresh re-renders the server page from the
// orchestrator; Download fetches report.md through the /api/tdm79 proxy; Save stores a snapshot in
// the media store and logs report.saved (POST /tickets/{id}/report/save).

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useState, useTransition } from 'react';

type SaveState = { kind: 'idle' } | { kind: 'saving' } | { kind: 'saved'; uri: string; fake: boolean } | { kind: 'error'; msg: string };

export function ReportToolbar({ ticketId }: { ticketId: string }) {
  const router = useRouter();
  const [refreshing, startRefresh] = useTransition();
  const [save, setSave] = useState<SaveState>({ kind: 'idle' });

  async function saveSnapshot() {
    setSave({ kind: 'saving' });
    try {
      const res = await fetch(`/api/tdm79/tickets/${ticketId}/report/save`, { method: 'POST' });
      const body = (await res.json().catch(() => ({}))) as { uri?: string; fake?: boolean; detail?: unknown };
      if (!res.ok || !body.uri) {
        const d = body.detail;
        const msg = typeof d === 'string' ? d : Array.isArray(d) ? d.map((x: { msg?: string }) => x.msg).join('; ') : `HTTP ${res.status}`;
        setSave({ kind: 'error', msg });
        return;
      }
      setSave({ kind: 'saved', uri: body.uri, fake: Boolean(body.fake) });
      startRefresh(() => router.refresh());
    } catch (e) {
      setSave({ kind: 'error', msg: (e as Error).message });
    }
  }

  const btn =
    'inline-flex items-center rounded-md border px-3 py-1.5 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--r-after)] disabled:opacity-50';

  return (
    <div className="r-toolbar">
      <Link href={`/tickets/${ticketId}`} className="r-back">
        Ticket #{ticketId}
      </Link>
      <div className="r-actions">
        <button type="button" className={`${btn} r-btn`} disabled={refreshing} onClick={() => startRefresh(() => router.refresh())}>
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
        <a className={`${btn} r-btn`} href={`/api/tdm79/tickets/${ticketId}/report.md`} download={`ticket-${ticketId}-report.md`}>
          Download .md
        </a>
        <button type="button" className={`${btn} r-btn`} disabled={save.kind === 'saving'} onClick={saveSnapshot}>
          {save.kind === 'saving' ? 'Saving…' : 'Save snapshot'}
        </button>
        <button type="button" className={`${btn} r-btn r-btn-primary`} onClick={() => window.print()}>
          Print
        </button>
      </div>
      {save.kind === 'saved' && (
        <p className="r-toolbar-note" role="status">
          Saved to <code>{save.uri}</code>
          {save.fake ? ' (marked fake: built from harness data)' : ''}.
        </p>
      )}
      {save.kind === 'error' && (
        <p className="r-toolbar-note r-toolbar-error" role="alert">
          Couldn&apos;t save the snapshot: {save.msg}
        </p>
      )}
    </div>
  );
}
