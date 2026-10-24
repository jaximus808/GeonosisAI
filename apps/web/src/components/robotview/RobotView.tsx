'use client';

// TDM-98: the robot view. One component that shows a robot as a 3D twin (TDM-97) next to its camera
// feed (TDM-55), with a Sim | Live switch over the source registry (lib/robotview/sources.ts).
//
//   <RobotView />                              picks a robot per mode from the registry
//   <RobotView robotId={1} defaultMode="live" /> robot 1 pinned; defaultMode = the ticket's robot mode (TDM-107), absent = sim
//   <RobotView compact hud={<RobotHud ... />} />  smaller switch, camera as picture-in-picture
//   <RobotView simCameras={(c) => <SimCameras ... />} />  TDM-105's rendered sim cams replace the stream in Sim
//
// Rules this component keeps:
//   * Sim always shows the SIMULATED badge and a tinted, hatched frame.
//   * Live never shows sim data. No live robot, an offline one, or a live robot whose state feed says
//     it is simulated all render as an explicit state ("Live robot offline", ...), never the sim twin.
//   * The choice of mode, and of robot per mode, is remembered per viewer in localStorage
//     (try/catch: blocked storage just forgets).
//   * No camera feed (cameras off, no stream announced, no token) shows the twin alone with a note.
//
// Camera access follows TDM-55 / TDM-78: the claimant of a live-leased task on the robot gets
// TaskCameraFeed; an ops token in the URL fragment (#stream_token=..., optional &stream_url=...)
// gets CameraFeed. Tokens are robot-bound, so a token is used only for the robot it names.

import dynamic from 'next/dynamic';
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { CameraFeed, TaskCameraFeed, type FeedHealth } from '@/components/teleop/CameraFeed';
import { Tdm78Boundary } from '@/components/tdm78/Tdm78Boundary';
import { useContributor } from '@/lib/contributor/hooks';
import { useRows } from '@/lib/spacetime/hooks';
import { useTdm78Now } from '@/lib/tdm78/useRobotConsole';
import { staticStreamGrant } from '@/lib/teleop/tdm55StreamGrant';
import {
  MODE_KEY,
  buildRegistry,
  initialMode,
  pickSource,
  readStored,
  robotKey,
  tokenRobotId,
  writeStored,
  type RobotRegistry,
  type RobotSource,
  type RobotViewMode,
} from '@/lib/robotview/sources';
import type { RobotLive } from '@/lib/robotview/types';
import type { TwinCamera } from './RobotTwin3D';
import { useRobotLive, type RobotLiveResult, type UseRobotLiveOptions } from '@/lib/robotview/useRobotLive';
import { useRobotScene } from '@/lib/robotview/useRobotScene';

const RobotTwin3D = dynamic(() => import('./RobotTwin3D').then((m) => m.RobotTwin3D), {
  ssr: false,
  loading: () => <div className="grid h-full place-items-center text-sm text-zinc-400">Loading the 3D view…</div>,
});

export interface RobotViewProps {
  /** Pin this robot for its own kind (sim or live). The other mode still picks from the registry. */
  robotId?: number;
  /** Mode on a viewer's first visit: the ticket's robot mode (TDM-107). A remembered choice wins; absent = sim. */
  defaultMode?: RobotViewMode;
  /** Smaller chrome; the camera becomes a picture-in-picture over the twin. */
  compact?: boolean;
  /**
   * HUD slot (TDM-99 <RobotHud/>), rendered under the panes inside the view's frame. A function gets
   * the same context as simCameras (TDM-100), so the HUD reads this view's state feed and follows the
   * robot the view is showing: hud={({ live, robotId }) => <RobotHud robotId={robotId} state={live.state} stale={live.stale} />}.
   * The function form renders only while a robot is selected.
   */
  hud?: ReactNode | ((ctx: SimCamerasContext) => ReactNode);
  /** Camera feed health of the streamed pane (null when no feed is shown), for pages with a health readout (TDM-100). */
  onFeedHealth?: (h: FeedHealth | null) => void;
  /**
   * Sim camera slot (TDM-105 <SimCameras/>): shown in sim mode in place of the streamed camera feed,
   * only while a sim robot is online. A function gets this view's state feed, so the panes need no
   * second socket: simCameras={({ live, robotId, compact }) => <SimCameras state={live.state} ... />}.
   */
  simCameras?: ReactNode | ((ctx: SimCamerasContext) => ReactNode);
}

