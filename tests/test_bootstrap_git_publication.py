"""A failed push must not become an Argo CD revision on the next bootstrap."""
import subprocess

import pytest

from test_canary import bootstrap


def git(directory, *args):
    return subprocess.check_output(['git', '-C', str(directory), *args], text=True).strip()


def test_retry_stops_on_clean_but_unpublished_bootstrap_commit(tmp_path, monkeypatch):
    remote = tmp_path / 'remote.git'
    local = tmp_path / 'checkout'
    git(tmp_path, 'init', '--bare', str(remote))
    git(tmp_path, 'init', '-b', 'main', str(local))
    git(local, 'config', 'user.name', 'Test')
    git(local, 'config', 'user.email', 'test@example.invalid')
    (local / 'baseline').write_text('baseline')
    git(local, 'add', '.')
    git(local, 'commit', '-m', 'baseline')
    git(local, 'remote', 'add', 'origin', str(remote))
    git(local, 'push', '-u', 'origin', 'main')
    runtime = local / bootstrap.PROD / bootstrap.RUNTIME
    runtime.parent.mkdir(parents=True)
    runtime.write_text('local-only runtime patch')
    git(local, 'add', '.')
    git(local, 'commit', '-m', 'bootstrap commit whose push failed')
    monkeypatch.setattr(bootstrap, 'ROOT', local)
    assert git(local, 'status', '--porcelain') == ''
    with pytest.raises(ValueError, match='not published'):
        bootstrap.git_clean()
    # Explicit publication fixes the precondition without resetting local history.
    git(local, 'push', 'origin', 'HEAD:main')
    bootstrap.git_clean()
    assert git(local, 'rev-parse', 'HEAD') == git(local, 'rev-parse', 'origin/main')


@pytest.mark.parametrize('message, hint', [
    ('Unable to resolve revision', 'Git revision could not be resolved'),
    ('Deployment selector field is immutable', 'immutable resource field'),
    ('Manifest generation error: kustomize build failed', 'Manifest generation failed'),
    ('permission denied', 'access was rejected'),
    ('unknown failure', 'status.operationState.message'),
])
def test_sync_failure_provides_fixed_hint_without_echoing_api_data(message, hint):
    secret = 'sensitive-payload-that-must-not-be-printed'
    error = bootstrap.sync_failure({'message': message + ' ' + secret,
                                    'syncResult': {'resources': [{'message': secret}]}})
    assert hint in error
    assert secret not in error
