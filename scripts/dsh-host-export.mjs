/** DSH Host plugin: publish this process's loopback address to local Tower config. */
import { mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute } from 'node:path';
import { randomUUID } from 'node:crypto';

export const name = 'agent-tower-host-export';
export const inject = ['connection', 'webServer'];

export function apply(ctx, config) {
  const output = config?.outputFile;
  if (typeof output !== 'string' || !isAbsolute(output)) {
    throw new Error('Agent Tower Host config needs an absolute outputFile');
  }
  let stopped = false;
  let publishing = false;
  let lastUrl = '';
  const publish = async () => {
    if (stopped || publishing) return;
    publishing = true;
    let temporary;
    try {
      const port = ctx.webServer.port;
      if (ctx.webServer.protocol !== 'http:' || !Number.isInteger(port) || port < 1) return;
      const url = ctx.connection.authenticatedUrl(`http://127.0.0.1:${port}/`);
      const parsed = new URL(url);
      if (parsed.hostname !== '127.0.0.1' || !parsed.searchParams.get('token')) return;
      if (url === lastUrl) {
        try { if (JSON.parse(await readFile(output, 'utf8')).url === url) return; } catch {}
      }
      await mkdir(dirname(output), { recursive: true, mode: 0o700 });
      temporary = `${output}.${randomUUID()}.tmp`;
      await writeFile(temporary, JSON.stringify({ url, source: name, pid: process.pid }),
                      { mode: 0o600, flag: 'wx' });
      if (stopped) return;
      await rename(temporary, output);
      lastUrl = url;
    } catch {
      // A failed local publish must not expose the address or disrupt DSH.
    } finally {
      if (temporary) await unlink(temporary).catch(() => {});
      publishing = false;
    }
  };
  ctx.effect(() => {
    const timer = setInterval(() => { void publish(); }, 5000);
    timer.unref?.();
    void publish();
    return () => { stopped = true; clearInterval(timer); };
  });
}
