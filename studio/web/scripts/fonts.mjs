// Copies Saans from a slng-ai/frontend checkout into public/fonts (see public/fonts/README.md).
import { copyFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const from = resolve(process.env.SLNG_FONTS ?? join(here, '../../../../frontend/libs/ui/src/assets/fonts'));
const files = ['Saans-Regular.woff2', 'Saans-SemiBold.woff2', 'Saans-Heavy.woff2'];

if (!existsSync(from)) {
  console.error(`no fonts at ${from}: clone slng-ai/frontend next to this repository, or set SLNG_FONTS`);
  process.exit(1);
}
for (const file of files) {
  copyFileSync(join(from, file), join(here, '../public/fonts', file));
  console.log(`copied ${file}`);
}
