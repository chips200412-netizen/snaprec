import { cleanupShareIntents } from './share-intent';
import { handleShareRequest, isShareTargetRequest } from './share-worker-handler';

// Handler v1 and IndexedDB record/schema v1 ship together. Future incompatible
// formats must reject old records, not migrate/replay unknown shared content.
export { SHARE_HANDLER_VERSION } from './share-worker-handler';

// This deliberately small surface avoids mixing Window and Worker ambient libs.
export interface ShareWorkerScope {
  location: { origin: string };
  addEventListener(type: 'activate', listener: (event: { waitUntil(promise: Promise<unknown>): void }) => void): void;
  addEventListener(type: 'fetch', listener: (event: { request: Request; respondWith(response: Promise<Response>): void }) => void): void;
}

export function installShareWorker(scope: ShareWorkerScope): void {
  scope.addEventListener('activate', (event) => {
    event.waitUntil(cleanupShareIntents().catch(() => {
      throw new Error('SHARE_ACTIVATION_UNAVAILABLE');
    }));
  });
  scope.addEventListener('fetch', (event) => {
    if (isShareTargetRequest(event.request, scope.location.origin)) {
      event.respondWith(handleShareRequest(event.request, scope.location.origin));
    }
  });
  // No skipWaiting, clients.claim, caching, prefetching, background replay, or logs.
}

installShareWorker(globalThis as unknown as ShareWorkerScope);
