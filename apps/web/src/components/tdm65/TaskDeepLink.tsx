'use client';

// TDM-65: a push notification opens /marketplace?task=<id>. Scroll that task into view and ring it,
// or say plainly that someone else already took it (the other open tasks are right below).

import { useEffect, useState } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { useContributor } from '@/lib/contributor/hooks';

export function TaskDeepLink() {
  const [taskId, setTaskId] = useState<bigint | null>(null);
  const [tasks, ready] = useRows('task');
  const [done, setDone] = useState(false);
  const { contributor } = useContributor();

  useEffect(() => {
    const read = () => {
      const raw = new URLSearchParams(window.location.search).get('task');
      setTaskId(raw && /^\d{1,19}$/.test(raw) ? BigInt(raw) : null);
      setDone(false);
    };
    read();
    // An already-open tab gets the tap as a postMessage from the service worker.
    const onMsg = (e: MessageEvent) => {
      if (e.data?.type === 'tdm65-open' && typeof e.data.url === 'string') {
        window.history.replaceState(null, '', e.data.url);
        read();
      }
    };
    navigator.serviceWorker?.addEventListener('message', onMsg);
    return () => navigator.serviceWorker?.removeEventListener('message', onMsg);
  }, []);

  const task = taskId == null ? undefined : tasks.find((t) => t.id === taskId);
  const open = task?.status === 'open';

  useEffect(() => {
    if (taskId == null || !ready || done || !open) return;
    let tries = 0;
    const timer = window.setInterval(() => {
      const el = document.getElementById(`task-${taskId}`);
      if (el || ++tries > 40) {
        window.clearInterval(timer);
        setDone(true);
        if (!el) return;
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.animate(
          [{ boxShadow: '0 0 0 4px #f5b301' }, { boxShadow: '0 0 0 4px #f5b301', offset: 0.7 }, { boxShadow: '0 0 0 0 transparent' }],
          { duration: 3200, easing: 'ease-out' },
        );
      }
    }, 100);
    return () => window.clearInterval(timer);
  }, [taskId, ready, done, open]);

  if (taskId == null || !ready || open) return null;
  if (task && contributor && task.assigneeId === contributor.id) return null; // you claimed it: the held-claim card shows it
  return (
    <p role="status" className="mb-4 rounded-md border border-zinc-300 bg-zinc-50 px-3 py-2 text-sm text-zinc-700 dark:border-zinc-700 dark:bg-zinc-900/60 dark:text-zinc-300">
      {task == null
        ? `Task #${taskId} isn't available any more.`
        : `Task #${taskId} was already ${task.status === 'claimed' ? 'claimed by someone else' : task.status}.`}{' '}
      Other open tasks are below.
    </p>
  );
}
