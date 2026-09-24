// Zero-dependency Vercel build for the static AegisFleet dashboard.
// Vercel exposes project environment variables to the build process. The
// generated value is embedded into dist/js/runtime-config.js for browser use.
import { copyFile, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';

const frontendOnly = process.argv.includes('--frontend-only');
const root = process.cwd();
const source = frontendOnly ? root : resolve(root, 'frontend');
const output = resolve(root, 'dist');
const backendUrl = (process.env.AEGIS_BACKEND_URL || '').trim().replace(/\/+$/, '');

if (backendUrl && !/^https?:\/\//i.test(backendUrl)) {
  throw new Error('AEGIS_BACKEND_URL must start with http:// or https://');
}

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });

const ignored = new Set(['.git', 'dist', 'node_modules', 'vercel.json']);
async function copyTree(source, destination) {
  await mkdir(destination, { recursive: true });
  for (const entry of await readdir(source, { withFileTypes: true })) {
    if (ignored.has(entry.name)) continue;
    const from = join(source, entry.name);
    const to = join(destination, entry.name);
    if (entry.isDirectory()) await copyTree(from, to);
    else if (entry.isFile()) await copyFile(from, to);
  }
}
await copyTree(source, output);

const configPath = resolve(output, 'js', 'runtime-config.js');
const config = await readFile(configPath, 'utf8');
const marker = 'const BUILD_BACKEND_URL = "";';
if (!config.includes(marker)) {
  throw new Error(`runtime-config marker not found in ${configPath}`);
}
await writeFile(
  configPath,
  config.replace(marker, `const BUILD_BACKEND_URL = ${JSON.stringify(backendUrl)};`),
  'utf8',
);

console.log(`AegisFleet static build ready: ${output}`);
console.log(backendUrl ? `Backend: ${backendUrl}` : 'Backend: same-origin (set AEGIS_BACKEND_URL for a separate host)');
