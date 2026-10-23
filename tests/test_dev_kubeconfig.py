import importlib.util
from pathlib import Path
import os
import base64
import copy
import json
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dev_kubeconfig', ROOT / 'scripts/prepare-dev-kubeconfig.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_config_contains_only_restricted_identity_not_admin_auth():
    source = {'server': 'https://api.example.invalid:6443', 'certificate-authority-data': 'public-ca',
              'client-key-data': 'must-not-copy', 'token': 'must-not-copy'}
    config = module.make_config(source, 'test-only-dev-token')
    assert config['users'] == [{'name': 'jenkins-dev-verifier', 'user': {'token': 'test-only-dev-token'}}]
    assert config['clusters'][0]['cluster'] == {'server': source['server'], 'certificate-authority-data': 'public-ca'}
    assert config['contexts'][0]['context']['namespace'] == 'dev'
    assert 'must-not-copy' not in yaml.safe_dump(config)


def test_failed_authorization_preserves_existing_file(tmp_path):
    output = tmp_path / 'dev-kubeconfig.yaml'
    output.write_text('existing')
    def fail(path): raise ValueError('verification failed')
    with pytest.raises(ValueError):
        module.write_config({'new': 'config'}, output, verifier=fail)
    assert output.read_text() == 'existing'
    assert list(tmp_path.iterdir()) == [output]


def test_successful_file_is_private_and_validated(tmp_path):
    output = tmp_path / 'private/dev-kubeconfig.yaml'
    calls = []
    module.write_config({'kind': 'Config'}, output, verifier=lambda p: calls.append(yaml.safe_load(p.read_text())))
    assert calls == [{'kind': 'Config'}]
    assert yaml.safe_load(output.read_text()) == {'kind': 'Config'}
    if os.name != 'nt':
        assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('cluster', [
    {'server': 'http://api.invalid', 'certificate-authority-data': 'ca'},
    {'server': 'https://api.invalid'},
    {'server': 'https://api.invalid', 'certificate-authority-data': 'ca', 'insecure-skip-tls-verify': True},
])
def test_unverified_cluster_config_is_rejected(cluster):
    with pytest.raises(ValueError): module.make_config(cluster, 'test-only-token')


def secret_token(namespace, account, claims=None):
    claims = claims if claims is not None else {'sub': f'system:serviceaccount:{namespace}:{account}'}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    token = 'test-header.' + payload + '.test-signature'
    return token, {'type': 'kubernetes.io/service-account-token', 'metadata': {'name': account + '-token',
        'annotations': {'kubernetes.io/service-account.name': account, 'kubernetes.io/service-account.uid': 'sa-uid'}},
        'data': {'token': base64.b64encode(token.encode()).decode()}}


@pytest.mark.parametrize('namespace,account', [('dev', 'jenkins-dev-verifier'), ('ops', 'jenkins-canary')])
@pytest.mark.parametrize('existing', [False, True])
def test_nonexpiring_tokens_are_created_once_and_reused(monkeypatch, capsys, namespace, account, existing):
    token, secret = secret_token(namespace, account)
    calls, writes = [], []
    def kubectl(*args, **kwargs):
        calls.append(args)
        if 'serviceaccount' in args:
            return json.dumps({'metadata': {'uid': 'sa-uid'}})
        if args[:3] == ('create', '-f', '-'):
            writes.append(json.loads(kwargs['payload']))
            return ''
        if '--ignore-not-found' in args and not existing:
            return ''
        return json.dumps(secret)
    monkeypatch.setattr(module, 'kubectl', kubectl)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    assert module.service_account_token(namespace, account) == token
    assert not any('token' in call and 'create' in call for call in calls)
    assert not any(call[0] in ['apply', 'patch', 'delete'] for call in calls)
    if existing:
        assert not writes
    else:
        assert writes == [{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': account + '-token',
            'namespace': namespace, 'annotations': {'kubernetes.io/service-account.name': account}},
            'type': 'kubernetes.io/service-account-token'}]
    assert token not in capsys.readouterr().out


