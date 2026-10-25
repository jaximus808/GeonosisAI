// TDM-114: the twin draws the scene the robot actually has loaded (lib/robotview/tdm114LoadedScene.ts).
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { tdm114LoadedScene, tdm114SceneMismatch } = (await import(
  new URL('../src/lib/robotview/tdm114LoadedScene.ts', import.meta.url).href
)) as typeof import('../src/lib/robotview/tdm114LoadedScene');

const cubeBin = { version: 1, objects: [{ id: 'cube' }] };
const boxes = { version: 1, objects: [{ id: 'box_a' }, { id: 'box_b' }] };
const dep = (id: number, robot: number, ticket: number, deploy: string, spec: unknown) => ({
  id, type: 'scene.deployed', payload: { robot_id: robot, ticket_id: ticket, deploy_id: deploy, revision: 1, spec },
});
const loaded = (id: number, robot: number, ticket: number, deploy: string, ok = true) => ({
  id, type: 'scene.loaded', payload: { robot_id: robot, ticket_id: ticket, deploy_id: deploy, revision: 1, ok },
});

describe('tdm114LoadedScene', () => {
  it('keeps the loaded scene while a newer deploy for another ticket is pending (the TKT-0003/TKT-0004 case)', () => {
    const evs = [dep(10, 3, 3, 'd3', cubeBin), loaded(11, 3, 3, 'd3'), dep(20, 3, 4, 'd4', boxes)];
    const s = tdm114LoadedScene(evs, 3);
    assert.deepEqual(s.spec, cubeBin);
    assert.equal(s.ticketId, 3);
    assert.equal(s.deployId, 'd3');
    assert.deepEqual(s.pending, { deployId: 'd4', ticketId: 4 });
    assert.equal(tdm114SceneMismatch(s, 4), "Robot is set up for TKT-0003's scene; this ticket's scene loads when its eval starts.");
    assert.equal(tdm114SceneMismatch(s, 3), null);
  });

  it('switches once the robot confirms the new scene; a failed load does not count', () => {
    const base = [dep(10, 3, 3, 'd3', cubeBin), loaded(11, 3, 3, 'd3'), dep(20, 3, 4, 'd4', boxes)];
    assert.equal(tdm114LoadedScene([...base, loaded(21, 3, 4, 'd4', false)], 3).ticketId, 3);
    const s = tdm114LoadedScene([...base, loaded(22, 3, 4, 'd4')], 3);
    assert.deepEqual(s.spec, boxes);
    assert.equal(s.ticketId, 4);
    assert.equal(s.pending, null);
  });

  it('other robots, and nothing loaded yet', () => {
    const evs = [dep(10, 5, 3, 'd3', cubeBin), loaded(11, 5, 3, 'd3')];
    const s = tdm114LoadedScene(evs, 3);
    assert.equal(s.spec, null);
    assert.equal(s.ticketId, 0);
    assert.equal(tdm114SceneMismatch(s, 4), null);
    const p = tdm114LoadedScene([dep(10, 3, 4, 'd4', boxes)], 3);
    assert.equal(p.spec, null);
    assert.deepEqual(p.pending, { deployId: 'd4', ticketId: 4 });
  });
});