export interface SimCamerasContext {
  live: RobotLiveResult;
  robotId: number;
  compact: boolean;
}

type Preset = TwinCamera;

// ---------------------------------------------------------------------------------------------
// Hooks (exported so the pages that mount RobotView can share the registry)

/** Every robot, classified sim / live with an online flag, live. */
export function useRobotSources(): { registry: RobotRegistry; ready: boolean; now: number } {
  const [rows, ready] = useRows('robot');
  const now = useTdm78Now(2000);
  const registry = useMemo(() => buildRegistry(rows, now), [rows, now]);
  return { registry, ready, now };
}

/** The Sim | Live mode and the robot picked per mode, remembered per viewer. */
export function useRobotViewMode(registry: RobotRegistry, opts: { robotId?: number; defaultMode?: RobotViewMode }) {
  // Read storage after mount only, so the server render and the first client render agree.
  const [stored, setStored] = useState<{ mode: string | null; sim: number | null; live: number | null } | null>(null);
  useEffect(() => {
    const num = (k: string) => {
      const v = Number(readStored(k));
      return Number.isFinite(v) && v > 0 ? v : null;
    };
    setStored({ mode: readStored(MODE_KEY), sim: num(robotKey('sim')), live: num(robotKey('live')) });
  }, []);
  const mode = initialMode(stored?.mode ?? null, opts.defaultMode);
  const source = pickSource(registry, mode, opts.robotId, stored?.[mode] ?? null);

  const setMode = useCallback((m: RobotViewMode) => {
    writeStored(MODE_KEY, m);
    setStored((s) => ({ sim: null, live: null, ...s, mode: m }));
  }, []);
  const pickRobot = useCallback((s: RobotSource) => {
    writeStored(MODE_KEY, s.kind);
    writeStored(robotKey(s.kind), String(s.id));
    setStored((p) => ({ sim: null, live: null, ...p, mode: s.kind, [s.kind]: s.id }));
  }, []);
  return { mode, source, setMode, pickRobot };
}

