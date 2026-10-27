"""Guard the temporary dry-run boundary, retaining the real inspection jobs."""
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(overlay):
    return [d for d in yaml.safe_load_all(subprocess.check_output(
        ['kubectl', 'kustomize', str(ROOT / 'chargeops-chatops/overlays' / overlay)],
        text=True)) if d]


def test_ops_runs_both_real_checks_in_explicit_dry_run():
    template = next(d for d in render('canary') if d['kind'] == 'AnalysisTemplate')
    spec = template['spec']
    expected = {'availability-error-latency-restarts', 'five-level-service-gate'}
    assert {d['metricName'] for d in spec['dryRun']} == expected
    assert {m['name'] for m in spec['metrics']} == expected
    for metric in spec['metrics']:
        assert metric['failureLimit'] == 0  # Preserve truthful inspection results.
        job = metric['provider']['job']['spec']
        assert job['backoffLimit'] == 0
        container = job['template']['spec']['containers'][0]
        assert container['command'] in [
            ['python', '/probe/probe.py'], ['python', '/probe/service_gates.py']]
    assert template['metadata']['annotations']['chargeops.io/analysis-mode'] == 'observe-only'


def test_observation_does_not_relax_readiness_or_step_structure():
    rollout = next(d for d in render('canary') if d['kind'] == 'Rollout')['spec']
    assert rollout['replicas'] == 4
    assert rollout['progressDeadlineAbort'] is True
    strategy = rollout['strategy']['canary']
    assert strategy['maxUnavailable'] == 0
    assert [s['setWeight'] for s in strategy['steps'] if 'setWeight' in s] == [25, 50, 100]
    assert sum('analysis' in step for step in strategy['steps']) == 3


def test_temporary_bypass_is_not_applied_to_dev():
    for overlay in ['development', 'development-rolling', 'development-canary']:
        assert all(not d.get('spec', {}).get('dryRun') for d in render(overlay)
                   if d['kind'] in ['AnalysisTemplate', 'Rollout'])
