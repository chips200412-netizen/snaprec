// @vitest-environment node
import { beforeEach, afterEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ cleanup: vi.fn(), matches: vi.fn(), handle: vi.fn() }));
vi.mock('./share-intent', () => ({ cleanupShareIntents: mocks.cleanup }));
vi.mock('./share-worker-handler', () => ({ isShareTargetRequest: mocks.matches, handleShareRequest: mocks.handle }));
const callbacks = new Map<string, (event: any) => void>();
beforeEach(async () => {
  vi.resetModules(); vi.clearAllMocks(); callbacks.clear();
  vi.stubGlobal('location', { origin: 'https://local.example' });
  vi.stubGlobal('addEventListener', (name: string, fn: (event: any) => void) => callbacks.set(name, fn));
  await import('./share-worker');
});
afterEach(() => vi.unstubAllGlobals());
it('activation waits for bounded own-store cleanup', async () => {
  mocks.cleanup.mockResolvedValue(undefined);
  const waitUntil = vi.fn();
  callbacks.get('activate')!({ waitUntil });
  await expect(waitUntil.mock.calls[0][0]).resolves.toBeUndefined();
  expect(mocks.cleanup).toHaveBeenCalledTimes(1);
  expect([...callbacks.keys()]).toEqual(['activate', 'fetch']);
});
it('activation rejects incompatible storage with only a fixed error', async () => {
  mocks.cleanup.mockRejectedValue(new Error('private payload'));
  const waitUntil = vi.fn();
  callbacks.get('activate')!({ waitUntil });
  await expect(waitUntil.mock.calls[0][0]).rejects.toThrow('SHARE_ACTIVATION_UNAVAILABLE');
});
it('leaves unrelated requests untouched', () => {
  mocks.matches.mockReturnValue(false);
  const respondWith = vi.fn();
  callbacks.get('fetch')!({ request: new Request('https://local.example/api'), respondWith });
  expect(respondWith).not.toHaveBeenCalled();
  expect(mocks.handle).not.toHaveBeenCalled();
});
it('responds to matched navigation through the public handler', async () => {
  mocks.matches.mockReturnValue(true);
  const response = new Response(null, { status: 303 });
  mocks.handle.mockResolvedValue(response);
  const request = new Request('https://local.example/share-target');
  const respondWith = vi.fn();
  callbacks.get('fetch')!({ request, respondWith });
  expect(mocks.handle).toHaveBeenCalledWith(request, 'https://local.example');
  await expect(respondWith.mock.calls[0][0]).resolves.toBe(response);
});
