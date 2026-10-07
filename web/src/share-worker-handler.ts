import { createShareIntent } from './share-intent';
import { parseShareIntake } from './share-intake';

export const SHARE_HANDLER_VERSION = 1;
export function isShareTargetRequest(request: Request, origin: string): boolean {
  try {
    const url = new URL(request.url);
    return url.origin === origin && url.pathname === '/share-target' &&
      request.method === 'POST' && request.mode === 'navigate';
  } catch { return false; }
}

export function shareFailureResponse(): Response {
  return new Response('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>暂时无法接收分享 · 瞬时录</title></head><body><main><h1>暂时无法接收分享</h1><p>请重新分享，或回到素材库手动粘贴。</p><a href="/" rel="noreferrer">回到素材库手动粘贴</a></main></body></html>', {
    status: 400,
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'none'; base-uri 'none'; frame-ancestors 'none'", 'X-Content-Type-Options': 'nosniff' },
  });
}

export async function handleShareRequest(request: Request, origin: string): Promise<Response> {
  if (!isShareTargetRequest(request, origin)) return shareFailureResponse();
  try {
    const fields = await parseShareIntake(request);
    const token = await createShareIntent(fields);
    return new Response(null, { status: 303, headers: {
      Location: `/capture/share#intent=${token}`,
      'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
    } });
  } catch { return shareFailureResponse(); }
}
