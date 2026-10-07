// @vitest-environment node
import { createServer, request as httpRequest } from 'node:http';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { bundleShareWorker, SHARE_FALLBACK_HTML, shareTransportMiddleware } from '../share-transport-plugin';

const servers = [];
afterEach(async () => { await Promise.all(servers.splice(0).map(server => new Promise(resolve => server.close(() => resolve())))); });

async function fixture(worker) {
  const downstream = vi.fn();
  let bodyListeners = 0;
  const middleware = shareTransportMiddleware(worker);
  const server = createServer((req, res) => {
    const originalOn = req.on.bind(req);
    req.on = ((event, listener) => {
      if (event === 'data' || event === 'readable') bodyListeners++;
      return originalOn(event, listener);
    });
    middleware(req, res, () => {
      downstream(req.url);
      res.setHeader('Cache-Control', 'max-age=600');
      res.setHeader('Referrer-Policy', 'origin');
      res.end('ordinary');
    });
  });
  servers.push(server);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('fixture_address');
  return { downstream, bodyListeners: () => bodyListeners, send: (path, method = 'GET', body) => new Promise((resolve, reject) => {
    const req = httpRequest({ host: '127.0.0.1', port: address.port, path, method }, res => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString('utf8') }));
    });
    req.on('error', reject);
    req.end(body);
  }) };
}

describe('share transport server boundary', () => {
  it('builds the real worker as a self-contained browser artifact without HMR', async () => {
    const source = await bundleShareWorker(fileURLToPath(new URL('..', import.meta.url)), 'test');
    const events = [];
    vi.stubGlobal('addEventListener', name => events.push(name));
    try {
      await import(/* @vite-ignore */ `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
    } finally {
      vi.unstubAllGlobals();
    }
    expect(events).toContain('fetch');
    expect(events).toContain('activate');
    expect(source).not.toMatch(/@vite\/client|import\.meta\.hot|sourceMappingURL/);
  });
  it.each(['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])('fixed %s fallback never reads, echoes or forwards payload', async method => {
    const app = await fixture();
    const response = await app.send('/share-target?private_url=https://secret.invalid', method, 'private_title_token_payload');
    expect(response.status).toBe(503);
    expect(response.body).toBe(SHARE_FALLBACK_HTML);
    expect(response.headers['cache-control']).toBe('no-store');
    expect(response.headers['referrer-policy']).toBe('no-referrer');
    expect(response.headers.connection).toBe('close');
    expect(app.bodyListeners()).toBe(0);
    expect(app.downstream).not.toHaveBeenCalled();
  });
  it('HEAD fallback returns headers with no body', async () => {
    const app = await fixture();
    expect((await app.send('/share-target', 'HEAD')).body).toBe('');
  });
  it('capture privacy headers survive downstream writes including query URLs', async () => {
    const app = await fixture();
    const response = await app.send('/capture/share?private_data');
    expect(response.body).toBe('ordinary');
    expect(response.headers['cache-control']).toBe('no-store');
    expect(response.headers['referrer-policy']).toBe('no-referrer');
  });
  it('other routes retain normal middleware behavior', async () => {
    const app = await fixture();
    for (const path of ['/api/v1/collection-previews', '/share-target/extra', '/capture/share-other', '/']) {
      const response = await app.send(path);
      expect(response.body).toBe('ordinary');
      expect(response.headers['cache-control']).toBe('max-age=600');
    }
    expect(app.downstream).toHaveBeenCalledTimes(4);
  });
  it('serves compiled worker with root scope and privacy policies', async () => {
    const source = vi.fn(async () => 'self.addEventListener("fetch", () => {});');
    const app = await fixture(source);
    const response = await app.send('/share-worker.js');
    expect(response.status).toBe(200);
    expect(response.body).toContain('self.addEventListener');
    expect(response.headers['service-worker-allowed']).toBe('/');
    expect(response.headers['cache-control']).toBe('no-store');
    expect(response.headers['content-type']).toBe('text/javascript; charset=utf-8');
    expect(app.downstream).not.toHaveBeenCalled();
  });
  it('worker compiler failure is fixed and quiet', async () => {
    const app = await fixture(async () => { throw new Error('private compiler details'); });
    const response = await app.send('/share-worker.js');
    expect(response.status).toBe(503);
    expect(response.body).toBe('share_worker_unavailable');
  });
  it('preview worker file falls through with protected static headers', async () => {
    const app = await fixture();
    const response = await app.send('/share-worker.js');
    expect(response.headers['service-worker-allowed']).toBe('/');
    expect(response.headers['cache-control']).toBe('no-store');
  });
});
