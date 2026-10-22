import importlib.util
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('suffix', [':dev', '@sha256:' + 'a' * 64, ':dev@sha256:' + 'a' * 64])
def test_image_repository_preserves_port_and_strips_tag_digest(suffix):
    spec = importlib.util.spec_from_file_location('freeze_reference', ROOT / 'scripts/freeze-production.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repository = 'registry.chargeops.lab:5000/chargeops/chargeops-ai-gateway'
    assert module.image_repository(repository + suffix) == repository
    assert module.image_identity(repository + ':dev@sha256:' + 'a' * 64) == repository + '@sha256:' + 'a' * 64


@pytest.mark.parametrize('scenario', ['matching', 'tag_moved', 'mixed_pods', 'missing_live_manifest',
                                      'pinned_git_live_tag', 'different_repository'])
def test_freeze_handles_rendered_tag_and_digest_through_complete_preflight(tmp_path, monkeypatch, scenario):
    import base64
    import copy
    import json
    import yaml
    spec = importlib.util.spec_from_file_location('freeze_complete', ROOT / 'scripts/freeze-production.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config_path = Path('chargeops-chatops/overlays/prod/kustomization.yaml')
    config = yaml.safe_load((ROOT / config_path).read_text(encoding='utf-8'))
    rendered = subprocess.check_output(['kubectl', 'kustomize', 'chargeops-chatops/overlays/prod'], cwd=ROOT, text=True, encoding='utf-8')
    if scenario in ['pinned_git_live_tag', 'different_repository']:
        entry = next(i for i in config['images'] if i['name'].endswith('/chargeops-csms'))
        entry['digest'] = 'sha256:' + 'a' * 64
        entry.pop('newTag', None)
        documents = list(yaml.safe_load_all(rendered))
        for document in documents:
            if document and document['kind'] == 'Deployment' and document['metadata']['name'] == 'csms':
                document['spec']['template']['spec']['containers'][0]['image'] = entry['newName'] + '@' + entry['digest']
        rendered = yaml.safe_dump_all(documents)
    deployments = {d['metadata']['name']: d for d in yaml.safe_load_all(rendered) if d and d['kind'] == 'Deployment'}
    target = tmp_path / config_path
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(config), encoding='utf-8')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *args, **kwargs: rendered)
    def image_entry(reference):
        return next(i for i in config['images'] if reference.startswith(i['newName'] + ':') or reference.startswith(i['newName'] + '@'))
    def digest(entry): return entry.get('digest', 'sha256:' + 'a' * 64)
    def manifest(reference):
        entry = image_entry(reference)
        if 'chargeops-csms@sha256:' in reference and not reference.endswith(digest(entry)):
            if scenario == 'missing_live_manifest':
                raise module.RegistryLookupError('Existing Pod manifest is not in the Registry')
            old_digest = reference.split('@', 1)[1]
            return entry['newName'], old_digest, {old_digest}
        return entry['newName'], digest(entry), {digest(entry)}
    monkeypatch.setattr(module, 'manifest', manifest)
    def kubectl(*args):
        if 'application' in args: return {'spec': {'destination': {'namespace': 'ops'}}}
        if 'imageupdater' in args: return {'spec': {'applicationRefs': [{'namePattern': 'chargeops-dev'}]}}
        if 'secret' in args: return {'data': {'DB_DSN': base64.b64encode(b'shared test-only DB settings').decode(), 'CSMS_API_TOKEN': 'present'}}
        if 'deployment' in args:
            live = copy.deepcopy(deployments[args[-1]])
            if args[-1] == 'csms' and scenario in ['pinned_git_live_tag', 'different_repository']:
                repository = 'apps' if scenario == 'different_repository' else 'chargeops-csms'
                live['spec']['template']['spec']['containers'][0]['image'] = module.REGISTRY + '/chargeops/' + repository + ':dev'
            return live
        if 'pods' in args:
            d = next(d for d in deployments.values() if args[-1] == ','.join(f'{k}={v}' for k,v in d['spec']['selector']['matchLabels'].items()))
            c = d['spec']['template']['spec']['containers'][0]
            entry = image_entry(c['image'])
            return {'items': [{'metadata': {'name': f'{d["metadata"]["name"]}-{i}'}, 'spec': {'nodeName': 'test-worker'},
                               'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                                          'containerStatuses': [{'name': c['name'], 'imageID': entry['newName'] + '@' +
                                            (('sha256:' + ('c' if scenario == 'mixed_pods' and i else 'b') * 64)
                                             if d['metadata']['name'] == 'csms' and scenario in ['tag_moved', 'mixed_pods', 'missing_live_manifest'] else digest(entry))}]}}
                              for i in range(d['spec']['replicas'])]}
        raise AssertionError('Unexpected cluster request')
    monkeypatch.setattr(module, 'kubectl', kubectl)
    if scenario in ['mixed_pods', 'missing_live_manifest', 'different_repository']:
        original = target.read_text()
        with pytest.raises((AssertionError, module.RegistryLookupError)):
            module.main()
        assert target.read_text() == original
        assert not (tmp_path / 'artifacts/ops-baseline.json').exists()
        return
    module.main()
    frozen = yaml.safe_load(target.read_text())
    assert all(i.get('digest') and 'newTag' not in i for i in frozen['images'])
    csms = next(i for i in frozen['images'] if i['name'].endswith('/chargeops-csms'))
    expected = 'sha256:' + 'b' * 64 if scenario == 'tag_moved' else digest(next(i for i in config['images'] if i['name'].endswith('/chargeops-csms')))
    assert csms['digest'] == expected
    assert {d['deployment'] for d in json.loads((tmp_path / 'artifacts/ops-baseline.json').read_text())['deployments']} == {'csms', 'ai-gateway', 'notifier'}


