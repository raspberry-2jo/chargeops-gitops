#!/usr/bin/env python3
"""Read-only recorder for ChargeOps DEV rollout tuning. No apply, patch or delete."""
import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--context', required=True)
p.add_argument('--namespace', default='dev', choices=['dev'])
p.add_argument('--rollout', default='csms')
p.add_argument('--selector', required=True, help='Verified pod label selector, e.g. app=csms')
p.add_argument('--seconds', type=int, default=900)
p.add_argument('--every', type=int, default=5)
p.add_argument('--output', required=True)
a = p.parse_args()
if not (10 <= a.seconds <= 7200 and 2 <= a.every <= 60):
    p.error('--seconds must be 10..7200; --every must be 2..60')


def get(resource, selector=None):
    command = ['kubectl', '--context', a.context, '-n', a.namespace, 'get', resource]
    if selector:
        command += ['-l', selector]
    command += ['-o', 'json', '--request-timeout=10s']
    res = subprocess.run(command, capture_output=True, text=True, timeout=18)
    if res.returncode != 0:
        raise RuntimeError(' '.join(command) + ': ' + res.stderr.strip())
    return json.loads(res.stdout)


def pod_summary(pod):
    status = pod.get('status', {})
    cs = status.get('containerStatuses', [])
    ready = next((c for c in status.get('conditions', []) if c['type']=='Ready'), {})
    meta = pod['metadata']
    return {
        'name': meta['name'], 'created':meta.get('creationTimestamp'),
        'deletionTimestamp':meta.get('deletionTimestamp'),
        'node':pod.get('spec', {}).get('nodeName'), 'phase':status.get('phase'),
        'ready':ready.get('status') == 'True', 'ready_at':ready.get('lastTransitionTime'),
        'restarts':sum(c.get('restartCount',0) for c in cs),
        'pod_template_hash':meta.get('labels',{}).get('rollouts-pod-template-hash') or meta.get('labels',{}).get('pod-template-hash')
    }


def simple_list(obj):
    return [{'name':i['metadata']['name'], 'phase':i.get('status',{}).get('phase'),
             'started':i.get('status',{}).get('startTime'),
             'finished':i.get('status',{}).get('completionTime'),
             'deadline':i.get('spec',{}).get('activeDeadlineSeconds'),
             'failed':i.get('status',{}).get('failed',0)} for i in obj.get('items',[])]

# Check nonempty selector *before* entering loop.
try:
    get('rollout/'+a.rollout)
    pod_check = get('pods', a.selector)
    if not pod_check['items']:
        sys.exit('Selected zero pods. Verify selector with kubectl -n dev get pods --show-labels')
except Exception as exc:
    sys.exit('Preflight failed: '+str(exc))

path = Path(a.output)
path.parent.mkdir(parents=True, exist_ok=True)
end = time.monotonic() + a.seconds
print('READ-ONLY: recording DEV objects to',path,flush=True)
with path.open('w', encoding='utf-8') as out:
    while time.monotonic() < end:
        try:
            rollout = get('rollout/'+a.rollout)
            pods = get('pods', a.selector)
            analyses = get('analysisruns')
            jobs = get('jobs')
            state = rollout.get('status', {})
            record = {'captured_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                      'rollout': {
                          'phase':state.get('phase'),
                          'desired':rollout.get('spec',{}).get('replicas'),
                          'ready':state.get('readyReplicas',0),
                          'available':state.get('availableReplicas',0),
                          'updated':state.get('updatedReplicas',0),
                          'pause_conditions':state.get('pauseConditions',[]),
                          'current_step_index':state.get('currentStepIndex'),
                          'abort':state.get('abort',False),
                          'conditions':state.get('conditions',[])},
                      'pods':[pod_summary(i) for i in pods['items']],
                      'analyses':simple_list(analyses),
                      'lab_jobs':[i for i in simple_list(jobs) if i['name'].startswith('tuning-probe-')]}
            out.write(json.dumps(record,ensure_ascii=False)+'\n');out.flush()
            print(record['captured_at'], 'phase=',state.get('phase'), 'ready=',state.get('readyReplicas',0),
                  'available=',state.get('availableReplicas',0),
                  'pods=',len(record['pods']), flush=True)
        except Exception as exc:
            record = {'captured_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'error':str(exc)}
            out.write(json.dumps(record,ensure_ascii=False)+'\n');out.flush()
            print('WARN: sample failed:',exc,file=sys.stderr,flush=True)
        time.sleep(min(a.every, max(0,end-time.monotonic())))
print('Finished:',path)
