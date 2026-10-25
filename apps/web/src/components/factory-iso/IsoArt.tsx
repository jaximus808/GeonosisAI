'use client';

// TDM-115: the drawing. Solids from ./geometry painted back to front with the vendored Anatomy
// components (kit/react/draw.tsx). State lives in data attributes (data-st, data-mark) that
// factory-iso.css colours, so a new row from SpacetimeDB changes classes, never geometry.

import { memo, useLayoutEffect, useRef, type ReactNode } from 'react';
import { Dots, FaceText, Line, Solid } from './kit/react/draw';
import type { Point3 } from './kit/iso-kit';
import {
  AGENT_SPOTS,
  BAY_SPOTS,
  HIT,
  PART_SPOTS,
  VIEW_H,
  VIEW_W,
  bayArt,
  beltArt,
  beltRoute,
  collectArt,
  crateArt,
  depth,
  diagArt,
  droidArt,
  evalArt,
  floorArt,
  moveTo,
  planArt,
  reviewArt,
  robotArt,
  shipArt,
  trainArt,
  BELT_STOPS,
  LIGHT_COLS,
} from './geometry';
import type { Tdm115Agent, Tdm115Cell, Tdm115MachineKey } from '@/lib/tdm115/iso';

type Key = Tdm115MachineKey;

const Floor = memo(function Floor() {
  return (
    <g key="floor" className="fiso-floor">
      <path className="iso-halo" d={floorArt.slab.fill} />
      <Solid paths={floorArt.slab} tone="lo" crease="faint" />
      <Line d={floorArt.seam} tone="faint" />
      <Dots points={floorArt.dots} size={0.7} tone="faint" />
      <Line d={floorArt.ticksFront.minor + floorArt.ticksLeft.minor} tone="faint" />
      <Line d={floorArt.ticksFront.major + floorArt.ticksLeft.major} tone="lo" />
      <Dots points={floorArt.screws} size={1.3} tone="mid" />
      <Solid paths={bayArt.pad} tone="faint" crease="none" flat />
      <Line d={bayArt.hatch} tone="faint" />
    </g>
  );
});

function Belt({ running }: { running: boolean }) {
  return (
    <g key="belt" className="fiso-belt" data-running={running ? '' : undefined}>
      <Solid paths={beltArt.outer} tone="mid" />
      <Line d={beltArt.edgeOut} tone="faint" />
      <Line d={beltArt.edgeIn} tone="faint" />
      <Line d={beltArt.tread} tone={running ? 'lit' : 'lo'} flow={running} dashed={!running} className="fiso-tread" />
      <Solid paths={beltArt.island} tone="lo" />
    </g>
  );
}

// ------------------------------------------------------------------------------ machines

const LIGHT_MARK = { pass: 'pass', fail: 'fail', todo: 'todo', now: 'now' } as const;

function EvalBench({ cell }: { cell: Tdm115Cell }) {
  const rows = [cell.lights.baseline, cell.lights.finetuned];
  return (
    <g className="fiso-machine" data-machine="eval" data-st={cell.machines.eval.state}>
      <path className="iso-halo" d={evalArt.base.fill} />
      <Solid paths={evalArt.base} tone="lo" />
      <Dots points={evalArt.feet} size={0.6} tone="mid" />
      <Solid paths={evalArt.panel} tone="mid" />
      <Solid paths={evalArt.cap} tone="mid" />
      {evalArt.lights.map((row, r) =>
        row.map((d, i) => {
          const l = rows[r];
          const mark = l && i < l.trials ? LIGHT_MARK[l.marks[i] ?? 'todo'] : 'off';
          return <path key={`${r}-${i}`} className="fiso-light" data-mark={mark} d={d} />;
        }),
      )}
      <FaceText transform={evalArt.rowText[0]} size={2.4} tone="lo">
        B
      </FaceText>
      <FaceText transform={evalArt.rowText[1]} size={2.4} tone="lo">
        F
      </FaceText>
    </g>
  );
}

