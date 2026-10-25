'use client';

// Eval stage on a REAL robot: the policy as the robot's own cameras see it (exo / top and the wrist cams),
// with the teleop page's view switcher (Tdm86ViewTabs): Twin / Top / Wrist / All.
//
// Camera access is the operator's TDM-96 view token: POST /api/robotview/state-token {robot_id} (operator
// login; read-only, never /control), the same path RobotView's state socket uses. Feeds run only while the
// pane is on screen and the browser tab is visible; the pane has one fixed size, so switching views or
// connecting never moves the page.

import { useMemo, useRef } from 'react';
import { useInView } from '@/components/factory-iso/kit/react/live';
import { CameraFeed } from '@/components/teleop/CameraFeed';
import { Tdm86ViewTabs } from '@/components/teleop/Tdm86Phone';
import { operatorLoginHref, useOperator } from '@/lib/operator/useOperator';
import type { StreamGrant } from '@/lib/teleop/tdm55CameraClient';
import type { Tdm116CamView } from '@/lib/tdm116/cockpit';
import { useTdm116PageVisible } from './hooks';

function operatorViewGrant(robotId: number): () => Promise<StreamGrant> {
  return async () => {
    const res = await fetch('/api/robotview/state-token', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ robot_id: robotId }),
      cache: 'no-store',
    });
    const b = (await res.json().catch(() => ({}))) as StreamGrant & { detail?: unknown };
    if (!res.ok || !b.stream_url) throw new Error(typeof b.detail === 'string' ? b.detail : `HTTP ${res.status}`);
    return b;
  };
}

/** The view switcher row over whatever the eval's left pane shows. */
export function Tdm116EvalViewTabs({ views, value, onChange }: { views: readonly Tdm116CamView[]; value: string; onChange: (id: string) => void }) {
  return (
    <div className="tdm116-evalcam-tabs">
      <Tdm86ViewTabs views={views} value={value} onChange={onChange} testPrefix="eval-cam" />
    </div>
  );
}

export function Tdm116EvalCams({ robotId, view }: { robotId: number; view: Tdm116CamView }) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref);
  const visible = useTdm116PageVisible();
  const { operator } = useOperator();
  const getGrant = useMemo(() => operatorViewGrant(robotId), [robotId]);
  const live = inView && visible && operator === true;
  const many = view.cams.length > 1;

  return (
    <div ref={ref} className="tdm116-evalcam" data-testid="eval-cams" data-view={view.id}>
      {operator === false ? (
        <div className="tdm116-empty">
          <strong>Cameras are for operators</strong>
          <a href={operatorLoginHref(typeof window === 'undefined' ? '/dashboard' : `${window.location.pathname}${window.location.search}`)}>Log in as operator</a> to watch the policy through the robot&apos;s cameras.
        </div>
      ) : live ? (
        <CameraFeed key={`${robotId}:${view.id}`} getGrant={getGrant} cams={view.cams} layout={many ? 'grid' : 'stack'} fps={many ? 12 : undefined} className="tdm116-evalcam-feed" />
      ) : (
        <div className="tdm116-empty">
          <strong>Cameras paused</strong>
          {visible ? 'They connect when this pane is on screen.' : 'They resume when this tab is visible again.'}
        </div>
      )}
    </div>
  );
}
