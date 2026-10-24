'use client';

// TDM-59: canvas views of the practice-mode simulated arm: a top camera and a side camera,
// standing in for the robot's real top + wrist feeds. Drawn every animation frame from the sim.

import { useEffect, useRef, type MutableRefObject } from 'react';
import type { V3 } from '@/lib/teleop/tdm56Input';
import { SIM_BIN, SIM_BLOCK, SIM_SHOULDER, SIM_WS, elbowFor, type Tdm59SimArm } from '@/lib/teleop/tdm59Sim';

export type SimViewKind = 'top' | 'side';

// world window of each view (metres); both 9:8
const TOP = { yL: 0.36, yR: -0.36, xT: 0.6, xB: -0.04 };
const SIDE = { xL: -0.06, xR: 0.615, zT: 0.58, zB: -0.02 };
export const SIM_VIEW_ASPECT = 9 / 8;

const C = {
  bg: '#1d2630',
  grid: 'rgba(255,255,255,0.045)',
  grid10: 'rgba(255,255,255,0.085)',
  ws: 'rgba(255,255,255,0.22)',
  table: '#2a3541',
  tableEdge: '#5b6a78',
  bin: '#d6c4a0',
  binFill: 'rgba(214,196,160,0.13)',
  block: '#2f6fde',
  blockEdge: '#8fb4f5',
  blockDone: '#1f9d6b',
  base: '#3a4654',
  link: '#c9d2db',
  palm: '#8796a5',
  finger: '#eef2f5',
  text: 'rgba(230,236,242,0.75)',
  drive: '#f0b400',
};

export function Tdm59SimView({ sim, kind, engagedRef }: { sim: MutableRefObject<Tdm59SimArm>; kind: SimViewKind; engagedRef: MutableRefObject<boolean> }) {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const cv = canvas.current!;
    const fit = () => {
      const r = cv.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = Math.max(1, Math.round(r.width * dpr));
      const h = Math.max(1, Math.round(r.height * dpr));
      if (cv.width !== w || cv.height !== h) {
        cv.width = w;
        cv.height = h;
      }
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(cv);
    let raf = 0;
    const frame = () => {
      const ctx = cv.getContext('2d');
      if (ctx) (kind === 'top' ? drawTop : drawSide)(ctx, cv.width, cv.height, sim.current, engagedRef.current);
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [kind, sim, engagedRef]);

  return <canvas ref={canvas} className="block h-full w-full" aria-label={kind === 'top' ? 'Simulated top camera' : 'Simulated side camera'} />;
}

// ------------------------------------------------------------------ top camera (looking down)

function drawTop(ctx: CanvasRenderingContext2D, W: number, H: number, sim: Tdm59SimArm, engaged: boolean) {
  const s = sim.s;
  const k = W / (TOP.yL - TOP.yR);
  const P = (x: number, y: number): [number, number] => [(TOP.yL - y) * k, (TOP.xT - x) * k];
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, W, H);

  // 5 cm grid on the table
  for (let g = -0.35; g <= 0.6; g += 0.05) {
    const ten = Math.abs(Math.round(g * 100) % 10) === 0;
    ctx.strokeStyle = ten ? C.grid10 : C.grid;
    ctx.lineWidth = 1;
    const [ax, ay] = P(g, TOP.yL);
    const [bx, by] = P(g, TOP.yR);
    line(ctx, ax, ay, bx, by);
    const [cx, cy] = P(TOP.xT, g);
    const [dx, dy] = P(TOP.xB, g);
    line(ctx, cx, cy, dx, dy);
  }

  // reachable box
  ctx.setLineDash([6 * (W / 400), 5 * (W / 400)]);
  ctx.strokeStyle = C.ws;
  ctx.lineWidth = Math.max(1, W / 400);
  const [wx0, wy0] = P(SIM_WS.x[1], SIM_WS.y[1]);
  const [wx1, wy1] = P(SIM_WS.x[0], SIM_WS.y[0]);
  ctx.strokeRect(wx0, wy0, wx1 - wx0, wy1 - wy0);
  ctx.setLineDash([]);

  // bin
  const [bx0, by0] = P(SIM_BIN.x[1], SIM_BIN.y[1]);
  const [bx1, by1] = P(SIM_BIN.x[0], SIM_BIN.y[0]);
  ctx.fillStyle = C.binFill;
  ctx.fillRect(bx0, by0, bx1 - bx0, by1 - by0);
  ctx.strokeStyle = C.bin;
  ctx.lineWidth = Math.max(2, W / 160);
  ctx.strokeRect(bx0, by0, bx1 - bx0, by1 - by0);
  label(ctx, 'Bin', bx0 + 6 * (W / 400), by0 + 15 * (W / 400), W, C.bin);

  // block (drawn before the hand so the fingers sit on top)
  const b = s.block;
  const [px, py] = P(b.pos[0], b.pos[1]);
  ctx.save();
  ctx.translate(px, py);
  ctx.rotate(-b.yaw); // screen y is flipped relative to world x
  const e = SIM_BLOCK * k * (1 + (b.held ? (s.ee[2] - SIM_BLOCK / 2) * 1.2 : 0));
  ctx.fillStyle = b.inBin && !b.held ? C.blockDone : C.block;
  ctx.strokeStyle = C.blockEdge;
  ctx.lineWidth = Math.max(1.5, W / 260);
  ctx.fillRect(-e / 2, -e / 2, e, e);
  ctx.strokeRect(-e / 2, -e / 2, e, e);
  ctx.restore();

  // arm: base -> elbow -> wrist, seen from above
  const wrist: V3 = [s.ee[0], s.ee[1], s.ee[2] + 0.07];
  const elbow = elbowFor(SIM_SHOULDER, wrist);
  const [ox, oy] = P(0, 0);
  const [ex, ey] = P(elbow[0], elbow[1]);
  const [tx, ty] = P(s.ee[0], s.ee[1]);
  ctx.fillStyle = C.base;
  circle(ctx, ox, oy, 0.05 * k);
  ctx.strokeStyle = C.link;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.lineWidth = 0.032 * k;
  ctx.globalAlpha = 0.9;
  ctx.beginPath();
  ctx.moveTo(ox, oy);
  ctx.lineTo(ex, ey);
  ctx.lineTo(tx, ty);
  ctx.stroke();
  ctx.globalAlpha = 1;
  ctx.fillStyle = C.palm;
  circle(ctx, ex, ey, 0.02 * k);

  // height ring: grows as the hand rises, so height reads from above too
  ctx.strokeStyle = 'rgba(255,255,255,0.28)';
  ctx.lineWidth = Math.max(1, W / 400);
  ctx.beginPath();
  ctx.arc(tx, ty, (0.012 + s.ee[2] * 0.28) * k, 0, Math.PI * 2);
  ctx.stroke();

  // gripper: fingers close along u = (-sin yaw, cos yaw) in world xy
  drawFingersTop(ctx, tx, ty, s.yaw, s.jaw, k, engaged);

  label(ctx, 'base', ox, oy + 0.075 * k, W, C.text, 'center');
}

