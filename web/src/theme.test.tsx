import { StrictMode } from "react";
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ThemeProvider,
  useThemePreferences,
  type ThemeCommitScheduler,
} from "./theme";

const appearanceKey = "video-knowledge-theme";
const accentKey = "video-knowledge-accent";
const combinationKey = "collection-appearance-v1";

function storeCombination(appearance = "dark", accent = "teal", version?: number) {
  const combined = JSON.stringify({ ...(version === undefined ? {} : { version }), appearance, accent });
  window.localStorage.setItem(appearanceKey, appearance);
  window.localStorage.setItem(accentKey, accent);
  window.localStorage.setItem(combinationKey, combined);
  return combined;
}

function controlledScheduler() {
  const commits: Array<() => void> = [];
  const schedule: ThemeCommitScheduler = (commit) => commits.push(commit);
  return {
    schedule,
    commits,
    run(index: number) { act(() => commits[index]()); },
  };
}

function renderPreferences(scheduleCommit?: ThemeCommitScheduler, strict = false) {
  let latest: ReturnType<typeof useThemePreferences>;
  function Probe({ route = "settings" }: { route?: string }) {
    latest = useThemePreferences();
    return (
      <section aria-label={route}>
        <output data-testid="visible-combination">{latest.appearance}/{latest.accent}</output>
        <output data-testid="restore-state">{latest.restoreState}</output>
        <output data-testid="mutation">{latest.mutation.token}/{latest.mutation.origin}/{latest.mutation.phase}</output>
        <button type="button" onClick={() => latest.setAppearance("dark", route)}>深色</button>
      </section>
    );
  }
  const tree = (route = "settings") => {
    const element = <ThemeProvider scheduleCommit={scheduleCommit}><Probe route={route} /></ThemeProvider>;
    return strict ? <StrictMode>{element}</StrictMode> : element;
  };
  const view = render(tree());
  return {
    get preferences() { return latest!; },
    changeRoute(route: string) { view.rerender(tree(route)); },
    unmount: view.unmount,
  };
}

function expectVisible(appearance: string, accent: string) {
  expect(screen.getByTestId("visible-combination")).toHaveTextContent(`${appearance}/${accent}`);
  expect(document.documentElement).toHaveAttribute("data-theme", appearance);
  expect(document.documentElement).toHaveAttribute("data-accent", accent);
  const radixTheme = document.querySelector(".radix-themes");
  expect(radixTheme).toHaveClass(appearance);
  expect(radixTheme).toHaveAttribute("data-accent-color", accent === "indigo" ? "iris" : accent);
}

