// @vitest-environment node
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { claimShareIntent, createShareIntent, SHARE_INTENT_DATABASE } from './share-intent';
import { handleShareRequest, isShareTargetRequest } from './share-worker-handler';

const origin = 'https://capture.example';
function navigation(fields = { shared_url: 'https://example.com/private', shared_title: 'private title', shared_text: 'private text' }, path = '/share-target') {
  const body = new FormData();
  for (const [name, value] of Object.entries(fields)) body.append(name, value);
  const request = new Request(`${origin}${path}`, { method: 'POST', body });
  // Request constructors forbid navigate; the browser supplies it for SW events.
  Object.defineProperty(request, 'mode', { value: 'navigate' });
  return request;
}
beforeEach(() => vi.stubGlobal('indexedDB', new IDBFactory()));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('share worker public request seam', () => {
  it('accepts only same-origin exact POST navigation without swallowing other routes', () => {
    expect(isShareTargetRequest(navigation(), origin)).toBe(true);
    for (const request of [new Request(`${origin}/share-target`), new Request(`${origin}/share-target`, { method: 'POST' }), navigation(undefined, '/share-target/'), navigation(undefined, '/api/v1/collection-previews'), navigation(undefined, '/capture/share')]) expect(isShareTargetRequest(request, origin)).toBe(false);
    expect(isShareTargetRequest(navigation(), 'https://other.example')).toBe(false);
  });
  it('writes then 303 redirects with only an opaque fragment and single atomic claim', async () => {
    const response = await handleShareRequest(navigation(), origin);
    expect(response.status).toBe(303);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(response.headers.get('referrer-policy')).toBe('no-referrer');
    const location = response.headers.get('location')!;
    expect(location).toMatch(/^\/capture\/share#intent=[a-f0-9]{32}$/);
    expect(await response.text()).toBe('');
    const token = location.split('=')[1];
    await expect(claimShareIntent(token)).resolves.toEqual({ shared_url: 'https://example.com/private', shared_title: 'private title', shared_text: 'private text' });
    await expect(claimShareIntent(token)).resolves.toBeNull();
  });
  it('fixed failure never reflects payload/errors, creates intents, logs, or calls APIs', async () => {
    const logging = [vi.spyOn(console, 'log'), vi.spyOn(console, 'warn'), vi.spyOn(console, 'error')];
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    const response = await handleShareRequest(navigation({ shared_url: 'private'.repeat(2000), shared_title: '', shared_text: '' }), origin);
    expect(response.status).toBe(400);
    expect(response.headers.get('location')).toBeNull();
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(response.headers.get('referrer-policy')).toBe('no-referrer');
    const text = await response.text();
    expect(text).toContain('回到素材库手动粘贴'); expect(text).not.toContain('private');
    expect(await indexedDB.databases()).toEqual([]);
    expect(fetch).not.toHaveBeenCalled(); logging.forEach((spy) => expect(spy).not.toHaveBeenCalled());
  });
  it('capacity rejection is fixed and preserves the existing three intents', async () => {
    const fields = { shared_url: 'https://x.test', shared_title: '', shared_text: '' };
    const tokens = await Promise.all([createShareIntent(fields), createShareIntent(fields), createShareIntent(fields)]);
    expect((await handleShareRequest(navigation(), origin)).status).toBe(400);
    for (const token of tokens) await expect(claimShareIntent(token)).resolves.toEqual(fields);
  });
  it('storage unavailable fails locally without redirect', async () => {
    vi.stubGlobal('indexedDB', undefined);
    const response = await handleShareRequest(navigation(), origin);
    expect(response.status).toBe(400); expect(response.headers.get('location')).toBeNull();
  });
  it('future database schema is not migrated or replayed', async () => {
    await new Promise<void>((resolve) => {
      const request = indexedDB.open(SHARE_INTENT_DATABASE, 2);
      request.onsuccess = () => { request.result.close(); resolve(); };
    });
    expect((await handleShareRequest(navigation(), origin)).status).toBe(400);
    expect((await indexedDB.databases())[0].version).toBe(2);
  });
  it('defensive handler misuse does not read a non-target body', async () => {
    const request = navigation(undefined, '/elsewhere');
    expect((await handleShareRequest(request, origin)).status).toBe(400);
    expect(request.bodyUsed).toBe(false);
    expect(await indexedDB.databases()).toEqual([]);
  });
});
