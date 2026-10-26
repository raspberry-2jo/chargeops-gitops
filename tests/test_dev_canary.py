"""Exercise the actual Dev probe with real HTTP responses and rendered scope."""
import importlib.util
import copy
import json
from pathlib import Path
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / 'chargeops-chatops/overlays/development-canary'


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load(OVERLAY / 'probe.py')
metrics = load(OVERLAY / 'probe_metrics.py')
bootstrap = load(ROOT / 'scripts/dev-canary.py')


def docs(name):
    return [d for d in yaml.safe_load_all(subprocess.check_output(
        ['kubectl', 'kustomize', str(ROOT / 'chargeops-chatops/overlays' / name)], text=True)) if d]


def test_opt_in_does_not_modify_legacy_development_or_ops_workloads():
    legacy = {(d['kind'], d['metadata']['name']): d for d in docs('development')}
    canary = {(d['kind'], d['metadata']['name']): d for d in docs('development-canary')}
    for key, value in legacy.items():
        if key in [('Deployment', 'csms'), ('Role', 'jenkins-dev-verifier')]:
            continue
        rendered = copy.deepcopy(canary[key])
        if key == ('ConfigMap', 'chargeops-dev-config'):
            assert rendered['data'].pop('DEV_SIMULATION_ENABLED') == 'true'
            assert rendered['data'].pop('DEV_SIMULATION_IDS').split(',') == [
                'CI-DEV-SIM-01', 'CI-DEV-SIM-02', 'CI-DEV-SIM-03', 'CI-DEV-CANARY-01']
        assert rendered == value
    deployment = canary['Deployment', 'csms']
    assert deployment['spec']['replicas'] == 0
    # Jenkins pins a separately verified digest in the child overlay. Preserve
    # every runtime field while allowing that intentional image override.
    template = copy.deepcopy(deployment['spec']['template'])
    legacy_template = legacy['Deployment', 'csms']['spec']['template']
    legacy_image = next(c['image'] for c in legacy_template['spec']['containers'] if c['name'] == 'csms')
    next(c for c in template['spec']['containers'] if c['name'] == 'csms')['image'] = legacy_image
    env = next(c for c in template['spec']['containers'] if c['name'] == 'csms')['env']
    namespace_env = next(e for e in env if e['name'] == 'POD_NAMESPACE')
    assert namespace_env['valueFrom']['fieldRef']['fieldPath'] == 'metadata.namespace'
    env.remove(namespace_env)
    assert template == legacy_template
    assert all(d['metadata'].get('namespace', 'dev') == 'dev' for d in canary.values())
    r = canary['Rollout', 'csms']['spec']
    assert r['replicas'] == 2 and r['workloadRef']['scaleDown'] == 'never'
    assert r['strategy']['canary']['maxUnavailable'] == 0
    assert [s['setWeight'] for s in r['strategy']['canary']['steps'] if 'setWeight' in s] == [50, 100]
    job = canary['AnalysisTemplate', 'csms-dev-availability']['spec']['metrics'][0]['provider']['job']['spec']
    assert job['template']['spec']['serviceAccountName'] == 'csms-dev-canary-probe'
    service = canary['Service', 'csms-dev-canary-probe-metrics']
    monitor = canary['ServiceMonitor', 'csms-dev-canary-probe']
    assert monitor['spec']['namespaceSelector']['matchNames'] == ['dev']
    assert monitor['spec']['selector']['matchLabels'] == service['metadata']['labels']
    assert service['spec']['selector'] == job['template']['metadata']['labels']
    assert 'ops.svc' not in (OVERLAY / 'probe.py').read_text()


