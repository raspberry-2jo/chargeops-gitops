import importlib.util
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('code', [401, 404, 503])
def test_registry_failure_identifies_image_and_status_without_response_payload(monkeypatch, code):
    from urllib.error import HTTPError
    from io import BytesIO
    spec = importlib.util.spec_from_file_location('freeze_registry', ROOT / 'scripts/freeze-production.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def fail(*args, **kwargs):
        raise HTTPError('http://proxy.invalid/private', code, 'response-message-must-not-be-logged',
                        {}, BytesIO(b'response-body-must-not-be-logged'))
    monkeypatch.setattr(module, 'urlopen', fail)
    reference = 'registry.chargeops.lab:5000/chargeops/chargeops-csms:dev'
    with pytest.raises(Exception) as error:
        module.manifest(reference)
    message = str(error.value)
    assert f'HTTP {code}' in message
    assert reference in message
    assert 'response-message' not in message and 'response-body' not in message


def test_environment_boundaries():
    subprocess.run([sys.executable, str(ROOT / 'scripts/validate-environments.py')], cwd=ROOT, check=True)


def test_original_production_render_preserved():
    # The migration must preserve the reviewed baseline, including external refs.
    # Fixture-free check: the compatibility path and prod are independently rendered.
    import yaml
    def render(name):
        return list(yaml.safe_load_all(subprocess.check_output(
            ['kubectl', 'kustomize', f'chargeops-chatops/overlays/{name}'], cwd=ROOT, text=True, encoding='utf-8')))
    assert render('dev') == render('prod')


def test_existing_db_settings_are_retained_without_printing_secrets(monkeypatch):
    import base64
    spec = importlib.util.spec_from_file_location('freeze', ROOT / 'scripts/freeze-production.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = {'data': {'DB_DSN': base64.b64encode(b'existing settings').decode(), 'CSMS_API_TOKEN': 'present'}}
    monkeypatch.setattr(module, 'kubectl', lambda *args: data)
    module.check_database_settings()


def test_dev_secret_preparation_copies_only_two_keys_without_logging(monkeypatch, capsys):
    import json
    spec = importlib.util.spec_from_file_location('prepare_dev', ROOT / 'scripts/prepare-dev-secret.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payloads = []
    def execute(args, payload=None):
        if payload:
            payloads.append(json.loads(payload))
            return ''
        return json.dumps({'data': {'DB_DSN': 'test-encoded-dsn', 'CSMS_API_TOKEN': 'test-encoded-token',
                                    'SLACK_BOT_TOKEN': 'must-not-copy'}})
    monkeypatch.setattr(module, 'execute', execute)
    module.main()
    secret = next(d for d in payloads if d['kind'] == 'Secret')
    assert secret['metadata'] == {'name': 'chargeops-dev-secrets', 'namespace': 'dev'}
    assert secret['data'] == {'DB_DSN': 'test-encoded-dsn', 'CSMS_API_TOKEN': 'test-encoded-token'}
    assert 'test-encoded' not in capsys.readouterr().out