function useHashStreamToken() {
  const [v, setV] = useState<{ token: string; url?: string } | null>(null);
  useEffect(() => {
    const read = () => {
      const p = new URLSearchParams(window.location.hash.replace(/^#/, ''));
      const token = p.get('stream_token');
      setV(token ? { token, url: p.get('stream_url') ?? undefined } : null);
    };
    read();
    window.addEventListener('hashchange', read);
    return () => window.removeEventListener('hashchange', read);
  }, []);
  return v;
}

type CameraAccess =
  | { kind: 'task'; taskId: bigint }
  | { kind: 'ops'; streamUrl: string; token: string; http?: string }
  | { kind: 'none'; why: string; hint?: string };

/**
 * How this browser may watch `src`: its cameras (CameraAccess) and the options for useRobotLive's
 * state socket. Same two paths for both: the live claimant of a task on the robot, or an ops token in
 * the fragment. Without either, useRobotLive asks /api/robotview/state-token (operator login).
 */
function useRobotAccess(src: RobotSource | null, now: number): { camera: CameraAccess; live: UseRobotLiveOptions } {
  const { contributor, identity } = useContributor();
  const [claimed] = useRows('task', (r) => r.status.eq('claimed'));
  const hash = useHashStreamToken();
  if (!src) return { camera: { kind: 'none', why: 'No robot selected.' }, live: { enabled: false } };
  const live: UseRobotLiveOptions = { enabled: src.online };
  const mine = contributor
    ? claimed.find(
        (t) =>
          Number(t.robotId) === src.id &&
          t.assigneeId === contributor.id &&
          t.leaseExpiresAt != null &&
          Number(t.leaseExpiresAt.microsSinceUnixEpoch / 1000n) > now,
      )
    : undefined;
  const hashFor = hash?.token ? tokenRobotId(hash.token) : null;
  const hashOk = !!hash?.token && (hashFor == null || hashFor === src.id);
  const camUrl = hash?.url ?? src.stream?.url ?? null;
  if (mine && identity) {
    live.taskId = mine.id;
    live.identity = identity;
  } else if (hashOk) {
    const stateUrl = src.stream?.stateUrl ?? (camUrl ? camUrl.replace(/\/ws\/cameras$/, '/ws/state') : null);
    if (stateUrl) {
      live.url = stateUrl;
      live.token = hash!.token;
    }
  }

  let camera: CameraAccess;
  if (src.cameras === 'off') camera = { kind: 'none', why: 'Cameras are off on this robot.' };
  else if (!camUrl)
    camera = {
      kind: 'none',
      why: src.cameras ? 'Cameras are on, but the robot has not announced a stream server.' : "The robot didn't report any cameras.",
    };
  else if (mine) camera = { kind: 'task', taskId: mine.id };
  else if (hashOk) camera = { kind: 'ops', streamUrl: camUrl, token: hash!.token, http: hash?.url ? undefined : (src.stream?.http ?? undefined) };
  else if (hash?.token) camera = { kind: 'none', why: `The stream token in this link is for robot #${hashFor}, not ${src.name}.` };
  else
    camera = {
      kind: 'none',
      why: 'The camera feed shows to whoever holds arm control.',
      hint: `Operators: add #stream_token=<token> to this address (python -m rdf_common.stream_token mint --robot-id ${src.id} --robot-name ${src.name} --scope view).`,
    };
  return { camera, live };
}

// ---------------------------------------------------------------------------------------------
// Presentation

function ago(ms: number | null, now: number) {
  if (ms == null) return 'never';
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

type Quality = { level: 0 | 1 | 2 | 3; text: string };

/** Connection quality from the state feed (and the camera, when one is shown). */
function quality(live: RobotLive, feed: FeedHealth | null): Quality {
  if (!live.state || live.stale) return { level: 0, text: live.state ? 'State feed stalled' : 'No state yet' };
  const lat = Math.max(live.latencyMs ?? 0, feed?.worstLatencyMs ?? 0);
  const hz = live.hz;
  const parts = [`${hz.toFixed(0)} Hz`, live.latencyMs != null ? `${Math.round(live.latencyMs)} ms` : null].filter(Boolean).join(', ');
  if (feed?.stale) return { level: 1, text: `${parts}, camera stale` };
  if (lat < 150 && hz >= 8) return { level: 3, text: parts };
  if (lat < 400 && hz >= 3) return { level: 2, text: parts };
  return { level: 1, text: parts };
}

function QualityBars({ q }: { q: Quality }) {
  const tone = q.level >= 3 ? 'bg-emerald-500' : q.level === 2 ? 'bg-amber-500' : q.level === 1 ? 'bg-red-500' : 'bg-zinc-400';
  return (
    <span className="inline-flex items-center gap-2 text-xs tabular-nums text-zinc-600 dark:text-zinc-300" title="State feed rate and latency">
      <span className="flex h-3 items-end gap-[2px]" aria-hidden>
        {[1, 2, 3].map((i) => (
          <span key={i} className={`w-[3px] rounded-sm ${i <= q.level ? tone : 'bg-zinc-300 dark:bg-zinc-700'}`} style={{ height: `${i * 4}px` }} />
        ))}
      </span>
      <span>{q.text}</span>
    </span>
  );
}

function ModeSwitch({
  mode,
  onChange,
  registry,
  compact,
}: {
  mode: RobotViewMode;
  onChange: (m: RobotViewMode) => void;
  registry: RobotRegistry;
  compact?: boolean;
}) {
  const seg = (m: RobotViewMode, label: string) => {
    const list = registry[m];
    const on = list.filter((s) => s.online).length;
    const active = mode === m;
    const activeTone =
      m === 'sim'
        ? 'bg-steel text-ground'
        : 'bg-ink text-ground';
    return (
      <button
        type="button"
        role="radio"
        aria-checked={active}
        onClick={() => onChange(m)}
        className={`flex min-w-0 flex-1 items-baseline justify-center gap-2 rounded-sm font-semibold transition-colors ${
          compact ? 'px-3 py-1 text-sm' : 'px-5 py-2 text-base'
        } ${active ? activeTone : 'text-ink-muted hover:bg-surface'}`}
      >
        {label}
        {!compact && (
          <span className={`text-xs font-normal tabular-nums ${active ? 'opacity-80' : 'text-ink-muted'}`}>
            {list.length === 0 ? 'none' : `${on} of ${list.length} online`}
          </span>
        )}
      </button>
    );
  };
  return (
    <div
      role="radiogroup"
      aria-label="Robot source"
      className={`flex rounded-sm bg-surface-sunken p-1 ring-1 ring-line ${compact ? 'w-44' : 'w-full sm:w-80'}`}
    >
      {seg('sim', 'Sim')}
      {seg('live', 'Live')}
    </div>
  );
}

function RobotPicker({ registry, source, onPick, now }: { registry: RobotRegistry; source: RobotSource | null; onPick: (s: RobotSource) => void; now: number }) {
  const all = registry.sim.length + registry.live.length;
  if (all === 0) return <span className="text-sm text-zinc-500">No robots registered yet.</span>;
  const opt = (s: RobotSource) => (
    <option key={s.id} value={s.id}>
      {s.name} {s.online ? (s.status === 'online' ? '(online)' : `(online, ${s.status})`) : `(offline, seen ${ago(s.lastHeartbeatMs, now)})`}
    </option>
  );
  return (
    <label className="flex min-w-0 items-center gap-2 text-sm">
      <span className="shrink-0 text-zinc-500">Robot</span>
      <select
        className="min-w-0 max-w-full truncate rounded-sm border border-line-strong bg-surface px-2 py-1.5 font-mono text-sm text-ink"
        value={source ? String(source.id) : ''}
        onChange={(e) => {
          const s = registry.byId.get(Number(e.target.value));
          if (s) onPick(s);
        }}
      >
        {!source && <option value="">Choose a robot</option>}
        {registry.sim.length > 0 && <optgroup label="Simulated">{registry.sim.map(opt)}</optgroup>}
        {registry.live.length > 0 && <optgroup label="Live robots">{registry.live.map(opt)}</optgroup>}
      </select>
    </label>
  );
}

/** The striped tape used for everything simulated: badge background and frame edge. */
const SIM_TAPE = 'repeating-linear-gradient(135deg, var(--steel) 0 8px, var(--steel-soft) 8px 16px)';

function SimBadge({ name }: { name?: string }) {
  return (
    <span
      className="gx-tag gx-tag-sim shadow-[inset_0_0_0_1px_var(--steel)]"
      title="Everything in this view comes from a simulated robot, not real hardware."
    >
      SIMULATED
      {name && <span className="font-medium normal-case tracking-normal opacity-90">{name}</span>}
    </span>
  );
}

function PanelNote({ title, children, tone = 'neutral', action }: { title: string; children?: ReactNode; tone?: 'neutral' | 'warn' | 'sim'; action?: ReactNode }) {
  const t =
    tone === 'warn'
      ? 'text-red-200'
      : tone === 'sim'
        ? 'text-ink'
        : 'text-zinc-200';
  return (
    <div role="status" className="grid h-full place-items-center p-6 text-center">
      <div className="max-w-sm space-y-2">
        <p className={`text-base font-semibold ${t}`}>{title}</p>
        {children && <div className="text-sm text-zinc-400">{children}</div>}
        {action}
      </div>
    </div>
  );
}

function PresetSwitch({ preset, onChange }: { preset: Preset; onChange: (p: Preset) => void }) {
  return (
    <div className="flex overflow-hidden rounded-md bg-black/50 text-xs text-white backdrop-blur" role="group" aria-label="3D camera">
      {(['top', 'side', 'front', 'wrist'] as const).map((p) => (
        <button
          key={p}
          type="button"
          aria-pressed={preset === p}
          onClick={() => onChange(p)}
          className={`px-2 py-1 capitalize focus-visible:outline focus-visible:outline-2 focus-visible:outline-white ${preset === p ? 'bg-white/25 font-semibold' : 'hover:bg-white/10'}`}
        >
          {p}
        </button>
      ))}
    </div>
  );
}

function CamSwitch({ cams, cam, onChange }: { cams: string[]; cam: string; onChange: (c: string) => void }) {
  if (cams.length < 2) return null;
  return (
    <div className="flex overflow-hidden rounded-md bg-black/55 text-xs text-white" role="group" aria-label="Camera">
      {cams.map((c) => (
        <button
          key={c}
          type="button"
          aria-pressed={cam === c}
          onClick={() => onChange(c)}
          className={`px-2 py-1 capitalize focus-visible:outline focus-visible:outline-2 focus-visible:outline-white ${cam === c ? 'bg-white/25 font-semibold' : 'hover:bg-white/10'}`}
        >
          {c === 'exo' ? 'top' : c}
        </button>
      ))}
    </div>
  );
}

const FEED_STATE: Record<FeedHealth['state'], string> = {
  connecting: 'Connecting to the camera',
  live: 'Camera connected',
  fallback: 'Camera on snapshots (slow link)',
  reconnecting: 'Camera reconnecting',
  denied: 'Camera refused this browser',
  error: 'Camera error',
  closed: 'Camera closed',
};

function feedLine(h: FeedHealth | null, synthetic: boolean) {
  const parts = [h ? FEED_STATE[h.state] : FEED_STATE.connecting];
  if (h?.worstLatencyMs != null) parts.push(`${Math.round(h.worstLatencyMs)} ms`);
  if (h?.stale && h.state === 'live') parts.push('image stale');
  const line = parts.join(', ') + '.';
  return synthetic ? `${line} Synthetic test pattern, not real images.` : line;
}

function CameraPane({
  access,
  source,
  cams,
  cam,
  onCam,
  onHealth,
  health,
  pip,
}: {
  access: CameraAccess;
  source: RobotSource;
  cams: string[];
  cam: string;
  onCam: (c: string) => void;
  onHealth: (h: FeedHealth | null) => void;
  health: FeedHealth | null;
  pip?: boolean;
}) {
  const ops = access.kind === 'ops' ? access : null;
  const opsUrl = ops?.streamUrl;
  const opsToken = ops?.token;
  const opsHttp = ops?.http;
  const grant = useMemo(
    () => (opsUrl && opsToken ? staticStreamGrant(opsUrl, opsToken, opsHttp) : null),
    [opsUrl, opsToken, opsHttp],
  );
  useEffect(() => {
    if (access.kind === 'none') onHealth(null);
  }, [access.kind, onHealth]);
  // Key on robot + cam so a switch reconnects cleanly. CameraFeed's own footer says "Live" for a
  // connected socket, which reads wrong inside a sim frame; it is hidden and replaced by `feedLine`.
  const key = `${source.id}:${cam}`;
  const hideFooter = '[&>div:last-child]:hidden';
  return (
    <div className={`relative ${pip ? '' : 'h-full'}`}>
      {access.kind === 'task' ? (
        <TaskCameraFeed key={key} taskId={access.taskId} cams={[cam]} onHealth={onHealth} layout="stack" className={hideFooter} />
      ) : grant ? (
        <CameraFeed key={key} getGrant={grant} cams={[cam]} onHealth={onHealth} layout="stack" className={hideFooter} />
      ) : null}
      {access.kind !== 'none' && (
        <div className={`absolute right-2 ${pip ? 'bottom-1' : 'bottom-7'}`}>
          <CamSwitch cams={cams} cam={cam} onChange={onCam} />
        </div>
      )}
      {!pip && access.kind !== 'none' && <p className="mt-1 px-1 text-xs text-zinc-400">{feedLine(health, source.syntheticCameras)}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------

export function RobotView({ robotId, defaultMode, compact = false, hud, simCameras, onFeedHealth }: RobotViewProps) {
  const { registry, ready, now } = useRobotSources();
  const { mode, source, setMode, pickRobot } = useRobotViewMode(registry, { robotId, defaultMode });
  const { camera: access, live: liveOpts } = useRobotAccess(source, now);
  const live = useRobotLive(source?.id ?? null, liveOpts);
  // TDM-97: 'top' matches the sim's top camera and is the twin's default; 'front' is a true front view.
  const [preset, setPreset] = useState<Preset>('top');
  const [feed, setFeed] = useState<FeedHealth | null>(null);
  const onHealth = useCallback((h: FeedHealth | null) => setFeed(h), []);

  const cams = useMemo(() => {
    const c = source?.stream?.cams.length ? source.stream.cams : ['top', 'wrist'];
    return c;
  }, [source?.stream?.cams]);
  const [camPick, setCamPick] = useState<string | null>(null);
  const cam = camPick && cams.includes(camPick) ? camPick : cams[0];

  const sim = mode === 'sim';
  const scene = useRobotScene(sim ? source?.id : null); // TDM-104: the deployed scene spec (sim only)
  // What the twin pane may show. Live never shows sim data; sim never claims to be live.
  let block: { title: string; body?: ReactNode; tone: 'neutral' | 'warn' | 'sim'; offerOther?: boolean } | null = null;
  if (!ready) block = { title: 'Connecting to the factory…', tone: 'neutral' };
  else if (!source)
    block = sim
      ? { title: 'No simulated robot registered', body: 'Start one on the laptop with rdf-robot-client --sim.', tone: 'sim', offerOther: registry.live.length > 0 }
      : { title: 'No live robot registered', body: 'Plug in a robot and run the robot-client on it. Sim data is never shown here.', tone: 'warn', offerOther: registry.sim.length > 0 };
  else if (!source.online)
    block = sim
      ? { title: 'Sim robot offline', body: `${source.name} last checked in ${ago(source.lastHeartbeatMs, now)}.`, tone: 'sim' }
      : {
          title: 'Live robot offline',
          body: `${source.name} last checked in ${ago(source.lastHeartbeatMs, now)}. Its cameras and state are unavailable until the robot-client is back.`,
          tone: 'warn',
          offerOther: registry.sim.some((s) => s.online),
        };
  else if (!sim && live.source === 'sim')
    block = { title: 'This robot reports simulated data', body: `${source.name} is registered as a live robot, but its state feed is simulated. Switch to Sim to view it.`, tone: 'warn', offerOther: true };

  const twinState = block ? null : live.state;
  const q = quality(live, access.kind === 'none' ? null : feed);
  // Sim camera slot (TDM-105) replaces the streamed feed in sim mode; never shown in Live.
  const simSlot =
    sim && simCameras && source && !block ? (typeof simCameras === 'function' ? simCameras({ live, robotId: source.id, compact }) : simCameras) : null;
  const showCams = !block && !simSlot && access.kind !== 'none';
  const hudNode = typeof hud === 'function' ? (source ? hud({ live, robotId: source.id, compact }) : null) : hud;
  const reported = showCams ? feed : null;
  useEffect(() => {
    onFeedHealth?.(reported);
  }, [onFeedHealth, reported]);
  const camNote = !block && !simSlot && access.kind === 'none' ? access : null;
  const sideColumn = showCams || (!!simSlot && !compact);

  // Frame (TDM-109): sim is a steel frame with the striped steel tape edge; live is neutral, red-edged when offline.
  const frame = sim
    ? 'ring-2 ring-steel bg-surface-sunken'
    : block?.tone === 'warn'
      ? 'ring-2 ring-slag/70 bg-surface-sunken'
      : 'ring-1 ring-line bg-surface-sunken';

  const switchOther = block?.offerOther ? (
    <button
      type="button"
      onClick={() => setMode(sim ? 'live' : 'sim')}
      className="mt-2 rounded-md bg-white/10 px-3 py-1.5 text-sm font-medium text-white hover:bg-white/20 focus-visible:outline focus-visible:outline-2 focus-visible:outline-white"
    >
      Switch to {sim ? 'Live' : 'Sim'}
    </button>
  ) : null;

  const twinPane = (
    <div className="relative aspect-[4/3] overflow-hidden rounded-lg bg-black/30">
      {block ? (
        <PanelNote title={block.title} tone={block.tone} action={switchOther}>
          {block.body}
        </PanelNote>
      ) : (
        <>
          <Tdm78Boundary label="3D view">
            <div className={`h-full w-full transition-opacity ${live.stale ? 'opacity-60' : ''}`}>
              <RobotTwin3D state={twinState} camera={preset} sim={sim} spec={sim ? scene.spec : null} ghost={twinState?.chunk_preview} className="h-full w-full" />
            </div>
          </Tdm78Boundary>
          {!live.state && (
            <div className="pointer-events-none absolute inset-0 bg-black/55">
              <PanelNote title={`Waiting for state from ${source!.name}…`} tone={sim ? 'sim' : 'neutral'}>
                The robot is online; its state feed hasn&apos;t sent a sample yet.
                {live.error ? <span className="mt-1 block text-xs text-zinc-500">{live.error}</span> : null}
              </PanelNote>
            </div>
          )}
          {live.state && live.stale && (
            <span className="gx-tag gx-tag-failed absolute bottom-2 left-2">
              State stalled, last sample {ago(live.state.t_ms, now)}
            </span>
          )}
          <div className="absolute right-2 top-2">
            <PresetSwitch preset={preset} onChange={setPreset} />
          </div>
        </>
      )}
      {sim && (
        <div className="absolute left-2 top-2">
          <SimBadge name={compact ? undefined : source?.name} />
        </div>
      )}
      {compact && showCams && source && (
        <div className="absolute bottom-2 right-2 w-[38%] min-w-32 overflow-hidden rounded-md shadow-lg ring-1 ring-black/40">
          <CameraPane access={access} source={source} cams={cams} cam={cam} onCam={setCamPick} onHealth={onHealth} health={feed} pip />
        </div>
      )}
    </div>
  );

  return (
    <section aria-label="Robot view" className="space-y-3" data-mode={mode} data-robot={source?.id ?? ''} data-transport={live.transport}>
      <div className={`flex flex-wrap items-center gap-x-4 gap-y-2 ${compact ? '' : 'sm:flex-nowrap'}`}>
        <ModeSwitch mode={mode} onChange={setMode} registry={registry} compact={compact} />
        {!compact && <RobotPicker registry={registry} source={source} onPick={pickRobot} now={now} />}
        <div className="ml-auto shrink-0">{source?.online && !block && <QualityBars q={q} />}</div>
      </div>

      <div className={`gx-foundry relative overflow-hidden rounded-md p-2 ${frame}`}>
        {sim && <div aria-hidden className="absolute inset-x-0 top-0 h-1.5" style={{ backgroundImage: SIM_TAPE }} />}
        {compact ? (
          <>
            {twinPane}
            {simSlot && <div className="mt-2">{simSlot}</div>}
          </>
        ) : (
          <div className={`grid gap-2 ${sideColumn ? 'lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]' : ''}`}>
            {twinPane}
            {simSlot && <div className="min-w-0">{simSlot}</div>}
            {showCams && source && (
              <div className="min-w-0 rounded-lg bg-black/30 p-1">
                <CameraPane access={access} source={source} cams={cams} cam={cam} onCam={setCamPick} onHealth={onHealth} health={feed} />
              </div>
            )}
          </div>
        )}
        {camNote && (
          <p className="mt-2 px-1 text-xs text-zinc-400">
            Twin only. {camNote.why} {!compact && camNote.hint ? <span className="text-zinc-500">{camNote.hint}</span> : null}
          </p>
        )}
        {hudNode && <div className="mt-2">{hudNode}</div>}
      </div>
    </section>
  );
}

export default RobotView;
