"""Read-only proof collection through an operator's local Prometheus port-forward."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'chargeops-chatops/overlays/canary'))
from ws_evaluator import Queries


def main():
    p=argparse.ArgumentParser();p.add_argument('--namespace',choices=['dev','ops'],default='dev');p.add_argument('--workload',default='csms')
    p.add_argument('--prom-url',default='http://127.0.0.1:19090');p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if not __import__('re').fullmatch(r'[a-z0-9-]+',a.workload):p.error('Invalid workload name')
    a.output.mkdir(parents=True,exist_ok=True)
    def get(kind,name=None):
        cmd=['kubectl','--request-timeout=10s','-n',a.namespace,'get',kind]+([name] if name else [])+['-o','json']
        return json.loads(subprocess.check_output(cmd,text=True))
    rollout=get('rollout',a.workload);s=rollout.get('status',{});candidate=s.get('currentPodHash');stable=s.get('stableRS')
    if not candidate or not stable:raise SystemExit('No current candidate/stable hashes; capture once a baseline Rollout exists')
    report={'captured_at':datetime.now(timezone.utc).isoformat(),'namespace':a.namespace,'workload':a.workload,'rollout':rollout,
            'pods':get('pods'),'analysisruns':get('analysisruns'),'queries':{}}
    q=Queries(a.namespace,a.workload,candidate,stable)
    expressions={role+'_'+key:expr for role,hash_value in (('candidate',candidate),('stable',stable)) for key,expr in q.role(hash_value).items()}
    expressions['oom_coverage']=f'container_oom_events_total{{namespace="{a.namespace}",container="csms"}}'
    for name,expr in expressions.items():
        try:
            with urlopen(a.prom_url.rstrip('/')+'/api/v1/query?'+urlencode({'query':expr,'time':time.time()}),timeout=10) as response:data=json.load(response)
            report['queries'][name]={'expression':expr,'response':data}
        except Exception as e:report['queries'][name]={'expression':expr,'error':type(e).__name__}
    # No secrets, environment values, config-map contents, auth headers, or DB rows are collected.
    # Pod specs can reference secret names but never contain their values.
    for pod in report['pods'].get('items',[]):pod.pop('spec',None)
    (a.output/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    missing=[k for k,v in report['queries'].items() if not v.get('response',{}).get('data',{}).get('result')]
    print('Captured read-only evidence; missing/empty queries: '+', '.join(missing))
    print('This inventory is NOT a Canary success or a live load-test result.')

if __name__=='__main__':main()
