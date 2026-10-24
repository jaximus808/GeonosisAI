'use client';

// TDM-108: "Share" on a marketplace task: copy the task's short link (/j/{id}/share) or download its
// share card (the same PNG the job drops post, rendered by the orchestrator).

import { useEffect, useRef, useState } from 'react';

export function ShareTask({ taskId, className = '' }: { taskId: bigint; className?: string }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const id = String(taskId);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  async function copy() {
    const url = `${window.location.origin}/j/${id}/share`;
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      window.prompt('Copy this link', url);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }

  const item =
    'block w-full rounded px-3 py-2 text-left text-sm outline-none hover:bg-zinc-100 focus-visible:ring-2 focus-visible:ring-zinc-500 dark:hover:bg-zinc-800';
  return (
    <div ref={box} className={`relative inline-block ${className}`}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="rounded border border-zinc-300 px-2 py-1 text-[13px] font-medium text-zinc-700 outline-none focus-visible:ring-2 focus-visible:ring-zinc-500 dark:border-zinc-700 dark:text-zinc-300"
      >
        Share
      </button>
      {open && (
        <div className="absolute left-0 z-20 mt-1 w-52 rounded-md border border-zinc-200 bg-white p-1 shadow-lg dark:border-zinc-700 dark:bg-zinc-900">
          <button type="button" className={item} onClick={() => void copy()}>
            {copied ? 'Link copied' : 'Copy link'}
          </button>
          <a className={item} href={`/api/tdm108/share/${id}.png?size=square&download=1`} download>
            Download card (square)
          </a>
          <a className={item} href={`/api/tdm108/share/${id}.png?size=wide&download=1`} download>
            Download card (wide)
          </a>
        </div>
      )}
    </div>
  );
}
