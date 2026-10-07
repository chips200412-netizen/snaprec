import { StrictMode, useEffect, useState, useSyncExternalStore } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ShareSession, ShareSnapshot } from "./share-bootstrap";

const share = vi.hoisted(() => ({ session: null as unknown as ShareSession, realPage: false }));
vi.mock("./share-bootstrap", () => ({ getShareSession: () => share.session }));
vi.mock("./share-capture-page", async (original) => {
  const { ShareCapturePage: RealPage } = await original<typeof import("./share-capture-page")>();
  function MockPage({ session, onSubmit, onLeave, registerLeaveGuard }: {
    session: ShareSession;
    onSubmit: (input: string) => void;
    onLeave: () => void;
    registerLeaveGuard: (guard: ((leave: () => void) => void) | null) => void;
  }) {
    const snapshot = useSyncExternalStore(session.subscribe, session.getSnapshot);
    const [pending, setPending] = useState<(() => void) | null>(null);
    const [localFailure, setLocalFailure] = useState(false);
    useEffect(() => {
      registerLeaveGuard((leave) => setPending(() => leave));
      return () => registerLeaveGuard(null);
    }, [registerLeaveGuard]);
    return <main>
      <h1>隔离接收 {snapshot.status}</h1>
      <button disabled={snapshot.status !== "ready"} onClick={() => { try { onSubmit("原文 https://example.com/share"); } catch { setLocalFailure(true); } }}>提交快照</button>
      {localFailure && <p>本地交接失败，草稿保留</p>}
      <button onClick={() => { session.end(); onLeave(); }}>手动粘贴</button>
      {pending && <><button onClick={() => setPending(null)}>继续编辑</button><button onClick={() => { const leave = pending; setPending(null); session.end(); leave(); }}>确认离开</button></>}
    </main>;
  }
  return { ShareCapturePage: (props: Parameters<typeof RealPage>[0]) => share.realPage ? <RealPage {...props} /> : <MockPage {...props} /> };
});
vi.mock("./collection-api", async (original) => ({
  ...await original<typeof import("./collection-api")>(),
  collectionApi: {
    createPreview: vi.fn(), listItems: vi.fn(), createItem: vi.fn(), getItem: vi.fn(),
    updateItem: vi.fn(), transcribeInspiration: vi.fn(), getDeepAnalysis: vi.fn(),
    startDeepAnalysis: vi.fn(), retryDeepAnalysis: vi.fn(),
  },
}));
import { App } from "./App";
import { collectionApi } from "./collection-api";

function readySession() {
  let snapshot: ShareSnapshot = {
    status: "ready", fields: { shared_title: "原文", shared_text: "", shared_url: "https://example.com/share" },
  };
  const listeners = new Set<() => void>();
  return {
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    end: vi.fn(() => { snapshot = { status: "unavailable", fields: null }; for (const listener of listeners) listener(); }),
  };
}