function expectPersisted(appearance: string, accent: string) {
  expect(window.localStorage.getItem(appearanceKey)).toBe(appearance);
  expect(window.localStorage.getItem(accentKey)).toBe(accent);
  expect(JSON.parse(window.localStorage.getItem(combinationKey)!)).toEqual({ version: 1, appearance, accent });
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.dataset.theme = "dark";
  document.documentElement.dataset.accent = "orange";
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("D04/M04 complete preference restoration", () => {
  it.each([
    ["unversioned v1", JSON.stringify({ appearance: "dark", accent: "teal" })],
    ["explicit v1", JSON.stringify({ version: 1, appearance: "dark", accent: "teal" })],
  ])("restores a compatible full %s combination without migrating mirrors", (_name, raw) => {
    window.localStorage.setItem(combinationKey, raw);
    window.localStorage.setItem(appearanceKey, "light");
    window.localStorage.setItem(accentKey, "orange");
    const write = vi.spyOn(Storage.prototype, "setItem");
    const remove = vi.spyOn(Storage.prototype, "removeItem");
    const { preferences } = renderPreferences();

    expectVisible("dark", "teal");
    expect(preferences.restoreState).toBe("stored");
    expect(preferences.mutation).toEqual({ token: 0, origin: null, phase: "idle" });
    expect(write).not.toHaveBeenCalled();
    expect(remove).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(combinationKey)).toBe(raw);
    expect(window.localStorage.getItem(appearanceKey)).toBe("light");
    expect(window.localStorage.getItem(accentKey)).toBe("orange");
  });

  it.each([
    ["missing", null, "missing_default"],
    ["invalid JSON", "{broken", "invalid_default"],
    ["null", "null", "invalid_default"],
    ["primitive", "true", "invalid_default"],
    ["array", "[]", "invalid_default"],
    ["empty object", "{}", "invalid_default"],
    ["partial appearance", JSON.stringify({ appearance: "dark" }), "invalid_default"],
    ["partial accent", JSON.stringify({ accent: "teal" }), "invalid_default"],
    ["invalid appearance", JSON.stringify({ appearance: "system", accent: "teal" }), "invalid_default"],
    ["invalid accent", JSON.stringify({ appearance: "dark", accent: "blue" }), "invalid_default"],
    ["future version", JSON.stringify({ version: 2, appearance: "dark", accent: "teal" }), "invalid_default"],
    ["string version", JSON.stringify({ version: "1", appearance: "dark", accent: "teal" }), "invalid_default"],
    ["null version", JSON.stringify({ version: null, appearance: "dark", accent: "teal" }), "invalid_default"],
  ])("uses the full safe default for %s and neither exposes nor rewrites the raw value", (_name, raw, restoreState) => {
    if (raw !== null) window.localStorage.setItem(combinationKey, raw);
    window.localStorage.setItem(appearanceKey, "dark");
    window.localStorage.setItem(accentKey, "orange");
    const write = vi.spyOn(Storage.prototype, "setItem");
    const remove = vi.spyOn(Storage.prototype, "removeItem");
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule, true);

    expectVisible("light", "indigo");
    expect(view.preferences.restoreState).toBe(restoreState);
    const mutation = view.preferences.mutation;
    act(() => {
      expect(view.preferences.setAppearance("light", "settings")).toBe(true);
      expect(view.preferences.setAccent("indigo", "settings")).toBe(true);
      expect(view.preferences.setPreferences("light", "indigo", "settings")).toBe(true);
    });
    expect(view.preferences.mutation).toBe(mutation);
    expect(scheduler.commits).toHaveLength(0);
    expect(write).not.toHaveBeenCalled();
    expect(remove).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(combinationKey)).toBe(raw);
    expect(window.localStorage.getItem(appearanceKey)).toBe("dark");
    expect(window.localStorage.getItem(accentKey)).toBe("orange");
    expect(screen.getByTestId("restore-state")).toHaveTextContent(restoreState);
  });

  it("keeps the application usable when storage itself is unavailable", () => {
    const unavailable = vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new DOMException("Storage unavailable", "SecurityError");
    });
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    expectVisible("light", "indigo");
    expect(view.preferences.restoreState).toBe("invalid_default");
    act(() => { view.preferences.setPreferences("dark", "orange", "settings"); });
    expectVisible("dark", "orange");
    scheduler.run(0);
    expectVisible("light", "indigo");
    expect(view.preferences.restoreState).toBe("invalid_default");
    expect(view.preferences.mutation).toEqual({ token: 1, origin: "settings", phase: "failed" });
    unavailable.mockRestore();
  });
});

