import { Microphone, Stop } from "@phosphor-icons/react";
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type PointerEvent } from "react";
import { collectionApi } from "./collection-api";

export type VoiceState = "idle" | "permission" | "recording" | "stopping" | "transcribing" | "draft" | "error";
const allowedTypes = new Set(["audio/webm", "audio/ogg", "audio/mp4", "audio/wav"]);
const maxBytes = 10 * 1024 * 1024;
type Session = {
  generation: number; identity: string; cancelled: boolean; waiting: boolean;
  phase: VoiceState; stream?: MediaStream; recorder?: MediaRecorder;
  chunks: Blob[]; bytes: number; timer?: number; limit?: number;
  controller?: AbortController; detachTracks?: () => void;
};
type Press = { pointerId: number; generation: number; target: HTMLButtonElement; inside: boolean };

export function VoiceCapture({ onDraft, disabled, onStateChange, registerCancel, identity, hasDraft }: {
  onDraft: (value: string) => void; disabled: boolean; identity: string;
  hasDraft?: boolean;
  onStateChange: (state: VoiceState) => void; registerCancel: (cancel: () => void) => void;
}) {
  const [state, setState] = useState<VoiceState>("idle");
  const [message, setMessage] = useState("松手转文字，上滑取消");
  const [seconds, setSeconds] = useState(0);
  const [tap, setTap] = useState(false);
  const [outside, setOutside] = useState(false);
  const statusId = useId();
  const groupRef = useRef<HTMLDivElement>(null);
  const focusFrame = useRef<number | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const tapActionRef = useRef<HTMLButtonElement>(null);
  const cancelActionRef = useRef<HTMLButtonElement | null>(null);
  const restoreRemovedActionFocus = useRef(false);
  const suppressFocusScroll = useRef(false);
  const restoreTapFocus = useRef(false);
  const live = useRef(true);
  const latest = useRef({ onDraft, onStateChange, disabled, identity });
  latest.current = { onDraft, onStateChange, disabled, identity };
  const session = useRef<Session | null>(null);
  const generation = useRef(0);
  const press = useRef<Press | null>(null);
  const prepared = useRef(false);
  const permission = useRef<PermissionStatus | null>(null);
  const available = globalThis.isSecureContext !== false && Boolean(navigator.mediaDevices?.getUserMedia) && typeof MediaRecorder !== "undefined";

  const captureCancelAction = useCallback((node: HTMLButtonElement | null) => {
    // Ref detachment happens before DOM removal, while ownership of focus is
    // still observable. A control the user already left must not reclaim it.
    if (!node && cancelActionRef.current === document.activeElement) restoreRemovedActionFocus.current = true;
    cancelActionRef.current = node;
  }, []);
  useLayoutEffect(() => {
    if (!restoreRemovedActionFocus.current || cancelActionRef.current) return;
    restoreRemovedActionFocus.current = false;
    const target = tap ? tapActionRef.current : buttonRef.current;
    if (!target || target.disabled || document.activeElement !== document.body) return;
    suppressFocusScroll.current = true;
    try { target.focus({ preventScroll: true }); }
    finally { suppressFocusScroll.current = false; }
  }, [state, tap]);

  const publish = useCallback((next: VoiceState, text: string) => {
    if (!live.current) return;
    setState(next); setMessage(text); latest.current.onStateChange(next);
  }, []);
  useEffect(() => {
    if (hasDraft === false && state === "draft") publish("idle", "松手转文字，上滑取消");
  }, [hasDraft, state, publish]);
  const current = useCallback((job: Session) => live.current && session.current === job && !job.cancelled && latest.current.identity === job.identity, []);
  const release = useCallback((job: Session) => {
    if (job.timer !== undefined) window.clearInterval(job.timer);
    if (job.limit !== undefined) window.clearTimeout(job.limit);
    job.timer = undefined; job.limit = undefined;
    job.detachTracks?.(); job.detachTracks = undefined;
    const recorder = job.recorder;
    job.recorder = undefined;
    if (recorder) {
      recorder.onstop = null; recorder.ondataavailable = null; recorder.onerror = null;
      if (recorder.state !== "inactive") { try { recorder.stop(); } catch { /* Already stopped by the device. */ } }
    }
    job.stream?.getTracks().forEach((track) => track.stop()); job.stream = undefined;
    job.chunks = []; job.bytes = 0;
  }, []);
  const finish = useCallback((job: Session, next: VoiceState, text: string) => {
    release(job);
    if (session.current !== job) return;
    session.current = null; press.current = null;
    if (live.current && latest.current.identity === job.identity) { setOutside(false); publish(next, text); }
  }, [publish, release]);
  const cancel = useCallback((text = "已取消录音，文字和已有草稿已保留。") => {
    const job = session.current;
    press.current = null;
    if (!job) return;
    job.cancelled = true; job.controller?.abort(); release(job);
    if (live.current) setOutside(false);
    // Each job owns its resources. Late settlement cleans that job only, while
    // cancellation immediately returns text saving and new activation to the user.
    finish(job, "idle", text);
  }, [finish, publish, release]);

  useEffect(() => {
    live.current = true;
    return () => { live.current = false; cancel(); if (focusFrame.current !== null) cancelAnimationFrame(focusFrame.current); };
  }, [cancel]);
  useEffect(() => { registerCancel(() => cancel()); }, [cancel, registerCancel]);
  useEffect(() => {
    prepared.current = false;
    restoreTapFocus.current = false;
    cancel();
    // Review identity changes invalidate all callbacks, even while mounted.
    publish("idle", "松手转文字，上滑取消");
  }, [identity, cancel, publish]);
  useEffect(() => {
    const target = tapActionRef.current;
    if (!restoreTapFocus.current || !tap || !target || target.disabled) return;
    restoreTapFocus.current = false;
    // Only hand over a removed keyboard/AT trigger. A user who already moved
    // focus elsewhere while permission was pending keeps that chosen focus.
    if (document.activeElement === document.body || document.activeElement === target) {
      target.focus({ preventScroll: true });
    }
  }, [state, tap, disabled]);
  useEffect(() => {
    const interrupt = () => {
      const job = session.current;
      if (job && ["permission", "recording", "stopping"].includes(job.phase)) {
        prepared.current = false;
        cancel("录音已中断，文字和已有草稿已保留。请重新开始。");
      }
    };
    const visibility = () => { if (document.visibilityState === "hidden") interrupt(); };
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("blur", interrupt);
    window.addEventListener("pagehide", interrupt);
    return () => {
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("blur", interrupt);
      window.removeEventListener("pagehide", interrupt);
    };
  }, [cancel]);
  useEffect(() => {
    const revoked = () => {
      if (permission.current?.state !== "granted") {
        prepared.current = false;
        cancel("麦克风权限已变更，文字和已有草稿已保留。请重新准备。");
      }
    };
    let disposed = false;
    if (navigator.permissions?.query) {
      void navigator.permissions.query({ name: "microphone" as PermissionName }).then((result) => {
        if (disposed) return;
        permission.current = result; result.addEventListener("change", revoked);
      }).catch(() => { /* Recording support does not require the Permissions API. */ });
    }
    return () => { disposed = true; permission.current?.removeEventListener("change", revoked); permission.current = null; };
  }, [cancel]);

  const transcribe = async (job: Session, blob: Blob) => {
    if (!current(job)) return;
    const mime = blob.type.split(";", 1)[0].trim().toLowerCase();
    if (!blob.size || blob.size > maxBytes || !allowedTypes.has(mime)) {
      finish(job, "error", !blob.size ? "没有录到可用声音，请重新录制。" : blob.size > maxBytes ? "本次录音超过 10 MiB，已清理且不会上传。" : "当前录音格式不受支持，已清理且不会上传。");
      return;
    }
    release(job);
    job.phase = "transcribing"; job.waiting = true; job.controller = new AbortController();
    publish("transcribing", "正在转文字…");
    // The backend bounds conversion/provider work; this also releases the UI
    // when the transport never settles. It never retries retained audio.
    job.limit = window.setTimeout(() => {
      if (!current(job)) return;
      job.cancelled = true; job.controller?.abort();
      finish(job, "error", "语音转写超时，文字和已有草稿已保留。请重新录制或使用文字。");
    }, 120_000);
    try {
      const result = await collectionApi.transcribeInspiration(blob, job.controller.signal);
      if (!current(job)) return;
      if (!result.content.trim()) { finish(job, "error", "没有识别到可用文字，请重新录制。"); return; }
      latest.current.onDraft(result.content);
      finish(job, "draft", "转写草稿已就绪，请确认追加或替换后再保存。");
    } catch (error) {
      if (current(job)) finish(job, "error", error instanceof Error ? error.message : "语音转写失败，文字输入仍可用。请重新录制。");
    } finally {
      job.waiting = false; job.controller = undefined;
      if (job.cancelled) finish(job, "idle", "已取消转写，文字和已有草稿已保留。");
    }
  };
  const stop = (job = session.current) => {
    if (!job || !current(job) || job.phase !== "recording" || !job.recorder) return;
    job.phase = "stopping"; press.current = null; setOutside(false);
    publish("stopping", "正在停止录音…");
    if (job.timer !== undefined) window.clearInterval(job.timer);
    if (job.limit !== undefined) window.clearTimeout(job.limit);
    try { job.recorder.stop(); } catch { finish(job, "error", "录音未能完成，已清理。请重新录制。"); }
  };
  const start = async (mode: "tap" | "hold", held?: Omit<Press, "generation">) => {
    if (latest.current.disabled || session.current || !available) return;
    const job: Session = { generation: ++generation.current, identity: latest.current.identity, cancelled: false, waiting: true, phase: "permission", chunks: [], bytes: 0 };
    session.current = job;
    if (held) press.current = { ...held, generation: job.generation };
    const prepareOnly = !prepared.current || (permission.current !== null && permission.current.state !== "granted");
    publish("permission", prepareOnly ? "请允许使用麦克风" : "正在连接麦克风…");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      job.waiting = false;
      if (!current(job)) { stream.getTracks().forEach((track) => track.stop()); finish(job, "idle", "本次操作已取消，请再次按住说话。"); return; }
      job.stream = stream;
      if (prepareOnly) {
        prepared.current = true;
        const stillHeld = press.current;
        finish(job, "idle", "已准备好，请按住说话");
        // The preparation gesture must end before a second activation can record.
        if (stillHeld) press.current = stillHeld;
        return;
      }
      if (mode === "hold" && (!press.current || press.current.generation !== job.generation)) {
        finish(job, "idle", "未持续按住，已取消本次录音。请再次按住说话。"); return;
      }
      if (stream.getTracks().some((track) => track.readyState === "ended")) { prepared.current = false; finish(job, "error", "麦克风已断开，请重新准备。"); return; }
      const interrupted = () => { prepared.current = false; cancel("录音已中断，文字和已有草稿已保留。请重新开始。"); };
      const tracks = stream.getTracks();
      tracks.forEach((track) => { track.addEventListener?.("ended", interrupted); track.addEventListener?.("mute", interrupted); });
      job.detachTracks = () => tracks.forEach((track) => { track.removeEventListener?.("ended", interrupted); track.removeEventListener?.("mute", interrupted); });
      const recorder = new MediaRecorder(stream); job.recorder = recorder;
      recorder.ondataavailable = (event) => {
        if (!current(job) || !event.data.size) return;
        job.chunks.push(event.data); job.bytes += event.data.size;
        if (job.bytes > maxBytes) finish(job, "error", "本次录音超过 10 MiB，已清理且不会上传。");
        else if (job.bytes === maxBytes && job.phase === "recording") stop(job);
      };
      recorder.onerror = () => { if (current(job)) finish(job, "error", "录音未能完成，已清理。请重新录制。"); };
      recorder.onstop = () => {
        if (!current(job)) return;
        if (job.phase !== "stopping") { interrupted(); return; }
        void transcribe(job, new Blob(job.chunks, { type: recorder.mimeType }));
      };
      // Bounded chunks enforce the memory limit during recording, not only on stop.
      recorder.start(250); job.phase = "recording"; setSeconds(0);
      publish("recording", mode === "hold" ? "松手转文字" : "正在录音，请结束或取消。");
      const started = Date.now();
      job.timer = window.setInterval(() => { if (current(job)) setSeconds(Math.min(120, Math.floor((Date.now() - started) / 1000))); }, 1000);
      job.limit = window.setTimeout(() => stop(job), 120_000);
    } catch {
      job.waiting = false;
      if (current(job)) { prepared.current = false; finish(job, "error", "麦克风权限未开放或录音不可用。你的文字和来源草稿都已保留，请重新录制或使用文字。"); }
      else finish(job, "idle", "本次操作已取消，请重新开始。");
    }
  };
  const inside = (event: PointerEvent<HTMLButtonElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const tolerance = 12;
    return event.clientX >= rect.left - tolerance && event.clientX <= rect.right + tolerance && event.clientY >= rect.top - tolerance && event.clientY <= rect.bottom + tolerance;
  };
  const beginHold = (event: PointerEvent<HTMLButtonElement>) => {
    if (!event.isPrimary || event.button !== 0 || tap || session.current || press.current || disabled) return;
    event.preventDefault();
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch { return; }
    void start("hold", { pointerId: event.pointerId, target: event.currentTarget, inside: true });
  };
  const endHold = (event: PointerEvent<HTMLButtonElement>, cancelled = false) => {
    const held = press.current; const job = session.current;
    if (!held || event.pointerId !== held.pointerId) return;
    if (!job) { press.current = null; return; }
    if (held.generation !== job.generation) return;
    press.current = null;
    // Preparation always disposes its stream and never records, even after release.
    if (job.phase === "permission" && !prepared.current && !cancelled) return;
    if (cancelled || !inside(event) || job.phase === "permission") cancel();
    else stop(job);
  };
  const active = ["permission", "recording", "stopping", "transcribing"].includes(state);
  const displayedMessage = tap && message === "松手转文字，上滑取消" ? "点按开始，结束后转文字"
    : tap && message === "已准备好，请按住说话" ? "已准备好，请点击开始录音" : message;
  return <div ref={groupRef} className="voice-capture" data-voice-state={state} data-voice-mode={tap ? "tap" : "hold"}
    onFocusCapture={(event) => {
      const target = event.target;
      if (suppressFocusScroll.current || !(target instanceof HTMLElement) || !target.matches(":focus-visible")) return;
      if (focusFrame.current !== null) cancelAnimationFrame(focusFrame.current);
      // Native focus scrolling does not account for a fixed Dock or the status
      // below the microphone. Measure once after that user focus has settled.
      focusFrame.current = requestAnimationFrame(() => {
        focusFrame.current = null;
        const group = groupRef.current;
        if (!live.current || !group || document.activeElement !== target || !target.matches(":focus-visible")) return;
        const page = group.closest(".collection-page");
        const dock = page?.querySelector(".save-review-bar")?.getBoundingClientRect();
        if (!dock) return;
        const header = page?.querySelector(".collection-global-header")?.getBoundingClientRect();
        const viewport = window.visualViewport;
        const viewportTop = viewport?.offsetTop ?? 0;
        const viewportBottom = viewportTop + (viewport?.height ?? window.innerHeight);
        const top = Math.max(viewportTop, header && header.top <= viewportTop && header.bottom > viewportTop ? header.bottom : viewportTop) + 8;
        const bottom = Math.min(viewportBottom, dock.bottom > viewportTop ? dock.top : viewportBottom) - 8;
        const room = bottom - top;
        if (room <= 0) return;
        let bounds = group.getBoundingClientRect();
        if (bounds.height > room) {
          const control = target.getBoundingClientRect();
          const status = document.getElementById(statusId)?.getBoundingClientRect();
          const start = Math.min(control.top, status?.top ?? control.top);
          const end = Math.max(control.bottom, status?.bottom ?? control.bottom);
          bounds = end - start <= room ? { ...control, top: start, bottom: end } : control;
        }
        const delta = bounds.bottom > bottom ? bounds.bottom - bottom : bounds.top < top ? bounds.top - top : 0;
        if (Math.abs(delta) > 1) window.scrollBy({ top: delta, left: 0, behavior: "instant" });
      });
    }}>
    {available ? <>
      {!tap && <button ref={buttonRef} type="button" className={`voice-hold-start${state === "recording" ? " is-recording" : ""}${outside ? " is-cancel-armed" : ""}`}
        aria-label="按住说话" aria-describedby={statusId} aria-disabled={disabled || (active && state !== "recording")} disabled={disabled}
        onPointerDown={beginHold} onPointerMove={(event) => {
          const held = press.current;
          if (!held || held.pointerId !== event.pointerId || session.current?.phase !== "recording") return;
          const next = inside(event);
          if (held.inside !== next) { held.inside = next; setOutside(!next); setMessage(next ? "松手转文字" : "松手取消"); }
        }} onPointerUp={(event) => endHold(event)} onPointerCancel={(event) => endHold(event, true)} onLostPointerCapture={(event) => endHold(event, true)}
        onContextMenu={(event) => event.preventDefault()} onClick={(event) => { if (event.detail === 0 && !active && !disabled) { restoreTapFocus.current = true; setTap(true); void start("tap"); } }}>
        <Microphone size={34} aria-hidden />
      </button>}
      {!tap && <strong className="voice-label">{state === "error" ? "重新录制" : "按住说话"}</strong>}
      {tap && <div className="voice-actions">
        {state === "recording" ? <button ref={tapActionRef} type="button" className="voice-tap-action" onClick={() => stop()}><Stop size={18} aria-hidden />结束录音</button>
          : <button ref={tapActionRef} type="button" className="voice-tap-action" disabled={disabled} aria-disabled={disabled || active} onClick={() => { if (!active && !disabled) void start("tap"); }}><Microphone size={18} aria-hidden />开始录音</button>}
      </div>}
      {state === "recording" && <span className="recording-time" aria-live="off">{String(Math.floor(seconds / 60)).padStart(2, "0")}:{String(seconds % 60).padStart(2, "0")}</span>}
      <p id={statusId} role="status">{displayedMessage}</p>
      {active && <button ref={captureCancelAction} type="button" className="voice-secondary" onClick={() => cancel(state === "transcribing" ? "已取消本地转写，正在结束处理；已发送的请求可能继续处理并计费。" : undefined)}>{state === "transcribing" ? "取消转写" : "取消录音"}</button>}
      {!active && <button type="button" className="voice-secondary voice-mode-toggle" disabled={disabled} onClick={() => { setTap(!tap); if (tap) requestAnimationFrame(() => buttonRef.current?.focus({ preventScroll: true })); }}>{tap ? "改用长按" : "点按录音"}</button>}
    </> : <p role="status">当前浏览器或连接不支持录音，请继续使用文字输入。</p>}
  </div>;
}
