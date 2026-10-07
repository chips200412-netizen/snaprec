import { build, type Connect, type Plugin, type ResolvedConfig } from 'vite';

export const SHARE_FALLBACK_HTML = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>分享暂不可用 · 瞬时录</title><main><h1>分享暂不可用</h1><p>请重新分享，或回到素材库手动粘贴。</p><a href="/">回到素材库手动粘贴</a></main></html>';

type WorkerSource = () => Promise<string>;

/** Runs before Vite/proxy middleware; never subscribes to or parses incoming bodies. */
export function shareTransportMiddleware(workerSource?: WorkerSource): Connect.NextHandleFunction {
  return (request, response, next): void => {
    const pathname = (request.url ?? '').split('?', 1)[0];
    if (!['/share-target', '/capture/share', '/share-worker.js'].includes(pathname)) {
      next();
      return;
    }
    // Downstream HTML/static handlers must not weaken these response policies.
    const setHeader = response.setHeader.bind(response);
    response.setHeader = (name: string, value: string | number | readonly string[]) => {
      const key = name.toLowerCase();
      return setHeader(name, key === 'cache-control' ? 'no-store'
        : key === 'referrer-policy' ? 'no-referrer' : value);
    };
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('Referrer-Policy', 'no-referrer');
    if (pathname === '/capture/share') {
      next();
      return;
    }
    if (pathname === '/share-target') {
      response.statusCode = 503;
      response.setHeader('Content-Type', 'text/html; charset=utf-8');
      response.setHeader('Content-Security-Policy', "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
      response.setHeader('X-Content-Type-Options', 'nosniff');
      // Do not keep a connection with an unread/unbounded upload alive.
      response.setHeader('Connection', 'close');
      response.end(request.method === 'HEAD' ? undefined : SHARE_FALLBACK_HTML);
      return;
    }
    response.setHeader('Service-Worker-Allowed', '/');
    if (!workerSource) {
      next();
      return;
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      response.statusCode = 405;
      response.end();
      return;
    }
    void workerSource().then((source) => {
      response.setHeader('Content-Type', 'text/javascript; charset=utf-8');
      response.end(request.method === 'HEAD' ? undefined : source);
    }, () => {
      // A compiler exception may contain paths/source: never expose or log it here.
      response.statusCode = 503;
      response.setHeader('Content-Type', 'text/plain; charset=utf-8');
      response.end('share_worker_unavailable');
    });
  };
}

export async function bundleShareWorker(root: string, mode: string): Promise<string> {
  const result = await build({
    root, mode, configFile: false, publicDir: false, logLevel: 'silent',
    build: {
      write: false, minify: false, sourcemap: false,
      lib: { entry: `${root}/src/share-worker.ts`, formats: ['es'], fileName: () => 'share-worker.js' },
      rolldownOptions: { output: { codeSplitting: false } },
    },
  });
  const outputs = (Array.isArray(result) ? result : [result]).flatMap((item) => 'output' in item ? item.output : []);
  if (outputs.length !== 1 || outputs[0].type !== 'chunk' || outputs[0].imports.length || outputs[0].dynamicImports.length) {
    throw new Error('share_worker_bundle_invalid');
  }
  return outputs[0].code;
}

export function shareTransportPlugin(): Plugin {
  let config: ResolvedConfig;
  return {
    name: 'shunshilu-share-transport',
    enforce: 'pre',
    configResolved(resolved) { config = resolved; },
    configureServer(server) {
      // Compile only when requested, without injecting the Vite client into the worker.
      let pending: Promise<string> | undefined;
      server.middlewares.use(shareTransportMiddleware(() => {
        pending ??= bundleShareWorker(config.root, config.mode).finally(() => { pending = undefined; });
        return pending;
      }));
    },
    configurePreviewServer(server) { server.middlewares.use(shareTransportMiddleware()); },
    async generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'share-worker.js', source: await bundleShareWorker(config.root, config.mode) });
    },
  };
}
