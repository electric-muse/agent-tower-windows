import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apply } from './dsh-host-export.mjs';

const folder = await mkdtemp(join(tmpdir(), 'tower-host-export-'));
let dispose;
try {
  const outputFile = join(folder, 'dsh-host.json');
  const ctx = {
    connection: { authenticatedUrl: base => `${base}?token=offline-test` },
    webServer: { protocol: 'http:', port: 12345 },
    effect: callback => { dispose = callback(); },
  };
  apply(ctx, { outputFile });
  let saved;
  for (let attempt = 0; attempt < 30; attempt++) {
    try { saved = JSON.parse(await readFile(outputFile, 'utf8')); break; } catch {}
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  assert.equal(saved.url, 'http://127.0.0.1:12345/?token=offline-test');
  assert.equal(saved.source, 'agent-tower-host-export');
  assert.equal(saved.pid, process.pid);
  dispose();
  dispose = null;
  assert.throws(() => apply(ctx, { outputFile: 'relative.json' }), /absolute outputFile/);
  console.log('DSH Host export offline checks passed');
} finally {
  dispose?.();
  await rm(folder, { recursive: true, force: true });
}
