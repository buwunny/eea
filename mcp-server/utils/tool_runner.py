"""Shared filesystem operation lifecycle for MCP and user-triggered local exports."""
from .telemetry import LOCK, workspace_path, record, read_telemetry, atomic_json, ToolActivity, ACTIVITY_LOCK, progress


def execute(name, stage, operation):
    with LOCK:
        activity = ToolActivity(workspace_path(), name, {
            'list_curated_parts': 'Loading parts catalog',
            'synthesize_pcb': 'Building board',
            'run_spice_simulation': 'Simulating circuit',
            'autoroute_pcb': 'Routing board',
            'run_drc_verification': 'Checking design rules',
            'export_manufacturing_files': 'Exporting manufacturing files',
            'build_and_verify': 'Building and verifying board',
        }.get(name, name))
        try:
            activity.start()
            if stage:
                with ACTIVITY_LOCK:
                    telemetry = read_telemetry(workspace_path())
                    telemetry.setdefault('stages', {})[stage] = 'in_progress'
                    telemetry['pipeline_stage'] = stage
                    downstream = {
                        'synthesize_pcb': ['SIMULATION', 'AUTOROUTING', 'DRC_VERIFICATION', 'MANUFACTURING'],
                        'autoroute_pcb': ['DRC_VERIFICATION', 'MANUFACTURING'],
                    }.get(name, [])
                    for pending in downstream:
                        telemetry['stages'][pending] = 'pending'
                    metrics = telemetry.setdefault('metrics', {})
                    if name == 'synthesize_pcb':
                        metrics.update(sim_passed=None, sim_summary='Simulation pending for this design')
                    if downstream:
                        metrics.update(drc_errors=None, unconnected_items=None)
                    atomic_json(workspace_path() / 'telemetry.json', telemetry)
            report = operation()
            status = report.get('status', 'success')
            failed = status in ('error', 'failed') or report.get('sim_passed') is False
            state = 'failed' if failed else 'completed'
            if status in ('degraded', 'unavailable', 'unsupported', 'partial'):
                # An attempted operation must not disappear back into 'pending'.
                state = 'completed' if stage == 'SCHEMATIC_PCB' and report.get('artifacts') else 'failed'
            if stage == 'AUTOROUTING' and report.get('unconnected_items', 0):
                state = 'failed'
            if stage in ('DRC_VERIFICATION', 'MANUFACTURING'):
                verified = report.get('verified') if stage == 'DRC_VERIFICATION' else report.get('manufacturing_ready')
                if verified is not True:
                    state = 'failed'
                elif stage == 'DRC_VERIFICATION' and (report.get('drc_errors', 0) or report.get('unconnected_items', 0)):
                    state = 'failed' 
            metrics = dict(report.get('metrics', {}))
            for key in ('drc_errors', 'unconnected_items', 'sim_passed', 'sim_summary', 'component_count', 'net_count', 'estimated_cost_usd'):
                if key in report:
                    metrics[key] = report[key]
            if name == 'synthesize_pcb' and not failed:
                with ACTIVITY_LOCK:
                    telemetry = read_telemetry(workspace_path())
                    telemetry.setdefault('stages', {})['ARCHITECTURE'] = 'completed'
                    atomic_json(workspace_path() / 'telemetry.json', telemetry)
            summary = report.get('summary') or report.get('sim_summary') or {
                'synthesize_pcb': f"Board built: {report.get('component_count', 0)} components",
                'autoroute_pcb': f"{report.get('unconnected_items', 'Unknown')} unrouted connections ({report.get('backend', 'router')})",
                'run_drc_verification': f"DRC: {report.get('drc_errors')} errors, {report.get('unconnected_items')} unconnected",
            }.get(name, f"{status} via {report.get('backend', 'catalog')}")
            if state == 'failed' or status != 'success':
                report.setdefault('next_action', 'Review warnings and change the design or install the missing native tool. Do not repeat the same call unchanged.')
                report.setdefault('retryable_unchanged', False)
            report['summary'] = summary
            progress(workspace_path(), 'Saving workspace history')
            record(name, report.get('summary', f'{status} via {report.get("backend", "catalog")}'), stage, state, metrics, report.get('bom'), report.get('warnings'))
            activity.finish('failed' if state == 'failed' else ('warning' if report.get('warnings') else 'completed'), summary)
            return report
        except Exception as error:
            report = {'status': 'error', 'error': str(error), 'tool': name}
            try:
                activity.finish('failed', str(error)[:300])
                record(name, str(error)[:150], stage, 'failed', warnings=[str(error)])
            except Exception as history_error:
                report['history_error'] = str(history_error)
            return report
        finally:
            if not activity.stop.is_set():
                activity.finish('failed', 'Tool interrupted; check CLI output')


