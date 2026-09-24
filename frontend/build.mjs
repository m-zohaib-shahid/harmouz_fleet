// Self-contained Vercel build for a project whose Root Directory is frontend.
// The Vercel project environment variable is embedded into the browser bundle.
import { copyFile, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';

const root = process.cwd();
const output = resolve(root, 'dist');
const backendUrl = (process.env.AEGIS_BACKEND_URL || '').trim().replace(/\/+$/, '');

if (backendUrl && !/^https?:\/\//i.test(backendUrl)) {
  throw new Error('AEGIS_BACKEND_URL must start with http:// or https://');
}

const ignored = new Set(['.git', 'dist', 'node_modules', 'vercel.json', 'build.mjs']);
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

await rm(output, { recursive: true, force: true });
await copyTree(root, output);

const configPath = resolve(output, 'js', 'runtime-config.js');
const config = await readFile(configPath, 'utf8');
const marker = 'const BUILD_BACKEND_URL = "";';
if (!config.includes(marker)) throw new Error(`runtime-config marker not found in ${configPath}`);
await writeFile(configPath, config.replace(marker, `const BUILD_BACKEND_URL = ${JSON.stringify(backendUrl)};`), 'utf8');

console.log(`AegisFleet frontend build ready: ${output}`);
console.log(backendUrl ? `Backend: ${backendUrl}` : 'Backend: same-origin (set AEGIS_BACKEND_URL for a separate host)');
