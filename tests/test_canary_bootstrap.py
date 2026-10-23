import copy
import json
from pathlib import Path
import types

import pytest
import yaml

from test_canary import bootstrap, live


@pytest.mark.parametrize('failure', [None, 'baseline', 'analysis'])
@pytest.mark.parametrize('migration', [False, True])
def test_handover_never_scales_down_before_verified_baseline(tmp_path, monkeypatch, failure, migration):
    events = []
    directory = tmp_path / bootstrap.PROD
    directory.mkdir(parents=True)
    (directory / 'kustomization.yaml').write_text(yaml.safe_dump({
        'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization', 'resources': []}))
    image = 'registry.chargeops.lab:5000/chargeops/chargeops-csms@sha256:' + 'a' * 64
    source = live()
    source['spec']['replicas'] = 4
    source['status'] = {'availableReplicas': 4}
    state = {'replicas': 4}
    if migration:
        pod = source['spec']['template']['spec']
        pod['volumes'].append({'name': 'app-code', 'configMap': {'name': 'csms-app-test'}})
        pod['containers'][0]['volumeMounts'] = [
            {'name': 'ca', 'mountPath': '/etc/chargeops-ca'},
            {'name': 'app-code', 'mountPath': '/app/csms/app.py', 'subPath': 'app.py'}]
    frozen = copy.deepcopy(source)
    frozen.update(apiVersion='apps/v1', kind='Deployment', metadata={'name': 'csms'})
    frozen['spec']['template']['spec']['containers'][0]['image'] = image
    additions = [{'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'Rollout', 'metadata': {'name': 'csms'}}]
    monkeypatch.setattr(bootstrap, 'ROOT', tmp_path)
    monkeypatch.setattr(bootstrap, 'git_clean', lambda: None)
    monkeypatch.setattr(bootstrap, 'render', lambda path: [frozen] + (additions if path == bootstrap.CANARY else []))
    monkeypatch.setattr(bootstrap, 'install_controller', lambda: events.append('install'))
    monkeypatch.setattr(bootstrap, 'create_kubeconfig', lambda duration: events.append('credential'))
    monkeypatch.setattr(bootstrap, 'module', lambda name: types.SimpleNamespace(manifest=lambda ref: ('repository', 'a' * 64, {'sha256:' + 'a' * 64})))
    def run(*args, **kwargs):
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
            actual['spec']['replicas'] = state['replicas']
            return actual
        if 'configmap' in args:
            return {'metadata': {'resourceVersion': '42'}, 'data': {'app.py': 'VALUE = 1\n'}}
        if 'pods' in args:
            return {'items': [{'metadata': {'name': str(i)}, 'status': {'containerStatuses': [
                {'name': 'csms', 'ready': True, 'imageID': image}]}} for i in range(4)]}
        raise AssertionError('Unexpected API call')
    monkeypatch.setattr(bootstrap, 'get', get)
    def sync(revision, resources):
        if resources[0]['kind'] == 'Deployment':
            state['replicas'] = yaml.safe_load((directory / bootstrap.RUNTIME).read_text())['spec']['replicas']
        events.append(('sync', resources[0]['kind'], state['replicas']))
    monkeypatch.setattr(bootstrap, 'sync', sync)
    def baseline(ref):
        events.append('baseline')
        if failure == 'baseline': raise ValueError('not ready')
    def analysis():
        events.append('analysis')
        if failure == 'analysis': raise ValueError('failed')
    monkeypatch.setattr(bootstrap, 'wait_baseline', baseline)
    monkeypatch.setattr(bootstrap, 'baseline_analysis', analysis)
    if failure:
        with pytest.raises(ValueError): bootstrap.bootstrap('24h', migrate_code_mount=migration)
        assert yaml.safe_load((directory / bootstrap.RUNTIME).read_text())['spec']['replicas'] == 4
        assert ('commit', 0) not in events
        assert state['replicas'] == 4
    else:
        bootstrap.bootstrap('24h', migrate_code_mount=migration)
        assert events.index('baseline') < events.index('analysis') < events.index(('commit', 0))
        assert events.index('analysis') < events.index(('sync', 'Deployment', 0))
        captured = yaml.safe_load((directory / bootstrap.RUNTIME).read_text())
        assert captured['spec']['replicas'] == 0
        assert state['replicas'] == 0
        if migration:
            assert captured['spec']['template']['spec']['containers'][0]['volumeMounts'] == [{'name': 'ca', 'mountPath': '/etc/chargeops-ca'}]


def test_repeated_revision_still_waits_for_its_own_sync_operation(monkeypatch):
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
    bootstrap.sync('c' * 40, [{'group': 'apps', 'kind': 'Deployment', 'name': 'csms'}])
    assert len(calls) == 3


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
