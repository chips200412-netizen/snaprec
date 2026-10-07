// @vitest-environment node
import { describe, expect, it, vi } from 'vitest';
import { parseShareIntake, SHARE_RAW_LIMIT } from './share-intake';

const origin = 'https://capture.example';
function form(entries: [string, string | Blob][]) {
  const body = new FormData();
  for (const [name, value] of entries) body.append(name, value);
  return new Request(`${origin}/share-target`, { method: 'POST', body });
}
function raw(body: string | Uint8Array, type = 'multipart/form-data; boundary=boundary') {
  return new Request(`${origin}/share-target`, { method: 'POST', body: body as BodyInit, headers: { 'content-type': type } });
}
const part = (name: string, value: string) => `--boundary\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n--boundary--\r\n`;

describe('bounded share intake', () => {
  it('accepts browser FormData without changing Unicode or nested URL text', async () => {
    const fields = { shared_title: ' 字标😀 ', shared_text: 'e\u0301\r\n https://example.com/?url=https://x.test', shared_url: '' };
    await expect(parseShareIntake(form(Object.entries(fields)))).resolves.toEqual(fields);
  });
  it('allows optional missing fields and zero/multiple URLs for visible correction', async () => {
    await expect(parseShareIntake(form([['shared_text', 'https://a.test https://b.test']]))).resolves.toEqual({ shared_title: '', shared_text: 'https://a.test https://b.test', shared_url: '' });
    await expect(parseShareIntake(form([]))).resolves.toEqual({ shared_title: '', shared_text: '', shared_url: '' });
  });
  it.each([
    [['shared_url', 'a'], ['shared_url', 'b']],
    [['unknown', 'private']],
    [['shared_url', new Blob(['private'])]],
    [['shared_title', 'x'.repeat(10001)]],
    [['shared_title', '😀'.repeat(10000)], ['shared_text', '😀'.repeat(6385)]],
  ] as [string, string | Blob][][])('rejects invalid fields %i', async (...entries) => {
    await expect(parseShareIntake(form(entries))).rejects.toThrow('SHARE_INTAKE_INVALID');
  });
  it('accepts exact per-field codepoint and combined UTF-8 boundaries', async () => {
    await expect(parseShareIntake(form([['shared_title', '😀'.repeat(10000)], ['shared_text', '😀'.repeat(6384)]]))).resolves.toMatchObject({ shared_title: '😀'.repeat(10000) });
  });
  it.each([
    'text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data',
    'multipart/form-data; boundary=boundary; boundary=other',
  ])('rejects content type %s before reading', async (type) => {
    const request = raw('private', type);
    await expect(parseShareIntake(request)).rejects.toThrow('SHARE_INTAKE_INVALID');
    expect(request.bodyUsed).toBe(false);
  });
  it.each([
    part('shared_text', 'x').replace('name="shared_text"', 'name="shared_text"; filename=""'),
    part('shared_text', 'x').replace('name="shared_text"', 'name="shared_text"; filename*=UTF-8\'\'x'),
    part('shared_text', 'x').replace('\r\n\r\n', '\r\nContent-Transfer-Encoding: base64\r\n\r\n'),
    part('shared_text', 'x').replace('\r\n\r\n', '\r\nContent-Disposition: form-data; name="shared_url"\r\n\r\n'),
    part('shared_text', 'x').replace('form-data;', 'form-data;\n'),
    part('shared_text', 'x').replace('\r\n\r\n', '\r\nContent-Type: text/plain; charset="utf-8\r\n\r\n'),
    part('shared_text', 'x').slice(0, -5),
    part('shared_text', 'x') + 'private',
    part('SHARED_TEXT', 'x'),
  ])('rejects malformed MIME without lenient field dropping', async (body) => {
    await expect(parseShareIntake(raw(body))).rejects.toThrow('SHARE_INTAKE_INVALID');
  });
  it('accepts quoted boundaries, optional UTF-8 text type and delimiter-like text', async () => {
    const text = 'before\r\n--boundary-not-a-delimiter\r\nafter';
    const body = part('shared_text', text).replace('\r\n\r\n', '\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n');
    await expect(parseShareIntake(raw(body, 'multipart/form-data; boundary="boundary"'))).resolves.toMatchObject({ shared_text: text });
  });
  it('rejects malformed UTF-8 rather than replacing bytes', async () => {
    const bytes = new TextEncoder().encode(part('shared_text', 'x'));
    bytes[bytes.indexOf(120)] = 0xff;
    await expect(parseShareIntake(raw(bytes))).rejects.toThrow('SHARE_INTAKE_INVALID');
  });
  it('retains a literal leading field BOM', async () => {
    await expect(parseShareIntake(raw(part('shared_text', '\ufefftext')))).resolves.toMatchObject({ shared_text: '\ufefftext' });
  });
  it('decodes UTF-8 split across arbitrary transport chunks and ignores false content lengths', async () => {
    const bytes = new TextEncoder().encode(part('shared_text', '😀中文'));
    let offset = 0;
    const body = new ReadableStream({ pull(controller) {
      if (offset === bytes.length) controller.close();
      else controller.enqueue(bytes.slice(offset, ++offset));
    } });
    const request = new Request(`${origin}/share-target`, { method: 'POST', body, duplex: 'half', headers: { 'content-type': 'multipart/form-data; boundary=boundary', 'content-length': '1' } } as RequestInit);
    await expect(parseShareIntake(request)).resolves.toMatchObject({ shared_text: '😀中文' });
  });
  it('rejects Content-Encoding and already-consumed bodies', async () => {
    const encoded = raw(part('shared_text', 'x'));
    encoded.headers.set('content-encoding', 'gzip');
    await expect(parseShareIntake(encoded)).rejects.toThrow('SHARE_INTAKE_INVALID');
    expect(encoded.bodyUsed).toBe(false);
    const used = raw(part('shared_text', 'x')); await used.text();
    await expect(parseShareIntake(used)).rejects.toThrow('SHARE_INTAKE_INVALID');
  });
  it('accepts the exact raw boundary, rejects limit + 1 and cancels before later chunks', async () => {
    // Header whitespace pads the transport without violating field limits.
    const base = part('shared_text', 'a');
    const atLimit = base.replace('form-data;', `form-data;${' '.repeat(SHARE_RAW_LIMIT - base.length)}`);
    expect(new TextEncoder().encode(atLimit)).toHaveLength(SHARE_RAW_LIMIT);
    await expect(parseShareIntake(raw(atLimit))).resolves.toMatchObject({ shared_text: 'a' });
    const cancelled = vi.fn();
    const pull = vi.fn((controller: ReadableStreamDefaultController<Uint8Array>) => { controller.enqueue(new Uint8Array(SHARE_RAW_LIMIT + 1)); });
    const body = new ReadableStream({ pull, cancel: cancelled }, { highWaterMark: 0 });
    const request = new Request(`${origin}/share-target`, { method: 'POST', body, duplex: 'half', headers: { 'content-type': 'multipart/form-data; boundary=boundary' } } as RequestInit);
    await expect(parseShareIntake(request)).rejects.toThrow('SHARE_INTAKE_INVALID');
    expect(cancelled).toHaveBeenCalledOnce();
    expect(pull).toHaveBeenCalledOnce();
  });
  it('fails closed on stream errors and does not call unbounded formData', async () => {
    const request = form([['shared_text', 'ok']]);
    const spy = vi.spyOn(request, 'formData');
    await parseShareIntake(request);
    expect(spy).not.toHaveBeenCalled();
    const body = new ReadableStream({ start(controller) { controller.error(new Error('private')); } });
    const failed = new Request(`${origin}/share-target`, { method: 'POST', body, duplex: 'half', headers: { 'content-type': 'multipart/form-data; boundary=boundary' } } as RequestInit);
    await expect(parseShareIntake(failed)).rejects.toThrow('SHARE_INTAKE_INVALID');
  });
});