@pytest.mark.parametrize('fault', ['0', '20'])
def test_real_http_probe_normal_and_controlled_404(monkeypatch, fault):
    counts = {'ok': 0, 'error': 0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            bad = self.path == '/__chargeops_dev_canary_failure__'
            counts['error' if bad else 'ok'] += 1
            data = json.dumps({'ok': not bad}).encode()
            self.send_response(404 if bad else 200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real_request = probe.request
    def request(url):
        assert '.dev.svc.cluster.local' in url
        return real_request('http://127.0.0.1:' + str(server.server_port) + '/' + url.split('/', 3)[3])
    monkeypatch.setattr(probe, 'request', request)
    monkeypatch.setattr(probe.time, 'sleep', lambda value: None)
    items = [{'metadata': {'uid': str(i)}, 'status': {'conditions': [
        {'type': 'Ready', 'status': 'True'}], 'containerStatuses': []}} for i in range(2)]
    monkeypatch.setattr(probe, 'pods', lambda: items)
    monkeypatch.setenv('DEV_ERROR_EVERY', fault)
    observer = metrics.Observer(.01, 1, 2, 2, 60)
    try:
        if fault == '20':
            with pytest.raises(ValueError, match='one percent'):
                probe.main(observer)
            assert counts['error'] == 3
            assert 'chargeops_canary_probe_error_ratio{target="canary"} 0.05' in observer.render().decode()
        else:
            probe.main(observer)
            assert counts['error'] == 0
        assert counts['ok'] + counts['error'] == 180
    finally:
        server.shutdown()
        server.server_close()


def test_bootstrap_cannot_patch_ops_or_the_ops_application(monkeypatch):
    monkeypatch.setattr(bootstrap, 'run', lambda *a, **k: pytest.fail('must reject before execution'))
    with pytest.raises(ValueError, match='scope'):
        bootstrap.patch('ops', 'deployment', 'csms', [])
    with pytest.raises(ValueError, match='scope'):
        bootstrap.patch('argocd', 'application', 'chargeops-chatops-dev', [])


def test_handover_ignores_api_defaults_but_not_runtime_changes():
    template = next(d for d in docs('development') if d['kind'] == 'Deployment')['spec']['template']
    live = copy.deepcopy(template)
    live['metadata']['creationTimestamp'] = None
    live['spec'].update(restartPolicy='Always', dnsPolicy='ClusterFirst', schedulerName='default-scheduler', enableServiceLinks=True)
    for port in live['spec']['containers'][0].get('ports', []):
        port['protocol'] = 'TCP'
    for env in live['spec']['containers'][0].get('env', []):
        if 'fieldRef' in env.get('valueFrom', {}):
            env['valueFrom']['fieldRef']['apiVersion'] = 'v1'
    live['spec']['containers'][0].update(terminationMessagePath='/dev/termination-log', terminationMessagePolicy='File')
    live['spec']['containers'][0]['readinessProbe'].update(timeoutSeconds=1, successThreshold=1, failureThreshold=3)
    assert bootstrap.comparable_template(live) == bootstrap.comparable_template(template)
    live['spec']['containers'][0]['command'] = ['changed-command']
    assert bootstrap.comparable_template(live) != bootstrap.comparable_template(template)


def test_rehearsal_restores_template_probe_mode_and_auto_sync(monkeypatch, tmp_path):
    template = {'metadata': {'labels': {'app': 'csms'}}, 'spec': {
        'containers': [{'name': 'csms', 'image': 'same-pinned-image'}]}}
    objects = {
        'application': {'metadata': {'resourceVersion': '1', 'annotations': {}}, 'spec': {
            'destination': {'namespace': 'dev'}, 'source': {'path': bootstrap.OVERLAY,
            'repoURL': 'https://github.com/raspberry-2jo/chargeops-gitops.git', 'targetRevision': 'main'},
            'syncPolicy': {'automated': {'selfHeal': True}, 'syncOptions': ['CreateNamespace=true']}}},
        'deployment': {'metadata': {'generation': 1}, 'spec': {'replicas': 0, 'template': copy.deepcopy(template)}},
        'rollout': {'metadata': {'uid': 'rollout', 'generation': 1, 'annotations': {bootstrap.FAULT: '0'}},
                    'spec': {'replicas': 2}, 'status': {'phase': 'Healthy', 'stableRS': 'old', 'currentPodHash': 'old',
                    'observedGeneration': 1, 'workloadObservedGeneration': 1, 'readyReplicas': 2, 'availableReplicas': 2}},
    }
    original_policy = copy.deepcopy(objects['application']['spec']['syncPolicy'])
    calls = []
    def run(*args, obj=None):
        assert args[0] == '-n' and args[1] in ('dev', 'argocd')
        calls.append(args)
        if args[2] == 'get':
            kind = args[3]
            if kind == 'analysisrun':
                return json.dumps({'items': [{'metadata': {'name': 'csms-new-analysis', 'uid': 'analysis',
                    'ownerReferences': [{'uid': 'rollout'}]}, 'status': {'phase': 'Failed'}}]})
            if kind == 'jobs':
                return json.dumps({'items': [{'metadata': {'name': 'probe-job', 'ownerReferences': [{'uid': 'analysis'}]}}]})
            return json.dumps(objects[kind])
        if args[2] == 'logs':
            return 'Analysis failed: HTTP availability error rate exceeded one percent'
        assert args[2] == 'patch'
        current = objects[args[3]]
        for operation in json.loads(args[-1]):
            pointer = current
            parts = operation['path'].strip('/').split('/')
            for key in parts[:-1]:
                pointer = pointer[key]
            key = parts[-1]
            if operation['op'] == 'test':
                assert pointer[key] == operation['value']
            else:
                pointer[key] = copy.deepcopy(operation['value'])
        if args[3] == 'deployment':
            fault = bootstrap.NONCE in current['spec']['template']['metadata'].get('annotations', {})
            objects['rollout']['status'].update(phase='Degraded' if fault else 'Healthy',
                abort=fault, currentPodHash='new' if fault else 'old')
        return '{}'
    monkeypatch.setattr(bootstrap, 'run', run)
    monkeypatch.setattr(bootstrap, 'STATE', tmp_path / 'receipt.json')
    bootstrap.rehearse()
    assert objects['deployment']['spec']['template'] == template
    assert objects['rollout']['metadata']['annotations'][bootstrap.FAULT] == '0'
    assert objects['application']['spec']['syncPolicy'] == original_policy
    assert bootstrap.MARKER not in objects['application']['metadata']['annotations']
    assert json.loads(bootstrap.STATE.read_text())['phase'] == 'recovered'
    assert all(call[1] != 'ops' for call in calls)


def test_baseline_analysis_is_dev_only_and_never_enables_faults(monkeypatch):
    def get(namespace, kind, name):
        assert namespace == 'dev'
        if kind == 'analysistemplate':
            return {'spec': {'metrics': [{'name': 'existing-job-metric'}]}}
        return {'status': {'phase': 'Successful'}}
    created = []
    def run(*args, obj=None):
        assert args[:2] == ('-n', 'dev')
        created.append(obj)
        return '{"metadata":{"name":"csms-dev-baseline-test"}}'
    monkeypatch.setattr(bootstrap, 'get', get)
    monkeypatch.setattr(bootstrap, 'run', run)
    bootstrap.baseline_analysis()
    assert created[0]['metadata']['namespace'] == 'dev'
    assert {'name': 'dev-error-every', 'value': '0'} in created[0]['spec']['args']
    assert created[0]['spec']['metrics'] == [{'name': 'existing-job-metric'}]
