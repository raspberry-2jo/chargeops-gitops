"""Exercise real Rollouts success/abort in a disposable namespace, without a DB.

Requires the Rollouts controller. Does not alter ops or its Application.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('canary_bootstrap', ROOT / 'scripts/bootstrap-canary.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
NS = 'canary-rehearsal'
IMAGE = 'registry.chargeops.lab:5000/chargeops/python:3.13-slim@sha256:d879ed88565a7a3c614b980b369d4a5025a9d057b71f93d09fc6d0b2ca823a18'
SERVER = '''from http.server import BaseHTTPRequestHandler, HTTPServer
import os
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/ready' else int(os.environ['STATUS']))
        self.end_headers()
        self.wfile.write(b'{"ok":true}')
    def log_message(self, *args): pass
HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
'''
PROBE = '''import urllib.request, time
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for i in range(30):
    with opener.open('http://fixture-canary:8000/healthz', timeout=2) as response:
        assert response.status == 200
    time.sleep(1)
'''


def manifests():
    labels = {'app': 'canary-rehearsal'}
    job = {'backoffLimit': 0, 'activeDeadlineSeconds': 90, 'ttlSecondsAfterFinished': 3600,
           'template': {'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                                'containers': [{'name': 'check', 'image': IMAGE, 'command': ['python', '-c', PROBE],
                                                'resources': {'requests': {'cpu': '20m', 'memory': '32Mi'}, 'limits': {'cpu': '200m', 'memory': '64Mi'}}}]}}}
    rollout = {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'Rollout', 'metadata': {'name': 'fixture', 'namespace': NS},
               'spec': {'replicas': 4, 'progressDeadlineSeconds': 180, 'progressDeadlineAbort': True,
                        'selector': {'matchLabels': labels}, 'template': {'metadata': {'labels': labels}, 'spec': {
                            'automountServiceAccountToken': False,
                            'containers': [{'name': 'http', 'image': IMAGE, 'command': ['python', '-c', SERVER],
                                            'env': [{'name': 'STATUS', 'value': '200'}, {'name': 'REVISION', 'value': 'baseline'}],
                                            'ports': [{'containerPort': 8000}],
                                            'readinessProbe': {'httpGet': {'path': '/ready', 'port': 8000}, 'periodSeconds': 2},
                                            'resources': {'requests': {'cpu': '20m', 'memory': '32Mi'}, 'limits': {'cpu': '200m', 'memory': '64Mi'}}}]}},
                        'strategy': {'canary': {'maxSurge': 1, 'maxUnavailable': 0, 'stableService': 'fixture-stable', 'canaryService': 'fixture-canary',
                            'steps': [item for weight in [25, 50, 100] for item in [
                                {'setWeight': weight}, {'analysis': {'templates': [{'templateName': 'fixture-http'}]}}]]}}}}
    return [
        {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': NS, 'labels': {'chargeops.test/canary': 'true'}}},
        {'apiVersion': 'v1', 'kind': 'ResourceQuota', 'metadata': {'name': 'fixture-budget', 'namespace': NS},
         'spec': {'hard': {'requests.cpu': '1', 'requests.memory': '512Mi', 'limits.cpu': '2', 'limits.memory': '1Gi', 'pods': '12'}}},
        *[{'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': name, 'namespace': NS},
           'spec': {'selector': labels, 'ports': [{'port': 8000, 'targetPort': 8000}]}} for name in ['fixture', 'fixture-stable', 'fixture-canary']],
        {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'AnalysisTemplate', 'metadata': {'name': 'fixture-http', 'namespace': NS},
         'spec': {'metrics': [{'name': 'http-success', 'count': 1, 'failureLimit': 0, 'provider': {'job': {'spec': job}}}]}},
        rollout]


def wait(healthy, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rollout = bootstrap.get('-n', NS, 'get', 'rollout', 'fixture')
        state = rollout.get('status', {})
        if state.get('phase') == 'Healthy' and state.get('stableRS') == state.get('currentPodHash'):
            if healthy:
                return
        if state.get('abort'):
            if healthy:
                raise ValueError('Healthy fixture unexpectedly aborted')
            pods = bootstrap.get('-n', NS, 'get', 'pods', '-l', 'app=canary-rehearsal')['items']
            available = [p for p in pods if not p['metadata'].get('deletionTimestamp')
                         and all(e.get('value') != '500' for c in p['spec']['containers'] for e in c.get('env', []) if e['name'] == 'STATUS')
                         and any(c['type'] == 'Ready' and c['status'] == 'True' for c in p['status'].get('conditions', []))]
            if len(available) >= 4:
                return
        time.sleep(3)
    raise ValueError('Rehearsal timed out; inspect the fixture AnalysisRuns')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup', action='store_true', help='Delete only this script-created disposable namespace')
    args = parser.parse_args()
    existing = json.loads(bootstrap.run('kubectl', 'get', 'namespace', NS, '--ignore-not-found', '-o', 'json') or 'null')
    if existing and existing['metadata'].get('labels', {}).get('chargeops.test/canary') != 'true':
        raise ValueError('Refusing to modify a namespace not owned by this rehearsal')
    if args.cleanup:
        if existing:
            bootstrap.run('kubectl', 'delete', 'namespace', NS, '--wait=false')
        return
    if existing:
        raise ValueError('Clean up the previous rehearsal with --cleanup before rerunning')
    documents = manifests()
    bootstrap.run('kubectl', 'apply', '-f', '-', payload=yaml.safe_dump_all(documents))
    wait(True)
    rollout = documents[-1]
    rollout['spec']['template']['spec']['containers'][0]['env'][1]['value'] = 'healthy-update'
    bootstrap.run('kubectl', 'apply', '-f', '-', payload=yaml.safe_dump(rollout))
    # Wait until the controller observes the new generation before accepting health.
    bootstrap.run('kubectl', '-n', NS, 'wait', '--for=jsonpath={.status.phase}=Progressing', 'rollout/fixture', '--timeout=60s')
    wait(True)
    print('PASS: healthy update completed 25/50/100 percent analysis', flush=True)
    rollout['spec']['template']['spec']['containers'][0]['env'] = [{'name': 'STATUS', 'value': '500'}, {'name': 'REVISION', 'value': 'failed-update'}]
    bootstrap.run('kubectl', 'apply', '-f', '-', payload=yaml.safe_dump(rollout))
    wait(False)
    print('PASS: HTTP 500 candidate aborted; four healthy stable fixture Pods preserved')
    print('This tests the real controller, not production DB/OCPP or Jenkins Git write-back. Cleanup: python scripts/rehearse-canary.py --cleanup')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Rehearsal stopped (' + error.__class__.__name__ + '); inspect only namespace ' + NS)
        raise SystemExit(1)
