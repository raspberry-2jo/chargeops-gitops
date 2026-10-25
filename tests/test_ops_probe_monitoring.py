import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ops_probe_monitoring', ROOT / 'scripts/ops-probe-monitoring.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_apply_sends_only_telemetry_resources_and_checks_workloads(monkeypatch):
    docs = module.documents()
    monkeypatch.setattr(module, 'preflight', lambda: None)
    snapshots = []
    def snapshot():
        snapshots.append(True)
        return {'deployment': {'image': 'current'}, 'rollout': {'replicas': 4}}
    monkeypatch.setattr(module, 'workload_snapshot', snapshot)
    monkeypatch.setattr(module, 'documents', lambda: docs)
    writes = []
    monkeypatch.setattr(module, 'run', lambda *args, **kw: writes.append((args, kw)) or '')
    module.apply()
    assert len(snapshots) == 2
    assert len(writes) == 1
    sent = list(yaml.safe_load_all(writes[0][1]['payload']))
    assert {(d['kind'], d['metadata']['name']) for d in sent} == module.ALLOWED
    assert not any(d['kind'] in ('Deployment', 'Rollout', 'Secret', 'Application') for d in sent)


@pytest.mark.parametrize('fault', ['active_sync', 'automatic_sync', 'wrong_namespace', 'unhealthy', 'active_analysis'])
def test_preflight_refuses_unsafe_installation(monkeypatch, fault):
    app = {'spec': {'destination': {'namespace': 'ops'}, 'source': {'path': 'chargeops-chatops/overlays/canary'}}, 'status': {}}
    rollout = {'status': {'phase': 'Healthy', 'stableRS': 'same', 'currentPodHash': 'same', 'availableReplicas': 4}}
    runs = []
    if fault == 'active_sync': app['operation'] = {'sync': {}}
    if fault == 'automatic_sync': app['spec']['syncPolicy'] = {'automated': {}}
    if fault == 'wrong_namespace': app['spec']['destination']['namespace'] = 'dev'
    if fault == 'unhealthy': rollout['status']['phase'] = 'Progressing'
    if fault == 'active_analysis': runs = [{'status': {'phase': 'Running'}}]
    monkeypatch.setattr(module, 'get', lambda kind, *args: app if kind == 'application' else rollout)
    import json
    monkeypatch.setattr(module, 'run', lambda *args, **kw: json.dumps({'items': runs}))
    with pytest.raises(RuntimeError):
        module.preflight()
