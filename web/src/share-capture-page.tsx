import { CaretDown, ChatText, Link as LinkIcon, LockSimple, ShieldCheck } from '@phosphor-icons/react';
import { Dialog } from '@radix-ui/themes';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type CSSProperties } from 'react';
import type { ShareSession } from './share-bootstrap';
import { draftFromFields, validateShareDraft, type ShareDraft } from './share-draft';
import './share-capture-page.css';

type Validation = ReturnType<typeof validateShareDraft>;
type Issue = Extract<Validation, { ok: false }>;
type LeaveGuard = (leave: () => void) => void;
interface Props {
  session: ShareSession;
  onSubmit: (inputText: string) => void;
  onLeave: () => void;
  registerLeaveGuard: (guard: LeaveGuard | null) => void;
}

const unavailableMessage = '这次分享已失效或无法在本机打开。请重新分享，或回到素材库手动粘贴。';

function reveal(element: HTMLElement | null) {
  if (!element?.isConnected) return;
  const viewport = window.visualViewport;
  const top = viewport?.offsetTop ?? 0;
  const bottom = top + (viewport?.height ?? window.innerHeight);
  const bounds = element.getBoundingClientRect();
  const delta = bounds.top < top + 12 ? bounds.top - top - 12
    : bounds.bottom > bottom - 12 ? Math.min(bounds.top - top - 12, bounds.bottom - bottom + 12) : 0;
  if (delta) window.scrollBy({ top: delta, behavior: 'auto' });
}

function isDirty(current: ShareDraft | null, initial: ShareDraft | null) {
  return Boolean(current && initial && (current.url !== initial.url || current.text !== initial.text));
}

