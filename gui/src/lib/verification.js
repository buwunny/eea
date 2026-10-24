/** Normalize native KiCad and fallback reports without inventing coordinates. */
export function drcFindings(report) {
  const violations = Array.isArray(report?.violations) ? report.violations : [];
  const unconnected = Array.isArray(report?.unconnected) ? report.unconnected : [];
  return [...violations, ...unconnected].filter(item => item && typeof item === 'object').map(item => {
    const descriptions = (Array.isArray(item.items) ? item.items : []).map(part => part?.description).filter(text => typeof text === 'string');
    const positions = [item.position, ...(Array.isArray(item.items) ? item.items.map(part => part?.pos) : [])];
    const position = positions.find(point => point && Number.isFinite(point.x) && Number.isFinite(point.y));
    return {
      title: typeof item.description === 'string' ? item.description : typeof item.type === 'string' ? item.type.replaceAll('_', ' ') : 'Unconnected items',
      severity: item.severity || 'error', details: descriptions.join('; '), position: position || null,
    };
  });
}

export function reportMatchesBoard(report, digest) {
  return typeof digest === 'string' && /^[a-f0-9]{64}$/i.test(digest) && report?.board_sha256 === digest;
}

export function simulationResult(report, passed) {
  if (!report) return '—';
  if (report.verified === false) return 'ESTIMATE';
  const result = report.sim_passed ?? passed;
  if (result === false) return 'REVISE';
  if (report.functional_verified === false || report.coverage_complete === false) return 'PARTIAL';
  return result === true ? 'PASS' : 'ESTIMATE';
}
