import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('app_access', ROOT / 'scripts/configure-ci-app-access.py')
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


@pytest.mark.parametrize('invalid', [None, 'namespace', 'path', 'branch', 'auto'])
def test_app_access_only_writes_exact_named_application_roles(monkeypatch, invalid):
    writes = []
    def run(*args, payload=None):
        if 'application' in args:
            dev = 'actual-dev' in args
            spec = {'destination': {'namespace': 'dev' if dev else 'ops'},
                    'source': {'targetRevision': 'main', 'path': 'chargeops-chatops/overlays/' + ('development' if dev else 'canary')}}
            if not dev:
                if invalid == 'namespace': spec['destination']['namespace'] = 'dev'
                if invalid == 'path': spec['source']['path'] = 'wrong'
                if invalid == 'branch': spec['source']['targetRevision'] = 'dev'
                if invalid == 'auto': spec['syncPolicy'] = {'automated': {}}
            return json.dumps({'spec': spec})
        if 'apply' in args:
            writes.extend(yaml.safe_load_all(payload))
        return ''
    monkeypatch.setattr(access, 'run', run)
    if invalid:
        with pytest.raises(ValueError): access.configure('actual-dev', 'actual-ops')
        assert writes == []
    else:
        access.configure('actual-dev', 'actual-ops')
        assert len(writes) == 4
        assert all(d['kind'] in ['Role', 'RoleBinding'] and d['metadata']['namespace'] == 'argocd' for d in writes)
        for role in [d for d in writes if d['kind'] == 'Role']:
            expected = 'actual-dev' if role['metadata']['name'] == 'jenkins-dev-application-reader' else 'actual-ops'
            assert role['rules'][0]['resourceNames'] == [expected]
