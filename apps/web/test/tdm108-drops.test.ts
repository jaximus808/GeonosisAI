// TDM-108: the /api/tdm108 allow-list and the /j short link (lib/tdm108/proxy.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { tdm108Route, tdm108ShortTarget } = (await import(new URL('../src/lib/tdm108/proxy.ts', import.meta.url).href)) as typeof import(
  '../src/lib/tdm108/proxy'
);

const q = (s = '') => new URLSearchParams(s);

describe('tdm108Route', () => {
  it('operator routes need a login, share and claim-src do not', () => {
    assert.deepEqual(tdm108Route('GET', ['drops'], q()), { target: 'drops', operator: true });
    assert.deepEqual(tdm108Route('GET', ['drops', 'task-12', 'card', 'discord.png'], q()), {
      target: 'drops/task-12/card/discord.png',
      operator: true,
    });
    assert.deepEqual(tdm108Route('POST', ['drops', 'task-12', 'post'], q()), { target: 'drops/task-12/post', operator: true });
    assert.deepEqual(tdm108Route('POST', ['drops', 'preview'], q()), { target: 'drops/preview', operator: true });
    assert.deepEqual(tdm108Route('POST', ['claim-src'], q()), { target: 'drops/claim-src', operator: false });
    assert.deepEqual(tdm108Route('GET', ['share', '12.png'], q('size=wide&download=1')), {
      target: 'drops/share/12.png?size=wide',
      operator: false,
      download: 'geonosisai-task-12-wide.png',
    });
    assert.equal(tdm108Route('GET', ['share', '12.png'], q('size=huge'))?.target, 'drops/share/12.png?size=square');
  });
  it('refuses everything else', () => {
    for (const parts of [['drops', '..', 'card', 'x.png'], ['drops', 'task-1', 'card', 'mastodon.png'], ['share', 'abc.png'],
      ['share', '12'], ['push', 'subscribe'], ['drops', 'task-1'], ['tickets']]) {
      assert.equal(tdm108Route('GET', parts, q()), null, parts.join('/'));
    }
    assert.equal(tdm108Route('POST', ['drops'], q()), null);
    assert.equal(tdm108Route('POST', ['drops', 'Task_1', 'post'], q()), null);
  });
});

describe('tdm108ShortTarget', () => {
  it('maps /j/N/src to the marketplace deep link', () => {
    assert.equal(tdm108ShortTarget('12', 'discord'), '/marketplace?task=12&src=discord');
    assert.equal(tdm108ShortTarget('0', 'x'), null);
    assert.equal(tdm108ShortTarget('12', 'https://evil'), null);
    assert.equal(tdm108ShortTarget('1e3', 'x'), null);
  });
});
