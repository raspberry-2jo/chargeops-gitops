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


def test_git_pull_failure_reports_step_without_echoing_sensitive_child_output(monkeypatch):
    def failed(*args, **kwargs):
        return subprocess.CompletedProcess(args, 128, '', 'fatal: Authentication failed https://user:private-token@example.invalid')
    monkeypatch.setattr(bootstrap.subprocess, 'run', failed)
    with pytest.raises(ValueError, match='git pull') as error:
        bootstrap.run('git', 'pull', '--ff-only', 'origin', 'main')
    assert 'authentication' in str(error.value).lower()
    assert 'private-token' not in str(error.value)


def test_api_failure_reports_step_and_fixed_cause(monkeypatch):
    monkeypatch.setattr(bootstrap.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, '', 'Unable to connect to the server: x509: certificate signed by unknown authority'))
    with pytest.raises(ValueError, match='kubectl get.*CA'):
        bootstrap.run('kubectl', '-n', 'ops', 'get', 'deployment', 'csms', '-o', 'json')
