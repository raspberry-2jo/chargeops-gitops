"""Exercise rollout shape, read-only metric gates and safe runtime capture."""
import copy
import importlib.util
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


probe = load('chargeops-chatops/overlays/canary/probe.py', 'probe')
bootstrap = load('scripts/bootstrap-canary.py', 'bootstrap')


def test_canary_overlay_adds_only_rollout_analysis_probe_and_services():
    def render(env):
        return [d for d in yaml.safe_load_all(subprocess.check_output(
            ['kubectl', 'kustomize', 'chargeops-chatops/overlays/' + env], cwd=ROOT, text=True)) if d]
    prod, canary = render('prod'), render('canary')
    identity = lambda d: (d['kind'], d['metadata']['name'])
    actual = {identity(d): d for d in canary}
    for d in prod:
        assert actual[identity(d)] == d
    additions = set(actual) - {identity(d) for d in prod}
    assert additions == {('Rollout', 'csms'), ('AnalysisTemplate', 'csms-availability'),
                         ('Service', 'csms-stable'), ('Service', 'csms-canary'), ('ConfigMap', 'csms-canary-probe'),
                         ('NetworkPolicy', 'csms-canary-probe'), ('Service', 'csms-canary-probe-metrics'),
                         ('ServiceMonitor', 'csms-canary-probe'), ('NetworkPolicy', 'csms-canary-probe-metrics')}
    rollout = actual['Rollout', 'csms']['spec']
    assert rollout['replicas'] == 4
    assert rollout['workloadRef']['scaleDown'] == 'never'
    assert rollout['progressDeadlineAbort'] is True
    strategy = rollout['strategy']['canary']
    assert strategy['maxUnavailable'] == 0 and strategy['maxSurge'] == 1
    assert [s['setWeight'] for s in strategy['steps'] if 'setWeight' in s] == [25, 50, 100]
    assert sum('analysis' in s for s in strategy['steps']) == 3
    metric = actual['AnalysisTemplate', 'csms-availability']['spec']['metrics'][0]
    assert metric['failureLimit'] == 0
    job = metric['provider']['job']['spec']
    assert job['backoffLimit'] == 0 and job['activeDeadlineSeconds'] == 240
    assert 'envFrom' not in job['template']['spec']['containers'][0]
    # The existing ops default-deny/csms-ingress policies do not admit an
    # unlabelled analysis Pod. This additive rule must select its exact label.
    labels = job['template']['metadata']['labels']
    policy = actual['NetworkPolicy', 'csms-canary-probe']['spec']
    assert policy['podSelector'] == {'matchLabels': {'app': 'csms'}}
    assert policy['policyTypes'] == ['Ingress']
    assert policy['ingress'] == [{'from': [{'podSelector': {'matchLabels': labels}}],
                                  'ports': [{'protocol': 'TCP', 'port': 8000}]}]
    assert labels == {'chargeops.io/role': 'canary-probe'}


@pytest.mark.parametrize('failure', ['errors', 'slow', 'relative', 'empty', 'undersampled'])
def test_metric_gate_fails_closed(failure):
    results = {'service': [(True, 0.02)] * 60, 'stable': [(True, 0.01)] * 60, 'canary': [(True, 0.02)] * 60}
    if failure == 'errors': results['canary'][0] = (False, 0.02)
    if failure == 'slow': results['canary'] = [(True, 1.1)] * 60
    if failure == 'relative': results['canary'] = [(True, 0.3)] * 60
    if failure == 'empty': results['canary'] = []
    if failure == 'undersampled': results['canary'] = [(True, 0.01)] * 59
    with pytest.raises(ValueError):
        probe.evaluate(results)


def test_metric_gate_works_at_100_percent_without_stable_pods():
    assert probe.evaluate({'service': [(True, 0.01)] * 60, 'canary': [(True, 0.02)] * 60})['canary']['errors'] == 0


def pod(uid, restarts=0, ready=True):
    return {'metadata': {'uid': uid}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True' if ready else 'False'}],
                                              'containerStatuses': [{'restartCount': restarts}]}}


@pytest.mark.parametrize('failure', ['restart', 'unready', 'disappeared'])
def test_availability_gate_detects_pod_failure(failure):
    items = [pod(str(i)) for i in range(4)]
    previous = probe.check_pods(items, {})
    if failure == 'restart': items[0] = pod('0', restarts=1)
    if failure == 'unready': items[0] = pod('0', ready=False)
    if failure == 'disappeared': items[0] = pod('replacement')
    with pytest.raises(ValueError):
        probe.check_pods(items, previous)


