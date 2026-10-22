"""Read-only cluster checks, then pin current production registry manifests locally.

Never applies Kubernetes resources, creates secrets, commits or pushes.
Run after disabling ops auto Sync and removing ops from Image Updater.
"""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = 'registry.chargeops.lab:5000'


def kubectl(*args):
    return json.loads(subprocess.check_output(['kubectl', *args, '-o', 'json'], text=True, encoding='utf-8'))


def check_database_settings():
    # Read in memory only. Never print, save or copy credentials.
    dev = kubectl('-n', 'dev', 'get', 'secret', 'chargeops-dev-secrets')['data']
    ops = kubectl('-n', 'ops', 'get', 'secret', 'chargeops-secrets')['data']
    assert dev.get('CSMS_API_TOKEN'), 'DEV CSMS_API_TOKEN is required'
    assert base64.b64decode(dev['DB_DSN']) == base64.b64decode(ops['DB_DSN']), 'Keep existing DB settings as requested'


def manifest(reference):
    repository, selector = reference.split('@', 1) if '@' in reference else reference.rsplit(':', 1)
    assert repository.startswith(REGISTRY + '/chargeops/')
    path = repository.removeprefix(REGISTRY + '/')
    assert re.fullmatch(r'[a-z0-9/._-]+', path) and re.fullmatch(r'[A-Za-z0-9._:-]+', selector)
    request = Request(f'http://{REGISTRY}/v2/{path}/manifests/{selector}', headers={'Accept': ', '.join([
        'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'])})
    with urlopen(request, timeout=30) as response:
        body, digest = response.read(), response.headers.get('Docker-Content-Digest')
    assert digest == 'sha256:' + hashlib.sha256(body).hexdigest(), 'Registry manifest digest mismatch'
    children = {m['digest'] for m in json.loads(body).get('manifests', [])}
    return repository, digest, children | {digest}


def main():
    app = kubectl('-n', 'argocd', 'get', 'application', 'chargeops-chatops-dev')
    assert app['spec']['destination']['namespace'] == 'ops'
    assert not app['spec'].get('syncPolicy', {}).get('automated'), 'Disable OPS auto Sync first'
    updater = kubectl('-n', 'argocd', 'get', 'imageupdater', 'chargeops-image-updater')
    assert all(r['namePattern'] == 'chargeops-dev' for r in updater['spec']['applicationRefs']), 'Apply DEV-only ImageUpdater first'
    check_database_settings()
    path = ROOT / 'chargeops-chatops/overlays/prod/kustomization.yaml'
    config = yaml.safe_load(path.read_text(encoding='utf-8'))
    docs = [d for d in yaml.safe_load_all(subprocess.check_output(
        ['kubectl', 'kustomize', 'chargeops-chatops/overlays/prod'], cwd=ROOT, text=True, encoding='utf-8')) if d]
    resolved = {}
    baseline = []
    for image in config['images']:
        repository = image.get('newName', image['name'])
        reference = repository + ('@' + image['digest'] if image.get('digest') else ':' + image['newTag'])
        resolved[repository] = manifest(reference)
    for d in docs:
        if d['kind'] != 'Deployment' or d['spec']['replicas'] == 0:
            continue
        live = kubectl('-n', 'ops', 'get', 'deployment', d['metadata']['name'])
        desired = d['spec']['template']['spec']['containers'][0]['image']
        assert live['spec']['template']['spec']['containers'][0]['image'] == desired, 'Live/Git image differs; reconcile OPS baseline first'
        selector = ','.join(f'{k}={v}' for k, v in live['spec']['selector']['matchLabels'].items())
        pods = kubectl('-n', 'ops', 'get', 'pods', '-l', selector)['items']
        pods = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
        assert len(pods) == live['spec']['replicas'], 'OPS deployment is not stable'
        repository = desired.split('@')[0].rsplit(':', 1)[0] if '@' not in desired else desired.split('@')[0]
        for pod in pods:
            assert any(c['type'] == 'Ready' and c['status'] == 'True' for c in pod['status'].get('conditions', [])), 'OPS Pod is not Ready'
            status = next(c for c in pod['status']['containerStatuses'] if c['name'] == d['spec']['template']['spec']['containers'][0]['name'])
            digest = re.search(r'sha256:[a-f0-9]{64}', status.get('imageID', ''))
            assert digest and digest.group() in resolved[repository][2], 'Registry tag moved since the live Pod started; resolve baseline manually'
        baseline.append({'deployment': d['metadata']['name'], 'replicas': live['spec']['replicas'],
                         'image': desired, 'pods': [
                             {'name': p['metadata']['name'], 'node': p['spec'].get('nodeName'),
                              'image_ids': [c.get('imageID') for c in p['status'].get('containerStatuses', [])]}
                             for p in pods]})
    for image in config['images']:
        repository = image.get('newName', image['name'])
        image['digest'] = resolved[repository][1]
        image.pop('newTag', None)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    output = ROOT / 'artifacts/ops-baseline.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({'deployments': baseline}, indent=2) + '\n', encoding='utf-8')
    print('PASS: existing DB settings retained, OPS stable, registry digests match live Pods.')
    print('Only prod/kustomization.yaml was pinned locally; non-secret Pod/image baseline saved to artifacts/ops-baseline.json. Review before commit/push; no cluster changes made.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not echo exceptions or DSNs from parsers, registry or kubectl.
        print(f'Preflight stopped ({error.__class__.__name__}). Check readiness, DB identities, updater, auto Sync and image baseline. No credentials printed.')
        raise SystemExit(1)