function Robot({ cell }: { cell: Tdm115Cell }) {
  const running = cell.machines.eval.state === 'working';
  return (
    <g className="fiso-robot" data-running={running ? '' : undefined}>
      {robotArt.legs.map((l, i) => (
        <Solid key={i} paths={l} tone="lo" />
      ))}
      <Solid paths={robotArt.shelf} tone="lo" />
      <path className="iso-halo" d={robotArt.top.fill} />
      <Solid paths={robotArt.top} tone="mid" />
      <Line d={robotArt.topSeam} tone="faint" />
      <Solid paths={robotArt.plate} tone="mid" />
      <Line d={robotArt.plateRim} tone="faint" />
      <Solid paths={robotArt.base} tone="mid" />
      <Dots points={robotArt.baseBolts} size={0.5} tone="mid" />
      <Solid paths={robotArt.turret} tone="hi" />
      <Solid paths={robotArt.shoulder} tone="mid" />
      <Solid paths={robotArt.upper} tone="hi" />
      <Line d={robotArt.cable} tone="lo" />
      <Solid paths={robotArt.fingerBack} tone="hi" />
      <Solid paths={robotArt.cube} tone="hi" lit={running} className="fiso-cube" />
      <Solid paths={robotArt.wrist} tone="hi" />
      <path className="fiso-wristband" d={robotArt.wristBand} />
      <Solid paths={robotArt.forearm} tone="hi" />
      <Solid paths={robotArt.elbowCap} tone="mid" />
      <Solid paths={robotArt.gripper} tone="hi" />
      <Solid paths={robotArt.fingerFront} tone="hi" />
    </g>
  );
}

function Diagnose({ cell }: { cell: Tdm115Cell }) {
  return (
    <g className="fiso-machine" data-machine="diagnose" data-st={cell.machines.diagnose.state}>
      <path className="iso-halo" d={diagArt.body.fill} />
      <Solid paths={diagArt.body} tone="mid" />
      <Solid paths={diagArt.hood} tone="mid" />
      <Line d={diagArt.vent} tone="faint" />
      <path className="fiso-slit" d={diagArt.slit} />
      {diagArt.bins.map((b, i) => {
        const bin = cell.bins[i];
        return (
          <g key={i} className="fiso-bin" data-full={bin ? '' : undefined}>
            <Solid paths={b.body} tone={bin ? 'mid' : 'lo'} />
            <path className="fiso-cavity" d={b.cavity} />
            {bin && (
              <FaceText transform={b.text} size={4} tone="hi">
                {bin.count}
              </FaceText>
            )}
          </g>
        );
      })}
    </g>
  );
}

function PlanDesk({ cell }: { cell: Tdm115Cell }) {
  return (
    <g className="fiso-machine" data-machine="plan" data-st={cell.machines.plan.state}>
      {planArt.legs.map((l, i) => (
        <Solid key={i} paths={l} tone="lo" />
      ))}
      <path className="iso-halo" d={planArt.top.fill} />
      <Solid paths={planArt.top} tone="mid" />
      <Solid paths={planArt.stand} tone="lo" />
      <Solid paths={planArt.monitor} tone="mid" />
      <path className="fiso-screen" data-on={cell.plan ? '' : undefined} d={planArt.screen} />
      {cell.plan && (
        <g className="fiso-sheet" data-waiting={cell.plan.waiting ? '' : undefined}>
          <Solid paths={planArt.sheet} tone="hi" crease="none" />
          <Line d={planArt.sheetLines} tone="lo" />
          <Solid paths={planArt.tag} tone="hi" className="fiso-price" crease="none" />
        </g>
      )}
    </g>
  );
}

function CollectDock({ cell }: { cell: Tdm115Cell }) {
  const c = cell.collect;
  const marks = [...Array(c.done).fill('done'), ...Array(c.claimed).fill('claimed'), ...Array(c.open).fill('open')].slice(0, collectArt.cards.length) as string[];
  return (
    <g className="fiso-machine" data-machine="collect" data-st={cell.machines.collect.state}>
      <path className="iso-halo" d={collectArt.deck.fill} />
      <Solid paths={collectArt.deck} tone="lo" />
      <Line d={collectArt.stripes} tone="faint" />
      <Solid paths={collectArt.posts[0]} tone="mid" />
      <Solid paths={collectArt.rail} tone="mid" />
      <Line d={collectArt.hooks} tone="lo" />
      {collectArt.cards.map((card, i) => (
        <g key={i} className="fiso-ticketcard" data-mark={marks[i] ?? 'none'}>
          {marks[i] ? <Solid paths={card} tone="hi" crease="none" /> : null}
        </g>
      ))}
      <Solid paths={collectArt.posts[1]} tone="mid" />
      <Solid paths={collectArt.mast} tone="mid" />
      <Solid paths={collectArt.beacon} tone="hi" className="fiso-beacon" />
    </g>
  );
}