def live():
    return {'spec': {'selector': {'matchLabels': {'app': 'csms'}}, 'template': {
        'metadata': {'labels': {'app': 'csms', 'app.kubernetes.io/name': 'csms'}},
        'spec': {'containers': [{'name': 'csms', 'image': 'test:dev',
                    'command': ['sh', '-c', 'ulimit -n 65536 && exec uvicorn csms.app:app --host 0.0.0.0 --port 8000'],
                    'envFrom': [{'configMapRef': {'name': 'chargeops-config'}}, {'secretRef': {'name': 'chargeops-secrets'}}],
                    'env': [{'name': 'DB_POOL', 'value': '20'}, {'name': 'API_TOKEN', 'valueFrom': {'secretKeyRef': {'name': 'chargeops-secrets', 'key': 'CSMS_API_TOKEN'}}}]}],
                 'affinity': {'podAntiAffinity': {'preferredDuringSchedulingIgnoredDuringExecution': []}},
                 'topologySpreadConstraints': [{'maxSkew': 1, 'topologyKey': 'kubernetes.io/hostname', 'whenUnsatisfiable': 'DoNotSchedule', 'labelSelector': {'matchLabels': {'app': 'csms'}}}],
                 'volumes': [{'name': 'ca', 'configMap': {'name': 'chargeops-ca'}}]}}}}


def test_runtime_capture_preserves_commands_refs_ca_and_placement():
    source = live()
    original = copy.deepcopy(source)
    patch = bootstrap.runtime_patch(source)
    assert source == original
    template = patch['spec']['template']
    assert template['$patch'] == 'replace'
    for field in ['containers', 'volumes', 'affinity', 'topologySpreadConstraints']:
        assert template['spec'][field] == source['spec']['template']['spec'][field]
    assert template['spec']['terminationGracePeriodSeconds'] >= 40
    assert patch['spec']['strategy']['rollingUpdate']['maxUnavailable'] == 0


@pytest.mark.parametrize('failure', ['secret_literal', 'unknown_command', 'code_mount', 'sidecar'])
def test_runtime_capture_never_commits_credentials_or_code_override(failure):
    source = live()
    container = source['spec']['template']['spec']['containers'][0]
    if failure == 'secret_literal': container['env'].append({'name': 'DB_DSN', 'value': 'do-not-log'})
    if failure == 'unknown_command': container['command'] = ['sh', '-c', 'echo do-not-log']
    if failure == 'code_mount': container['volumeMounts'] = [{'name': 'csms-app-test', 'mountPath': '/app/csms/app.py'}]
    if failure == 'sidecar': source['spec']['template']['spec']['containers'].append({'name': 'unknown'})
    with pytest.raises(ValueError) as error:
        bootstrap.runtime_patch(source)
    assert 'do-not-log' not in str(error.value)


def test_runtime_replacement_does_not_merge_back_obsolete_base_env(tmp_path, monkeypatch):
    source = live()
    patch = bootstrap.runtime_patch(source)
    monkeypatch.setattr(bootstrap, 'ROOT', tmp_path)
    base = copy.deepcopy(source)
    base.update(apiVersion='apps/v1', kind='Deployment', metadata={'name': 'csms'})
    base['spec']['template']['spec']['containers'][0]['env'].append({'name': 'REQUIRE_CN', 'value': 'false'})
    (tmp_path / 'deployment.yaml').write_text(yaml.safe_dump(base))
    (tmp_path / 'runtime.yaml').write_text(yaml.safe_dump(patch))
    (tmp_path / 'kustomization.yaml').write_text(yaml.safe_dump({
        'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization', 'resources': ['deployment.yaml'],
        'patches': [{'path': 'runtime.yaml'}]}))
    actual = yaml.safe_load(subprocess.check_output(['kubectl', 'kustomize', str(tmp_path)], text=True))
    env = actual['spec']['template']['spec']['containers'][0]['env']
    assert 'REQUIRE_CN' not in {e['name'] for e in env}


def test_only_existing_ops_application_is_used_and_updater_stays_dev_only():
    app = yaml.safe_load((ROOT / 'chargeops-chatops/argocd/application-canary.yaml').read_text())
    assert app['metadata']['name'] == 'chargeops-chatops-dev'
    assert app['spec']['source']['path'].endswith('/canary')
    assert not app['spec']['syncPolicy'].get('automated')
    rbac = list(yaml.safe_load_all((ROOT / 'chargeops-chatops/argocd/canary-rbac.yaml').read_text()))
    roles = [r for r in rbac if r['kind'] == 'Role']
    assert not any('secrets' in rule['resources'] for role in roles for rule in role['rules'])
    assert not any(role['metadata']['namespace'] == 'ops' and 'patch' in rule['verbs']
                   and rule['resources'] != ['rollouts/status'] for role in roles for rule in role['rules'])
