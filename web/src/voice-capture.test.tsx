import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { VoiceCapture } from "./voice-capture";
import { collectionApi } from "./collection-api";
import userEvent from "@testing-library/user-event";

vi.mock("./collection-api", () => ({ collectionApi: { transcribeInspiration: vi.fn() } }));
class Pointer extends MouseEvent {
  pointerId: number;
  isPrimary: boolean;
  constructor(type: string, options: PointerEventInit = {}) { super(type, options); this.pointerId = options.pointerId ?? 1; this.isPrimary = options.isPrimary ?? true; }
}
let tracks: Array<EventTarget & { stop: ReturnType<typeof vi.fn>; readyState: string }>;
let getUserMedia: ReturnType<typeof vi.fn>;
let recorders: Recorder[];
class Recorder {
  state: RecordingState = "inactive";
  mimeType = "audio/webm";
  ondataavailable: ((event: BlobEvent) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  start = vi.fn(() => { this.state = "recording"; });
  stop = vi.fn(() => { this.state = "inactive"; this.ondataavailable?.({ data: new Blob(["synthetic"]) } as BlobEvent); this.onstop?.(); });
  constructor() { recorders.push(this); }
}
const stream = () => {
  const track = Object.assign(new EventTarget(), { stop: vi.fn(), readyState: "live" });
  tracks.push(track);
  return { getTracks: () => [track] } as unknown as MediaStream;
};
function mount() {
  const onDraft = vi.fn();
  const onStateChange = vi.fn();
  const result = render(<VoiceCapture disabled={false} identity="preview-a" onDraft={onDraft} onStateChange={onStateChange} registerCancel={() => {}} />);
  return { ...result, onDraft, onStateChange };
}
function hold() {
  const target = screen.getByRole("button", { name: "按住说话" });
  vi.spyOn(target, "getBoundingClientRect").mockReturnValue({ left: 50, top: 50, right: 150, bottom: 150, width: 100, height: 100, x: 50, y: 50, toJSON() {} });
  return target;
}
const down = (target: HTMLElement, pointerId = 1) => fireEvent.pointerDown(target, { pointerId, isPrimary: true, button: 0, clientX: 100, clientY: 100 });
const up = (target: HTMLElement, x = 100, pointerId = 1) => fireEvent.pointerUp(target, { pointerId, clientX: x, clientY: 100 });
async function prepare(target: HTMLElement) {
  down(target);
  up(target);
  await screen.findByText("已准备好，请按住说话");
}
beforeEach(() => {
  tracks = []; recorders = [];
  vi.stubGlobal("PointerEvent", Pointer);
  vi.stubGlobal("MediaRecorder", Recorder);
  vi.stubGlobal("isSecureContext", true);
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", { configurable: true, value: vi.fn() });
  getUserMedia = vi.fn().mockImplementation(async () => stream());
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
  Object.defineProperty(navigator, "permissions", { configurable: true, value: undefined });
  vi.mocked(collectionApi.transcribeInspiration).mockReset().mockResolvedValue({ content: "新灵感", input_mode: "voice", transcription_status: "draft" });
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("HOLD-VOICE1 lifecycle", () => {
  it.each([["hold", "recording"], ["tap", "recording"], ["hold", "transcribing"], ["tap", "transcribing"]])("focused %s %s cancellation returns focus to its stable main control", async (mode, phase) => {
    const user = userEvent.setup(); const view = mount(); const target = hold(); await prepare(target);
    if (mode === "tap") { fireEvent.click(screen.getByRole("button", { name: "点按录音" })); fireEvent.click(screen.getByRole("button", { name: "开始录音" })); }
    else down(target);
    await waitFor(() => expect(recorders).toHaveLength(1));
    let resolve!: (value: { content: string; input_mode: "voice"; transcription_status: "draft" }) => void;
    if (phase === "transcribing") {
      vi.mocked(collectionApi.transcribeInspiration).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
      if (mode === "tap") fireEvent.click(screen.getByRole("button", { name: "结束录音" })); else up(target);
      await screen.findByText("正在转文字…");
    }
    const cancel = screen.getByRole("button", { name: phase === "transcribing" ? "取消转写" : "取消录音" });
    cancel.focus(); const focus = vi.spyOn(HTMLElement.prototype, "focus");
    await user.keyboard("{Enter}");
    const fallback = screen.getByRole("button", { name: mode === "tap" ? "开始录音" : "按住说话" });
    expect(fallback).toHaveFocus(); expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });
    if (phase === "transcribing") {
      await act(async () => resolve({ content: "已取消的旧结果", input_mode: "voice", transcription_status: "draft" }));
      expect(view.onDraft).not.toHaveBeenCalled(); expect(fallback).toHaveFocus();
    }
    expect(collectionApi.transcribeInspiration).toHaveBeenCalledTimes(phase === "transcribing" ? 1 : 0);
  });
  it.each([false, true])("an asynchronous failure restores a removed cancel control only while it owns focus (%s)", async (moveFocus) => {
    const user = userEvent.setup(); render(<input aria-label="其他文字" />); mount();
    const target = hold(); await prepare(target);
    let reject!: (error: Error) => void;
    vi.mocked(collectionApi.transcribeInspiration).mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    down(target); await screen.findByText("松手转文字"); up(target); await screen.findByText("正在转文字…");
    screen.getByRole("button", { name: "取消转写" }).focus();
    const other = screen.getByRole("textbox", { name: "其他文字" });
    if (moveFocus) await user.click(other);
    await act(async () => reject(new Error("模拟异步失败")));
    expect(moveFocus ? other : target).toHaveFocus();
  });
  it("automatic duration completion restores focus only when the disappearing cancel control is focused", async () => {
    mount(); const target = hold(); await prepare(target);
    vi.useFakeTimers(); await act(async () => { down(target); });
    screen.getByRole("button", { name: "取消录音" }).focus();
    await act(async () => { vi.advanceTimersByTime(120_000); });
    expect(target).toHaveFocus();
    expect(collectionApi.transcribeInspiration).toHaveBeenCalledOnce();
  });
  it.each([false, true])("asynchronous stopping/transcription retains the active keyboard control without reclaiming user-moved focus (%s)", async (moveFocus) => {
    const user = userEvent.setup();
    render(<input aria-label="独立文字输入" />);
    mount(); hold().focus(); await user.keyboard("{Enter}");
    await screen.findByText("已准备好，请点击开始录音");
    await user.keyboard("{Enter}");
    const action = await screen.findByRole("button", { name: "结束录音" });
    let resolve!: (value: { content: string; input_mode: "voice"; transcription_status: "draft" }) => void;
    vi.mocked(collectionApi.transcribeInspiration).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    recorders[0].stop.mockImplementation(() => {
      recorders[0].state = "inactive";
      recorders[0].ondataavailable?.({ data: new Blob(["synthetic delayed audio"]) } as BlobEvent);
    });
    await user.keyboard(" "); await screen.findByText("正在停止录音…");
    const waitingAction = screen.getByRole("button", { name: "开始录音" });
    expect(waitingAction).toBe(action);
    // Native disabled removes keyboard focus in browsers; aria-disabled retains
    // the focused element while the session guard rejects repeated activation.
    expect(waitingAction).not.toHaveAttribute("disabled");
    expect(waitingAction).toHaveAttribute("aria-disabled", "true");
    expect(waitingAction).toHaveFocus();
    await user.keyboard("{Enter}"); expect(getUserMedia).toHaveBeenCalledTimes(2);
    act(() => recorders[0].onstop?.()); await screen.findByText("正在转文字…");
    expect(waitingAction).toHaveFocus();
    await user.keyboard(" "); expect(collectionApi.transcribeInspiration).toHaveBeenCalledOnce();
    const other = screen.getByRole("textbox", { name: "独立文字输入" });
    if (moveFocus) await user.click(other);
    await act(async () => resolve({ content: "延迟返回草稿", input_mode: "voice", transcription_status: "draft" }));
    await screen.findByText(/转写草稿已就绪/);
    expect(moveFocus ? other : waitingAction).toHaveFocus();
    expect(waitingAction).toHaveAttribute("aria-disabled", "false");
    expect(getUserMedia).toHaveBeenCalledTimes(2);
  });
  it.each([[380.7, 102.3], [480.7, 54.3]])("keyboard focus clears the Dock for a group ending at %s without scrolling touch focus or state changes", async (groupBottom, scrollDelta) => {
    const onStateChange = vi.fn();
    render(<div className="collection-page"><header className="collection-global-header" />
      <VoiceCapture identity="focus-preview" disabled={false} onDraft={() => {}} onStateChange={onStateChange} registerCancel={() => {}} />
      <footer className="save-review-bar" /></div>);
    const rect = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 90, width: 90, height: bottom - top, x: 0, y: top, toJSON() {} });
    const target = screen.getByRole("button", { name: "按住说话" });
    const group = target.closest(".voice-capture")!;
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(rect(172.5, groupBottom));
    vi.spyOn(document.querySelector(".collection-global-header")!, "getBoundingClientRect").mockReturnValue(rect(0, 62));
    vi.spyOn(document.querySelector(".save-review-bar")!, "getBoundingClientRect").mockReturnValue(rect(286.4, 390.4));
    vi.spyOn(target, "getBoundingClientRect").mockReturnValue(rect(172.5, 262.5));
    vi.spyOn(screen.getByRole("status"), "getBoundingClientRect").mockReturnValue(rect(311.7, 332.7));
    const visible = vi.spyOn(target, "matches").mockReturnValue(true);
    const scroll = vi.spyOn(window, "scrollBy").mockImplementation(() => {});
    target.focus();
    await waitFor(() => expect(scroll).toHaveBeenCalledOnce());
    expect((scroll.mock.calls[0][0] as ScrollToOptions).top).toBeCloseTo(scrollDelta);
    expect((scroll.mock.calls[0][0] as ScrollToOptions).behavior).toBe("instant");
    scroll.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "点按录音" }));
    await act(async () => { await new Promise((resolve) => requestAnimationFrame(resolve)); });
    expect(scroll).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "改用长按" }));
    const nextTarget = screen.getByRole("button", { name: "按住说话" });
    vi.spyOn(nextTarget, "matches").mockReturnValue(false);
    visible.mockRestore();
    nextTarget.focus();
    await act(async () => { await new Promise((resolve) => requestAnimationFrame(resolve)); });
    expect(scroll).not.toHaveBeenCalled();
  });
  it("processed draft returns its hint to idle without resetting another active job or an error", async () => {
    const view = mount(); const target = hold(); await prepare(target);
    down(target); await screen.findByText("松手转文字"); up(target);
    await screen.findByText(/转写草稿已就绪/);
    const update = () => view.rerender(<VoiceCapture identity="preview-a" hasDraft={false} disabled={false} onDraft={view.onDraft} onStateChange={view.onStateChange} registerCancel={() => {}} />);
    update(); expect(screen.queryByText(/转写草稿已就绪/)).not.toBeInTheDocument();
    expect(screen.getByText("松手转文字，上滑取消")).toBeInTheDocument();
    down(target); await screen.findByText("松手转文字"); update();
    expect(screen.getByText("松手转文字")).toBeInTheDocument();
    vi.mocked(collectionApi.transcribeInspiration).mockRejectedValueOnce(new Error("安全转写错误"));
    up(target); await screen.findByText("安全转写错误"); update();
    expect(screen.getByText("安全转写错误")).toBeInTheDocument();
  });
  it("keyboard and AT activation transfer focus to the corresponding tap action without scrolling", async () => {
    const user = userEvent.setup(); mount();
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    const target = hold(); target.focus();
    await user.keyboard("{Enter}");
    await screen.findByText("已准备好，请点击开始录音");
    expect(screen.getByRole("button", { name: "开始录音" })).toHaveFocus();
    expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });
    await user.click(screen.getByRole("button", { name: "改用长按" }));
    const held = screen.getByRole("button", { name: "按住说话" });
    await waitFor(() => expect(held).toHaveFocus());
    await user.keyboard("{Enter}");
    const finish = await screen.findByRole("button", { name: "结束录音" });
    expect(finish).toHaveFocus(); expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });
    await user.keyboard(" ");
    await screen.findByText(/转写草稿已就绪/);
    expect(collectionApi.transcribeInspiration).toHaveBeenCalledOnce();
  });
  it("first activation only prepares, and a stable same pointer can leave, return and release once", async () => {
    const { onDraft } = mount(); const target = hold();
    await prepare(target);
    expect(recorders).toHaveLength(0); expect(tracks[0].stop).toHaveBeenCalledOnce();
    down(target); await screen.findByText("松手转文字");
    expect(screen.getByRole("button", { name: "按住说话" })).toBe(target);
    fireEvent.pointerMove(target, { pointerId: 1, clientX: 300, clientY: 100 });
    expect(screen.getByText("松手取消")).toBeInTheDocument();
    up(target, 100, 2); expect(recorders[0].stop).not.toHaveBeenCalled();
    fireEvent.pointerMove(target, { pointerId: 1, clientX: 100, clientY: 100 });
    expect(screen.getByText("松手转文字")).toBeInTheDocument();
    up(target); up(target);
    await waitFor(() => expect(onDraft).toHaveBeenCalledWith("新灵感"));
    expect(collectionApi.transcribeInspiration).toHaveBeenCalledOnce(); expect(recorders[0].stop).toHaveBeenCalledOnce();
  });
  it("outside release and track interruption clean without submitting", async () => {
    mount(); const target = hold(); await prepare(target);
    down(target); await screen.findByText("松手转文字"); up(target, 300);
    expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled(); expect(tracks[1].stop).toHaveBeenCalledOnce();
    down(target); await screen.findByText("松手转文字");
    act(() => tracks[2].dispatchEvent(new Event("ended")));
    expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled(); expect(tracks[2].stop).toHaveBeenCalledOnce();
  });
  it("late permission on unmount stops tracks and never starts a recorder", async () => {
    let resolve!: (value: MediaStream) => void;
    getUserMedia.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const { unmount } = mount(); down(hold()); unmount();
    await act(async () => resolve(stream()));
    expect(recorders).toHaveLength(0); expect(tracks[0].stop).toHaveBeenCalledOnce();
  });
  it("tap mode changes no permissions and offers explicit preparation, start, finish and cancellation", async () => {
    mount(); fireEvent.click(screen.getByRole("button", { name: "点按录音" }));
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(screen.getByText("点按开始，结束后转文字")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开始录音" })); await screen.findByText("已准备好，请点击开始录音");
    expect(recorders).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "开始录音" }));
    const stop = await screen.findByRole("button", { name: "结束录音" });
    expect(screen.queryByRole("button", { name: "改用长按" })).not.toBeInTheDocument();
    fireEvent.click(stop); await screen.findByText(/转写草稿已就绪/);
    expect(collectionApi.transcribeInspiration).toHaveBeenCalledOnce();
  });
  it("cancelled ASR releases saving and re-recording immediately; late success cannot affect the new job", async () => {
    let resolve!: (value: { content: string; input_mode: "voice"; transcription_status: "draft" }) => void;
    vi.mocked(collectionApi.transcribeInspiration).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const { onDraft } = mount(); const target = hold(); await prepare(target);
    down(target); await screen.findByText("松手转文字"); up(target);
    await screen.findByText("正在转文字…"); fireEvent.click(screen.getByRole("button", { name: "取消转写" }));
    down(target); await screen.findByText("松手转文字"); expect(getUserMedia).toHaveBeenCalledTimes(3);
    await act(async () => resolve({ content: "过期结果", input_mode: "voice", transcription_status: "draft" }));
    expect(onDraft).not.toHaveBeenCalled();
    expect(tracks[2].stop).not.toHaveBeenCalled();
    up(target); await waitFor(() => expect(onDraft).toHaveBeenCalledWith("新灵感"));
  });
  it("release during a second pending acquisition cancels and lets a new generation start", async () => {
    mount(); const target = hold(); await prepare(target);
    let resolve!: (value: MediaStream) => void;
    getUserMedia.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    down(target); up(target);
    down(target, 3); await screen.findByText("松手转文字");
    await act(async () => resolve(stream()));
    expect(recorders).toHaveLength(1); expect(tracks[1].stop).not.toHaveBeenCalled(); expect(tracks[2].stop).toHaveBeenCalledOnce();
    up(target, 100, 3); await screen.findByText(/转写草稿已就绪/);
    expect(collectionApi.transcribeInspiration).toHaveBeenCalledOnce();
  });
  it.each(["pointerCancel", "lostPointerCapture"] as const)("%s only cancels the active pointer", async (event) => {
    mount(); const target = hold(); await prepare(target);
    down(target); await screen.findByText("松手转文字");
    fireEvent[event](target, { pointerId: 2 }); expect(recorders[0].stop).not.toHaveBeenCalled();
    fireEvent[event](target, { pointerId: 1 }); up(target);
    expect(recorders[0].stop).toHaveBeenCalledOnce(); expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled();
  });
  it("ignores a secondary or non-left pointer, and duplicate starts cannot create another job", async () => {
    mount(); const target = hold();
    fireEvent.pointerDown(target, { pointerId: 2, isPrimary: false });
    fireEvent.pointerDown(target, { pointerId: 1, isPrimary: true, button: 2 });
    expect(getUserMedia).not.toHaveBeenCalled();
    down(target); down(target, 2); await screen.findByText("已准备好，请按住说话");
    expect(getUserMedia).toHaveBeenCalledOnce();
    down(target); expect(getUserMedia).toHaveBeenCalledOnce();
    up(target); down(target); await screen.findByText("松手转文字");
    expect(getUserMedia).toHaveBeenCalledTimes(2);
  });
  it("120 seconds stops and transcribes exactly once without broadcasting timer changes", async () => {
    mount(); const target = hold(); await prepare(target);
    vi.useFakeTimers();
    await act(async () => { down(target); });
    await act(async () => { vi.advanceTimersByTime(120_000); });
    up(target);
    expect(recorders[0].stop).toHaveBeenCalledOnce(); expect(collectionApi.transcribeInspiration).toHaveBeenCalledOnce();
  });
  it.each(["audio/mpeg", ""])("rejects unsupported or absent final MIME %s locally", async (mime) => {
    const { onDraft } = mount(); const target = hold(); await prepare(target);
    down(target); await screen.findByText("松手转文字"); recorders[0].mimeType = mime; up(target);
    expect(screen.getByText(/当前录音格式不受支持/)).toBeInTheDocument();
    expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled(); expect(onDraft).not.toHaveBeenCalled(); expect(tracks[1].stop).toHaveBeenCalledOnce();
  });
  it("cleans size overflow while recording and rejects empty audio or empty recognition", async () => {
    const { onDraft } = mount(); const target = hold(); await prepare(target);
    down(target); await screen.findByText("松手转文字");
    act(() => recorders[0].ondataavailable?.({ data: new Blob([new Uint8Array(10 * 1024 * 1024 + 1)]) } as BlobEvent));
    expect(screen.getByText(/超过 10 MiB/)).toBeInTheDocument(); expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled();
    down(target); await screen.findByText("松手转文字");
    recorders[1].stop.mockImplementation(() => { recorders[1].state = "inactive"; recorders[1].onstop?.(); }); up(target);
    expect(screen.getByText(/没有录到可用声音/)).toBeInTheDocument(); expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled();
    vi.mocked(collectionApi.transcribeInspiration).mockResolvedValueOnce({ content: "  ", input_mode: "voice", transcription_status: "draft" });
    down(target); await screen.findByText("松手转文字"); up(target); await screen.findByText(/没有识别到可用文字/);
    expect(onDraft).not.toHaveBeenCalled();
  });
  it("foreground interruption cancels capture, while ordinary control blur leaves capture and frozen ASR intact", async () => {
    mount(); const target = hold(); await prepare(target);
    down(target); await screen.findByText("松手转文字"); fireEvent.blur(target);
    expect(recorders[0].stop).not.toHaveBeenCalled();
    fireEvent(window, new Event("blur")); expect(recorders[0].stop).toHaveBeenCalledOnce();
    expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled();
    down(target); await screen.findByText("已准备好，请按住说话"); up(target);
    down(target); await screen.findByText("松手转文字"); up(target); fireEvent.blur(target);
    await screen.findByText(/转写草稿已就绪/); expect(collectionApi.transcribeInspiration).toHaveBeenCalledOnce();
  });
  it("permission revocation cancels capture and requires preparation again", async () => {
    const status = Object.assign(new EventTarget(), { state: "granted" });
    Object.defineProperty(navigator, "permissions", { configurable: true, value: { query: vi.fn().mockResolvedValue(status) } });
    mount(); const target = hold(); await prepare(target);
    down(target); await screen.findByText("松手转文字");
    act(() => { status.state = "denied"; status.dispatchEvent(new Event("change")); });
    expect(collectionApi.transcribeInspiration).not.toHaveBeenCalled(); expect(tracks[1].stop).toHaveBeenCalledOnce();
    down(target); await screen.findByText("已准备好，请按住说话"); expect(recorders).toHaveLength(1);
  });
  it("identity changes discard pending success and failure; transcribing unmount aborts its signal", async () => {
    let resolve!: (value: { content: string; input_mode: "voice"; transcription_status: "draft" }) => void;
    vi.mocked(collectionApi.transcribeInspiration).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const view = mount(); const target = hold(); await prepare(target);
    down(target); await screen.findByText("松手转文字"); up(target); await screen.findByText("正在转文字…");
    const signal = vi.mocked(collectionApi.transcribeInspiration).mock.calls[0][1];
    view.rerender(<VoiceCapture disabled={false} identity="preview-b" onDraft={view.onDraft} onStateChange={view.onStateChange} registerCancel={() => {}} />);
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve({ content: "不属于当前预览", input_mode: "voice", transcription_status: "draft" }));
    expect(view.onDraft).not.toHaveBeenCalled();
    await prepare(target);
    vi.mocked(collectionApi.transcribeInspiration).mockImplementationOnce(() => new Promise(() => {}));
    down(target); await screen.findByText("松手转文字"); up(target); await screen.findByText("正在转文字…");
    const newSignal = vi.mocked(collectionApi.transcribeInspiration).mock.calls[1][1]; view.unmount();
    expect(newSignal?.aborted).toBe(true);
  });
  it("timeout cleans and aborts once without accepting a delayed result", async () => {
    let resolve!: (value: { content: string; input_mode: "voice"; transcription_status: "draft" }) => void;
    vi.mocked(collectionApi.transcribeInspiration).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const { onDraft } = mount(); const target = hold(); await prepare(target);
    down(target); await screen.findByText("松手转文字");
    vi.useFakeTimers(); await act(async () => up(target));
    await act(async () => { vi.advanceTimersByTime(120_000); });
    expect(screen.getByText(/语音转写超时/)).toBeInTheDocument();
    expect(vi.mocked(collectionApi.transcribeInspiration).mock.calls[0][1]?.aborted).toBe(true);
    await act(async () => resolve({ content: "迟到", input_mode: "voice", transcription_status: "draft" }));
    expect(onDraft).not.toHaveBeenCalled(); expect(collectionApi.transcribeInspiration).toHaveBeenCalledOnce();
  });
  it("unsupported or insecure phones retain the local text fallback without permission requests", () => {
    vi.stubGlobal("isSecureContext", false); mount();
    expect(screen.getByText(/当前浏览器或连接不支持录音/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument(); expect(getUserMedia).not.toHaveBeenCalled();
  });
});
