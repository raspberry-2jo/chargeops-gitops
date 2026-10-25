"""Apply only Ops probe telemetry resources, or run an independent read-only check.

Run on mgmt with its existing administrator kubecontext. Never applies CSMS
Deployment, Rollout, images, Secrets or Argo Application resources.
"""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / 'chargeops-chatops/overlays/canary'
ALLOWED = {('ConfigMap', 'csms-canary-probe'), ('AnalysisTemplate', 'csms-availability'),
           ('Service', 'csms-canary-probe-metrics'), ('ServiceMonitor', 'csms-canary-probe'),
           ('NetworkPolicy', 'csms-canary-probe-metrics')}


def run(*args, payload=None):
    result = subprocess.run(args, input=payload, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(args[:4]) + '; inspect this resource from mgmt')
    return result.stdout


def get(kind, name, namespace='ops'):
    return json.loads(run('kubectl', '-n', namespace, 'get', kind, name, '-o', 'json'))


def documents():
    docs = [d for d in yaml.safe_load_all(run('kubectl', 'kustomize', str(OVERLAY))) if d]
    selected = [d for d in docs if (d['kind'], d['metadata']['name']) in ALLOWED]
    if {(d['kind'], d['metadata']['name']) for d in selected} != ALLOWED:
        raise RuntimeError('Expected exactly the five probe telemetry resources')
    if any(d['metadata'].get('namespace') != 'ops' for d in selected):
        raise RuntimeError('Probe telemetry must be scoped to ops')
    return selected


def preflight():
    app = get('application', 'chargeops-chatops-dev', 'argocd')
    if (app['spec']['destination']['namespace'] != 'ops'
            or app['spec']['source']['path'] != 'chargeops-chatops/overlays/canary'
            or (app.get('status', {}).get('operationState') or {}).get('phase') in ('Running', 'Terminating')
            or app.get('operation')):
        raise RuntimeError('Expected the idle existing Ops Canary Application')
    if 'automated' in app['spec'].get('syncPolicy', {}):
        raise RuntimeError('Ops automatic Sync is enabled; use an agreed deployment window')
    rollout = get('rollout', 'csms')
    state = rollout.get('status', {})
    if (state.get('phase') != 'Healthy' or state.get('abort')
            or not state.get('stableRS') or state.get('stableRS') != state.get('currentPodHash')
            or state.get('availableReplicas', 0) < 4):
        raise RuntimeError('Wait for a Healthy stable Ops Rollout with four available Pods')
    runs = json.loads(run('kubectl', '-n', 'ops', 'get', 'analysisruns', '-o', 'json'))['items']
    if any(a.get('status', {}).get('phase') in (None, 'Pending', 'Running') for a in runs):
        raise RuntimeError('An Ops analysis is active; wait before changing its mounted ConfigMap')
    pods = json.loads(run('kubectl', '-n', 'ops', 'get', 'pods', '-l',
                          'chargeops.io/role=canary-probe', '-o', 'json'))['items']
    if any(p.get('status', {}).get('phase') not in ('Succeeded', 'Failed') for p in pods):
        raise RuntimeError('An Ops probe Pod is still active; wait for it to finish')
    return rollout


def workload_snapshot():
    return {kind: get(kind, 'csms')['spec'] for kind in ('deployment', 'rollout')}


def apply():
    preflight()
    before = workload_snapshot()
    docs = documents()
    print(run('kubectl', 'apply', '-f', '-', payload=yaml.safe_dump_all(docs)), end='')
    if workload_snapshot() != before:
        raise RuntimeError('CSMS workload changed concurrently; inspect the deployment before continuing')
    print('Applied five Ops probe telemetry resources. CSMS Deployment/Rollout specs unchanged.')


def check():
    preflight()
    template = get('analysistemplate', 'csms-availability')
    metric = template['spec']['metrics'][0]
    container = metric['provider']['job']['spec']['template']['spec']['containers'][0]
    if {'name': 'PROBE_METRICS', 'value': 'true'} not in container.get('env', []):
        raise RuntimeError('Apply Ops probe telemetry before running check')
    before = workload_snapshot()
    analysis = {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'AnalysisRun',
        'metadata': {'generateName': 'csms-ops-metrics-check-', 'namespace': 'ops',
                     'labels': {'chargeops.io/purpose': 'metrics-check'}},
        'spec': {'args': [{'name': 'check-stable', 'value': 'true'}],
                 'metrics': copy.deepcopy(template['spec']['metrics'])}}
    obj = json.loads(run('kubectl', 'create', '-f', '-', '-o', 'json', payload=json.dumps(analysis)))
    name = obj['metadata']['name']
    print('Read-only AnalysisRun: ' + name, flush=True)
    print('Grafana: select ops, all probe Pods and last 30 minutes.', flush=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        phase = get('analysisrun', name).get('status', {}).get('phase', 'Pending')
        if phase in ('Successful', 'Failed', 'Error', 'Inconclusive'):
            print('Analysis phase: ' + phase, flush=True)
            if workload_snapshot() != before:
                raise RuntimeError('CSMS workload changed concurrently during the check')
            if phase != 'Successful':
                raise RuntimeError('Read-only check did not pass; describe AnalysisRun ' + name)
            print('Read-only Ops check passed. This independent analysis does not promote or abort the Rollout.')
            return
        time.sleep(5)
    raise RuntimeError('Check deadline reached; inspect AnalysisRun ' + name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'apply', 'check'])
    args = parser.parse_args()
    try:
        if args.action == 'plan':
            for doc in documents():
                print(doc['kind'] + ' ops/' + doc['metadata']['name'])
        elif args.action == 'apply':
            apply()
        else:
            check()
    except RuntimeError as error:
        print('Stopped: ' + str(error))
        raise SystemExit(1)
