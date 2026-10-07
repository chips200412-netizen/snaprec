/** Registration is best effort: unsupported/failed installation never blocks manual capture.
 * Updates use the browser's normal waiting lifecycle; active drafts are never reloaded. */
const attempted = new WeakSet<Document>();

export async function registerShareWorker(): Promise<void> {
  if (attempted.has(document)) return;
  attempted.add(document);
  if (!window.isSecureContext || !('serviceWorker' in navigator)) return;
  try {
    const container = navigator.serviceWorker;
    const workerUrl = new URL('/share-worker.js', window.location.origin).href;
    const existing = await container.getRegistration('/');
    // Do not replace an unrelated application's root registration.
    if (existing && [existing.active, existing.waiting, existing.installing]
      .some((worker) => worker && worker.scriptURL !== workerUrl)) return;
    await container.register(workerUrl, { scope: '/', type: 'module', updateViaCache: 'none' });
  } catch {
    // No error objects or request details enter logs; the fixed action fallback remains usable.
  }
}
