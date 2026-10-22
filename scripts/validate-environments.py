"""Render and enforce the development/production boundary without cluster access."""
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(name):
    return [d for d in yaml.safe_load_all(subprocess.check_output(
        ['kubectl', 'kustomize', f'chargeops-chatops/overlays/{name}'],
        cwd=ROOT, text=True, encoding='utf-8')) if d]


def validate():
    prod, legacy, dev = render('prod'), render('dev'), render('development')
    assert prod == legacy, 'Legacy ops path must stay equivalent to prod'
    for name, docs, namespace in [('prod', prod, 'ops'), ('development', dev, 'dev')]:
        ids = [(d['kind'], d['metadata']['name'], d['metadata'].get('namespace')) for d in docs]
        assert len(ids) == len(set(ids)), f'{name}: duplicate resource IDs'
        for d in docs:
            if d['kind'] == 'Namespace':
                assert name == 'development' and d['metadata']['name'] == 'dev'
            else:
                assert d['metadata'].get('namespace') == namespace, f'{name}: wrong namespace'
            assert d['kind'] != 'Secret', 'Do not commit Secret values'
    deployments = [d for d in dev if d['kind'] == 'Deployment']
    assert len(deployments) == 1 and deployments[0]['metadata']['name'] == 'csms'
    d = deployments[0]
    assert d['spec']['replicas'] == 1
    assert d['spec']['selector']['matchLabels'] == {'app': 'csms'}
    c = d['spec']['template']['spec']['containers'][0]
    env = {entry['name']: entry for entry in c['env']}
    assert env['DB_DSN']['valueFrom']['secretKeyRef']['name'] == 'chargeops-dev-secrets'
    assert env['API_TOKEN']['valueFrom']['secretKeyRef']['name'] == 'chargeops-dev-secrets'
    assert c['envFrom'] == [{'configMapRef': {'name': 'chargeops-dev-config'}}]
    assert not any('ops.svc' in str(item) or 'chargeops-secrets' in str(item) for item in dev)
    assert not any(d['kind'] in ['CronJob', 'PersistentVolumeClaim'] for d in dev)
    argo = ROOT / 'chargeops-chatops/argocd'
    ops_app = yaml.safe_load((argo / 'application.yaml').read_text(encoding='utf-8'))
    dev_app = yaml.safe_load((argo / 'application-dev.yaml').read_text(encoding='utf-8'))
    assert ops_app['spec']['destination']['namespace'] == 'ops'
    assert ops_app['spec']['source']['path'].endswith('/prod')
    assert not ops_app['spec']['syncPolicy'].get('automated')
    assert dev_app['spec']['destination']['namespace'] == 'dev'
    assert dev_app['spec']['source']['path'].endswith('/development')
    assert dev_app['spec']['syncPolicy']['automated']['selfHeal'] is True
    updater = yaml.safe_load((argo / 'image-updater.yaml').read_text(encoding='utf-8'))
    assert len(updater['spec']['applicationRefs']) == 1
    assert updater['spec']['applicationRefs'][0]['namePattern'] == 'chargeops-dev'
    print('PASS: ops compatibility, namespace isolation, dev-only image updater, manual ops Sync')


if __name__ == '__main__':
    validate()