describe("D04/M04 latest complete-combination coordinator", () => {
  it("exposes applying intent atomically before persistence, then confirms all three keys", () => {
    const previous = storeCombination();
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    const focused = screen.getByRole("button", { name: "深色" });
    focused.focus();
    const write = vi.spyOn(Storage.prototype, "setItem");

    act(() => { expect(view.preferences.setPreferences("light", "orange", "settings-1")).toBe(true); });
    expectVisible("light", "orange");
    expect(view.preferences.mutation).toEqual({ token: 1, origin: "settings-1", phase: "applying" });
    expect(write).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(combinationKey)).toBe(previous);
    expect(focused).toHaveFocus();

    scheduler.run(0);
    expectVisible("light", "orange");
    expectPersisted("light", "orange");
    expect(view.preferences.mutation).toEqual({ token: 1, origin: "settings-1", phase: "applied" });
    expect(view.preferences.restoreState).toBe("stored");
    expect(write).toHaveBeenCalledTimes(3);
    expect(focused).toHaveFocus();
    scheduler.run(0);
    expect(write).toHaveBeenCalledTimes(3);
    view.unmount();
    const restored = renderPreferences(scheduler.schedule);
    expectVisible("light", "orange");
    expect(restored.preferences.restoreState).toBe("stored");
  });

  it("uses a microtask by default and does not treat accepted as already persisted", async () => {
    const view = renderPreferences();
    act(() => { expect(view.preferences.setAppearance("dark", "settings")).toBe(true); });
    expectVisible("dark", "indigo");
    expect(view.preferences.mutation.phase).toBe("applying");
    expect(window.localStorage.getItem(combinationKey)).toBeNull();
    await act(async () => { await Promise.resolve(); });
    expectPersisted("dark", "indigo");
    expect(view.preferences.mutation.phase).toBe("applied");
  });

  it("combines each single-dimension setter with the latest intent even in one event", () => {
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    const write = vi.spyOn(Storage.prototype, "setItem");
    act(() => {
      const sameRender = view.preferences;
      sameRender.setAppearance("dark", "settings");
      sameRender.setAccent("teal", "settings");
    });
    expectVisible("dark", "teal");
    expect(view.preferences.mutation.token).toBe(2);
    scheduler.run(0);
    expect(write).not.toHaveBeenCalled();
    expect(view.preferences.mutation.phase).toBe("applying");
    scheduler.run(1);
    expectPersisted("dark", "teal");
    expect(write).toHaveBeenCalledTimes(3);
  });

  it("keeps pending/current no-ops from creating tokens, writes or feedback identities", () => {
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    act(() => { view.preferences.setPreferences("dark", "teal", "settings-1"); });
    const applying = view.preferences.mutation;
    act(() => {
      view.preferences.setAppearance("dark", "other-route");
      view.preferences.setAccent("teal", "other-route");
      view.preferences.setPreferences("dark", "teal", "other-route");
    });
    expect(view.preferences.mutation).toBe(applying);
    expect(scheduler.commits).toHaveLength(1);
    scheduler.run(0);
    const applied = view.preferences.mutation;
    const write = vi.spyOn(Storage.prototype, "setItem");
    act(() => { view.preferences.setPreferences("dark", "teal", "other-route"); });
    expect(view.preferences.mutation).toBe(applied);
    expect(write).not.toHaveBeenCalled();
    expect(scheduler.commits).toHaveLength(1);
  });

  it("discards reordered old callbacks before they can write or report a stale failure", () => {
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    act(() => {
      view.preferences.setAppearance("dark", "settings-1");
      view.preferences.setAccent("teal", "settings-2");
      view.preferences.setPreferences("light", "orange", "settings-3");
    });
    const nativeWrite = Storage.prototype.setItem;
    const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (value === "dark" || value === "teal") throw new Error("An obsolete intent must never reach persistence");
      nativeWrite.call(this, key, value);
    });
    scheduler.run(2);
    const confirmed = view.preferences.mutation;
    scheduler.run(0);
    scheduler.run(1);
    scheduler.run(2);
    expectVisible("light", "orange");
    expectPersisted("light", "orange");
    expect(write).toHaveBeenCalledTimes(3);
    expect(view.preferences.mutation).toBe(confirmed);
    expect(confirmed).toEqual({ token: 3, origin: "settings-3", phase: "applied" });
  });

  it("continues pending work after the settings consumer leaves without remounting the provider", () => {
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    act(() => { view.preferences.setPreferences("dark", "orange", "settings-1"); });
    view.changeRoute("library-2");
    expect(screen.getByRole("region", { name: "library-2" })).toBeInTheDocument();
    expect(view.preferences.mutation).toEqual({ token: 1, origin: "settings-1", phase: "applying" });
    scheduler.run(0);
    expectVisible("dark", "orange");
    expectPersisted("dark", "orange");
    expect(view.preferences.mutation).toEqual({ token: 1, origin: "settings-1", phase: "applied" });
  });
});

