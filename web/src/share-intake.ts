import { validateShareFields, type ShareFields } from './share-intent';

export const SHARE_RAW_LIMIT = 131072;
export class ShareIntakeError extends Error {
  constructor() { super('SHARE_INTAKE_INVALID'); this.name = 'ShareIntakeError'; }
}
const invalid = (): never => { throw new ShareIntakeError(); };

/** Never aggregate an unbounded body or invoke formData() before the hard cap.
 * Transport chunk sizes belong to the UA; retain at most limit + 1 bytes and
 * cancel as soon as the first excess byte is observed. */
async function readBounded(request: Request): Promise<Uint8Array> {
  if (!request.body || request.bodyUsed) return invalid();
  const reader = request.body.getReader();
  const buffer = new Uint8Array(SHARE_RAW_LIMIT + 1);
  let size = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return buffer.subarray(0, size);
      if (!(value instanceof Uint8Array)) return invalid();
      const kept = Math.min(value.byteLength, buffer.length - size);
      buffer.set(value.subarray(0, kept), size);
      size += kept;
      if (size > SHARE_RAW_LIMIT) return invalid();
    }
  } catch {
    // A hostile/failed stream must not delay the fixed failure response forever.
    void reader.cancel().catch(() => undefined);
    return invalid();
  } finally { reader.releaseLock(); }
}

/** The target deliberately accepts only the browser's three text form fields,
 * not arbitrary MIME nesting, files, transfer encodings, or extended names. */
export async function parseShareIntake(request: Request): Promise<ShareFields> {
  try {
    const type = request.headers.get('content-type') ?? '';
    const match = /^multipart\/form-data\s*;\s*boundary=(?:"([0-9A-Za-z'()+_,\-./:=? ]{1,70})"|([0-9A-Za-z'()+_,\-./:=?]{1,70}))\s*$/i.exec(type);
    if (!match || request.headers.has('content-encoding')) return invalid();
    const boundary = match[1] ?? match[2];
    if (boundary.endsWith(' ')) return invalid();
    const bytes = await readBounded(request);
    const body = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
    const opening = `--${boundary}`;
    const fields: ShareFields = { shared_title: '', shared_text: '', shared_url: '' };
    const seen = new Set<string>();
    if (body === `${opening}--` || body === `${opening}--\r\n`) return fields;
    if (!body.startsWith(`${opening}\r\n`)) return invalid();
    const escaped = boundary.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const delimiter = new RegExp(`\\r\\n--${escaped}(?:--|\\r\\n)`, 'g');
    let cursor = opening.length + 2;
    for (;;) {
      const headerEnd = body.indexOf('\r\n\r\n', cursor);
      if (headerEnd < 0) return invalid();
      const headers = body.slice(cursor, headerEnd).split('\r\n');
      let name: string | undefined;
      let contentTypeSeen = false;
      for (const header of headers) {
        const disposition = /^Content-Disposition:[ \t]*form-data;[ \t]*name="(shared_title|shared_text|shared_url)"[ \t]*$/i.exec(header);
        if (disposition) {
          if (name !== undefined) return invalid();
          name = disposition[1];
        } else if (/^Content-Type:[ \t]*text\/plain(?:[ \t]*;[ \t]*charset=(?:"utf-8"|utf-8))?[ \t]*$/i.test(header) && !contentTypeSeen) {
          contentTypeSeen = true;
        } else return invalid();
      }
      if (!name || !(name in fields) || seen.has(name)) return invalid();
      seen.add(name);
      delimiter.lastIndex = headerEnd + 4;
      const next = delimiter.exec(body);
      if (!next) return invalid();
      fields[name as keyof ShareFields] = body.slice(headerEnd + 4, next.index);
      if (!validateShareFields(fields)) return invalid();
      cursor = delimiter.lastIndex;
      if (next[0].endsWith('--')) {
        if (body.slice(cursor) !== '' && body.slice(cursor) !== '\r\n') return invalid();
        return fields;
      }
    }
  } catch { return invalid(); }
}
