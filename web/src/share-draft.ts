import type { ShareFields } from './share-intent';

export type ShareDraft = { url: string; text: string };
export type ShareDraftValidation = { ok: true; inputText: string } | {
  ok: false; code: string; message: string; field: 'url' | 'text';
};

// Match the public capture service's token boundaries. URL identity is deliberately
// exact here: platform canonicalization remains the backend's responsibility.
const wrappers: Record<string, string> = {
  '(': ')', '[': ']', '{': '}', "'": "'", '（': '）', '【': '】',
  '《': '》', '「': '」', '『': '』',
};
type Candidate = { value: string; start: number; end: number; ambiguous: boolean };
const occurrences = (text: string, character: string) => text.split(character).length - 1;

function candidates(text: string): Candidate[] {
  const whole = /^https?:\/\/[^\s<>"]+$/i.test(text.trim());
  return [...text.matchAll(/https?:\/\/[^\s<>"]+/gi)].map((match) => {
    let value = match[0];
    const start = match.index!;
    if (!whole) {
      const opening: string[] = [];
      for (let index = start - 1; index >= 0 && wrappers[text[index]]; index -= 1) opening.push(text[index]);
      if (!opening.length && start + value.length === text.trimEnd().length && value.endsWith('。')) value = value.slice(0, -1);
      for (const mark of opening.reverse()) {
        const closing = wrappers[mark];
        if (!value.endsWith(closing)) break;
        if (mark !== closing && occurrences(value, closing) <= occurrences(value, mark)) break;
        value = value.slice(0, -1);
      }
    }
    const dataIndex = value.search(/[?#]/);
    const boundary = dataIndex < 0 ? value.length : dataIndex;
    const ambiguous = [...value.matchAll(/https?:\/\//gi)].slice(1).some((scheme) => scheme.index! < boundary);
    return { value, start, end: start + value.length, ambiguous };
  });
}

function withoutCandidates(text: string, matches = candidates(text)): string {
  let remaining = '';
  let offset = 0;
  for (const match of matches) {
    remaining += text.slice(offset, match.start);
    offset = match.end;
  }
  return remaining + text.slice(offset);
}

const joinFields = (parts: string[]) => parts.filter((part) => part !== '').join('\n');

export function draftFromFields(fields: ShareFields): ShareDraft {
  const all = [fields.shared_title, fields.shared_text, fields.shared_url].flatMap(candidates);
  const unique = [...new Set(all.map((match) => match.value))];
  const text = joinFields([fields.shared_title, fields.shared_text]);
  if (unique.length !== 1 || all.some((match) => match.ambiguous)) return { url: fields.shared_url, text };
  // Original title/text remain editable, including repeated links. Only the
  // immutable submission snapshot removes duplicates; user text is never trimmed.
  return { url: unique[0], text: joinFields([text, withoutCandidates(fields.shared_url)]) };
}

export function isValidShareUnicode(value: string): boolean {
  for (const character of value) {
    const point = character.codePointAt(0)!;
    if (point >= 0xd800 && point <= 0xdfff) return false;
  }
  return true;
}

function error(code: string, message: string, field: 'url' | 'text'): ShareDraftValidation {
  return { ok: false, code, message, field };
}

export function validateShareDraft(draft: ShareDraft): ShareDraftValidation {
  // A received text editor can contain all three 10,000-code-point fields plus
  // two separators. Bound user-edited input before allocating candidate arrays.
  if (draft.url.length > 20000) return error('SHARE_INPUT_TOO_LONG', '链接过长，请编辑后重试。', 'url');
  if (draft.text.length > 60004) return error('SHARE_INPUT_TOO_LONG', '附带文本过长，请编辑后重试。', 'text');
  for (const field of ['url', 'text'] as const) {
    if (!isValidShareUnicode(draft[field])) return error('SHARE_FIELDS_INVALID', '分享内容包含无效字符，请修改后重试。', field);
  }
  const urlMatches = candidates(draft.url);
  const textMatches = candidates(draft.text);
  const all = [...urlMatches, ...textMatches];
  const unique = [...new Set(all.map((match) => match.value))];
  if (!unique.length) return error('SHARE_LINK_REQUIRED', '请填写一个公开 HTTP/HTTPS 链接。', 'url');
  const ambiguousField = urlMatches.some((match) => match.ambiguous) || new Set(urlMatches.map((match) => match.value)).size > 1 ? 'url' : 'text';
  if (unique.length !== 1 || all.some((match) => match.ambiguous)) return error('SHARE_LINK_AMBIGUOUS', '一次只能收进一个链接，请修改链接或附带文本。', ambiguousField);
  const selected = unique[0];
  try {
    const parsed = new URL(selected);
    const hostname = parsed.hostname.replace(/\.$/, '');
    const authority = selected.slice(selected.indexOf('://') + 3).split(/[/?#]/, 1)[0];
    if (!hostname || !['http:', 'https:'].includes(parsed.protocol) || authority.includes('@') || authority.includes('%') ||
      /[\s\u0000-\u0020\u007f\u0085\\]/.test(selected) || /%(?![a-f0-9]{2})/i.test(selected) || parsed.port === '0') throw new Error();
    if (!hostname.startsWith('[') && (hostname.length > 253 || hostname.split('.').some((label) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label)))) throw new Error();
  } catch {
    return error('SHARE_LINK_INVALID', '链接格式无效，请检查后重试。', urlMatches.length ? 'url' : 'text');
  }
  const prose = joinFields([withoutCandidates(draft.url, urlMatches), withoutCandidates(draft.text, textMatches)]);
  // Explicit angle boundaries keep legal terminal punctuation intact even when
  // the existing backend reads the URL as part of sharing prose.
  const inputText = prose === '' ? selected : `${prose}\n<${selected}>`;
  if ([...inputText].length > 10000) return error('SHARE_INPUT_TOO_LONG', '链接和附带文本合计不能超过 10,000 个字符，请编辑后重试。', [...draft.url].length > 10000 ? 'url' : 'text');
  return { ok: true, inputText };
}
