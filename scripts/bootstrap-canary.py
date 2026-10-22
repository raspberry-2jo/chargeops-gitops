"""Install pinned Rollouts, preserve live CSMS settings and hand over via Argo CD.

Run on mgmt with the existing administrator context and Git main push access.
Never reads Secret data. Token is saved only outside Git, never logged.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
from urllib.request import urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROD = 'chargeops-chatops/overlays/prod'
CANARY = 'chargeops-chatops/overlays/canary'
APPLICATION = 'chargeops-chatops-dev'
VERSION = 'v1.10.1'
INSTALL_SHA256 = '149148429a6f8258b789003ffb9bc6698a526d611ecb1b185624217cf51bae7f'
RUNTIME = 'csms-runtime.yaml'
SAFE_ENV = {'DB_POOL', 'POD_IP', 'PEERS_DNS', 'DRAIN_SECONDS', 'REQUIRE_CN',
            'HEARTBEAT_INTERVAL', 'OCPP_PORT', 'METRICS_PORT', 'ENVIRONMENT',
            'PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE', 'TZ', 'PORT', 'HOST'}


def run(*args, payload=None):
    result = subprocess.run(args, input=payload, text=True, capture_output=True, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(args[0] + ' failed; check connectivity and permissions; child output withheld')
    return result.stdout.strip()


def get(*args):
    return json.loads(run('kubectl', *args, '-o', 'json'))


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def render(path):
    return [d for d in yaml.safe_load_all(run('kubectl', 'kustomize', path)) if d]


def git_clean():
    if run('git', 'branch', '--show-current') != 'main' or run('git', 'status', '--porcelain'):
        raise ValueError('Use a clean GitOps main checkout; commit existing baseline edits first')
    run('git', 'pull', '--ff-only', 'origin', 'main')


def commit_runtime(message):
    allowed = {PROD + '/kustomization.yaml', PROD + '/' + RUNTIME}
    changed = set(run('git', 'diff', '--name-only').splitlines())
    untracked = set(run('git', 'ls-files', '--others', '--exclude-standard').splitlines())
    if (changed | untracked) - allowed:
        raise ValueError('Bootstrap only commits its reviewed CSMS runtime patch and prod kustomization')
    if changed or untracked:
        run('git', 'add', '--', *sorted(allowed))
        run('git', '-c', 'user.name=ChargeOps Bootstrap', '-c', 'user.email=chargeops-ci@example.invalid',
            'commit', '-m', message)
        run('git', 'push', 'origin', 'HEAD:main')
    return run('git', 'rev-parse', 'HEAD')


def runtime_patch(live):
    template = copy.deepcopy(live['spec']['template'])
    pod = template['spec']
    containers = pod.get('containers', [])
    if len(containers) != 1 or containers[0]['name'] != 'csms' or pod.get('initContainers'):
        raise ValueError('Unexpected CSMS sidecar/init containers; bootstrap will not copy them into Git')
    container = containers[0]
    for env in container.get('env', []):
        if 'value' in env and env['name'] not in SAFE_ENV:
            raise ValueError('Unexpected literal CSMS environment setting; move sensitive values to Secret references first')
    command = container.get('command', []) + container.get('args', [])
    allowed = [[], ['sh', '-c', 'ulimit -n 65536 && exec uvicorn csms.app:app --host 0.0.0.0 --port 8000'],
               ['uvicorn', 'csms.app:app', '--host', '0.0.0.0', '--port', '8000'],
               ['python', '-m', 'uvicorn', 'csms.app:app', '--host', '0.0.0.0', '--port', '8000']]
    if command not in allowed:
        raise ValueError('Unexpected CSMS command; refusing to copy arbitrary command text into Git')
    if container.get('lifecycle') or any(container.get(probe, {}).get('httpGet', {}).get('httpHeaders')
            for probe in ['readinessProbe', 'livenessProbe', 'startupProbe']):
        raise ValueError('Lifecycle commands or probe headers require review before copying into Git')
    # Only references to existing ConfigMaps, Secrets, PVCs or emptyDir are copied.
    for volume in pod.get('volumes', []):
        if set(volume) - {'name', 'configMap', 'secret', 'emptyDir', 'persistentVolumeClaim', 'projected'}:
            raise ValueError('Unexpected CSMS volume type; review it before migrating')
        if 'projected' in volume:
            raise ValueError('Projected volume requires review before copying into Git')
    labels = template['metadata'].get('labels', {})
    if labels.get('app') != 'csms' or labels.get('app.kubernetes.io/name') != 'csms':
        raise ValueError('CSMS labels must match existing Services before bootstrap')
    # Do not copy injector annotations or arbitrary metadata into Git.
    template['metadata'] = {'labels': labels, 'annotations': {
        k: v for k, v in template['metadata'].get('annotations', {}).items() if k.startswith('prometheus.io/')}}
    if any(m.get('mountPath', '').rstrip('/') in ['/app', '/app/csms', '/app/csms/app.py'] for m in container.get('volumeMounts', [])):
        raise ValueError('CSMS code is overridden by a ConfigMap mount. Remove that code override after putting its code in the app image; same-digest canary would otherwise test different code')
    pod['terminationGracePeriodSeconds'] = max(40, pod.get('terminationGracePeriodSeconds', 30))
    # A strategic merge must replace the captured template, not merge back stale
    # base env entries that override chargeops-config values.
    template['$patch'] = 'replace'
    return {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'csms'},
            'spec': {'replicas': 4, 'selector': copy.deepcopy(live['spec']['selector']),
                     'strategy': {'type': 'RollingUpdate', 'rollingUpdate': {'maxSurge': 1, 'maxUnavailable': 0}},
                     'template': template}}


def install_controller():
    print('Installing verified Argo Rollouts ' + VERSION + ' and mirroring its controller image', flush=True)
    url = f'https://github.com/argoproj/argo-rollouts/releases/download/{VERSION}/install.yaml'
    with urlopen(url, timeout=60) as response:
        body = response.read()
    if hashlib.sha256(body).hexdigest() != INSTALL_SHA256:
        raise ValueError('Pinned Rollouts installer checksum changed')
    docs = [d for d in yaml.safe_load_all(body) if d]
    deployment = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'argo-rollouts')
    container = deployment['spec']['template']['spec']['containers'][0]
    official = f'quay.io/argoproj/argo-rollouts:{VERSION}'
    if container['image'] != official:
        raise ValueError('Unexpected controller image in the pinned installer')
    mirror = f'registry.chargeops.lab:5000/chargeops/argo-rollouts:{VERSION}'
    run('docker', 'pull', official)
    run('docker', 'tag', official, mirror)
    pushed = run('docker', 'push', mirror)
    digests = re.findall(r'digest: (sha256:[a-f0-9]{64})', pushed)
    if not digests:
        raise ValueError('Cannot determine the pushed controller manifest digest')
    container['image'] = mirror.split(':v', 1)[0] + '@' + digests[-1]
    run('kubectl', 'apply', '-f', '-', payload=yaml.safe_dump({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'argo-rollouts'}}))
    run('kubectl', 'apply', '--server-side', '--field-manager=chargeops-rollouts-bootstrap', '-n', 'argo-rollouts', '-f', '-',
        payload=yaml.safe_dump_all(docs))
    for name in ['rollouts', 'analysistemplates', 'analysisruns']:
        run('kubectl', 'wait', '--for=condition=Established', 'crd/' + name + '.argoproj.io', '--timeout=120s')
    run('kubectl', '-n', 'argo-rollouts', 'rollout', 'status', 'deployment/argo-rollouts', '--timeout=180s')
    run('kubectl', 'apply', '-f', str(ROOT / 'chargeops-chatops/argocd/canary-rbac.yaml'))


def sync(revision, resources):
    app = get('-n', 'argocd', 'get', 'application', APPLICATION)
    if app['spec']['source']['path'] != CANARY or app['spec'].get('syncPolicy', {}).get('automated'):
        raise ValueError('Expected the manual ops Canary Application')
    if app.get('operation') or app.get('status', {}).get('operationState', {}).get('phase') in ['Running', 'Terminating']:
        raise ValueError('An Argo CD operation is running; finish it before retrying bootstrap')
    marker = 'chargeops-bootstrap-' + uuid.uuid4().hex[:12]
    operation = {'operation': {'initiatedBy': {'username': marker},
                 'sync': {'revision': revision, 'prune': False, 'resources': resources}}}
    run('kubectl', '-n', 'argocd', 'patch', 'application', APPLICATION, '--type=merge', '-p', json.dumps(operation))
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        app = get('-n', 'argocd', 'get', 'application', APPLICATION)
        state = app.get('status', {}).get('operationState', {})
        if (state.get('operation', {}).get('sync', {}).get('revision') == revision
                and state.get('operation', {}).get('initiatedBy', {}).get('username') == marker):
            if state.get('phase') == 'Succeeded':
                return
            if state.get('phase') in ['Failed', 'Error']:
                raise ValueError('Argo CD bootstrap Sync failed; existing resources were not deleted')
        time.sleep(5)
    raise ValueError('Argo CD bootstrap Sync timed out; do not scale down the existing Deployment')


def wait_baseline(image):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        rollout = get('-n', 'ops', 'get', 'rollout', 'csms')
        status = rollout.get('status', {})
        sets = get('-n', 'ops', 'get', 'replicasets', '-l', 'app=csms')['items']
        stable = [r for r in sets if any(o.get('uid') == rollout['metadata']['uid'] for o in r['metadata'].get('ownerReferences', []))
                  and r['metadata'].get('labels', {}).get('rollouts-pod-template-hash') == status.get('stableRS')]
        if (status.get('phase') == 'Healthy' and status.get('availableReplicas', 0) >= 4 and len(stable) == 1
                and stable[0].get('status', {}).get('availableReplicas', 0) >= 4
                and any(c['name'] == 'csms' and c['image'] == image for c in stable[0]['spec']['template']['spec']['containers'])):
            return
        time.sleep(5)
    raise ValueError('Baseline Rollout is not healthy; existing Deployment was not scaled down')


def create_kubeconfig(duration):
    helper = module('prepare-dev-kubeconfig')
    helper.ACCOUNT = 'jenkins-canary'
    source = get('config', 'view', '--raw', '--minify', '--flatten')
    config = helper.make_config(source['clusters'][0]['cluster'], 'validation-only')
    token = run('kubectl', '-n', 'ops', 'create', 'token', helper.ACCOUNT, '--duration=' + duration)
    config['users'][0]['user']['token'] = token
    config['contexts'][0]['name'] = config['current-context'] = 'chargeops-canary'
    config['contexts'][0]['context']['namespace'] = 'ops'
    def verify(path):
        probes = [(['get', 'rollouts.argoproj.io', '-n', 'ops'], 'yes'),
                  (['patch', 'rollouts.argoproj.io/csms', '--subresource=status', '-n', 'ops'], 'yes'),
                  (['patch', 'applications.argoproj.io/' + APPLICATION, '-n', 'argocd'], 'yes'),
                  (['get', 'secrets', '-n', 'ops'], 'no'),
                  (['patch', 'deployments', '-n', 'ops'], 'no')]
        for args, expected in probes:
            result = subprocess.run(['kubectl', '--kubeconfig', str(path), 'auth', 'can-i', *args], text=True, capture_output=True)
            if result.stdout.strip() != expected or result.returncode not in [0, 1]:
                raise ValueError('Canary kubeconfig permission verification failed')
    output = Path.home() / '.chargeops-jenkins/canary-kubeconfig.yaml'
    helper.write_config(config, output, verify)
    print('Created restricted kubeconfig: ' + str(output), flush=True)
    print('Upload as Jenkins Secret file ID chargeops-canary-kubeconfig; requested lifetime ' + duration)


def baseline_analysis():
    # The controller owns this ephemeral AnalysisRun and its Job.
    document = {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'AnalysisRun',
                'metadata': {'generateName': 'csms-bootstrap-', 'namespace': 'ops'},
                'spec': {'args': [{'name': 'check-stable', 'value': 'true'}],
                         'metrics': []}}
    template = get('-n', 'ops', 'get', 'analysistemplate', 'csms-availability')
    document['spec']['metrics'] = template['spec']['metrics']
    analysis = json.loads(run('kubectl', 'create', '-f', '-', '-o', 'json', payload=json.dumps(document)))
    name = analysis['metadata']['name']
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        phase = get('-n', 'ops', 'get', 'analysisrun', name).get('status', {}).get('phase')
        if phase == 'Successful':
            return
        if phase in ['Failed', 'Error', 'Inconclusive']:
            raise ValueError('Baseline availability analysis failed; existing Deployment was not scaled down')
        time.sleep(5)
    raise ValueError('Baseline analysis timed out; existing Deployment was not scaled down')


def bootstrap(duration):
    git_clean()
    live = get('-n', 'ops', 'get', 'deployment', 'csms')
    config_path = ROOT / PROD / 'kustomization.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    frozen = next(d for d in render(PROD) if d['kind'] == 'Deployment' and d['metadata']['name'] == 'csms')
    image = frozen['spec']['template']['spec']['containers'][0]['image']
    if not re.fullmatch(r'registry\.chargeops\.lab:5000/chargeops/chargeops-csms@sha256:[a-f0-9]{64}', image):
        raise ValueError('Freeze and commit production CSMS digest first')
    app = get('-n', 'argocd', 'get', 'application', APPLICATION)
    if app['spec'].get('syncPolicy', {}).get('automated'):
        raise ValueError('Turn off ops auto Sync before bootstrap')
    path = ROOT / PROD / RUNTIME
    if path.exists():
        patch = yaml.safe_load(path.read_text(encoding='utf-8'))
    else:
        if live['spec'].get('replicas') != 4 or live.get('status', {}).get('availableReplicas', 0) < 4:
            raise ValueError('Bootstrap requires four available original CSMS replicas')
        patch = runtime_patch(live)
        # Keep the template pinned even when the live Deployment used :dev.
        patch['spec']['template']['spec']['containers'][0]['image'] = image
        registry = module('freeze-production')
        repository, _, identities = registry.manifest(image)
        pods = get('-n', 'ops', 'get', 'pods', '-l', 'app=csms')['items']
        active = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
        if len(active) != 4:
            raise ValueError('Original CSMS Pods are not stable')
        for pod in active:
            status = next(c for c in pod['status']['containerStatuses'] if c['name'] == 'csms')
            found = re.search(r'sha256:[a-f0-9]{64}', status.get('imageID', ''))
            if not found or found.group() not in identities or not status.get('ready'):
                raise ValueError('Frozen baseline does not match every running CSMS Pod')
        path.write_text(yaml.safe_dump(patch, sort_keys=False), encoding='utf-8')
        config.setdefault('patches', []).append({'path': RUNTIME})
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
        commit_runtime('chore(gitops): preserve verified live CSMS runtime before canary handover')
    install_controller()
    # All application writes, including handover, are executed by Argo CD.
    run('kubectl', 'apply', '-f', str(ROOT / 'chargeops-chatops/argocd/application-canary.yaml'))
    planned = render(CANARY)
    selected = [d for d in planned if (d['kind'] == 'Deployment' and d['metadata']['name'] == 'csms')
                or d['kind'] in ['Rollout', 'AnalysisTemplate']
                or d['metadata']['name'] in ['csms-canary-probe', 'csms-stable', 'csms-canary']]
    run('kubectl', 'apply', '--dry-run=server', '-f', '-', payload=yaml.safe_dump_all(selected))
    revision = run('git', 'rev-parse', 'HEAD')
    deployment_resource = [{'group': 'apps', 'kind': 'Deployment', 'name': 'csms'}]
    if patch['spec']['replicas'] != 0:
        print('Pinning the existing Deployment through Argo CD before creating Rollout Pods', flush=True)
        sync(revision, deployment_resource)
        run('kubectl', '-n', 'ops', 'rollout', 'status', 'deployment/csms', '--timeout=600s')
    resources = [{'group': d['apiVersion'].split('/')[0] if '/' in d['apiVersion'] else '',
                  'kind': d['kind'], 'name': d['metadata']['name']} for d in render(CANARY)
                 if d['kind'] in ['Rollout', 'AnalysisTemplate'] or d['metadata']['name'] in ['csms-canary-probe', 'csms-stable', 'csms-canary']]
    sync(revision, resources)
    print('Waiting for four baseline Rollout Pods; original Deployment remains available', flush=True)
    wait_baseline(image)
    print('Running the read-only baseline availability analysis', flush=True)
    baseline_analysis()
    if patch['spec']['replicas'] != 0:
        patch['spec']['replicas'] = 0
        print('Baseline verified; handing replicas from Deployment to Rollout through Git and Argo CD', flush=True)
        path.write_text(yaml.safe_dump(patch, sort_keys=False), encoding='utf-8')
        revision = commit_runtime('chore(gitops): hand CSMS replicas to verified Rollout')
        sync(revision, deployment_resource)
    wait_baseline(image)
    if get('-n', 'ops', 'get', 'deployment', 'csms')['spec']['replicas'] != 0:
        raise ValueError('Deployment handover did not finish')
    create_kubeconfig(duration)
    print('PASS: same-image baseline healthy, availability analysis passed, Rollout owns four replicas')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', default='24h')
    parser.add_argument('--credential-only', action='store_true', help='Renew only the restricted kubeconfig after initial bootstrap')
    parser.add_argument('--controller-only', action='store_true', help='Install the controller/RBAC without changing the ops Application or CSMS')
    args = parser.parse_args()
    if not re.fullmatch(r'[1-9][0-9]*(s|m|h)', args.duration):
        raise ValueError('Invalid requested token duration')
    if args.credential_only and args.controller_only:
        raise ValueError('Choose only one bootstrap mode')
    if args.controller_only:
        install_controller()
    elif args.credential_only:
        create_kubeconfig(args.duration)
    else:
        bootstrap(args.duration)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Values are fixed local messages; child errors and HTTP bodies are hidden.
        if isinstance(error, ValueError):
            print('Bootstrap stopped: ' + str(error))
        else:
            print('Bootstrap stopped (' + error.__class__.__name__ + '); check the current step, API, Git and Registry connectivity')
        print('No Secret contents were printed. Do not delete the Deployment, PVCs or Rollout to retry.')
        raise SystemExit(1)
