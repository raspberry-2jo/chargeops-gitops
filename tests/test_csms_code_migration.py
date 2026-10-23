"""Only migrate an app.py mount after comparing code inside the pinned image."""
import copy
import json
import subprocess
import sys

import pytest

from test_canary import bootstrap, live

IMAGE = 'registry.chargeops.lab:5000/chargeops/chargeops-csms@sha256:' + 'a' * 64


def mounted():
    source = live()
    pod = source['spec']['template']['spec']
    pod['volumes'].append({'name': 'app-code', 'configMap': {'name': 'csms-app-test'}})
    pod['containers'][0]['volumeMounts'] = [
        {'name': 'ca', 'mountPath': '/etc/chargeops-ca', 'readOnly': True},
        {'name': 'app-code', 'mountPath': '/app/csms/app.py', 'subPath': 'app.py', 'readOnly': True}]
    return source


def configmap():
    return {'metadata': {'name': 'csms-app-test', 'resourceVersion': '42'}, 'data': {'app.py': 'VALUE = 1\n'}}


def test_verified_migration_removes_only_code_mount_and_keeps_ca_placement_and_refs(monkeypatch):
    source = mounted()
    original = copy.deepcopy(source)
    commands = []
    monkeypatch.setattr(bootstrap, 'get', lambda *args: configmap())
    def run(*args, **kwargs):
        commands.append((args, kwargs))
        return json.dumps({'match': True, 'image_ast_sha256': 'b' * 64, 'mounted_ast_sha256': 'b' * 64})
    monkeypatch.setattr(bootstrap, 'run', run)
    migrated, proof = bootstrap.remove_verified_code_mount(source, IMAGE)
    assert source == original
    pod = migrated['spec']['template']['spec']
    assert pod['containers'][0]['volumeMounts'] == original['spec']['template']['spec']['containers'][0]['volumeMounts'][:1]
    assert pod['volumes'] == original['spec']['template']['spec']['volumes'][:1]
    for field in ['affinity', 'topologySpreadConstraints']:
        assert pod[field] == original['spec']['template']['spec'][field]
    assert pod['containers'][0]['env'] == original['spec']['template']['spec']['containers'][0]['env']
    assert proof == {'configmap': 'csms-app-test', 'resource_version': '42', 'code_ast_sha256': 'b' * 64}
    args, kwargs = commands[0]
    assert args[:3] == ('docker', 'run', '--rm')
    assert '--network=none' in args and '--read-only' in args and '--cap-drop=ALL' in args
    assert args[args.index('--entrypoint') + 1] == 'python'
    assert IMAGE in args
    assert kwargs['payload'] == configmap()['data']['app.py']
    bootstrap.runtime_patch(migrated)


@pytest.mark.parametrize('failure', ['different_code', 'malformed_proof', 'directory_mount', 'not_configmap', 'missing_key', 'dynamic_subpath'])
def test_unverified_migration_never_changes_source_or_cluster(monkeypatch, failure):
    source = mounted()
    cm = configmap()
    result = {'match': True, 'image_ast_sha256': 'b' * 64, 'mounted_ast_sha256': 'b' * 64}
    if failure == 'different_code': result.update(match=False, mounted_ast_sha256='c' * 64)
    if failure == 'malformed_proof': result['image_ast_sha256'] = 'not-a-digest'
    if failure == 'directory_mount': source['spec']['template']['spec']['containers'][0]['volumeMounts'][1]['mountPath'] = '/app/csms'
    if failure == 'not_configmap': source['spec']['template']['spec']['volumes'][1] = {'name': 'app-code', 'secret': {'secretName': 'do-not-read'}}
    if failure == 'missing_key': cm['data'] = {'other.py': 'do-not-copy'}
    if failure == 'dynamic_subpath': source['spec']['template']['spec']['containers'][0]['volumeMounts'][1]['subPathExpr'] = '$(FILE)'
    original = copy.deepcopy(source)
    def get(*args):
        assert 'secret' not in args
        return cm
    def run(*args, **kwargs):
        assert args[0] == 'docker'
        return json.dumps(result)
    monkeypatch.setattr(bootstrap, 'get', get)
    monkeypatch.setattr(bootstrap, 'run', run)
    with pytest.raises(ValueError):
        bootstrap.remove_verified_code_mount(source, IMAGE)
    assert source == original


@pytest.mark.parametrize('same', [True, False])
def test_comparison_program_compares_ast_without_executing_app_code(tmp_path, same):
    sentinel = tmp_path / 'must-not-exist'
    image = tmp_path / 'image-app.py'
    text = f'from pathlib import Path\nPath({str(sentinel)!r}).touch()\nVALUE = 1\n'
    image.write_text(text, encoding='utf-8')
    mounted = '# Different comment and whitespace\n' + text if same else text.replace('VALUE = 1', 'VALUE = 2')
    result = subprocess.run([sys.executable, '-c', bootstrap.code_comparison_program(str(image))],
                            input=mounted, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)['match'] is same
    assert not sentinel.exists()


def test_migration_proof_rejects_configmap_changes_before_sync(monkeypatch):
    proof = {'configmap': 'csms-app-test', 'resource_version': '42', 'code_ast_sha256': 'b' * 64}
    changed = configmap()
    changed['metadata']['resourceVersion'] = '43'
    monkeypatch.setattr(bootstrap, 'get', lambda *args: changed)
    with pytest.raises(ValueError, match='ConfigMap changed'):
        bootstrap.check_code_migration(proof)
