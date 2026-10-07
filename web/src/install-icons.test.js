// @vitest-environment node
import { readFileSync } from 'node:fs';
import { inflateSync } from 'node:zlib';
import { describe, expect, it } from 'vitest';

const publicRoot = new URL('../public/', import.meta.url);
const manifest = JSON.parse(readFileSync(new URL('manifest.webmanifest', publicRoot), 'utf8'));

// Decode PNG pixels independently of the Windows rasterizer to check alpha and safe area.
function png(path) {
  const data = readFileSync(new URL(path.slice(1), publicRoot));
  expect(data.subarray(0, 8).toString('hex')).toBe('89504e470d0a1a0a');
  expect(data.length).toBeLessThan(65536);
  const width = data.readUInt32BE(16), height = data.readUInt32BE(20);
  expect(data[24]).toBe(8); expect(data[25]).toBe(6); expect(data[28]).toBe(0);
  const chunks = [];
  for (let offset = 8; offset < data.length;) {
    const length = data.readUInt32BE(offset);
    const type = data.toString('ascii', offset + 4, offset + 8);
    if (type === 'IDAT') chunks.push(data.subarray(offset + 8, offset + 8 + length));
    offset += length + 12;
  }
  const raw = inflateSync(Buffer.concat(chunks));
  const stride = width * 4, pixels = Buffer.alloc(stride * height);
  expect(raw.length).toBe((stride + 1) * height);
  function paeth(a, b, c) {
    const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
    return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
  }
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    expect(filter).toBeLessThanOrEqual(4);
    for (let x = 0; x < stride; x++) {
      const index = y * stride + x;
      const a = x >= 4 ? pixels[index - 4] : 0;
      const b = y ? pixels[index - stride] : 0;
      const c = y && x >= 4 ? pixels[index - stride - 4] : 0;
      const predictor = [0, a, b, Math.floor((a + b) / 2), paeth(a, b, c)][filter];
      pixels[index] = (raw[y * (stride + 1) + 1 + x] + predictor) & 255;
    }
  }
  return { width, height, pixels };
}

describe('installed wordmark icon contract', () => {
  it('declares local PNG sizes and separates general and maskable use', () => {
    expect(manifest.icons).toEqual([
      { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
      { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
      { src: '/icons/icon-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ]);
  });
  it.each(manifest.icons)('$src decodes at declared size with opaque background and nonempty ink', (icon) => {
    const { width, height, pixels } = png(icon.src);
    expect(`${width}x${height}`).toBe(icon.sizes);
    let ink = 0, transparent = 0, outsideSafeCircle = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      if (pixels[i + 3] !== 255) transparent++;
      if (pixels[i] !== 251 || pixels[i + 1] !== 251 || pixels[i + 2] !== 252) {
        ink++;
        const x = (i / 4) % width + 0.5, y = Math.floor(i / 4 / width) + 0.5;
        if (Math.hypot(x - width / 2, y - height / 2) > width * 0.4) outsideSafeCircle++;
      }
    }
    expect(transparent).toBe(0);
    expect(ink).toBeGreaterThan(width * height * 0.015);
    if (icon.purpose === 'maskable') expect(outsideSafeCircle).toBe(0);
  });
  it('does not expand the approved share target', () => {
    expect(manifest.name).toBe('瞬时录');
    expect(manifest.scope).toBe('/');
    expect(manifest.share_target).toEqual({ action: '/share-target', method: 'POST', enctype: 'multipart/form-data',
      params: { title: 'shared_title', text: 'shared_text', url: 'shared_url' } });
    expect(manifest.launch_handler).toBeUndefined();
  });
});
