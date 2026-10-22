"""Read-only cluster checks, then pin current production registry manifests locally.

Never applies Kubernetes resources, creates secrets, commits or pushes.
Run after disabling ops auto Sync and removing ops from Image Updater.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler
from urllib.error import HTTPError, URLError

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = 'registry.chargeops.lab:5000'


class RegistryLookupError(RuntimeError):
    """Only validated image references and fixed diagnostic text may be logged."""


def urlopen(request, timeout=30):
    if not request.full_url.startswith(f'https://{REGISTRY}/v2/'):
        raise RegistryLookupError('Registry requests must use the configured HTTPS endpoint.')
    context = ssl.create_default_context()
    ca = os.environ.get('CHARGEOPS_REGISTRY_CA')
    docker_ca = Path('/etc/docker/certs.d') / REGISTRY / 'ca.crt'
    if ca:
        context.load_verify_locations(cafile=ca)
    elif docker_ca.is_file():
        context.load_verify_locations(cafile=str(docker_ca))
    # This private registry is reached directly, independent of HTTP(S)_PROXY.
    return build_opener(ProxyHandler({}), HTTPSHandler(context=context)).open(request, timeout=timeout)


def kubectl(*args):
    return json.loads(subprocess.check_output(['kubectl', *args, '-o', 'json'], text=True, encoding='utf-8'))


def check_database_settings():
    # Read in memory only. Never print, save or copy credentials.
    dev = kubectl('-n', 'dev', 'get', 'secret', 'chargeops-dev-secrets')['data']
    ops = kubectl('-n', 'ops', 'get', 'secret', 'chargeops-secrets')['data']
    assert dev.get('CSMS_API_TOKEN'), 'DEV CSMS_API_TOKEN is required'
    assert base64.b64decode(dev['DB_DSN']) == base64.b64decode(ops['DB_DSN']), 'Keep existing DB settings as requested'


def image_repository(reference):
    """Strip a tag and/or digest without stripping the registry port."""
    name = reference.split('@', 1)[0]
    parent, slash, leaf = name.rpartition('/')
    return parent + slash + leaf.split(':', 1)[0]


def image_identity(reference):
    # With a digest present the optional tag does not affect image selection.
    if '@' in reference:
        return image_repository(reference) + '@' + reference.split('@', 1)[1]
    return reference


def manifest(reference, depth=0):
    repository, selector = reference.split('@', 1) if '@' in reference else reference.rsplit(':', 1)
    repository = image_repository(reference)
    assert repository.startswith(REGISTRY + '/chargeops/')
    path = repository.removeprefix(REGISTRY + '/')
    assert re.fullmatch(r'[a-z0-9/._-]+', path) and re.fullmatch(r'[A-Za-z0-9._:-]+', selector)
    request = Request(f'https://{REGISTRY}/v2/{path}/manifests/{selector}', headers={'Accept': ', '.join([
        'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'])})
    try:
        with urlopen(request, timeout=30) as response:
            body, digest = response.read(), response.headers.get('Docker-Content-Digest')
    except HTTPError as error:
        hints = {401: 'Registry authentication is required.',
                 403: 'Registry access is denied.',
                 404: 'Repository, tag or digest is missing; compare GitOps with the existing running image.'}
        hint = hints.get(error.code, 'Check the Registry/proxy HTTP service and connectivity.')
        raise RegistryLookupError(f'HTTP {error.code} while reading {reference}. {hint}') from None
    except URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            raise RegistryLookupError('Registry TLS certificate verification failed. Set CHARGEOPS_REGISTRY_CA to the trusted CA file, or install /etc/docker/certs.d/registry.chargeops.lab:5000/ca.crt.') from None
        raise RegistryLookupError(f'Cannot reach Registry while reading {reference}. Check DNS, port 5000 and proxy settings.') from None
    assert digest == 'sha256:' + hashlib.sha256(body).hexdigest(), 'Registry manifest digest mismatch'
    if selector.startswith('sha256:'):
        assert digest == selector, 'Registry returned a different digest than requested'
    document = json.loads(body)
    descriptors = document.get('manifests', [])
    identities = {m['digest'] for m in descriptors} | {digest}
    config_digest = document.get('config', {}).get('digest')
    if config_digest:
        identities.add(config_digest)
    # Some runtimes report a platform manifest or image configuration ID.
    # Verify these identities through the index, not by comparing raw hashes.
    if depth < 2:
        assert len(descriptors) <= 64, 'Registry image index is unexpectedly large'
        for item in descriptors:
            if item.get('platform', {}).get('architecture') == 'unknown':
                continue  # Build attestations are not runtime image platforms.
            _, _, child_ids = manifest(repository + '@' + item['digest'], depth + 1)
            identities.update(child_ids)
    return repository, digest, identities


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
    selected_live = {}
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
        live_image = live['spec']['template']['spec']['containers'][0]['image']
        # A mutable live tag and a pinned Git digest can select the same image.
        # Keep the repository boundary here; verify actual Pod identities against
        # Registry manifests below before choosing the production baseline.
        assert image_repository(live_image) == image_repository(desired), f'{d["metadata"]["name"]}: Live/Git image repository differs; reconcile OPS baseline first'
        selector = ','.join(f'{k}={v}' for k, v in live['spec']['selector']['matchLabels'].items())
        pods = kubectl('-n', 'ops', 'get', 'pods', '-l', selector)['items']
        pods = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
        assert len(pods) == live['spec']['replicas'], 'OPS deployment is not stable'
        repository = image_repository(desired)
        runtime_digests = set()
        for pod in pods:
            assert any(c['type'] == 'Ready' and c['status'] == 'True' for c in pod['status'].get('conditions', [])), 'OPS Pod is not Ready'
            status = next(c for c in pod['status']['containerStatuses'] if c['name'] == d['spec']['template']['spec']['containers'][0]['name'])
            digest = re.search(r'sha256:[a-f0-9]{64}', status.get('imageID', ''))
            assert digest, f'{d["metadata"]["name"]}: running Pod image digest is missing'
            runtime_digests.add(digest.group())
        chosen = resolved[repository]
        if not runtime_digests.issubset(chosen[2]):
            assert len(runtime_digests) == 1, f'{d["metadata"]["name"]}: running Pods have mixed image digests; wait for a stable baseline'
            current_digest = next(iter(runtime_digests))
            chosen = manifest(repository + '@' + current_digest)
            assert chosen[1] == current_digest, 'Existing Pod manifest could not be verified'
            print(f'{d["metadata"]["name"]}: tag differs from running Pods; preserving verified running digest {current_digest}')
        if repository in selected_live:
            assert runtime_digests.issubset(selected_live[repository][2]), 'Workloads sharing an image repository run different versions; resolve the baseline first'
            chosen = selected_live[repository]
        selected_live[repository] = chosen
        resolved[repository] = chosen
        baseline.append({'deployment': d['metadata']['name'], 'replicas': live['spec']['replicas'],
                         'image': live_image, 'git_image': desired, 'pods': [
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
    except RegistryLookupError as error:
        print(f'Preflight stopped: {error}')
        print('Production image settings were not written; Kubernetes was not changed.')
        raise SystemExit(1)
    except AssertionError as error:
        # These messages are fixed local validation rules, never DSNs or HTTP bodies.
        print(f'Preflight stopped: {error or "Invalid image configuration."}')
        print('Kubernetes was not changed.')
        raise SystemExit(1)
    except Exception as error:
        # Do not echo exceptions or DSNs from parsers, registry or kubectl.
        print(f'Preflight stopped ({error.__class__.__name__}). Check readiness, existing DB settings, updater, auto Sync and image baseline. No credentials printed.')
        raise SystemExit(1)
