"""Dev CSMS handover and reversible HTTP-error rehearsal. Never targets ops."""
import argparse
import copy
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = 'chargeops-chatops/overlays/development-canary'
LEGACY = 'chargeops-chatops/overlays/development'
MARKER = 'chargeops.io/dev-canary-session'
FAULT = 'chargeops.io/dev-error-every'
NONCE = 'chargeops.io/dev-rehearsal'
STATE = Path.home() / '.chargeops-jenkins/dev-canary-rehearsal.json'
BOOT = Path.home() / '.chargeops-jenkins/dev-canary-bootstrap.json'


def run(*args, obj=None):
    command = ['kubectl', *args]
    if obj is not None:
        command += ['-f', '-']
    result = subprocess.run(command, input=None if obj is None else json.dumps(obj),
                            text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def get(namespace, kind, name):
    if namespace not in ('dev', 'argocd') or (namespace == 'argocd' and (kind, name) != ('application', 'chargeops-dev')):
        raise ValueError('Resource is outside the Dev rehearsal scope')
    return json.loads(run('-n', namespace, 'get', kind, name, '-o', 'json'))


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        json.dump(state, file)
    os.chmod(path, 0o600)


def app():
    a = get('argocd', 'application', 'chargeops-dev')
    s = a['spec']
    if (s['destination']['namespace'] != 'dev' or s['source'].get('path') not in (LEGACY, OVERLAY)
            or s['source'].get('repoURL') != 'https://github.com/raspberry-2jo/chargeops-gitops.git'
            or s['source'].get('targetRevision') != 'main'):
        raise ValueError('chargeops-dev must target this GitOps main and the Dev namespace')
    if a.get('operation') or a.get('status', {}).get('operationState', {}).get('phase') in ('Running', 'Terminating'):
        raise ValueError('Dev Argo operation is active; wait for it to finish')
    return a


def patch(namespace, kind, name, operations):
    if namespace not in ('dev', 'argocd') or (namespace == 'argocd' and (kind, name) != ('application', 'chargeops-dev')):
        raise ValueError('Patch is outside the Dev rehearsal scope')
    return run('-n', namespace, 'patch', kind, name, '--type=json', '-p', json.dumps(operations))


def pause(a, session):
    policy = copy.deepcopy(a['spec'].get('syncPolicy', {}))
    policy.pop('automated', None)
    annotations = dict(a['metadata'].get('annotations', {}))
    if MARKER in annotations:
        raise ValueError('A Dev Canary session already owns the Application')
    annotations[MARKER] = session
    patch('argocd', 'application', 'chargeops-dev', [
        {'op': 'test', 'path': '/spec/source', 'value': a['spec']['source']},
        {'op': 'test', 'path': '/spec/syncPolicy', 'value': a['spec'].get('syncPolicy', {})},
        {'op': 'add', 'path': '/metadata/annotations', 'value': annotations},
        {'op': 'add', 'path': '/spec/syncPolicy', 'value': policy}])


def resume(state, source=None):
    a = app()
    if a['metadata'].get('annotations', {}).get(MARKER) != state['session']:
        raise ValueError('Another operator owns the Application; automatic restoration stopped')
    if a['spec']['source'] != state['source']:
        raise ValueError('Dev Application source changed; automatic restoration stopped')
    paused = copy.deepcopy(state['syncPolicy'])
    paused.pop('automated', None)
    if a['spec'].get('syncPolicy', {}) != paused:
        raise ValueError('Dev auto-sync policy changed; automatic restoration stopped')
    annotations = dict(a['metadata'].get('annotations', {}))
    annotations.pop(MARKER, None)
    patch('argocd', 'application', 'chargeops-dev', [
        {'op': 'test', 'path': '/spec/source', 'value': a['spec']['source']},
        {'op': 'test', 'path': '/spec/syncPolicy', 'value': paused},
        {'op': 'test', 'path': '/metadata/annotations', 'value': a['metadata'].get('annotations', {})},
        {'op': 'add', 'path': '/spec/source', 'value': source or state['source']},
        {'op': 'add', 'path': '/spec/syncPolicy', 'value': state['syncPolicy']},
        {'op': 'add', 'path': '/metadata/annotations', 'value': annotations}])


def healthy(expected_image, zero_deployment=True):
    d = get('dev', 'deployment', 'csms')
    r = get('dev', 'rollout', 'csms')
    s = r.get('status', {})
    image = next(c['image'] for c in d['spec']['template']['spec']['containers'] if c['name'] == 'csms')
    return (image == expected_image and (not zero_deployment or d['spec'].get('replicas') == 0)
            and r['spec'].get('replicas') == 2 and s.get('phase') == 'Healthy' and not s.get('abort')
            and int(s.get('observedGeneration', 0)) >= r['metadata']['generation']
            and int(s.get('workloadObservedGeneration', 0)) >= d['metadata']['generation']
            and s.get('stableRS') and s.get('stableRS') == s.get('currentPodHash')
            and s.get('readyReplicas', 0) >= 2 and s.get('availableReplicas', 0) >= 2)


def wait_healthy(image, zero=True):
    deadline = time.monotonic() + 600
    last = 0
    while time.monotonic() < deadline:
        if healthy(image, zero):
            return
        if time.monotonic() - last > 20:
            print('Waiting for the Dev Rollout baseline...', flush=True)
            last = time.monotonic()
        time.sleep(5)
    raise ValueError('Dev Rollout not healthy; baseline Deployment was not intentionally removed before readiness')


def rendered():
    docs = [d for d in yaml.safe_load_all(run('kustomize', str(ROOT / OVERLAY))) if d]
    if any(d['metadata'].get('namespace') not in ('dev', None) for d in docs):
        raise ValueError('Rendered resource is outside dev')
    return docs


def comparable_template(template):
    """Ignore only standard API defaults, retaining code/env/volumes/placement."""
    result = copy.deepcopy(template)
    result['metadata'].pop('creationTimestamp', None)
    defaults = {'restartPolicy': 'Always', 'dnsPolicy': 'ClusterFirst', 'schedulerName': 'default-scheduler'}
    for key, value in defaults.items():
        if result['spec'].get(key) == value:
            result['spec'].pop(key)
    for container in result['spec']['containers']:
        for key, value in [('terminationMessagePath', '/dev/termination-log'), ('terminationMessagePolicy', 'File')]:
            if container.get(key) == value:
                container.pop(key)
        for field in ('readinessProbe', 'livenessProbe', 'startupProbe'):
            probe = container.get(field, {})
            for key, value in [('timeoutSeconds', 1), ('successThreshold', 1), ('failureThreshold', 3), ('initialDelaySeconds', 0)]:
                if probe.get(key) == value:
                    probe.pop(key)
            if probe.get('httpGet', {}).get('scheme') == 'HTTP':
                probe['httpGet'].pop('scheme')
    if result['metadata'].get('annotations') == {}:
        result['metadata'].pop('annotations')
    return result


def capacity():
    def number(value):
        value = str(value)
        for suffix, factor in [('Gi', 1024**3), ('Mi', 1024**2), ('Ki', 1024), ('m', .001)]:
            if value.endswith(suffix):
                return Decimal(value[:-len(suffix)]) * Decimal(str(factor))
        return Decimal(value)
    # Extra capacity beyond the currently running single CSMS Pod, including Jobs.
    required = {'pods': '4', 'requests.cpu': '350m', 'requests.memory': '416Mi',
                'limits.cpu': '2750m', 'limits.memory': '2432Mi'}
    quotas = json.loads(run('-n', 'dev', 'get', 'resourcequota', '-o', 'json'))['items']
    for quota in quotas:
        status = quota.get('status', {})
        for key, extra in required.items():
            if key in status.get('hard', {}) and number(status.get('used', {}).get(key, '0')) + number(extra) > number(status['hard'][key]):
                raise ValueError('Dev quota has insufficient spare ' + key + '; finish other tests first')


def baseline_analysis():
    template = get('dev', 'analysistemplate', 'csms-dev-availability')
    analysis = {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'AnalysisRun',
        'metadata': {'generateName': 'csms-dev-baseline-', 'namespace': 'dev'},
        'spec': {'args': [{'name': 'check-stable', 'value': 'true'},
                          {'name': 'dev-error-every', 'value': '0'}],
                 'metrics': template['spec']['metrics']}}
    created = json.loads(run('-n', 'dev', 'create', '-o', 'json', obj=analysis))
    name = created['metadata']['name']
    print('Running read-only Dev baseline analysis: ' + name, flush=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        phase = get('dev', 'analysisrun', name).get('status', {}).get('phase')
        if phase == 'Successful':
            return
        if phase in ('Failed', 'Error', 'Inconclusive'):
            raise ValueError('Dev baseline analysis did not pass; original Deployment was retained')
        time.sleep(5)
    raise ValueError('Dev baseline analysis timed out; original Deployment was retained')


def bootstrap():
    a = app()
    if a['spec']['source']['path'] != LEGACY:
        raise ValueError('Dev is already switched; use status, not another handover')
    if BOOT.exists() and json.loads(BOOT.read_text()).get('phase') not in ('complete', 'rolled-back'):
        raise ValueError('An unfinished bootstrap receipt exists; inspect it before repeating the handover')
    # The active Argo main must contain exactly the overlay being installed.
    check = subprocess.run(['git', 'diff', '--exit-code', 'origin/main', '--', OVERLAY], cwd=ROOT, capture_output=True)
    remote = subprocess.run(['git', 'cat-file', '-e', 'origin/main:' + OVERLAY + '/kustomization.yaml'], cwd=ROOT, capture_output=True)
    if check.returncode or remote.returncode:
        raise ValueError('Publish the reviewed opt-in overlay to GitOps main and fetch origin before bootstrap')
    d = get('dev', 'deployment', 'csms')
    if d['spec'].get('replicas') != 1 or d.get('status', {}).get('availableReplicas') != 1:
        raise ValueError('Expected the original one-Pod Dev Deployment to be available')
    image = next(c['image'] for c in d['spec']['template']['spec']['containers'] if c['name'] == 'csms')
    docs = rendered()
    desired = next(x for x in docs if x['kind'] == 'Deployment' and x['metadata']['name'] == 'csms')
    if comparable_template(desired['spec']['template']) != comparable_template(d['spec']['template']):
        raise ValueError('Dev template differs from the reviewed baseline; reconcile before handover')
    capacity()
    existing = run('-n', 'dev', 'get', 'rollout', 'csms', '--ignore-not-found', '-o', 'json')
    if existing:
        raise ValueError('A Dev CSMS Rollout already exists; do not adopt or overwrite it')
    state = {'session': uuid.uuid4().hex, 'phase': 'starting', 'source': a['spec']['source'],
             'syncPolicy': a['spec'].get('syncPolicy', {}), 'image': image}
    save(BOOT, state)
    pause(a, state['session'])
    # Keep the original Deployment running until the new same-image replicas are Healthy.
    extra_names = {'csms-stable', 'csms-canary', 'csms-dev-availability', 'csms-dev-canary-probe',
                   'csms-dev-canary-probe-metrics', 'jenkins-dev-verifier'}
    selected = [x for x in docs if x['kind'] != 'Deployment' and (
        x['metadata']['name'] in extra_names or x['kind'] == 'Rollout')]
    bundle = {'apiVersion': 'v1', 'kind': 'List', 'items': selected}
    print(run('-n', 'dev', 'apply', '--dry-run=server', obj=bundle))
    print(run('-n', 'dev', 'apply', obj=bundle))
    state['rollout_uid'] = get('dev', 'rollout', 'csms')['metadata']['uid']
    save(BOOT, state)
    wait_healthy(image, zero=False)
    baseline_analysis()
    current = get('dev', 'deployment', 'csms')
    if current['spec']['template'] != d['spec']['template']:
        raise ValueError('Dev template changed during handover; original Deployment not scaled down')
    patch('dev', 'deployment', 'csms', [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
        {'op': 'replace', 'path': '/spec/replicas', 'value': 0}])
    wait_healthy(image)
    destination = copy.deepcopy(state['source'])
    destination['path'] = OVERLAY
    resume(state, source=destination)
    state['phase'] = 'complete'
    save(BOOT, state)
    print('Dev handover complete. Jenkins DEV_DELIVERY_MODE=auto now follows Dev Canary. Ops unchanged.')


def rollback_bootstrap():
    state = json.loads(BOOT.read_text(encoding='utf-8'))
    if state['phase'] == 'complete':
        raise ValueError('Bootstrap completed; this command only recovers an unfinished handover')
    a = app()
    if a['metadata'].get('annotations', {}).get(MARKER) != state['session']:
        raise ValueError('Application session changed; receipt preserved for review')
    d = get('dev', 'deployment', 'csms')
    if next(c['image'] for c in d['spec']['template']['spec']['containers'] if c['name'] == 'csms') != state['image']:
        raise ValueError('A newer Dev image exists; will not revert its replicas')
    patch('dev', 'deployment', 'csms', [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': d['metadata']['resourceVersion']},
        {'op': 'replace', 'path': '/spec/replicas', 'value': 1}])
    end = time.monotonic() + 180
    while time.monotonic() < end:
        d = get('dev', 'deployment', 'csms')
        if (d.get('status', {}).get('availableReplicas', 0) >= 1
                and d.get('status', {}).get('observedGeneration', 0) >= d['metadata']['generation']):
            break
        time.sleep(5)
    else:
        raise ValueError('Original Dev Deployment not available; Rollout was retained')
    raw = run('-n', 'dev', 'get', 'rollout', 'csms', '--ignore-not-found', '-o', 'json')
    if raw:
        if json.loads(raw)['metadata']['uid'] != state.get('rollout_uid'):
            raise ValueError('Rollout ownership is unknown; refusing deletion')
        print(run('-n', 'dev', 'delete', 'rollout', 'csms', '--cascade=foreground', '--wait=true', '--timeout=120s'))
    restored = copy.deepcopy(state)
    restored['source'] = a['spec']['source']
    resume(restored, source=state['source'])
    state['phase'] = 'rolled-back'
    save(BOOT, state)
    print('Original Dev Deployment and Argo source/policy restored. Keep Jenkins mode=deployment.')


def restore():
    state = json.loads(STATE.read_text(encoding='utf-8'))
    if state['phase'] == 'recovered':
        print('Dev rehearsal already recovered')
        return
    current = get('dev', 'deployment', 'csms')
    expected = copy.deepcopy(state['template'])
    expected['metadata'].setdefault('annotations', {})[NONCE] = state['session']
    if current['spec']['template'] not in (state['template'], expected):
        raise ValueError('A newer Dev template exists; will not overwrite it. Receipt preserved.')
    rollout = get('dev', 'rollout', 'csms')
    annotations = dict(rollout['metadata'].get('annotations', {}))
    if annotations.get(FAULT, '0') not in ('0', '20'):
        raise ValueError('Unexpected fault configuration; receipt preserved')
    annotations[FAULT] = '0'
    patch('dev', 'rollout', 'csms', [
        {'op': 'test', 'path': '/metadata/annotations', 'value': rollout['metadata'].get('annotations', {})},
        {'op': 'add', 'path': '/metadata/annotations', 'value': annotations}])
    if current['spec']['template'] == expected:
        patch('dev', 'deployment', 'csms', [
            {'op': 'test', 'path': '/spec/template', 'value': expected},
            {'op': 'replace', 'path': '/spec/template', 'value': state['template']}])
    wait_healthy(state['image'])
    resume(state)
    state['phase'] = 'recovered'
    save(STATE, state)
    print('Dev rehearsal recovery complete: original template, normal probe mode and Argo auto-sync policy restored.')


def rehearse():
    if STATE.exists() and json.loads(STATE.read_text(encoding='utf-8'))['phase'] != 'recovered':
        raise ValueError('Previous rehearsal is unfinished; run restore first')
    a = app()
    if a['spec']['source']['path'] != OVERLAY:
        raise ValueError('Bootstrap the Dev Canary first')
    d, r = get('dev', 'deployment', 'csms'), get('dev', 'rollout', 'csms')
    image = next(c['image'] for c in d['spec']['template']['spec']['containers'] if c['name'] == 'csms')
    if not healthy(image) or r['metadata'].get('annotations', {}).get(FAULT, '0') != '0':
        raise ValueError('Start only from a Healthy normal Dev Canary baseline')
    state = {'session': uuid.uuid4().hex, 'phase': 'testing', 'source': a['spec']['source'],
             'syncPolicy': a['spec'].get('syncPolicy', {}), 'template': d['spec']['template'],
             'image': image, 'stable': r['status']['stableRS'], 'rollout_uid': r['metadata']['uid']}
    save(STATE, state)
    pause(a, state['session'])
    try:
        annotations = dict(r['metadata'].get('annotations', {}))
        annotations[FAULT] = '20'
        patch('dev', 'rollout', 'csms', [
            {'op': 'test', 'path': '/metadata/annotations', 'value': r['metadata'].get('annotations', {})},
            {'op': 'add', 'path': '/metadata/annotations', 'value': annotations}])
        template = copy.deepcopy(state['template'])
        template['metadata'].setdefault('annotations', {})[NONCE] = state['session']
        patch('dev', 'deployment', 'csms', [
            {'op': 'test', 'path': '/spec/template', 'value': state['template']},
            {'op': 'replace', 'path': '/spec/template', 'value': template}])
        print('Watch Grafana: actual Dev CSMS 404 on every 20th canary probe request. Image and DB are unchanged.', flush=True)
        end = time.monotonic() + 600
        while time.monotonic() < end:
            current = get('dev', 'rollout', 'csms')
            s = current.get('status', {})
            if s.get('abort') and s.get('stableRS') == state['stable'] and s.get('currentPodHash') != state['stable']:
                analyses = json.loads(run('-n', 'dev', 'get', 'analysisrun', '-o', 'json'))['items']
                failed = [x for x in analyses if x.get('status', {}).get('phase') == 'Failed'
                    and s['currentPodHash'] in x['metadata']['name']
                    and any(o.get('uid') == state['rollout_uid'] for o in x['metadata'].get('ownerReferences', []))]
                jobs = json.loads(run('-n', 'dev', 'get', 'jobs', '-o', 'json'))['items']
                if any('HTTP availability error rate exceeded one percent' in run('-n', 'dev', 'logs',
                       'job/' + job['metadata']['name']) for job in jobs
                       if any(o.get('uid') in {x['metadata']['uid'] for x in failed}
                              for o in job['metadata'].get('ownerReferences', []))):
                    print('PASS: real CSMS HTTP errors caused analysis failure and aborted the Dev update.', flush=True)
                    break
            time.sleep(5)
        else:
            raise ValueError('Expected HTTP-error abort not verified; restoring baseline')
    finally:
        restore()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'bootstrap', 'rollback-bootstrap', 'rehearse', 'restore', 'status'])
    args = parser.parse_args()
    if args.action == 'plan':
        docs = rendered()
        print('Dev-only opt-in overlay:', OVERLAY)
        print('Steady CSMS Pods=2; peak CSMS Pods=3 plus bounded analysis/smoke Job.')
        print('Resources:', ', '.join(x['kind'] + '/' + x['metadata']['name'] for x in docs))
        print('Plan only; no cluster or Git writes.')
    elif args.action == 'bootstrap':
        bootstrap()
    elif args.action == 'rollback-bootstrap':
        rollback_bootstrap()
    elif args.action == 'rehearse':
        rehearse()
    elif args.action == 'restore':
        restore()
    else:
        print(run('-n', 'dev', 'get', 'deployment,rollout', 'csms', '-o', 'wide'))
        print(run('-n', 'dev', 'get', 'pods', '-l', 'app=csms', '-o', 'wide'))
        print(run('-n', 'dev', 'get', 'analysisrun'))


if __name__ == '__main__':
    main()
