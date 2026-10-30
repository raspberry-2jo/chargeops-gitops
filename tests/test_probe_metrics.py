import importlib.util
from pathlib import Path
import subprocess
from urllib.request import build_opener, ProxyHandler

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / 'chargeops-chatops/overlays/canary'


def load(filename):
    spec = importlib.util.spec_from_file_location(filename.removesuffix('.py'), OVERLAY / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metrics = load('probe_metrics.py')
probe = load('probe.py')


def observer():
    return metrics.Observer(probe.MAX_ERROR_RATE, probe.MAX_P95_SECONDS,
                            probe.MAX_RELATIVE_LATENCY, probe.MIN_READY_PODS, probe.SAMPLES)


def value(text, name, target=None):
    key = 'chargeops_canary_probe_' + name + (f'{{target="{target}"}}' if target else '') + ' '
    return float(next(line[len(key):] for line in text.splitlines() if line.startswith(key)))


@pytest.mark.parametrize('errors', [0, 1, 60])
def test_export_matches_actual_gate_including_failure_and_no_successes(errors):
    observations = [(False, 2)] * errors + [(True, .02)] * (60 - errors)
    o = observer()
    o.record({'canary': observations})
    text = o.render().decode()
    assert value(text, 'samples', 'canary') == 60
    assert value(text, 'errors', 'canary') == errors
    assert value(text, 'error_ratio', 'canary') == errors / 60
    assert value(text, 'error_ratio_limit') == probe.MAX_ERROR_RATE
    if errors:
        with pytest.raises(ValueError):
            probe.evaluate({'canary': observations})
    else:
        assert probe.evaluate({'canary': observations})['canary']['p95_seconds'] == value(text, 'p95_seconds', 'canary')
    if errors == 60:
        assert 'chargeops_canary_probe_p95_seconds{target="canary"}' not in text


def test_relative_latency_export_uses_existing_rounded_baseline_and_floor():
    o = observer()
    o.record({'stable': [(True, .010000004)] * 60, 'canary': [(True, .300000004)] * 60})
    assert value(o.render().decode(), 'relative_latency_ratio') == .3 / .2
    assert probe.evaluate({'stable': [(True, .010000004)] * 60,
                           'canary': [(True, .300000004)] * 60})['canary']['p95_seconds'] == .3


def test_missing_samples_and_missing_stable_are_not_zero_latency():
    o = observer()
    assert not any(line.startswith('chargeops_canary_probe_p95_seconds{') for line in o.render().decode().splitlines())
    o.record({'canary': [(True, .01)] * 60})
    assert not any(line.startswith('chargeops_canary_probe_relative_latency_ratio ') for line in o.render().decode().splitlines())


def test_real_http_export_includes_final_failure():
    o = observer()
    o.record({'canary': [(True, .01)] * 59 + [(False, 2)]})
    o.finish(False)
    server = metrics.serve(o, 0)
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{server.server_port}/metrics', timeout=3) as response:
            assert response.status == 200
            text = response.read().decode()
        assert value(text, 'result') == -1
        assert value(text, 'error_ratio', 'canary') > .01
    finally:
        server.shutdown()
        server.server_close()


def test_probe_records_failed_pod_check_before_exiting(monkeypatch):
    items = [{'metadata': {'uid': str(i)}, 'status': {'conditions': [
        {'type': 'Ready', 'status': 'True'}], 'containerStatuses': []}} for i in range(3)]
    monkeypatch.setattr(probe, 'pods', lambda: items)
    o = observer()
    with pytest.raises(ValueError, match='four Ready'):
        probe.main(o)
    text = o.render().decode()
    assert value(text, 'ready_pods') == 3
    assert value(text, 'pod_check_ok') == 0


@pytest.mark.parametrize('passed', [True, False])
def test_metrics_grace_never_changes_job_judgement(monkeypatch, passed):
    import sys
    monkeypatch.setitem(sys.modules, 'probe_metrics', metrics)
    monkeypatch.setenv('PROBE_METRICS', 'true')
    def execute(o):
        o.record({'canary': [(passed, .02)] * 60})
        if not passed:
            raise ValueError('HTTP availability error rate exceeded one percent')
    monkeypatch.setattr(probe, 'main', execute)
    sleeps = []
    monkeypatch.setattr(probe.time, 'sleep', sleeps.append)
    # Capture the final snapshot without binding the cluster metrics port.
    o = observer()
    class Server:
        def shutdown(self): pass
        def server_close(self): pass
    monkeypatch.setattr(metrics, 'Observer', lambda *args: o)
    monkeypatch.setattr(metrics, 'serve', lambda current: Server())
    assert probe.run() == (0 if passed else 1)
    assert value(o.render().decode(), 'result') == (1 if passed else -1)
    assert sleeps == [20]


def test_rendered_monitoring_scope_and_unchanged_workloads():
    def render(overlay):
        return [d for d in yaml.safe_load_all(subprocess.check_output(
            ['kubectl', 'kustomize', str(ROOT / 'chargeops-chatops/overlays' / overlay)], text=True)) if d]
    docs = render('canary')
    by_key = {(d['kind'], d['metadata']['name']): d for d in docs}
    for doc in render('prod'):
        assert by_key[doc['kind'], doc['metadata']['name']] == doc
    service = by_key['Service', 'csms-canary-probe-metrics']
    monitor = by_key['ServiceMonitor', 'csms-canary-probe']
    policy = by_key['NetworkPolicy', 'csms-canary-probe-metrics']
    assert monitor['spec']['selector']['matchLabels'] == service['metadata']['labels']
    assert monitor['spec']['namespaceSelector']['matchNames'] == ['ops']
    assert policy['spec']['podSelector'] == {'matchLabels': service['spec']['selector']}
    assert policy['spec']['ingress'][0]['ports'] == [{'port': 9110, 'protocol': 'TCP'}]
    assert policy['spec']['ingress'][0]['from'] == [{'namespaceSelector': {
        'matchLabels': {'kubernetes.io/metadata.name': 'monitoring'}}}]
    job = next(m for m in by_key['AnalysisTemplate', 'csms-availability']['spec']['metrics'] if m['name'] == 'availability-error-latency-restarts')['provider']['job']['spec']
    assert job['activeDeadlineSeconds'] == 240
    assert job['template']['spec']['containers'][0]['ports'] == [{'name': 'probe-metrics', 'containerPort': 9110}]