def test_registry_lookup_uses_https(monkeypatch):
    import hashlib
    spec = importlib.util.spec_from_file_location('freeze_https', ROOT / 'scripts/freeze-production.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    body = b'{"schemaVersion":2}'
    class Response:
        headers = {'Docker-Content-Digest': 'sha256:' + hashlib.sha256(body).hexdigest()}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return body
    def open_manifest(request, **kwargs):
        assert request.full_url.startswith('https://registry.chargeops.lab:5000/v2/')
        return Response()
    monkeypatch.setattr(module, 'urlopen', open_manifest)
    module.manifest('registry.chargeops.lab:5000/chargeops/chargeops-chatops-bot:dev')


def test_index_platform_and_runtime_config_ids_are_verified_together(monkeypatch):
    import hashlib
    import json
    spec = importlib.util.spec_from_file_location('freeze_index', ROOT / 'scripts/freeze-production.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config_id = 'sha256:' + 'c' * 64
    child = json.dumps({'schemaVersion': 2, 'config': {'digest': config_id}}).encode()
    child_id = 'sha256:' + hashlib.sha256(child).hexdigest()
    index = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': child_id, 'platform': {'os': 'linux', 'architecture': 'amd64'}}]}).encode()
    index_id = 'sha256:' + hashlib.sha256(index).hexdigest()
    def fetch(request, **kwargs):
        body = child if request.full_url.endswith(child_id) else index
        class Response:
            headers = {'Docker-Content-Digest': 'sha256:' + hashlib.sha256(body).hexdigest()}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return body
        return Response()
    monkeypatch.setattr(module, 'urlopen', fetch)
    _, pinned, identities = module.manifest('registry.chargeops.lab:5000/chargeops/chargeops-csms:dev')
    assert pinned == index_id
    assert {index_id, child_id, config_id}.issubset(identities)


def test_https_transport_keeps_tls_verification_and_bypasses_proxy(monkeypatch):
    import ssl
    from urllib.request import Request, ProxyHandler, HTTPSHandler
    spec = importlib.util.spec_from_file_location('freeze_transport', ROOT / 'scripts/freeze-production.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv('CHARGEOPS_REGISTRY_CA', raising=False)
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.example.invalid:8080')
    def opener(*handlers):
        proxy = next(h for h in handlers if isinstance(h, ProxyHandler))
        context = next(h for h in handlers if isinstance(h, HTTPSHandler))._context
        assert proxy.proxies == {}
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        class Client:
            def open(self, request, timeout): return 'verified-direct-https'
        return Client()
    monkeypatch.setattr(module, 'build_opener', opener)
    assert module.urlopen(Request('https://registry.chargeops.lab:5000/v2/')) == 'verified-direct-https'


def test_custom_registry_ca_is_loaded(monkeypatch):
    from types import SimpleNamespace
    from urllib.request import Request
    spec = importlib.util.spec_from_file_location('freeze_ca', ROOT / 'scripts/freeze-production.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loaded = []
    monkeypatch.setenv('CHARGEOPS_REGISTRY_CA', '/trusted/registry-ca.crt')
    monkeypatch.setattr(module.ssl, 'create_default_context', lambda: SimpleNamespace(load_verify_locations=lambda **kw: loaded.append(kw)))
    monkeypatch.setattr(module, 'build_opener', lambda *args: SimpleNamespace(open=lambda *a, **k: None))
    module.urlopen(Request('https://registry.chargeops.lab:5000/v2/'))
    assert loaded == [{'cafile': '/trusted/registry-ca.crt'}]


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
