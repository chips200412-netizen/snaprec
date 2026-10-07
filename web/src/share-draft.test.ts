import { describe, expect, it } from 'vitest';
import { draftFromFields, validateShareDraft } from './share-draft';

const fields = (shared_url = '', shared_text = '', shared_title = '') => ({ shared_url, shared_text, shared_title });

describe('share draft public seam', () => {
  it('extracts a URL from any field, stably deduplicates and preserves Unicode prose', () => {
    const url = 'https://example.com/watch?q=一#片段';
    const draft = draftFromFields(fields(url, ` e\u0301 😀 ${url} 尾\n`, ' 标题 '));
    expect(draft).toEqual({ url, text: ` 标题 \n e\u0301 😀 ${url} 尾\n` });
    const checked = validateShareDraft(draft);
    expect(checked.ok).toBe(true);
    if (checked.ok) {
      expect(checked.inputText.split(url)).toHaveLength(2);
      expect(checked.inputText).toContain(' 标题 \n e\u0301 😀  尾\n');
    }
    expect(draftFromFields(fields('', '', url)).url).toBe(url);
  });

  it('keeps all fields editable when zero or multiple links are received', () => {
    for (const source of [fields('原文', '😀文案', ' 标题 '), fields('https://a.example/', 'https://b.example/', '标题')]) {
      const draft = draftFromFields(source);
      expect(draft).toEqual({ url: source.shared_url, text: `${source.shared_title}\n${source.shared_text}` });
      expect(validateShareDraft(draft).ok).toBe(false);
    }
    expect(validateShareDraft({ url: '', text: '原文' })).toMatchObject({ code: 'SHARE_LINK_REQUIRED', field: 'url' });
    expect(validateShareDraft({ url: 'https://a.example/', text: 'https://b.example/' })).toMatchObject({ code: 'SHARE_LINK_AMBIGUOUS', field: 'text' });
  });

  it('treats embedded query/fragment URLs as data and preserves pure URL punctuation', () => {
    for (const url of ['https://a.example/?next=https://b.example/a。', 'https://a.example/#https://b.example/a)', 'https://a.example/path,', 'https://a.example/path。']) {
      expect(draftFromFields(fields(url)).url).toBe(url);
      const result = validateShareDraft({ url, text: '文案' });
      expect(result.ok).toBe(true);
      if (result.ok) expect(result.inputText).toContain(`<${url}>`);
    }
    expect(validateShareDraft({ url: 'https://a.example/a,https://b.example/b', text: '' }).ok).toBe(false);
  });

  it('recognizes demonstrated wrappers without changing balanced URL punctuation', () => {
    expect(draftFromFields(fields('', '看看（https://a.example/a(b)）')).url).toBe('https://a.example/a(b)');
    expect(draftFromFields(fields('', '看看 https://a.example/a。')).url).toBe('https://a.example/a');
  });

  it('uses exact identity and revalidates edited fields without mutation', () => {
    const draft = { url: 'https://a.example/', text: 'https://A.example/' };
    expect(validateShareDraft(draft).ok).toBe(false);
    expect(draft.text).toBe('https://A.example/');
    draft.text = '修改后';
    expect(validateShareDraft(draft).ok).toBe(true);
    expect(validateShareDraft({ url: 'https://user:secret@a.example/', text: '' }).ok).toBe(false);
    for (const url of ['https://a.example/bad%', 'https://a.example/%G0', 'https://a.example/a\u0001b', 'https://bad_host.example/', 'https://a.example:0/', 'https://@a.example/']) {
      expect(validateShareDraft({ url, text: '' })).toMatchObject({ ok: false, code: 'SHARE_LINK_INVALID', field: 'url' });
    }
  });

  it('counts code points, rejects malformed Unicode and never truncates oversized preview input', () => {
    const url = 'https://a.example/';
    const text = '😀'.repeat(10000 - [...url].length - 3);
    expect(validateShareDraft({ url, text }).ok).toBe(true);
    expect(validateShareDraft({ url, text: `${text}多` })).toMatchObject({ code: 'SHARE_INPUT_TOO_LONG', field: 'text' });
    expect(validateShareDraft({ url, text: '\ud800' })).toMatchObject({ code: 'SHARE_FIELDS_INVALID', field: 'text' });
  });
});