@pytest.mark.parametrize('failure', ['expiring', 'wrong_identity', 'wrong_uid', 'wrong_type', 'invalidated', 'malformed'])
def test_invalid_existing_token_is_not_reused_or_overwritten(monkeypatch, failure):
    token, secret = secret_token('dev', 'jenkins-dev-verifier')
    if failure == 'expiring':
        token, secret = secret_token('dev', 'jenkins-dev-verifier', {'sub': 'system:serviceaccount:dev:jenkins-dev-verifier', 'exp': 2000000000})
    if failure == 'wrong_identity': secret['metadata']['annotations']['kubernetes.io/service-account.name'] = 'another-account'
    if failure == 'wrong_uid': secret['metadata']['annotations']['kubernetes.io/service-account.uid'] = 'deleted-sa-uid'
    if failure == 'wrong_type': secret['type'] = 'Opaque'
    if failure == 'invalidated': secret['metadata']['labels'] = {'kubernetes.io/legacy-token-invalid-since': '2026-10-23'}
    if failure == 'malformed': secret['data']['token'] = base64.b64encode(b'do-not-print-malformed-token').decode()
    def kubectl(*args, **kwargs):
        assert args[2] == 'get'
        return json.dumps({'metadata': {'uid': 'sa-uid'}} if 'serviceaccount' in args else secret)
    monkeypatch.setattr(module, 'kubectl', kubectl)
    with pytest.raises(ValueError) as error:
        module.service_account_token('dev', 'jenkins-dev-verifier')
    assert token not in str(error.value)
    assert 'do-not-print' not in str(error.value)


def test_waits_for_secret_controller_without_printing_token(monkeypatch):
    token, populated = secret_token('dev', 'jenkins-dev-verifier')
    pending = copy.deepcopy(populated)
    pending.pop('data')
    responses = iter([pending, pending, populated])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    def kubectl(*args, **kwargs):
        return json.dumps({'metadata': {'uid': 'sa-uid'}} if 'serviceaccount' in args else next(responses))
    monkeypatch.setattr(module, 'kubectl', kubectl)
    assert module.service_account_token('dev', 'jenkins-dev-verifier') == token


def test_secret_population_timeout_preserves_kubeconfig(monkeypatch):
    _, pending = secret_token('dev', 'jenkins-dev-verifier')
    pending.pop('data')
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(module, 'kubectl', lambda *a, **kw: json.dumps({'metadata': {'uid': 'sa-uid'}} if 'serviceaccount' in a else pending))
    with pytest.raises(ValueError, match='sixty seconds'):
        module.service_account_token('dev', 'jenkins-dev-verifier')


def test_explicit_bounded_duration_remains_available(monkeypatch):
    calls = []
    monkeypatch.setattr(module, 'kubectl', lambda *a, **kw: calls.append(a) or 'bounded-token')
    assert module.service_account_token('dev', 'jenkins-dev-verifier', '24h') == 'bounded-token'
    assert calls == [('-n', 'dev', 'create', 'token', 'jenkins-dev-verifier', '--duration=24h')]


def test_dev_cli_defaults_to_nonexpiring_token_without_logging_it(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['prepare-dev-kubeconfig.py'])
    monkeypatch.setattr(module, 'kubectl', lambda *a, **kw: json.dumps({'clusters': [{'cluster': {
        'server': 'https://api.invalid:6443', 'certificate-authority-data': 'public-ca'}}]}))
    monkeypatch.setattr(module, 'prepare_rbac', lambda: None)
    def token(namespace, account, duration):
        assert (namespace, account, duration) == ('dev', 'jenkins-dev-verifier', 'unlimited')
        return 'test-only-private-token'
    monkeypatch.setattr(module, 'service_account_token', token)
    monkeypatch.setattr(module, 'write_config', lambda *a, **kw: None)
    module.main()
    output = capsys.readouterr().out
    assert 'Token expiration: none' in output
    assert 'test-only-private-token' not in output