function drawFingersTop(ctx: CanvasRenderingContext2D, x: number, y: number, yaw: number, jaw: number, k: number, engaged: boolean) {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(-yaw);
  // in this local frame: screen -y is world +x (forward), screen -x is world +y (left)
  const half = (jaw / 2) * k;
  const fw = 0.012 * k;
  const fl = 0.03 * k;
  ctx.fillStyle = C.palm;
  ctx.fillRect(-half - fw, -0.006 * k, 2 * (half + fw), 0.012 * k);
  ctx.fillStyle = engaged ? C.drive : C.finger;
  ctx.fillRect(-half - fw, -fl / 2, fw, fl);
  ctx.fillRect(half, -fl / 2, fw, fl);
  ctx.restore();
}

// ------------------------------------------------------------------ side camera (from the robot's right)

function drawSide(ctx: CanvasRenderingContext2D, W: number, H: number, sim: Tdm59SimArm, engaged: boolean) {
  const s = sim.s;
  const k = W / (SIDE.xR - SIDE.xL);
  const P = (x: number, z: number): [number, number] => [(x - SIDE.xL) * k, (SIDE.zT - z) * k];
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, W, H);
  for (let g = 0; g <= 0.6; g += 0.05) {
    const ten = Math.abs(Math.round(g * 100) % 10) === 0;
    ctx.strokeStyle = ten ? C.grid10 : C.grid;
    ctx.lineWidth = 1;
    const [ax, ay] = P(SIDE.xL, g);
    line(ctx, ax, ay, W, ay);
    const [cx] = P(g, 0);
    line(ctx, cx, 0, cx, H);
  }

  // table
  const [, t0] = P(0, 0);
  ctx.fillStyle = C.table;
  ctx.fillRect(0, t0, W, H - t0);
  ctx.strokeStyle = C.tableEdge;
  ctx.lineWidth = Math.max(2, W / 200);
  line(ctx, 0, t0, W, t0);

  // bin walls
  const [b0x, b0y] = P(SIM_BIN.x[0], SIM_BIN.h);
  const [b1x] = P(SIM_BIN.x[1], 0);
  ctx.fillStyle = C.binFill;
  ctx.fillRect(b0x, b0y, b1x - b0x, t0 - b0y);
  ctx.strokeStyle = C.bin;
  ctx.lineWidth = Math.max(2, W / 160);
  ctx.beginPath();
  ctx.moveTo(b0x, b0y);
  ctx.lineTo(b0x, t0);
  ctx.lineTo(b1x, t0);
  ctx.lineTo(b1x, b0y);
  ctx.stroke();
  label(ctx, 'Bin', (b0x + b1x) / 2, b0y - 6 * (W / 400), W, C.bin, 'center');

  // block
  const b = s.block;
  const [bx, by] = P(b.pos[0], b.pos[2]);
  const e = SIM_BLOCK * k;
  ctx.fillStyle = b.inBin && !b.held ? C.blockDone : C.block;
  ctx.strokeStyle = C.blockEdge;
  ctx.lineWidth = Math.max(1.5, W / 260);
  ctx.fillRect(bx - e / 2, by - e / 2, e, e);
  ctx.strokeRect(bx - e / 2, by - e / 2, e, e);

  // arm
  const wrist: V3 = [s.ee[0], s.ee[1], s.ee[2] + 0.07];
  const elbow = elbowFor(SIM_SHOULDER, wrist);
  const [sx, sy] = P(0, SIM_SHOULDER[2]);
  const [ex, ey] = P(elbow[0], elbow[2]);
  const [wx, wy] = P(wrist[0], wrist[2]);
  const [tx, ty] = P(s.ee[0], s.ee[2]);
  ctx.fillStyle = C.base;
  const [p0x] = P(-0.035, 0);
  const [p1x] = P(0.035, 0);
  ctx.fillRect(p0x, sy, p1x - p0x, t0 - sy);
  ctx.strokeStyle = C.link;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.lineWidth = 0.032 * k;
  ctx.beginPath();
  ctx.moveTo(sx, sy);
  ctx.lineTo(ex, ey);
  ctx.lineTo(wx, wy);
  ctx.stroke();
  ctx.fillStyle = C.palm;
  circle(ctx, sx, sy, 0.024 * k);
  circle(ctx, ex, ey, 0.02 * k);

  // gripper (schematic: the jaw gap is drawn face-on so open/closed always reads)
  const half = (s.jaw / 2) * k;
  const fw = 0.012 * k;
  ctx.fillStyle = C.palm;
  ctx.fillRect(wx - half - fw, wy - 0.008 * k, 2 * (half + fw), 0.016 * k);
  ctx.fillStyle = engaged ? C.drive : C.finger;
  ctx.fillRect(wx - half - fw, wy, fw, ty - wy);
  ctx.fillRect(wx + half, wy, fw, ty - wy);

  // height above the table
  ctx.setLineDash([4 * (W / 400), 4 * (W / 400)]);
  ctx.strokeStyle = 'rgba(255,255,255,0.3)';
  ctx.lineWidth = Math.max(1, W / 400);
  line(ctx, tx, ty, tx, t0);
  ctx.setLineDash([]);
  label(ctx, `${Math.round(s.ee[2] * 100)} cm`, tx + half + fw + 6 * (W / 400), (ty + t0) / 2 + 4, W, C.text);
}

// ------------------------------------------------------------------ helpers

function line(ctx: CanvasRenderingContext2D, ax: number, ay: number, bx: number, by: number) {
  ctx.beginPath();
  ctx.moveTo(ax, ay);
  ctx.lineTo(bx, by);
  ctx.stroke();
}

function circle(ctx: CanvasRenderingContext2D, x: number, y: number, r: number) {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fill();
}

function label(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, W: number, color: string, align: CanvasTextAlign = 'left') {
  ctx.font = `600 ${Math.max(10, Math.round(W / 34))}px ui-sans-serif, system-ui, sans-serif`;
  ctx.fillStyle = color;
  ctx.textAlign = align;
  ctx.fillText(text, x, y);
  ctx.textAlign = 'left';
}
