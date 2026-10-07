import { claimShareIntent, cleanupShareIntents, type ShareFields } from "./share-intent";

export interface ShareSnapshot {
  status: "loading" | "ready" | "unavailable";
  fields: ShareFields | null;
}

export interface ShareSession {
  getSnapshot(): ShareSnapshot;
  subscribe(listener: () => void): () => void;
  end(): void;
}

const sessions = new WeakMap<Document, ShareSession>();
const bootstrapCleanup = new WeakMap<Document, Promise<boolean>>();

function cleanDocumentIntents(): Promise<boolean> {
  const existing = bootstrapCleanup.get(document);
  if (existing) return existing;
  // Ordinary application startup also expires old intents. Resolve failure to a
  // fixed local result so a page without a share consumer has no rejected promise.
  const cleanup = Promise.resolve().then(() => cleanupShareIntents()).then(() => true, () => false);
  bootstrapCleanup.set(document, cleanup);
  return cleanup;
}

/** Consume navigation credentials before React or the collection router commits. */
export function bootstrapShareSession(): ShareSession | null {
  const cleanup = cleanDocumentIntents();
  if (window.location.pathname !== "/capture/share") return null;
  const existing = sessions.get(document);
  if (existing) {
    // A second navigation cannot replace the current Document's owned payload.
    if (window.location.hash || window.location.search) window.history.replaceState(null, "", "/capture/share");
    return existing;
  }
  const fragment = window.location.hash;
  // Raw share content and credentials never become history state or query data.
  window.history.replaceState(null, "", "/capture/share");
  const token = /^#intent=([a-f0-9]{32})$/.exec(fragment)?.[1] ?? null;
  let snapshot: ShareSnapshot = { status: token ? "loading" : "unavailable", fields: null };
  let generation = 0;
  const listeners = new Set<() => void>();
  const publish = (next: ShareSnapshot) => {
    snapshot = next;
    for (const listener of listeners) listener();
  };
  const session: ShareSession = {
    getSnapshot: () => snapshot,
    subscribe(listener) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    end() {
      generation += 1;
      publish({ status: "unavailable", fields: null });
    },
  };
  sessions.set(document, session);
  // pagehide includes BFCache departure. Hiding a still-active Document does not.
  window.addEventListener("pagehide", session.end, { once: true });
  const claimGeneration = generation;
  // One promise belongs to this Document, independently of React effect replay.
  void Promise.resolve().then(async () => {
    if (!await cleanup) return null;
    // Even a departed page may finish deleting its own unclaimed credential.
    // Generation checks below prevent the resulting payload from becoming owned.
    if (!token) return null;
    return claimShareIntent(token);
  }).then((fields) => {
    if (generation !== claimGeneration) return;
    publish(fields ? { status: "ready", fields } : { status: "unavailable", fields: null });
  }).catch(() => {
    if (generation === claimGeneration) publish({ status: "unavailable", fields: null });
  });
  return session;
}

export function getShareSession(): ShareSession {
  const existing = sessions.get(document);
  if (existing) return existing;
  const initial = bootstrapShareSession();
  if (initial) return initial;
  // History cannot manufacture a fresh claim after navigation credentials are gone.
  const unavailable: ShareSnapshot = { status: "unavailable", fields: null };
  const session: ShareSession = {
    getSnapshot: () => unavailable,
    subscribe: () => () => undefined,
    end: () => undefined,
  };
  sessions.set(document, session);
  return session;
}