function ReviewGate({ cell }: { cell: Tdm115Cell }) {
  const r = cell.review;
  return (
    <g className="fiso-machine" data-machine="review" data-st={cell.machines.review.state}>
      <path className="iso-halo" d={reviewArt.base.fill} />
      <Solid paths={reviewArt.base} tone="lo" />
      <Line d={reviewArt.rollers} tone="faint" />
      <Solid paths={reviewArt.postBack} tone="mid" />
      <Solid paths={reviewArt.reject} tone="mid" className="fiso-reject" />
      <path className="fiso-cavity" d={reviewArt.rejectCavity} />
      <FaceText transform={reviewArt.rejectText} size={3.6} tone="hi">
        {r.rejected}
      </FaceText>
      <Solid paths={reviewArt.pass} tone="mid" className="fiso-pass" />
      <FaceText transform={reviewArt.passText} size={3.6} tone="hi">
        {r.accepted}
      </FaceText>
      <Solid paths={reviewArt.postFront} tone="mid" />
      <Solid paths={reviewArt.bar} tone="mid" />
      <Solid paths={reviewArt.lamp} tone="hi" className="fiso-lamp" />
    </g>
  );
}

function Furnace({ cell }: { cell: Tdm115Cell }) {
  return (
    <g className="fiso-machine" data-machine="train" data-st={cell.machines.train.state}>
      <path className="iso-halo" d={trainArt.plinth.fill} />
      <Solid paths={trainArt.plinth} tone="lo" />
      <Solid paths={trainArt.body} tone="mid" />
      <path className="fiso-mouth" d={trainArt.mouth} />
      <Line d={trainArt.grate} tone="lo" />
      <Dots points={trainArt.rivets} size={0.6} tone="mid" />
      <Solid paths={trainArt.chimney} tone="mid" />
      <Solid paths={trainArt.chimneyCap} tone="mid" />
      <Solid paths={trainArt.label} tone="lo" crease="none" />
      {cell.train.tag && (
        <FaceText transform={trainArt.labelText} size={3.6} tone="hi">
          {cell.train.tag}
        </FaceText>
      )}
    </g>
  );
}

function ShipBay({ cell }: { cell: Tdm115Cell }) {
  return (
    <g className="fiso-machine" data-machine="ship" data-st={cell.machines.ship.state} data-shipped={cell.ship.shipped ? '' : undefined}>
      <path className="iso-halo" d={shipArt.deck.fill} />
      <Solid paths={shipArt.deck} tone="lo" />
      <Line d={shipArt.rails} tone="faint" />
      <Solid paths={shipArt.postL} tone="mid" />
      <Solid paths={shipArt.lintel} tone="mid" />
      <Solid paths={shipArt.crate} tone="hi" className="fiso-shipcrate" />
      <Line d={shipArt.crateBand} tone="lo" />
      <Solid paths={shipArt.postR} tone="mid" />
    </g>
  );
}

// ------------------------------------------------------------------------------ crates and droids

function Crate({ at, count }: { at: Point3; count: number }) {
  return (
    <g className="fiso-crate" style={{ transform: moveTo(at) }}>
      <Solid paths={crateArt.body} tone="hi" />
      <Line d={crateArt.band} tone="lo" />
      <FaceText transform={crateArt.text} size={4.2} tone="hi">
        {count > 99 ? '99+' : count}
      </FaceText>
    </g>
  );
}

/** Where a figure stands. Droids not acting line up in the bay at the head of the line. */
export function spotOf(a: Tdm115Agent): Point3 {
  if (a.at === 'bay') {
    const i = ['onboarding_droid', 'data_droid', 'training_droid'].indexOf(a.key);
    return BAY_SPOTS[Math.max(0, i)];
  }
  return AGENT_SPOTS[a.key] ?? AGENT_SPOTS.eval_runner;
}

function Droid({ a, selected, onSelect }: { a: Tdm115Agent; selected: boolean; onSelect: (k: string) => void }) {
  const spot = spotOf(a);
  return (
    <g
      className="fiso-droid"
      data-st={a.state}
      data-kind={a.kind}
      data-woke={a.woke ? '' : undefined}
      data-thinking={a.thinking ? '' : undefined}
      data-selected={selected ? '' : undefined}
      style={{ transform: moveTo(spot) }}
      onClick={() => onSelect(a.key)}
    >
      <path className="fiso-droid-ring" d={droidArt.ring} />
      <path className="fiso-droid-pulse" d={droidArt.ring} />
      <Solid paths={droidArt.wheel} tone="lo" />
      <Solid paths={droidArt.armL} tone="mid" />
      <Solid paths={droidArt.torso} tone="hi" />
      {a.kind === 'claude' && <path className="fiso-droid-chip" d={droidArt.chip} />}
      <Solid paths={droidArt.armR} tone="mid" />
      <Solid paths={droidArt.neck} tone="lo" />
      <Solid paths={droidArt.head} tone="hi" />
      <path className="fiso-visor" d={droidArt.visor} />
      <Line d={droidArt.antenna} tone="mid" />
      <circle className="fiso-tip" cx={droidArt.tip[0]} cy={droidArt.tip[1]} r={1.6} />
    </g>
  );
}