describe("D04/M04 failed commits preserve the last confirmed combination", () => {
  it.each([appearanceKey, accentKey, combinationKey])("compensates a write failure at %s and rolls back the full visible combination", (failedKey) => {
    const previous = storeCombination();
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    const nativeWrite = Storage.prototype.setItem;
    let failNext = true;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (key === failedKey && failNext) {
        failNext = false;
        throw new DOMException("Storage unavailable", "QuotaExceededError");
      }
      nativeWrite.call(this, key, value);
    });
    const focused = screen.getByRole("button", { name: "深色" });
    focused.focus();
    act(() => { view.preferences.setPreferences("light", "orange", "settings"); });
    expectVisible("light", "orange");
    scheduler.run(0);
    expectVisible("dark", "teal");
    expect(window.localStorage.getItem(combinationKey)).toBe(previous);
    expect(window.localStorage.getItem(appearanceKey)).toBe("dark");
    expect(window.localStorage.getItem(accentKey)).toBe("teal");
    expect(view.preferences.mutation).toEqual({ token: 1, origin: "settings", phase: "failed" });
    expect(focused).toHaveFocus();
  });

  it.each([appearanceKey, accentKey, combinationKey])("rejects inconsistent %s readback, including compatibility mirrors", (failedKey) => {
    const previous = storeCombination();
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    const nativeRead = Storage.prototype.getItem;
    const nativeWrite = Storage.prototype.setItem;
    let readingWritten = false;
    let mismatch = true;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      nativeWrite.call(this, key, value);
      if (key === combinationKey) readingWritten = true;
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(function (this: Storage, key) {
      if (readingWritten && mismatch && key === failedKey) {
        mismatch = false;
        return "inconsistent";
      }
      return nativeRead.call(this, key);
    });
    act(() => { view.preferences.setPreferences("light", "orange", "settings"); });
    scheduler.run(0);
    expectVisible("dark", "teal");
    expect(view.preferences.mutation.phase).toBe("failed");
    expect(window.localStorage.getItem(combinationKey)).toBe(previous);
    expect(window.localStorage.getItem(appearanceKey)).toBe("dark");
    expect(window.localStorage.getItem(accentKey)).toBe("teal");
  });

  it("does not write anything when capturing an original key fails", () => {
    const previous = storeCombination();
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    const nativeRead = Storage.prototype.getItem;
    const read = vi.spyOn(Storage.prototype, "getItem").mockImplementation(function (this: Storage, key) {
      if (key === accentKey) throw new Error("Unavailable");
      return nativeRead.call(this, key);
    });
    const write = vi.spyOn(Storage.prototype, "setItem");
    const remove = vi.spyOn(Storage.prototype, "removeItem");
    act(() => { view.preferences.setPreferences("light", "orange", "settings"); });
    scheduler.run(0);
    expectVisible("dark", "teal");
    expect(view.preferences.mutation.phase).toBe("failed");
    expect(write).not.toHaveBeenCalled();
    expect(remove).not.toHaveBeenCalled();
    read.mockRestore();
    expect(window.localStorage.getItem(combinationKey)).toBe(previous);
  });

  it.each([null, "{unrecoverable-json", JSON.stringify({ version: 8, appearance: "dark", accent: "teal" })])("preserves a missing/abnormal raw value on failure, and only removes its notice after success: %s", (raw) => {
    if (raw !== null) window.localStorage.setItem(combinationKey, raw);
    window.localStorage.setItem(appearanceKey, "dark");
    window.localStorage.setItem(accentKey, "teal");
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    const restoreState = raw === null ? "missing_default" : "invalid_default";
    const nativeWrite = Storage.prototype.setItem;
    let failNext = true;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (key === combinationKey && failNext) {
        failNext = false;
        throw new Error("Unavailable");
      }
      nativeWrite.call(this, key, value);
    });
    act(() => { view.preferences.setPreferences("dark", "orange", "settings"); });
    expect(view.preferences.restoreState).toBe(restoreState);
    scheduler.run(0);
    expectVisible("light", "indigo");
    expect(view.preferences.restoreState).toBe(restoreState);
    expect(window.localStorage.getItem(combinationKey)).toBe(raw);
    expect(window.localStorage.getItem(appearanceKey)).toBe("dark");
    expect(window.localStorage.getItem(accentKey)).toBe("teal");
    const failed = view.preferences.mutation;
    act(() => { view.preferences.setPreferences("light", "indigo", "new-route"); });
    expect(view.preferences.mutation).toBe(failed);
    act(() => { view.preferences.setPreferences("dark", "orange", "settings"); });
    scheduler.run(1);
    expectPersisted("dark", "orange");
    expect(view.preferences.restoreState).toBe("stored");
    expect(view.preferences.mutation).toEqual({ token: 2, origin: "settings", phase: "applied" });
  });

  it("rolls the newest failed attempt back to confirmed state, never to an unconfirmed predecessor", () => {
    const previous = storeCombination("light", "indigo");
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    act(() => {
      view.preferences.setAppearance("dark", "settings-1");
      view.preferences.setAccent("orange", "settings-2");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Unavailable"); });
    scheduler.run(1);
    expectVisible("light", "indigo");
    expect(view.preferences.mutation).toEqual({ token: 2, origin: "settings-2", phase: "failed" });
    scheduler.run(0);
    expectVisible("light", "indigo");
    expect(window.localStorage.getItem(combinationKey)).toBe(previous);
    expect(view.preferences.mutation.token).toBe(2);
  });

  it("takes the next failure baseline from the most recently confirmed successful selection", () => {
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    act(() => { view.preferences.setPreferences("dark", "teal", "settings"); });
    scheduler.run(0);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Unavailable"); });
    act(() => { view.preferences.setPreferences("light", "orange", "settings"); });
    scheduler.run(1);
    expectVisible("dark", "teal");
    expect(view.preferences.restoreState).toBe("stored");
    expect(view.preferences.mutation.phase).toBe("failed");
  });

  it("reports failure even if the storage system also rejects compensation", () => {
    storeCombination();
    const scheduler = controlledScheduler();
    const view = renderPreferences(scheduler.schedule);
    const nativeWrite = Storage.prototype.setItem;
    let writes = 0;
    const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      writes += 1;
      if (writes > 1) throw new Error("Unavailable, including rollback");
      nativeWrite.call(this, key, value);
    });
    act(() => { view.preferences.setPreferences("light", "orange", "settings"); });
    scheduler.run(0);
    expectVisible("dark", "teal");
    expect(view.preferences.mutation.phase).toBe("failed");
    // All three originals were attempted; the coordinator cannot promise durable
    // atomicity when the underlying storage itself also refuses compensation.
    expect(write).toHaveBeenCalledTimes(5);
  });

  it("handles a scheduler failure without writing or pretending the attempt applied", () => {
    const write = vi.spyOn(Storage.prototype, "setItem");
    const view = renderPreferences(() => { throw new Error("Scheduling unavailable"); });
    act(() => { expect(view.preferences.setAppearance("dark", "settings")).toBe(false); });
    expectVisible("light", "indigo");
    expect(view.preferences.mutation).toEqual({ token: 1, origin: "settings", phase: "failed" });
    expect(write).not.toHaveBeenCalled();
  });
});
