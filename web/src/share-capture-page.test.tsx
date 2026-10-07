import { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ThemeProvider } from './theme';
import { ShareCapturePage } from './share-capture-page';
import type { ShareSession } from './share-bootstrap';
import { validateShareDraft } from './share-draft';

// jsdom cannot resolve viewport clamp() font sizes during role queries. CSS is
// checked in the real-browser source-matched matrix, not simulated here.
vi.mock('./share-capture-page.css', () => ({}));

function readySession(text = '灵感原文') {
  const listeners = new Set<() => void>();
  let snapshot: ReturnType<ShareSession['getSnapshot']> = {
    status: 'ready', fields: { shared_url: 'https://example.com/cafe', shared_title: '', shared_text: text },
  };
  return {
    getSnapshot: () => snapshot,
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    end: vi.fn(() => { snapshot = { status: 'unavailable', fields: null }; listeners.forEach((listener) => listener()); }),
  } satisfies ShareSession;
}

function mount(session = readySession()) {
  const onSubmit = vi.fn();
  const onLeave = vi.fn();
  let guard: ((leave: () => void) => void) | null = null;
  render(<StrictMode><ThemeProvider><ShareCapturePage session={session} onSubmit={onSubmit} onLeave={onLeave}
    registerLeaveGuard={(value) => { guard = value; }} /></ThemeProvider></StrictMode>);
  return { session, onSubmit, onLeave, requestLeave: (leave: () => void) => guard?.(leave) };
}

beforeEach(() => {
  localStorage.clear();
  window.scrollTo = vi.fn();
  window.scrollBy = vi.fn();
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('locked share receipt behavior', () => {
  it('grows textarea height on viewport reflow without editing or losing content', () => {
    let height = 120;
    vi.spyOn(HTMLTextAreaElement.prototype, 'scrollHeight', 'get').mockImplementation(() => height);
    mount();
    fireEvent.click(screen.getByRole('button', { name: '分享附带文本（未验证）' }));
    const text = screen.getByRole('textbox', { name: '分享附带文本（未验证）' });
    expect(text.style.height).toBe('120px');
    height = 240;
    fireEvent(window, new Event('resize'));
    expect(text.style.height).toBe('240px');
    expect(text).toHaveValue('灵感原文');
  });

  it('cancels delayed viewport compensation after fresh user input', () => {
    vi.useFakeTimers();
    mount();
    const url = screen.getByRole('textbox', { name: '公开链接' });
    act(() => url.focus());
    vi.spyOn(url, 'getBoundingClientRect').mockReturnValue({ top: 1100, bottom: 1200 } as DOMRect);
    fireEvent(window, new Event('resize'));
    fireEvent.pointerDown(document.body);
    act(() => vi.advanceTimersByTime(170));
    expect(window.scrollBy).not.toHaveBeenCalled();
  });
  it('shows unverified content collapsed and does not request anything before consent', async () => {
    const fetch = vi.spyOn(window, 'fetch');
    const user = userEvent.setup();
    const result = mount();
    expect(await screen.findByRole('heading', { name: '要把这条链接收进来吗？' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '分享附带文本（未验证）' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '分享附带文本（未验证）' }));
    const text = screen.getByRole('textbox', { name: '分享附带文本（未验证）' });
    expect(text).toHaveValue('灵感原文');
    await user.clear(text);
    await user.type(text, '保留修改');
    await user.click(screen.getByRole('button', { name: '分享附带文本（未验证）' }));
    await user.click(screen.getByRole('button', { name: '分享附带文本（未验证）' }));
    expect(screen.getByRole('textbox', { name: '分享附带文本（未验证）' })).toHaveValue('保留修改');
    expect(fetch).not.toHaveBeenCalled();
    expect(result.onSubmit).not.toHaveBeenCalled();
    fetch.mockRestore();
  });

  it('revalidates before submit, reveals a collapsed text error and never hands off invalid content', async () => {
    const user = userEvent.setup();
    const result = mount(readySession('https://other.example/second'));
    await user.click(await screen.findByRole('button', { name: '收进来' }));
    const invalid = document.querySelector<HTMLElement>('[aria-invalid="true"]');
    expect(invalid).not.toBeNull();
    expect(invalid).toHaveFocus();
    expect(result.onSubmit).not.toHaveBeenCalled();
  });

  it('freezes one valid handoff and ignores duplicate activation', async () => {
    const result = mount();
    const submit = await screen.findByRole('button', { name: '收进来' });
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(result.onSubmit).toHaveBeenCalledTimes(1);
    const expected = validateShareDraft({ url: 'https://example.com/cafe', text: '灵感原文' });
    expect(expected.ok).toBe(true);
    if (expected.ok) expect(result.onSubmit).toHaveBeenCalledWith(expected.inputText);
  });

  it('only guards actual edits, restores exact trigger and executes the saved leave once', async () => {
    const user = userEvent.setup();
    const result = mount();
    const url = await screen.findByRole('textbox', { name: '公开链接' });
    await user.type(url, '/edited');
    const abandon = screen.getByRole('button', { name: '放弃' });
    await user.click(abandon);
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '继续编辑' })).toHaveFocus();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(abandon).toHaveFocus();
    expect(url).toHaveValue('https://example.com/cafe/edited');
    const target = vi.fn();
    act(() => result.requestLeave(target));
    await user.click(await screen.findByRole('button', { name: '确认离开' }));
    expect(target).toHaveBeenCalledTimes(1);
    expect(result.onLeave).not.toHaveBeenCalled();
    expect(result.session.end).toHaveBeenCalledTimes(1);
  });

  it('reverted content is pristine; disclosure alone never requires confirmation', async () => {
    const user = userEvent.setup();
    const result = mount();
    const url = await screen.findByRole('textbox', { name: '公开链接' });
    fireEvent.change(url, { target: { value: 'changed' } });
    fireEvent.change(url, { target: { value: 'https://example.com/cafe' } });
    await user.click(screen.getByRole('button', { name: '分享附带文本（未验证）' }));
    await user.click(screen.getByRole('button', { name: '放弃' }));
    expect(result.onLeave).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('real pagehide clears fields, whereas visibility changes retain them', async () => {
    const result = mount();
    const url = await screen.findByRole('textbox', { name: '公开链接' });
    fireEvent.change(url, { target: { value: 'https://example.com/edited' } });
    fireEvent(document, new Event('visibilitychange'));
    expect(url).toHaveValue('https://example.com/edited');
    fireEvent(window, new Event('pagehide'));
    await screen.findByRole('button', { name: '回到素材库手动粘贴' });
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(result.onSubmit).not.toHaveBeenCalled();
  });
});