export function ShareCapturePage({ session, onSubmit, onLeave, registerLeaveGuard }: Props) {
  const snapshot = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [draft, setDraft] = useState<ShareDraft | null>(() => snapshot.fields ? draftFromFields(snapshot.fields) : null);
  const draftRef = useRef(draft);
  const initialRef = useRef(draft);
  const initialized = useRef(Boolean(draft));
  const [expanded, setExpanded] = useState(false);
  const [issue, setIssue] = useState<Issue | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [leaveOpen, setLeaveOpen] = useState(false);
  const pendingLeave = useRef<(() => void) | null>(null);
  const leaveOrigin = useRef<HTMLElement | null>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const urlRef = useRef<HTMLTextAreaElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const continueRef = useRef<HTMLButtonElement>(null);
  const pageRef = useRef<HTMLElement>(null);
  const userInteracted = useRef(false);
  const scrollEpoch = useRef(0);
  const interactionEpoch = useRef(0);
  const titleFocused = useRef(false);
  const draftGeneration = useRef(0);
  const pendingErrorFocus = useRef<'url' | 'text' | null>(null);
  const seenAnnouncements = useRef(new Set<string>());
  const [polite, setPolite] = useState('');
  const [assertive, setAssertive] = useState<{ sequence: number; text: string }>({ sequence: 0, text: '' });
  const [viewport, setViewport] = useState(() => ({ top: window.visualViewport?.offsetTop ?? 0, height: window.visualViewport?.height ?? window.innerHeight }));

  const clearDraft = useCallback(() => {
    draftRef.current = null;
    initialRef.current = null;
    draftGeneration.current++;
    pendingErrorFocus.current = null;
    pendingLeave.current = null;
    if (urlRef.current) urlRef.current.value = '';
    if (textRef.current) textRef.current.value = '';
    setDraft(null);
    setIssue(null);
    setLeaveOpen(false);
  }, []);

  useEffect(() => {
    if (snapshot.status === 'ready' && snapshot.fields && !initialized.current) {
      const initial = draftFromFields(snapshot.fields);
      initialized.current = true;
      draftRef.current = initial;
      initialRef.current = initial;
      setDraft(initial);
      const result = validateShareDraft(initial);
      setIssue(result.ok ? null : result);
    } else if (snapshot.status === 'unavailable') clearDraft();
  }, [clearDraft, snapshot]);

  useEffect(() => {
    if (!draftRef.current || snapshot.status !== 'ready') return;
    const result = validateShareDraft(draftRef.current);
    setIssue(result.ok ? null : result);
    // This is the initial local check, not per-keystroke validation.
  }, [session]);

  useEffect(() => {
    const interacted = () => { userInteracted.current = true; interactionEpoch.current++; };
    const scrolled = () => { scrollEpoch.current++; userInteracted.current = true; };
    window.addEventListener('pointerdown', interacted, true);
    window.addEventListener('keydown', interacted, true);
    window.addEventListener('touchstart', interacted, true);
    window.addEventListener('focusin', interacted, true);
    window.addEventListener('scroll', scrolled, true);
    return () => {
      window.removeEventListener('pointerdown', interacted, true);
      window.removeEventListener('keydown', interacted, true);
      window.removeEventListener('touchstart', interacted, true);
      window.removeEventListener('focusin', interacted, true);
      window.removeEventListener('scroll', scrolled, true);
    };
  }, []);

  useEffect(() => {
    if (snapshot.status === 'loading' || titleFocused.current || userInteracted.current) return;
    let timer: ReturnType<typeof setTimeout>;
    const generation = draftGeneration.current;
    const schedule = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (userInteracted.current || titleFocused.current || generation !== draftGeneration.current) return;
        titleFocused.current = true;
        titleRef.current?.focus({ preventScroll: true });
        reveal(titleRef.current);
      }, 160);
    };
    schedule();
    window.visualViewport?.addEventListener('resize', schedule);
    return () => { clearTimeout(timer); window.visualViewport?.removeEventListener('resize', schedule); };
  }, [snapshot.status]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const update = () => {
      setViewport({ top: window.visualViewport?.offsetTop ?? 0, height: window.visualViewport?.height ?? window.innerHeight });
      clearTimeout(timer);
      const epoch = scrollEpoch.current;
      const interaction = interactionEpoch.current;
      const generation = draftGeneration.current;
      timer = setTimeout(() => {
        const element = document.activeElement as HTMLElement | null;
        if (epoch === scrollEpoch.current && interaction === interactionEpoch.current && generation === draftGeneration.current && pageRef.current?.contains(element)) reveal(element);
      }, 160);
    };
    window.visualViewport?.addEventListener('resize', update);
    window.visualViewport?.addEventListener('scroll', update);
    window.addEventListener('resize', update);
    let observedWidth = pageRef.current?.clientWidth;
    const observer = new ResizeObserver(() => {
      const width = pageRef.current?.clientWidth;
      if (width !== observedWidth) { observedWidth = width; update(); }
    });
    if (pageRef.current) observer.observe(pageRef.current);
    return () => {
      clearTimeout(timer);
      window.visualViewport?.removeEventListener('resize', update);
      window.visualViewport?.removeEventListener('scroll', update);
      window.removeEventListener('resize', update);
      observer.disconnect();
    };
  }, []);

  useEffect(() => {
    const hide = () => { clearDraft(); session.end(); };
    const unload = (event: BeforeUnloadEvent) => {
      if (!isDirty(draftRef.current, initialRef.current)) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('pagehide', hide);
    window.addEventListener('beforeunload', unload);
    return () => { window.removeEventListener('pagehide', hide); window.removeEventListener('beforeunload', unload); };
  }, [clearDraft, session]);

  useEffect(() => {
    if (snapshot.status === 'loading') return;
    const key = snapshot.status === 'unavailable' ? 'unavailable' : issue ? `error:${draftGeneration.current}:${issue.code}` : 'ready';
    if (seenAnnouncements.current.has(key)) return;
    seenAnnouncements.current.add(key);
    if (snapshot.status === 'unavailable' || issue) {
      setAssertive((previous) => ({ sequence: previous.sequence + 1, text: issue?.message ?? unavailableMessage }));
    } else setPolite('分享已在本机打开，尚未读取来源信息。');
  }, [issue, snapshot.status]);

  useLayoutEffect(() => {
    for (const element of [urlRef.current, textRef.current]) {
      if (!element) continue;
      element.style.height = 'auto';
      if (element.scrollHeight > 0) element.style.height = `${element.scrollHeight}px`;
    }
    if (!pendingErrorFocus.current || snapshot.status !== 'ready' || !issue) return;
    const element = pendingErrorFocus.current === 'text' ? textRef.current : urlRef.current;
    pendingErrorFocus.current = null;
    element?.focus({ preventScroll: true });
    reveal(element);
  }, [draft, expanded, issue, snapshot.status, viewport]);

  const leave = useCallback((action: () => void) => {
    if (busyRef.current || pendingLeave.current) return;
    if (!isDirty(draftRef.current, initialRef.current)) {
      clearDraft();
      session.end();
      action();
      return;
    }
    leaveOrigin.current = document.activeElement instanceof HTMLElement ? document.activeElement : titleRef.current;
    pendingLeave.current = action;
    setLeaveOpen(true);
  }, [clearDraft, session]);

  useEffect(() => {
    registerLeaveGuard(leave);
    return () => registerLeaveGuard(null);
  }, [leave, registerLeaveGuard]);

  const edit = (field: 'url' | 'text', value: string) => {
    if (!draftRef.current || busyRef.current) return;
    const next = { ...draftRef.current, [field]: value };
    draftRef.current = next;
    draftGeneration.current++;
    setDraft(next);
    setIssue(null);
  };
  const validate = () => {
    if (!draftRef.current || busyRef.current) return;
    const result = validateShareDraft(draftRef.current);
    setIssue(result.ok ? null : result);
  };
  const submit = () => {
    if (busyRef.current || !draftRef.current || session.getSnapshot().status !== 'ready') return;
    const result = validateShareDraft(draftRef.current);
    if (!result.ok) {
      pendingErrorFocus.current = result.field;
      if (result.field === 'text') setExpanded(true);
      setIssue({ ...result });
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setPolite('正在进入整理页…');
    try {
      onSubmit(result.inputText);
      clearDraft();
    } catch {
      busyRef.current = false;
      setBusy(false);
      setIssue({ ok: false, code: 'HANDOFF_FAILED', field: 'url', message: '暂时无法进入整理页，内容仍保留在本页，请再次点击“收进来”。' });
    }
  };
  const cancelLeave = () => { pendingLeave.current = null; setLeaveOpen(false); };
  const confirmLeave = () => {
    const action = pendingLeave.current;
    if (!action) return;
    clearDraft();
    session.end();
    action();
  };
  const ready = snapshot.status === 'ready' && draft;

  return <>
    <main className="collection-page share-capture-page" ref={pageRef} inert={leaveOpen} aria-busy={snapshot.status === 'loading' || busy}>
      <div className="share-brand-stage">
        <div className="collection-wordmark">瞬时录</div>
        <p className="share-brand-promise">把看到的，<br />变成以后用得上的</p>
      </div>
      <section className="share-confirmation" aria-labelledby="share-title">
        <div className="share-static-divider" aria-hidden="true" />
        <p className="share-origin">系统分享</p>
        <h1 id="share-title" ref={titleRef} tabIndex={-1}>{snapshot.status === 'unavailable' ? '这次分享无法打开' : '要把这条链接收进来吗？'}</h1>
        {snapshot.status === 'loading' && <p className="share-local-state">正在本机打开分享内容，尚未读取来源信息…</p>}
        {ready && <>
          <label className="share-link-field">
            <LinkIcon size={23} aria-hidden="true" />
            <span className="share-sr-only">公开链接</span>
            <textarea ref={urlRef} rows={1} aria-label="公开链接" value={draft.url} readOnly={busy} spellCheck={false}
              autoCapitalize="off" autoCorrect="off" aria-invalid={issue?.field === 'url' || undefined}
              aria-describedby={issue?.field === 'url' ? 'share-field-error' : undefined}
              onChange={(event) => edit('url', event.target.value)} onBlur={validate} />
          </label>
          <div className="share-attached">
            <button type="button" className="share-disclosure" aria-expanded={expanded} aria-controls="share-attached-editor"
              disabled={busy} onClick={() => setExpanded((value) => !value)}>
              <ChatText size={22} aria-hidden="true" /><span>分享附带文本（未验证）</span><CaretDown size={20} aria-hidden="true" />
            </button>
            {expanded && <textarea id="share-attached-editor" ref={textRef} rows={4} aria-label="分享附带文本（未验证）"
              value={draft.text} readOnly={busy} aria-invalid={issue?.field === 'text' || undefined}
              aria-describedby={issue?.field === 'text' ? 'share-field-error' : undefined}
              onChange={(event) => edit('text', event.target.value)} onBlur={validate} />}
          </div>
          {issue && <p className="share-error" id="share-field-error">{issue.message}</p>}
          <div className="share-boundaries">
            <p><ShieldCheck size={19} aria-hidden="true" />仅在此设备短暂保留</p>
            <p><LockSimple size={19} aria-hidden="true" />点击后才会读取来源信息</p>
          </div>
          {busy && <p className="share-local-state">正在进入整理页，暂时不能再次提交。</p>}
          <div className="share-actions">
            <button type="button" className="share-primary" disabled={busy} onClick={submit}>收进来</button>
            <button type="button" disabled={busy} onClick={() => leave(onLeave)}>放弃</button>
          </div>
        </>}
        {snapshot.status === 'unavailable' && <>
          <p className="share-local-state">{unavailableMessage}</p>
          <div className="share-actions"><button type="button" className="share-primary" onClick={() => leave(onLeave)}>回到素材库手动粘贴</button></div>
        </>}
      </section>
    </main>
    <div className="share-sr-only" role="status" aria-live="polite" aria-atomic="true">{polite}</div>
    <div className="share-sr-only" role="alert" aria-atomic="true"><span key={assertive.sequence}>{assertive.text}</span></div>
    <Dialog.Root open={leaveOpen} onOpenChange={(open) => { if (!open) cancelLeave(); }}>
      <Dialog.Content className="collection-page share-leave-dialog" maxWidth="430px"
        style={{ '--share-vv-top': `${viewport.top}px`, '--share-vv-height': `${viewport.height}px` } as CSSProperties}
        onPointerDownOutside={(event) => event.preventDefault()} onInteractOutside={(event) => event.preventDefault()}
        onOpenAutoFocus={(event) => { event.preventDefault(); continueRef.current?.focus({ preventScroll: true }); }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          if (!draftRef.current) return;
          const origin = leaveOrigin.current;
          const target = origin?.isConnected && !origin.closest('[inert]') ? origin : titleRef.current;
          target?.focus({ preventScroll: true });
          reveal(target);
        }}>
        <Dialog.Title>放弃这次修改吗？</Dialog.Title>
        <Dialog.Description>修改只保留在当前页面。离开后需要重新分享或手动粘贴。</Dialog.Description>
        <div className="share-actions">
          <button ref={continueRef} type="button" className="share-primary" onClick={cancelLeave}>继续编辑</button>
          <button type="button" onClick={confirmLeave}>确认离开</button>
        </div>
      </Dialog.Content>
    </Dialog.Root>
  </>;
}