describe("R27 share route ownership", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    share.realPage = false;
    share.session = readySession();
    window.localStorage.clear();
    window.history.replaceState({}, "", "/capture/share");
    Object.defineProperty(window, "scrollTo", { configurable: true, value: vi.fn() });
    Object.defineProperty(window, "matchMedia", { configurable: true, value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) });
    vi.mocked(collectionApi.listItems).mockResolvedValue({ items: [], total: 0, limit: 24, next_cursor: null, facets: { categories: [], platforms: [], tags: [] } });
    vi.mocked(collectionApi.createPreview).mockImplementation(() => new Promise(() => undefined));
  });

  it("StrictMode接收页和deep query均不挂载业务表面或发送任何collection请求", async () => {
    window.history.replaceState({}, "", "/capture/share?mode=deep-analysis");
    render(<StrictMode><App /></StrictMode>);
    await screen.findByText("隔离接收 ready");
    await act(async () => { await Promise.resolve(); });
    for (const method of Object.values(collectionApi)) expect(method).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/capture/share");
  });

  it("真实ShareCapturePage在StrictMode下审核零API，明确提交后才一次preview", async () => {
    share.realPage = true;
    render(<StrictMode><App /></StrictMode>);
    const submit = await screen.findByRole("button", { name: "收进来" });
    await act(async () => { await Promise.resolve(); });
    for (const method of Object.values(collectionApi)) expect(method).not.toHaveBeenCalled();
    expect(screen.getByLabelText("公开链接")).toHaveValue("https://example.com/share");
    fireEvent.click(submit);
    await waitFor(() => expect(collectionApi.createPreview).toHaveBeenCalledTimes(1));
    expect(collectionApi.createPreview).toHaveBeenCalledWith("原文\n<https://example.com/share>", false, expect.any(AbortSignal));
    expect(window.location.pathname).toBe("/capture/review");
    expect(share.session.getSnapshot().fields).toBeNull();
    expect(collectionApi.createItem).not.toHaveBeenCalled();
  });

  it("明确提交交一次冻结快照，StrictMode只由ReviewPage发一次preview", async () => {
    render(<StrictMode><App /></StrictMode>);
    const shareEntry = window.history.state;
    fireEvent.click(screen.getByText("提交快照"));
    await waitFor(() => expect(collectionApi.createPreview).toHaveBeenCalledTimes(1));
    expect(collectionApi.createPreview).toHaveBeenCalledWith("原文 https://example.com/share", false, expect.any(AbortSignal));
    expect(window.location.pathname).toBe("/capture/review");
    expect(JSON.stringify(window.history.state)).not.toContain("https://");
    expect(share.session.getSnapshot().fields).toBeNull();
    expect(collectionApi.createItem).not.toHaveBeenCalled();
    act(() => { window.history.replaceState(shareEntry, "", "/capture/share"); window.dispatchEvent(new PopStateEvent("popstate", { state: shareEntry })); });
    await screen.findByText("隔离接收 unavailable");
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(1);
    expect(screen.getByText("提交快照")).toBeDisabled();
  });

  it("远端失败留在既有review且重试必须明确触发", async () => {
    vi.mocked(collectionApi.createPreview).mockRejectedValue(new Error("固定失败"));
    render(<StrictMode><App /></StrictMode>);
    fireEvent.click(screen.getByText("提交快照"));
    await screen.findByText("固定失败");
    expect(window.location.pathname).toBe("/capture/review");
    expect(collectionApi.createPreview).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText("重试元信息"));
    await waitFor(() => expect(collectionApi.createPreview).toHaveBeenCalledTimes(2));
    expect(collectionApi.createPreview).toHaveBeenLastCalledWith("原文 https://example.com/share", true, expect.any(AbortSignal));
  });

  it("导航写入失败不产生review owner，保留可重试草稿且所有业务请求仍为零", async () => {
    render(<StrictMode><App /></StrictMode>);
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => { throw new Error("navigation unavailable"); });
    fireEvent.click(screen.getByText("提交快照"));
    await screen.findByText("本地交接失败，草稿保留");
    expect(window.location.pathname).toBe("/capture/share");
    expect(share.session.getSnapshot().status).toBe("ready");
    for (const method of Object.values(collectionApi)) expect(method).not.toHaveBeenCalled();
    push.mockRestore();
  });

  it("无草稿失败态可回素材库，离开后才读list且无preview/save", async () => {
    share.session.end();
    render(<StrictMode><App /></StrictMode>);
    await screen.findByText("隔离接收 unavailable");
    expect(collectionApi.listItems).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("手动粘贴"));
    await waitFor(() => expect(collectionApi.listItems).toHaveBeenCalledTimes(1));
    expect(window.location.pathname).toBe("/");
    expect(collectionApi.createPreview).not.toHaveBeenCalled();
    expect(collectionApi.createItem).not.toHaveBeenCalled();
  });

  it("外部/未拥有历史不被share guard push或replace改写", async () => {
    render(<StrictMode><App /></StrictMode>);
    const replace = vi.spyOn(window.history, "replaceState");
    const push = vi.spyOn(window.history, "pushState");
    act(() => { window.dispatchEvent(new PopStateEvent("popstate", { state: null })); });
    await waitFor(() => expect(share.session.end).toHaveBeenCalled());
    expect(replace).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
    expect(screen.queryByText("确认离开")).not.toBeInTheDocument();
    replace.mockRestore(); push.mockRestore();
  });

  it("已拥有历史先恢复当前条目，确认只执行保存的原始delta一次", async () => {
    render(<App />);
    const shareEntry = window.history.state;
    fireEvent.click(screen.getByText("提交快照"));
    await waitFor(() => expect(collectionApi.createPreview).toHaveBeenCalledTimes(1));
    const reviewEntry = window.history.state;
    act(() => { window.history.replaceState(shareEntry, "", "/capture/share"); window.dispatchEvent(new PopStateEvent("popstate", { state: shareEntry })); });
    await screen.findByText("隔离接收 unavailable");
    const go = vi.spyOn(window.history, "go").mockImplementation(() => undefined);
    const push = vi.spyOn(window.history, "pushState");
    act(() => { window.dispatchEvent(new PopStateEvent("popstate", { state: reviewEntry })); });
    expect(go).toHaveBeenCalledWith(-1);
    expect(screen.queryByText("确认离开")).not.toBeInTheDocument();
    act(() => { window.dispatchEvent(new PopStateEvent("popstate", { state: shareEntry })); });
    fireEvent.click(await screen.findByText("继续编辑"));
    expect(go).toHaveBeenCalledTimes(1);
    act(() => { window.dispatchEvent(new PopStateEvent("popstate", { state: reviewEntry })); });
    act(() => { window.dispatchEvent(new PopStateEvent("popstate", { state: shareEntry })); });
    fireEvent.click(await screen.findByText("确认离开"));
    expect(go.mock.calls.map((args) => args[0])).toEqual([-1, -1, 1]);
    expect(push).not.toHaveBeenCalled();
    go.mockRestore(); push.mockRestore();
  });
});
