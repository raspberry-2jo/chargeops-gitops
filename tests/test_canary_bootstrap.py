import copy
import json
from pathlib import Path
import types

import pytest
import yaml

from test_canary import bootstrap, live


@pytest.mark.parametrize('failure', [None, 'baseline', 'analysis'])
@pytest.mark.parametrize('migration', [False, True])
@pytest.mark.parametrize('resumed', [False, True])
def test_handover_never_scales_down_before_verified_baseline(tmp_path, monkeypatch, failure, migration, resumed):
    events = []
    directory = tmp_path / bootstrap.PROD
    directory.mkdir(parents=True)
    (directory / 'kustomization.yaml').write_text(yaml.safe_dump({
        'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization', 'resources': []}))
    image = 'registry.chargeops.lab:5000/chargeops/chargeops-csms@sha256:' + 'a' * 64
    source = live()
    source['spec']['replicas'] = 4
    source['status'] = {'availableReplicas': 4}
    state = {'replicas': 0 if resumed else 4, 'template': copy.deepcopy(source['spec']['template'])}
    if migration:
        pod = source['spec']['template']['spec']
        pod['volumes'].append({'name': 'app-code', 'configMap': {'name': 'csms-app-test'}})
        pod['containers'][0]['volumeMounts'] = [
            {'name': 'ca', 'mountPath': '/etc/chargeops-ca'},
            {'name': 'app-code', 'mountPath': '/app/csms/app.py', 'subPath': 'app.py'}]
    source['spec']['replicas'] = state['replicas']
    state['template'] = copy.deepcopy(source['spec']['template'])
    frozen = copy.deepcopy(source)
    frozen.update(apiVersion='apps/v1', kind='Deployment', metadata={'name': 'csms'})
    frozen['spec']['template']['spec']['containers'][0]['image'] = image
    if resumed:
        preserved_source = copy.deepcopy(source)
        pod = preserved_source['spec']['template']['spec']
        pod['volumes'] = [v for v in pod['volumes'] if v['name'] != 'app-code']
        pod['containers'][0]['volumeMounts'] = [{'name': 'ca', 'mountPath': '/etc/chargeops-ca'}]
        preserved = bootstrap.runtime_patch(preserved_source)
        preserved['spec']['replicas'] = 0
        preserved['spec']['template']['spec']['containers'][0]['image'] = image
        (directory / bootstrap.RUNTIME).write_text(yaml.safe_dump(preserved))
    def render(path):
        current = copy.deepcopy(frozen)
        runtime = directory / bootstrap.RUNTIME
        if runtime.exists():
            current = yaml.safe_load(runtime.read_text())
            current['spec']['template'].pop('$patch', None)
        return [current] + (additions if path == bootstrap.CANARY else [])
    additions = [{'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'Rollout', 'metadata': {'name': 'csms'}}]
    additions.append({'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
                      'metadata': {'name': 'csms-canary-probe'}})
    monkeypatch.setattr(bootstrap, 'ROOT', tmp_path)
    monkeypatch.setattr(bootstrap, 'git_clean', lambda: None)
    monkeypatch.setattr(bootstrap, 'render', render)
    monkeypatch.setattr(bootstrap, 'install_controller', lambda: events.append('install'))
    monkeypatch.setattr(bootstrap, 'create_kubeconfig', lambda duration: events.append('credential'))
    monkeypatch.setattr(bootstrap, 'module', lambda name: types.SimpleNamespace(manifest=lambda ref: ('repository', 'a' * 64, {'sha256:' + 'a' * 64})))
    def run(*args, **kwargs):
        if args[:2] == ('kubectl', 'replace'):
            assert '--dry-run=server' in args
            replacement = yaml.safe_load(kwargs['payload'])
            assert replacement['spec']['replicas'] == 0
            assert replacement['metadata']['resourceVersion'] == '42'
        if args[0] == 'docker':
            return json.dumps({'match': True, 'image_ast_sha256': 'b' * 64, 'mounted_ast_sha256': 'b' * 64})
        return 'c' * 40 if args[:2] == ('git', 'rev-parse') else ''
    monkeypatch.setattr(bootstrap, 'run', run)
    def commit(message):
        replica = yaml.safe_load((directory / bootstrap.RUNTIME).read_text())['spec']['replicas']
        events.append(('commit', replica))
        return 'c' * 40
    monkeypatch.setattr(bootstrap, 'commit_runtime', commit)
    def get(*args):
        assert 'secret' not in args
        if 'application' in args:
            return {'spec': {'source': {'path': bootstrap.CANARY}}}
        if 'deployment' in args:
            actual = copy.deepcopy(source)
            actual['metadata'] = {'resourceVersion': '42'}
            actual['spec']['replicas'] = state['replicas']
            actual['spec']['template'] = copy.deepcopy(state['template'])
            return actual
        if 'configmap' in args:
            return {'metadata': {'resourceVersion': '42'}, 'data': {'app.py': 'VALUE = 1\n'}}
        if 'pods' in args:
            return {'items': [{'metadata': {'name': str(i)}, 'status': {'containerStatuses': [
                {'name': 'csms', 'ready': True, 'imageID': image}]}} for i in range(4)]}
        raise AssertionError('Unexpected API call')
    monkeypatch.setattr(bootstrap, 'get', get)
    def sync(revision, resources, replace=False):
        if resources[0]['kind'] == 'Deployment':
            state['replicas'] = yaml.safe_load((directory / bootstrap.RUNTIME).read_text())['spec']['replicas']
            if replace:
                assert 'analysis' in events
                assert state['replicas'] == 0
                state['template'] = render(bootstrap.PROD)[0]['spec']['template']
        else:
            assert {'group': 'networking.k8s.io', 'kind': 'NetworkPolicy',
                    'name': 'csms-canary-probe'} in resources
            events.append('probe-network-allowed')
        events.append(('sync', resources[0]['kind'], state['replicas']))
    monkeypatch.setattr(bootstrap, 'sync', sync)
    def baseline(ref):
        events.append('baseline')
        if failure == 'baseline': raise ValueError('not ready')
    def analysis():
        assert 'probe-network-allowed' in events
        events.append('analysis')
        if failure == 'analysis': raise ValueError('failed')
    monkeypatch.setattr(bootstrap, 'wait_baseline', baseline)
    monkeypatch.setattr(bootstrap, 'baseline_analysis', analysis)
    if failure:
        with pytest.raises(ValueError): bootstrap.bootstrap('24h', migrate_code_mount=migration)
        assert yaml.safe_load((directory / bootstrap.RUNTIME).read_text())['spec']['replicas'] == (0 if resumed else 4)
        assert ('commit', 0) not in events
        assert state['replicas'] == (0 if resumed else 4)
    else:
        bootstrap.bootstrap('24h', migrate_code_mount=migration)
        assert events.index('baseline') < events.index('analysis')
        if not resumed:
            assert events.index('analysis') < events.index(('commit', 0))
        assert events.index('analysis') < events.index(('sync', 'Deployment', 0))
        captured = yaml.safe_load((directory / bootstrap.RUNTIME).read_text())
        assert captured['spec']['replicas'] == 0
        assert state['replicas'] == 0
        assert state['template'] == render(bootstrap.PROD)[0]['spec']['template']
        if migration:
            assert captured['spec']['template']['spec']['containers'][0]['volumeMounts'] == [{'name': 'ca', 'mountPath': '/etc/chargeops-ca'}]


@pytest.mark.parametrize('replace', [False, True])
def test_repeated_revision_still_waits_for_its_own_sync_operation(monkeypatch, replace):
    app = {'spec': {'source': {'path': bootstrap.CANARY}}}
    operation = {}
    calls = []
    def run(*args, **kwargs):
        operation.update(json.loads(args[-1])['operation'])
    monkeypatch.setattr(bootstrap, 'run', run)
    def get(*args):
        calls.append(args)
        if len(calls) == 1: return app
        current = copy.deepcopy(app)
        old = copy.deepcopy(operation)
        if len(calls) == 2: old['initiatedBy']['username'] = 'previous-operation'
        current['status'] = {'operationState': {'phase': 'Succeeded', 'operation': old}}
        return current
    monkeypatch.setattr(bootstrap, 'get', get)
    monkeypatch.setattr(bootstrap.time, 'sleep', lambda seconds: None)
    bootstrap.sync('c' * 40, [{'group': 'apps', 'kind': 'Deployment', 'name': 'csms'}], replace=replace)
    assert len(calls) == 3
    assert operation['sync'].get('syncOptions', []) == (['Replace=true'] if replace else [])
    assert operation['sync']['prune'] is False


def test_replacement_cannot_target_other_resources():
    with pytest.raises(ValueError, match='restricted'):
        bootstrap.sync('c' * 40, [{'group': '', 'kind': 'PersistentVolumeClaim', 'name': 'collector-raw'}], replace=True)


@pytest.mark.parametrize('stale', ['generation', 'template'])
def test_baseline_waits_for_rollout_to_adopt_the_reconciled_workload(monkeypatch, stale):
    image = 'pinned-image'
    spec = {'containers': [{'name': 'csms', 'image': image}], 'volumes': [{'name': 'ca'}]}
    rounds = []
    def get(*args):
        if 'rollout' in args:
            rounds.append(True)
            return {'metadata': {'uid': 'rollout'}, 'status': {
                'phase': 'Healthy', 'stableRS': 'stable', 'availableReplicas': 4,
                'workloadObservedGeneration': '1' if stale == 'generation' and len(rounds) == 1 else '2'}}
        if 'deployment' in args:
            return {'metadata': {'generation': 2}, 'spec': {'template': {'spec': spec}}}
        if 'replicasets' in args:
            current = copy.deepcopy(spec)
            if stale == 'template' and len(rounds) == 1:
                current['volumes'].append({'name': 'old-code'})
            return {'items': [{'metadata': {'labels': {'rollouts-pod-template-hash': 'stable'},
                                           'ownerReferences': [{'uid': 'rollout'}]},
                               'status': {'availableReplicas': 4},
                               'spec': {'template': {'spec': current}}}]}
        raise AssertionError(args)
    monkeypatch.setattr(bootstrap, 'get', get)
    monkeypatch.setattr(bootstrap.time, 'sleep', lambda seconds: None)
    bootstrap.wait_baseline(image)
    assert len(rounds) == 2


def test_rehearsal_is_self_contained_and_python_commands_compile():
    module = bootstrap.module('rehearse-canary')
    docs = module.manifests()
    assert all(d['metadata'].get('namespace', module.NS) == 'canary-rehearsal' for d in docs)
    assert not any(d['kind'] in ['Secret', 'PersistentVolumeClaim', 'Application'] for d in docs)
    compile(module.SERVER, '<rehearsal-server>', 'exec')
    compile(module.PROBE, '<rehearsal-probe>', 'exec')
    assert 'DB_DSN' not in json.dumps(docs)


def test_canary_kubeconfig_uses_nonexpiring_shared_secret_helper(monkeypatch, capsys):
    helper = bootstrap.module('prepare-dev-kubeconfig')
    calls, configs = [], []
    monkeypatch.setattr(helper, 'service_account_token', lambda namespace, account, duration: calls.append(
        (namespace, account, duration)) or 'test-only-canary-token')
    monkeypatch.setattr(helper, 'write_config', lambda config, output, verifier: configs.append(config))
    monkeypatch.setattr(bootstrap, 'module', lambda name: helper)
    monkeypatch.setattr(bootstrap, 'get', lambda *a: {'clusters': [{'cluster': {
        'server': 'https://api.invalid', 'certificate-authority-data': 'public-ca'}}]})
    bootstrap.create_kubeconfig()
    assert calls == [('ops', 'jenkins-canary', 'unlimited')]
    assert configs[0]['contexts'][0]['context']['namespace'] == 'ops'
    assert configs[0]['users'][0]['name'] == 'jenkins-canary'
    output = capsys.readouterr().out
    assert 'Token expiration: none' in output
    assert 'test-only-canary-token' not in output
