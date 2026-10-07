import { createElement, StrictMode, useSyncExternalStore } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./share-intent", () => ({
  cleanupShareIntents: vi.fn().mockResolvedValue(undefined),
  claimShareIntent: vi.fn(),
}));

const token = "0123456789abcdef0123456789abcdef";
const fields = { shared_title: "分享标题", shared_text: "备注", shared_url: "https://example.com/a" };

describe("share navigation bootstrap", () => {
  beforeEach(async () => {
    vi.resetModules();
    const store = await import("./share-intent");
    vi.mocked(store.cleanupShareIntents).mockReset().mockResolvedValue(undefined);
    window.history.replaceState({}, "", `/capture/share#intent=${token}`);
  });

  it("普通启动只清理一次，不创建失败session，后来进入share复用清理并正常领取", async () => {
    window.history.replaceState({ ordinary: true }, "", "/?query=kept#ordinary");
    const store = await import("./share-intent");
    vi.mocked(store.claimShareIntent).mockReset().mockResolvedValue(fields);
    const { bootstrapShareSession, getShareSession } = await import("./share-bootstrap");
    expect(bootstrapShareSession()).toBeNull();
    expect(bootstrapShareSession()).toBeNull();
    await waitFor(() => expect(store.cleanupShareIntents).toHaveBeenCalledTimes(1));
    expect(window.location.pathname + window.location.search + window.location.hash).toBe("/?query=kept#ordinary");
    expect(window.history.state).toEqual({ ordinary: true });
    expect(store.claimShareIntent).not.toHaveBeenCalled();
    window.history.replaceState({}, "", `/capture/share#intent=${token}`);
    const session = getShareSession();
    await waitFor(() => expect(session.getSnapshot().status).toBe("ready"));
    expect(store.cleanupShareIntents).toHaveBeenCalledTimes(1);
    expect(store.claimShareIntent).toHaveBeenCalledTimes(1);
  });

  it.each(["reject", "throw"])("普通启动cleanup %s安静收敛，share复用失败结果且不claim或泄露错误", async (failure) => {
    window.history.replaceState({}, "", "/");
    const store = await import("./share-intent");
    vi.mocked(store.claimShareIntent).mockReset();
    vi.mocked(store.cleanupShareIntents).mockImplementation(() => {
      if (failure === "throw") throw new Error("private cleanup payload");
      return Promise.reject(new Error("private cleanup payload"));
    });
    const log = vi.spyOn(console, "log");
    const warn = vi.spyOn(console, "warn");
    const error = vi.spyOn(console, "error");
    const fetch = vi.spyOn(globalThis, "fetch");
    const unhandled = vi.fn();
    window.addEventListener("unhandledrejection", unhandled);
    try {
      const { bootstrapShareSession } = await import("./share-bootstrap");
      expect(bootstrapShareSession()).toBeNull();
      expect(bootstrapShareSession()).toBeNull();
      await waitFor(() => expect(store.cleanupShareIntents).toHaveBeenCalledTimes(1));
      window.history.replaceState({}, "", `/capture/share#intent=${token}`);
      const session = bootstrapShareSession()!;
      await waitFor(() => expect(session.getSnapshot().status).toBe("unavailable"));
      expect(session.getSnapshot()).toEqual({ status: "unavailable", fields: null });
      expect(store.cleanupShareIntents).toHaveBeenCalledTimes(1);
      expect(store.claimShareIntent).not.toHaveBeenCalled();
      for (const call of [log, warn, error, fetch, unhandled]) expect(call).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("unhandledrejection", unhandled);
      log.mockRestore(); warn.mockRestore(); error.mockRestore(); fetch.mockRestore();
    }
  });

  it("清除fragment发生在claim之前，StrictMode只领取一次且不把token或正文写history", async () => {
    const store = await import("./share-intent");
    vi.mocked(store.claimShareIntent).mockReset().mockResolvedValue(fields);
    const { bootstrapShareSession, getShareSession } = await import("./share-bootstrap");
    const session = bootstrapShareSession()!;
    expect(window.location.href).not.toContain(token);
    expect(window.history.state).toBeNull();
    expect(store.claimShareIntent).not.toHaveBeenCalled();
    function Consumer() {
      const current = getShareSession();
      const snapshot = useSyncExternalStore(current.subscribe, current.getSnapshot);
      return createElement("p", null, snapshot.status);
    }
    const view = render(createElement(StrictMode, null, createElement(Consumer)));
    await screen.findByText("ready");
    expect(store.claimShareIntent).toHaveBeenCalledTimes(1);
    expect(getShareSession()).toBe(session);
    view.unmount();
    expect(session.getSnapshot().fields).toEqual(fields);
  });

  it("明确终止和真正pagehide都阻止迟到claim，BFCache不复活", async () => {
    const store = await import("./share-intent");
    let resolve!: (value: typeof fields) => void;
    vi.mocked(store.claimShareIntent).mockReset().mockImplementation(() => new Promise((done) => { resolve = done; }));
    const { bootstrapShareSession, getShareSession } = await import("./share-bootstrap");
    const session = bootstrapShareSession()!;
    await waitFor(() => expect(store.claimShareIntent).toHaveBeenCalledTimes(1));
    window.dispatchEvent(new Event("pagehide"));
    resolve(fields);
    await Promise.resolve();
    await Promise.resolve();
    window.dispatchEvent(new Event("pageshow"));
    expect(getShareSession()).toBe(session);
    expect(session.getSnapshot()).toEqual({ status: "unavailable", fields: null });
  });

  it("visibilitychange保留当前草稿，end清理且重复bootstrap不能重新消费", async () => {
    const store = await import("./share-intent");
    vi.mocked(store.claimShareIntent).mockReset().mockResolvedValue(fields);
    const { bootstrapShareSession } = await import("./share-bootstrap");
    const session = bootstrapShareSession()!;
    await waitFor(() => expect(session.getSnapshot().status).toBe("ready"));
    document.dispatchEvent(new Event("visibilitychange"));
    expect(session.getSnapshot().fields).toEqual(fields);
    session.end();
    window.history.replaceState({}, "", `/capture/share#intent=${token}`);
    expect(bootstrapShareSession()).toBe(session);
    expect(window.location.hash).toBe("");
    expect(session.getSnapshot().status).toBe("unavailable");
    expect(store.claimShareIntent).toHaveBeenCalledTimes(1);
  });

  it("在claim开始前离开也尽力消费自有intent，但不接纳迟到payload", async () => {
    const store = await import("./share-intent");
    vi.mocked(store.claimShareIntent).mockReset().mockResolvedValue(fields);
    const { bootstrapShareSession } = await import("./share-bootstrap");
    const session = bootstrapShareSession()!;
    session.end();
    await waitFor(() => expect(store.claimShareIntent).toHaveBeenCalledTimes(1));
    await Promise.resolve();
    expect(session.getSnapshot()).toEqual({ status: "unavailable", fields: null });
  });

  it.each(["", "#intent=wrong", `#intent=${token}&text=private`, `#intent=${token.toUpperCase()}`])("无效导航%s不claim且清空凭据", async (fragment) => {
    window.history.replaceState({ secret: "untrusted" }, "", `/capture/share?text=private${fragment}`);
    const store = await import("./share-intent");
    vi.mocked(store.claimShareIntent).mockReset();
    const { bootstrapShareSession } = await import("./share-bootstrap");
    const session = bootstrapShareSession()!;
    await act(async () => { await Promise.resolve(); });
    expect(session.getSnapshot().status).toBe("unavailable");
    expect(window.location.pathname + window.location.search + window.location.hash).toBe("/capture/share");
    expect(window.history.state).toBeNull();
    expect(store.claimShareIntent).not.toHaveBeenCalled();
  });

  it("storage失败以固定unavailable收敛且不暴露错误正文", async () => {
    const store = await import("./share-intent");
    vi.mocked(store.claimShareIntent).mockReset().mockRejectedValue(new Error("private payload"));
    const { bootstrapShareSession } = await import("./share-bootstrap");
    const session = bootstrapShareSession()!;
    await waitFor(() => expect(session.getSnapshot().status).toBe("unavailable"));
    expect(session.getSnapshot()).toEqual({ status: "unavailable", fields: null });
  });
});
