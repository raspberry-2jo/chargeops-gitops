import importlib.util
from pathlib import Path
import os

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
