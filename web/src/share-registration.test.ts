import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.resetModules(); });

async function setup(secure = true, existing?: unknown) {
  const register = vi.fn().mockResolvedValue({});
  const getRegistration = vi.fn().mockResolvedValue(existing);
  vi.stubGlobal('isSecureContext', secure);
  vi.stubGlobal('navigator', { serviceWorker: { register, getRegistration } });
  const { registerShareWorker } = await import('./share-registration');
  return { register, getRegistration, run: registerShareWorker };
}

describe('share worker registration', () => {
  it('registers the same-origin root module once, without cached updates', async () => {
    const { run, register } = await setup();
    await Promise.all([run(), run()]);
    expect(register).toHaveBeenCalledExactlyOnceWith(new URL('/share-worker.js', location.origin).href,
      { scope: '/', type: 'module', updateViaCache: 'none' });
  });
  it('does not register on an insecure origin', async () => {
    const { run, register, getRegistration } = await setup(false);
    await run();
    expect(register).not.toHaveBeenCalled();
    expect(getRegistration).not.toHaveBeenCalled();
  });
  it('leaves another root worker untouched', async () => {
    const { run, register } = await setup(true, { active: { scriptURL: `${location.origin}/other.js` } });
    await run();
    expect(register).not.toHaveBeenCalled();
  });
  it('allows a compatible worker update without forcing activation or reload', async () => {
    const { run, register } = await setup(true, { active: { scriptURL: `${location.origin}/share-worker.js` } });
    await run();
    expect(register).toHaveBeenCalledTimes(1);
  });
  it('contains registration failures without logging details or retrying', async () => {
    const { run, register } = await setup();
    register.mockRejectedValue(new Error('sensitive details'));
    const error = vi.spyOn(console, 'error');
    await expect(run()).resolves.toBeUndefined();
    await run();
    expect(register).toHaveBeenCalledTimes(1);
    expect(error).not.toHaveBeenCalled();
  });
  it('supports browsers without service workers', async () => {
    const { run } = await setup();
    vi.stubGlobal('navigator', {});
    await expect(run()).resolves.toBeUndefined();
  });
});
