'use client';

// TDM-115: "What is an agent?", one click away on every cell and open on the first visit.

import { useCallback, useEffect, useState } from 'react';
import type { Tdm115Agent } from '@/lib/tdm115/iso';

const SEEN = 'tdm115.agentGuideSeen';

export function useAgentGuide() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    try {
      if (!window.localStorage.getItem(SEEN)) {
        setOpen(true);
        window.localStorage.setItem(SEEN, '1');
      }
    } catch {
      /* storage blocked: stay closed */
    }
  }, []);
  const toggle = useCallback(() => setOpen((o) => !o), []);
  const close = useCallback(() => setOpen(false), []);
  return { open, toggle, close };
}

const KIND: Record<Tdm115Agent['kind'], string> = { claude: 'Claude', droid: 'Factory Droid', service: 'Program' };

export function AgentGuide({ agents, onClose }: { agents: Tdm115Agent[]; onClose: () => void }) {
  return (
    <aside className="fiso-guide" aria-label="What is an agent?">
      <div className="fiso-guide-head">
        <h3 className="gx-station-label m-0">What is an agent?</h3>
        <button type="button" className="fiso-x" onClick={onClose}>
          Close
        </button>
      </div>
      <p className="fiso-guide-lede">
        An agent is a program that makes decisions and acts on its own. Some here use Claude to reason (diagnose, plan, review); others are
        long-running Factory Droids that sleep until the database changes. You approve spending; they do the rest.
      </p>
      <ul className="fiso-guide-list">
        {agents.map((a) => (
          <li key={a.key} data-st={a.state}>
            <span className="fiso-guide-name">
              <i className="fiso-dot" aria-hidden />
              {a.name}
              <span className="fiso-kind">{KIND[a.kind]}</span>
            </span>
            <span className="gx-small">
              {a.does} {a.wakes}
            </span>
          </li>
        ))}
      </ul>
    </aside>
  );
}
