import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
const exec = promisify(execFile);
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

export async function environmentDoctor() {
  const venv = path.join(repo, 'mcp-server/.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const python = process.env.EEA_PYTHON || await fs.access(venv).then(() => venv).catch(() => 'python3');
  const { stdout } = await exec(python, [path.join(repo, 'mcp-server/doctor.py')], { timeout: 25000, maxBuffer: 256 * 1024 });
  return JSON.parse(stdout);
}
