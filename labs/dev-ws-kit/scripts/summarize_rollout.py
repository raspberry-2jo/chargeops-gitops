#!/usr/bin/env python3
"""Summarize read-only rollout samples. Gaps are observational, not controller deadlines."""
import argparse
import datetime as dt
import json
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('jsonl')
a=p.parse_args()
raw=[json.loads(line) for line in Path(a.jsonl).read_text(encoding='utf-8').splitlines() if line.strip()]
s=[r for r in raw if 'rollout' in r]
if len(s)<2: raise SystemExit('At least two good snapshots required')

def seconds(a,b):
    try:return (dt.datetime.fromisoformat(b.replace('Z','+00:00'))-dt.datetime.fromisoformat(a.replace('Z','+00:00'))).total_seconds()
    except (TypeError,ValueError):return None

print('Read-only observation:',a.jsonl)
print('Samples:',len(s),'collection duration (s):',round(seconds(s[0]['captured_at'],s[-1]['captured_at']),1))
print('Sample errors:',len(raw)-len(s))
print('Max observed Pod count:',max(len(r['pods']) for r in s))
print('Max observed Pending Pods:',max(sum(1 for p in r['pods'] if p['phase']=='Pending') for r in s))
print('Minimum observed available replicas:',min(r['rollout']['available'] for r in s))
print('Max restarts observed:',max([p['restarts'] for r in s for p in r['pods']] or [0]))
print('Observed phases:', ', '.join(dict.fromkeys(str(r['rollout']['phase']) for r in s)))
print('Abort observed:',any(r['rollout']['abort'] for r in s))
ready_times={}
for r in s:
 for pod in r['pods']:
  if pod['ready'] and pod['name'] not in ready_times:
   value=seconds(pod['created'],pod['ready_at'])
   if value is not None and value>=0: ready_times[pod['name']]=round(value,1)
print('First observed Pod creation->Ready duration (s), per Pod:',ready_times)
# No-progress observation based on changes in replicas/step/phase; not controller exact progress conditions.
longest_gap=0.0
last_change=s[0]['captured_at']
prev=None
for r in s:
 state=r['rollout']
 key=(state['ready'],state['available'],state['updated'],state['current_step_index'],state['phase'])
 if state['pause_conditions'] or state['phase']=='Paused':
  last_change=r['captured_at'];prev=None;continue
 if prev is None or key!=prev:
  last_change=r['captured_at']
 else:
  gap=seconds(last_change,r['captured_at'])
  if gap is not None:longest_gap=max(longest_gap,gap)
 prev=key
print('Max OBSERVED unchanged-progress gap excluding sampled pauses (s):',round(longest_gap,1))
print('NOTE: This is not a reliable exact progressDeadlineSeconds calculation. Use Rollout events/conditions too.')
print('NOTE: Missing samples, other controllers, and pod selectors can affect the result.')
