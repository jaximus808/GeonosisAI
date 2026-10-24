'use client';

// TDM-108: report which job-drop channel brought this browser in, once per task it claims here.
// Watches the task table instead of hooking the claim button, so a claim made from any surface of
// this tab counts. Tasks already held when the page loaded are not reported.

import { useEffect, useRef } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { useContributor } from '@/lib/contributor/hooks';
import { useTdm108Attribution } from '@/lib/tdm108/attribution';

export function Tdm108ClaimSource() {
  const report = useTdm108Attribution();
  const { contributor } = useContributor();
  const [tasks, ready] = useRows('task');
  const seen = useRef<Set<bigint> | null>(null);
  const me = contributor?.id;

  useEffect(() => {
    if (!ready || me == null) return;
    const mine = tasks.filter((t) => t.assigneeId === me && t.status !== 'open').map((t) => t.id);
    if (seen.current == null) {
      seen.current = new Set(mine);
      return;
    }
    for (const id of mine) {
      if (!seen.current.has(id)) {
        seen.current.add(id);
        report(id);
      }
    }
  }, [tasks, ready, me, report]);

  return null;
}
