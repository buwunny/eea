import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { startBridge } from './bridge.js';

test('environment endpoint is read-only and enforces the bridge origin policy', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'eea-doctor-'));
  const state = await fs.mkdtemp(path.join(os.tmpdir(), 'eea-doctor-state-'));
  const telemetry = '{"project_name":"Diagnostics"}';
  await fs.writeFile(path.join(root, 'telemetry.json'), telemetry);
  const report = { python: '3.14', tools: [], modules: [], capabilities: { native_drc: false } };
  let calls = 0;
  const bridge = await startBridge({ workspace: root, stateDirectory: state, port: 0, doctor: async () => { calls++; return report; } });
  try {
    const base = `http://127.0.0.1:${bridge.server.address().port}`;
    const response = await fetch(`${base}/api/environment`, { headers: { origin: 'http://localhost:5173' } });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), report);
    assert.equal((await fetch(`${base}/api/environment`, { headers: { origin: 'https://evil.example' } })).status, 403);
    assert.equal(calls, 1);
    assert.equal(await fs.readFile(path.join(root, 'telemetry.json'), 'utf8'), telemetry);
    assert.deepEqual(await fs.readdir(root), ['telemetry.json']);
  } finally {
    await bridge.close();
    await fs.rm(root, { recursive: true, force: true });
    await fs.rm(state, { recursive: true, force: true });
  }
});
