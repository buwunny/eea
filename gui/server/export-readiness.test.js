import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { exportInfo } from './exports.js';

test('changing design metadata invalidates an exported assembly inventory', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'eea-export-binding-'));
  const hash = text => createHash('sha256').update(text).digest('hex');
  const board = 'board'; const design = '{"components":[]}';
  const save = async (file, value) => fs.writeFile(path.join(root, file), typeof value === 'string' ? value : JSON.stringify(value));
  try {
    await fs.mkdir(path.join(root, 'build')); await fs.mkdir(path.join(root, 'manufacturing'));
    await save('build/board.kicad_pcb', board); await save('build/design.json', design);
    await save('build/drc_report.json', { verified: true, board_sha256: hash(board), drc_errors: 0, unconnected_items: 0 });
    await save('manufacturing/export_report.json', { manufacturing_ready: true, board_sha256: hash(board), source_design_sha256: hash(design) });
    for (const file of ['gerbers.zip', 'bom.csv', 'cpl.csv']) await save(`manufacturing/${file}`, 'fixture');
    assert.equal((await exportInfo(root, 'fixture')).ready, true);
    await save('build/design.json', '{"components":[{"sourcing":"changed"}]}');
    assert.equal((await exportInfo(root, 'fixture')).ready, false);
    assert.deepEqual((await exportInfo(root, 'fixture')).files, []);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});
