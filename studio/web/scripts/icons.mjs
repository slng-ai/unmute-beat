// Draws the studio's icons: the bar's yellow mark (#FBE566) on brand ink (#15120D), square corners.
// Writes public/icons/*.png for the web manifest and ../desktop/resources/icon.png for the desktop app.
// Plain PNG with Node's own zlib: the shapes are squares on whole pixels, so nothing needs smoothing.
// The maskable icon keeps the mark inside the safe circle (80 % of the side), so a round crop never cuts it.
// The desktop icon follows macOS's 1024 grid, the shape at 824, so it sits level with other apps in the Dock.
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { crc32, deflateSync } from 'node:zlib';

const here = dirname(fileURLToPath(import.meta.url));
const INK = [0x15, 0x12, 0x0d, 255];
const YELLOW = [0xfb, 0xe5, 0x66, 255];
const CLEAR = [0, 0, 0, 0];

/** A PNG of `size` px: an ink square inset by `margin` (transparent around it), the yellow mark `mark` of the size in its middle. */
function icon(size, mark, margin = 0) {
  const ink = [Math.round(size * margin), Math.round(size * (1 - margin))];
  const side = Math.round(size * mark);
  const from = Math.floor((size - side) / 2);
  const yellow = [from, from + side];
  const inside = ([a, b], v) => v >= a && v < b;
  const rows = [];
  for (let y = 0; y < size; y++) {
    const row = Buffer.alloc(1 + size * 4);
    for (let x = 0; x < size; x++) {
      const colour = inside(yellow, x) && inside(yellow, y) ? YELLOW : inside(ink, x) && inside(ink, y) ? INK : CLEAR;
      row.set(colour, 1 + x * 4);
    }
    rows.push(row);
  }
  const chunk = (type, data) => {
    const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length);
    const sum = Buffer.alloc(4);
    sum.writeUInt32BE(crc32(body));
    return Buffer.concat([length, body, sum]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header.set([8, 6, 0, 0, 0], 8);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header),
    chunk('IDAT', deflateSync(Buffer.concat(rows))),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

const out = {
  'public/icons/icon-192.png': icon(192, 0.44),
  'public/icons/icon-512.png': icon(512, 0.44),
  'public/icons/icon-maskable-512.png': icon(512, 0.36),
  'public/icons/apple-touch-icon.png': icon(180, 0.44),
  '../desktop/resources/icon.png': icon(1024, 0.36, 0.0977),
};
for (const [path, png] of Object.entries(out)) {
  const file = join(here, '..', path);
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, png);
  console.log(`wrote ${path} (${png.length} bytes)`);
}
