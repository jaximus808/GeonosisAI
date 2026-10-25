'use client';

// AgentWorkbench: the station's agent as a physical droid at a console. The same Anatomy-kit figure as the
// FactoryIso cell (factory-iso/geometry.ts droidArt), drawn bigger, with parts split out so CSS can move them:
//   working   the right arm works the console, sparks at the hand, the console screen scrolls
//   thinking  head tilts, visor breathes, a thought bubble of three dots rises
//   waiting   still, a slow clock over the head (the label beside it says on whom)
//   stalled   slumped, a "?" over the head, red dashed ring: nothing has happened for a while
//   done      upright, a check over the head          failed   head down, a "!" in slag
// CSS motion only; the parent sets data-paused (offscreen, hidden tab) and reduced motion turns it off.

import { memo } from 'react';
import { Dots, Line, Solid } from '@/components/factory-iso/kit/react/draw';
import { cylinder, dotGrid, pathOf, slabOf, type Flat, type Point3 } from '@/components/factory-iso/kit/iso-kit';
import { P, at, droidArt } from '@/components/factory-iso/geometry';
import type { AwMode } from '@/lib/agentwork/workbench';

const DS = 1.5;
// The console in front of the droid (+y is toward the viewer's left, where the visor faces).
const CONSOLE = slabOf({ x: -8, y: 9, w: 16, d: 9, r: 1 }, 0, 8, P, 4);
const SCREEN = pathOf([at([-6, 10, 8.05]), at([6, 10, 8.05]), at([6, 16.5, 8.05]), at([-6, 16.5, 8.05])], true);
const SCREEN_LINES = [11.3, 12.6, 13.9, 15.2].map((y, i) => pathOf([at([-5, y, 8.1]), at([i % 2 ? 1 : 4, y, 8.1])]));
const KEYS = dotGrid(-6, 17.2, 7, 1, 2, 8.1, P);
const LAMP = cylinder(6.2, 13, 0.9, 8, 1.4, P, 10);
const HAND = at([3.7 * DS, 1.5, 4 * DS]);
const SPARK: Flat = at([2, 11, 8.4]);
const HEAD = droidArt.head0;
const MARK: Flat = [HEAD[0] + 13, HEAD[1] - 6];

// Fit the box around the ring, the console and the head mark, with a little air.
const CORNERS: Point3[] = [
  [-10.5, -10.5, 0], [10.5, -10.5, 0], [-10.5, 18, 0], [10.5, 18, 0], [-5, -5, 30], [5, 5, 30],
];
const FLAT = CORNERS.map(at);
const minX = Math.min(...FLAT.map((p) => p[0])) - 2;
const maxX = Math.max(...FLAT.map((p) => p[0]), MARK[0] + 8) + 2;
const minY = Math.min(...FLAT.map((p) => p[1]), MARK[1] - 12) - 1;
const maxY = Math.max(...FLAT.map((p) => p[1])) + 2;
const VIEWBOX = `${minX.toFixed(1)} ${minY.toFixed(1)} ${(maxX - minX).toFixed(1)} ${(maxY - minY).toFixed(1)}`;
const r1 = (v: number) => Math.round(v * 10) / 10;

export const WorkbenchDroid = memo(function WorkbenchDroid({ mode, claude }: { mode: AwMode; claude: boolean }) {
  return (
    <svg className="aw-droid iso-svg" viewBox={VIEWBOX} data-mode={mode} role="img" aria-label={`Agent droid: ${mode}`}>
      <path className="iso-halo" d={droidArt.ring} />
      <path className="aw-ring" d={droidArt.ring} />
      <g className="aw-body">
        <Solid paths={droidArt.wheel} tone="lo" />
        <Solid paths={droidArt.armL} tone="mid" />
        <Solid paths={droidArt.torso} tone="hi" />
        {claude && <path className="aw-chip" d={droidArt.chip} />}
        <g className="aw-head">
          <Solid paths={droidArt.neck} tone="lo" />
          <Solid paths={droidArt.head} tone="hi" />
          <path className="aw-visor" d={droidArt.visor} />
          <Line d={droidArt.antenna} tone="mid" />
          <circle className="aw-tip" cx={r1(droidArt.tip[0])} cy={r1(droidArt.tip[1])} r={1.7} />
        </g>
      </g>
      <g className="aw-console">
        <Solid paths={CONSOLE} tone="mid" />
        <path className="aw-screen" d={SCREEN} />
        <g className="aw-screen-lines">
          {SCREEN_LINES.map((d, i) => (
            <path key={i} d={d} style={{ animationDelay: `${i * 0.35}s` }} />
          ))}
        </g>
        <Dots points={KEYS} size={0.6} tone="mid" />
        <Solid paths={LAMP} tone="lo" className="aw-lamp" />
      </g>
      {/* The arm is drawn over the console: it reaches onto the desk. */}
      <g className="aw-arm">
        <Solid paths={droidArt.armR} tone="mid" />
        <circle className="aw-hand" cx={r1(HAND[0])} cy={r1(HAND[1])} r={1.3} />
      </g>
      <g className="aw-sparks" transform={`translate(${r1(SPARK[0])} ${r1(SPARK[1])})`}>
        {[
          [0, -5],
          [4.5, -2.5],
          [-4, -3],
          [2, 2.5],
        ].map(([x, y], i) => (
          <path key={i} d={`M0 0L${x} ${y}`} style={{ animationDelay: `${i * 0.12}s` }} />
        ))}
      </g>
      <g className="aw-mark" transform={`translate(${r1(MARK[0])} ${r1(MARK[1])})`}>
        <g className="aw-think">
          <circle cx={-9} cy={6} r={1.4} />
          <circle cx={-5} cy={1} r={2} />
          <circle cx={1} cy={-5} r={2.8} />
        </g>
        <g className="aw-clock">
          <circle r={5.5} />
          <path className="aw-clock-hand" d="M0 0V-4" />
          <path d="M0 0H2.6" />
        </g>
        <path className="aw-check" d="M-4 0L-1 3L4.5 -3.5" />
        <text className="aw-glyph aw-bang" textAnchor="middle" dy="4">!</text>
        <text className="aw-glyph aw-what" textAnchor="middle" dy="4">?</text>
      </g>
    </svg>
  );
});
