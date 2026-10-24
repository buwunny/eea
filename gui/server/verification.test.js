import test from 'node:test';
import assert from 'node:assert/strict';
import { drcFindings, reportMatchesBoard, simulationResult } from '../src/lib/verification.js';

test('DRC findings preserve native item locations and fallback positions', () => {
  const result = drcFindings({ violations: [{ type: 'clearance', severity: 'error', description: 'Too close', items: [{ description: 'Track /VCC', pos: { x: 12, y: 8 } }] }, { type: 'possible_pad_overlap', position: { x: 0, y: 0 } }], unconnected: [{ items: [{ description: 'Pad R1.1', pos: { x: 10, y: 20 } }] }] });
  assert.equal(result.length, 3);
  assert.deepEqual(result[0].position, { x: 12, y: 8 });
  assert.equal(result[0].details, 'Track /VCC');
  assert.deepEqual(result[1].position, { x: 0, y: 0 });
  assert.equal(result[2].title, 'Unconnected items');
});

test('DRC findings do not invent locations for missing or invalid coordinates', () => {
  assert.deepEqual(drcFindings(null), []);
  assert.deepEqual(drcFindings({ violations: 'malformed' }), []);
  assert.equal(drcFindings({ violations: [null, { position: { x: NaN, y: 3 } }] })[0].position, null);
  const digest = 'a'.repeat(64);
  assert.equal(reportMatchesBoard({ board_sha256: digest }, digest), true);
  assert.equal(reportMatchesBoard({ board_sha256: 'b'.repeat(64) }, digest), false);
  assert.equal(reportMatchesBoard({}, null), false);
});

test('simulation readout respects historical results and incomplete model coverage', () => {
  assert.equal(simulationResult(null, true), '—');
  assert.equal(simulationResult({ verified: false, sim_passed: false }, true), 'ESTIMATE');
  assert.equal(simulationResult({ verified: true, sim_passed: true, functional_verified: false }, true), 'PARTIAL');
  assert.equal(simulationResult({ verified: true, sim_passed: false }, true), 'REVISE');
  assert.equal(simulationResult({ verified: true, sim_passed: true, functional_verified: true }, false), 'PASS');
});