// ------------------------------------------------------------------------------ the figure

const MACHINE_DEPTH: Record<Key | 'robot', number> = {
  plan: depth([35, 100, 0]),
  collect: depth([130, 35, 0]),
  diagnose: depth([35, 190, 0]),
  review: depth([250, 35, 0]),
  robot: depth([155, 175, 0]),
  eval: depth([227, 170, 0]),
  train: depth([365, 115, 0]),
  ship: depth([365, 240, 0]),
};

export function IsoArt({
  cell,
  selected,
  onSelect,
  onMachine,
  reduced,
  label,
}: {
  cell: Tdm115Cell;
  selected: string | null;
  onSelect: (key: string) => void;
  onMachine: (key: Key) => void;
  reduced: boolean;
  label: string;
}) {
  const items: { d: number; key: string; node: ReactNode }[] = [];
  const add = (d: number, key: string, node: ReactNode) => items.push({ d, key, node });
  add(MACHINE_DEPTH.plan, 'm-plan', <PlanDesk cell={cell} />);
  add(MACHINE_DEPTH.collect, 'm-collect', <CollectDock cell={cell} />);
  add(MACHINE_DEPTH.diagnose, 'm-diagnose', <Diagnose cell={cell} />);
  add(MACHINE_DEPTH.review, 'm-review', <ReviewGate cell={cell} />);
  add(MACHINE_DEPTH.robot, 'm-robot', <Robot cell={cell} />);
  add(MACHINE_DEPTH.eval, 'm-eval', <EvalBench cell={cell} />);
  add(MACHINE_DEPTH.train, 'm-train', <Furnace cell={cell} />);
  add(MACHINE_DEPTH.ship, 'm-ship', <ShipBay cell={cell} />);
  for (const p of cell.parts) if (p.count > 0) add(depth(PART_SPOTS[p.key]), `p-${p.key}`, <Crate at={PART_SPOTS[p.key]} count={p.count} />);
  for (const a of cell.agents) add(depth(spotOf(a)) + 0.5, `a-${a.key}`, <Droid a={a} selected={selected === a.key} onSelect={onSelect} />);
  items.sort((x, y) => x.d - y.d);

  return (
    <svg className="iso-svg fiso-svg" viewBox={`0 0 ${VIEW_W} ${VIEW_H}`} role="img" aria-label={label} focusable="false">
      <Floor />
      <Belt running={cell.active !== null && !cell.waitingForRobot} />
      <Handoff cell={cell} reduced={reduced} />
      {items.map((it) => (
        <g key={it.key}>{it.node}</g>
      ))}
      {/* Hit areas over every machine: click to see more. Drawn last, transparent. */}
      <g className="fiso-hits">
        {(Object.keys(HIT) as Key[]).map((k) => (
          <path key={k} d={HIT[k]} data-machine={k} onClick={() => onMachine(k)}>
            <title>{cell.machines[k].label}</title>
          </path>
        ))}
      </g>
    </svg>
  );
}

/** The part on the conveyor: when the active machine changes, a crate rides the belt to the next one. */
function Handoff({ cell, reduced }: { cell: Tdm115Cell; reduced: boolean }) {
  const ref = useRef<SVGGElement>(null);
  const prev = useRef<Key | null>(cell.active);
  useLayoutEffect(() => {
    const from = prev.current;
    const to = cell.active;
    prev.current = to;
    const el = ref.current;
    if (!el || reduced || !from || !to || from === to || typeof el.animate !== 'function') return;
    const order: Key[] = ['eval', 'diagnose', 'plan', 'collect', 'review', 'train', 'eval', 'ship'];
    const back = order.indexOf(to) < order.indexOf(from) && !(from === 'train' && to === 'eval');
    const route = beltRoute(from, to, back && to !== 'eval');
    if (route.length < 2) return;
    const frames = route.map((p, i) => ({ transform: moveTo(p), opacity: i === 0 || i === route.length - 1 ? 0 : 1, offset: i / (route.length - 1) }));
    el.animate(frames, { duration: Math.min(3200, 500 * route.length), easing: 'cubic-bezier(0.45, 0, 0.2, 1)' });
  }, [cell.active, reduced]);
  return (
    <g ref={ref} className="fiso-handoff" style={{ transform: moveTo(BELT_STOPS.eval), opacity: 0 }}>
      <Solid paths={crateArt.body} tone="hi" lit />
    </g>
  );
}

export { LIGHT_COLS };
