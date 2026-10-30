"""Explicit-context, dev-only setup for the real CSMS WebSocket Canary lab.
Never selects an Argo Application, changes quota, touches Ops, or prints/persists secret values.
"""
import argparse
import copy
from datetime import datetime,timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import yaml

ROOT=Path(__file__).resolve().parents[1]
OVERLAY=ROOT/'chargeops-chatops/overlays/dev-ws-lab'
sys.path.insert(0,str(OVERLAY))
from ws_evaluator import Prom,Queries

LAB='csms-ws-lab';LABEL='chargeops.test/workload';SELECTOR=LABEL+'='+LAB
IMAGE=re.compile(r'registry\.chargeops\.lab:5000/chargeops/chargeops-csms@sha256:[a-f0-9]{64}')


def pinned(image):
    if not IMAGE.fullmatch(image or '') or image.endswith('0'*64):raise ValueError('Provide the built CSMS immutable registry @sha256 digest')
    return image


def validate_documents(documents):
    allowed={'Rollout','AnalysisTemplate','Service','ConfigMap','Deployment','ServiceAccount','Role','RoleBinding','NetworkPolicy','ServiceMonitor','PodMonitor'}
    for doc in documents:
        m=doc.get('metadata',{})
        if (doc.get('kind') not in allowed or m.get('namespace')!='dev' or not m.get('name','').startswith(LAB)
            or m.get('labels',{}).get(LABEL)!=LAB):raise ValueError('Refusing a non-lab/non-dev resource')
    return documents


def render(image):
    pinned(image)
    output=subprocess.check_output(['kubectl','kustomize',str(OVERLAY)],text=True,encoding='utf-8')
    documents=[d for d in yaml.safe_load_all(output) if d]
    for doc in documents:
        if doc['kind']=='Rollout':containers=doc['spec']['template']['spec']['containers']
        elif doc['kind']=='Deployment' and doc['metadata']['name']==LAB+'-load':containers=doc['spec']['template']['spec']['containers']
        else:continue
        for container in containers:container['image']=image
    return validate_documents(documents)


def quantity(value):
    text=str(value)
    factors={'m':.001,'Ki':1024,'Mi':1024**2,'Gi':1024**3,'Ti':1024**4,'k':1000,'M':1000000,'G':1000000000}
    for suffix in sorted(factors,key=len,reverse=True):
        if text.endswith(suffix):return float(text[:-len(suffix)])*factors[suffix]
    return float(text)


def peak(documents):
    total={'pods':0,'requests.cpu':0,'limits.cpu':0,'requests.memory':0,'limits.memory':0}
    for doc in documents:
        if doc['kind'] not in ('Rollout','Deployment'):continue
        count=5 if doc['kind']=='Rollout' else doc['spec'].get('replicas',1)
        total['pods']+=count
        for container in doc['spec']['template']['spec']['containers']:
            for scope in ('requests','limits'):
                for resource in ('cpu','memory'):total[scope+'.'+resource]+=count*quantity(container['resources'][scope][resource])
    return total


def quota_errors(quotas,required):
    errors=[]
    for quota in quotas:
        status=quota.get('status',{});hard=status.get('hard',quota['spec']['hard']);used=status.get('used',{})
        for key,extra in required.items():
            if key in hard and quantity(used.get(key,0))+extra>quantity(hard[key]):
                errors.append(quota['metadata']['name']+': '+key+' lacks lab surge headroom')
    return errors


class Cluster:
    def __init__(self,context):self.context=context
    def command(self,*args,payload=None,allowed_exit=(0,)):
        result=subprocess.run(['kubectl','--context',self.context,'--request-timeout=20s','-n','dev',*args],
            input=payload,text=True,encoding='utf-8',stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if result.returncode not in allowed_exit:raise RuntimeError('kubectl '+args[0]+' failed: '+result.stderr.strip())
        return result.stdout
    def argo(self,*args):
        result=subprocess.run(['kubectl','argo','rollouts',*args,'--context',self.context,'-n','dev'],text=True,encoding='utf-8',capture_output=True)
        if result.returncode:raise RuntimeError('Lab Argo command failed: '+result.stderr.strip())
        return result.stdout
    def get(self,kind,name=None,selector=None,optional=False):
        args=['get',kind]+([name] if name else [])+(['-l',selector] if selector else [])
        if optional:args+=['--ignore-not-found']
        result=self.command(*args,'-o','json');return json.loads(result) if result.strip() else None
    def owned(self,kind,name,optional=False):
        doc=self.get(kind,name,optional=optional)
        if doc and (not name.startswith(LAB) or doc['metadata'].get('namespace')!='dev'
                    or doc['metadata'].get('labels',{}).get(LABEL)!=LAB):raise ValueError('Existing object is not owned by this Dev lab: '+name)
        return doc
    def patch(self,kind,name,body):
        self.owned(kind,name)
        return self.command('patch',kind,name,'--type=merge','-p',json.dumps(body))
    def mode(self,mode):
        self.owned('deployment',LAB+'-load')
        if mode not in ('general','canary'):raise ValueError('Invalid mode')
        self.command('exec','deployment/'+LAB+'-load','-c','canary','--','python','-c',
                     "from pathlib import Path;Path('/control/target').write_text('"+mode+"')")


def preflight(cluster,documents):
    validate_documents(documents)
    # Inspect key names only; values never leave this function or enter reports.
    secret_keys=cluster.command('get','secret','chargeops-dev-secrets','-o',
        'go-template={{range $k, $_ := .data}}{{printf "%s\\n" $k}}{{end}}').splitlines()
    if not {'DB_DSN','CSMS_API_TOKEN'}<=set(secret_keys):raise ValueError('Existing dev secret needs DB_DSN and CSMS_API_TOKEN keys')
    for doc in documents:cluster.owned(doc['kind'],doc['metadata']['name'],optional=True)
    existing=cluster.get('pods',selector=SELECTOR)['items']
    if existing:raise ValueError('Lab Pods already exist; collect/recover or clean up that lab before a new setup')
    problems=quota_errors(cluster.get('resourcequotas')['items'],peak(documents))
    if problems:raise ValueError('; '.join(problems)+'. Do not raise shared Dev quota automatically.')
    cluster.command('apply','--dry-run=server','-f','-',payload=yaml.safe_dump_all(documents))
    print('PASS: explicit context/dev resource ownership, secret key names, quota peak and server dry-run')
    print('Peak extra lab resources (including surge): '+json.dumps(peak(documents)))


def current_proof(cluster,prom):
    rollout=cluster.owned('rollout',LAB);s=rollout.get('status',{});h=s.get('stableRS')
    if (s.get('phase')!='Healthy' or s.get('abort') or not h or h!=s.get('currentPodHash')
        or s.get('availableReplicas',0)<4 or str(s.get('observedGeneration'))!=str(rollout['metadata']['generation'])):
        raise ValueError('The baseline lab must be Healthy at four Available Pods')
    q=Queries('dev',LAB,h,h);at=time.time();role={k:prom.scalar(v,at) for k,v in q.role(h,120).items()}
    if not 784<=role['connections']<=816:raise ValueError('Need the full 800-session baseline for120s; do not lower sample floors')
    if role['total']<600 or role['hb_total']<300:raise ValueError('Baseline request/Heartbeat samples are insufficient')
    pods=cluster.get('pods',selector='app='+LAB)['items'];expected={p['metadata']['name'] for p in pods}
    active={r['metric'].get('pod') for r in prom.vector(q.metric('chargeops_csms_active_sessions',h,',kind="service"'),at)}
    if active!=expected:raise ValueError('Prometheus must scrape every lab CSMS Pod; inspect PodMonitor selectors')
    oom=f'container_oom_events_total{{namespace="dev",container="csms",pod=~"{LAB}-{h}-.*"}}'
    ages=prom.vector('time()-timestamp('+oom+')',at)
    covered={r['metric'].get('pod') for r in ages if float(r['value'][1])<=45}
    if not expected<=covered:raise ValueError('cAdvisor OOM counters absent/stale; this is not a verified zero')
    return rollout,{'at':at,'stable_hash':h,'role':role,'oom_counter_age':ages}


def start(cluster,prom,args):
    if args.output.exists() and any(args.output.iterdir()):raise ValueError('Use a new empty run output directory')
    rollout,baseline=current_proof(cluster,prom)
    settings=cluster.owned('configmap',LAB+'-settings')
    args.output.mkdir(parents=True,exist_ok=True)
    token=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    record={'context':cluster.context,'rollout_uid':rollout['metadata']['uid'],'run_id':token,'previous_template':rollout['spec']['template'],
            'previous_settings':settings['data'],'baseline':baseline,'mode':args.mode}
    (args.output/'run.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    data=dict(settings['data'],GATE_BASELINE_AT=str(int(baseline['at'])),WS_LOAD_PROFILE_ID='dev-lab-'+token+'-HB30-MV60-800',
              WS_LOAD_PROFILE_CONFIRMED='false' if args.mode=='hold' else 'true')
    cluster.patch('configmap',LAB+'-settings',{'data':data})
    template=copy.deepcopy(rollout['spec']['template']);template['metadata'].setdefault('annotations',{})['chargeops.test/run-id']=token
    if args.candidate_image:template['spec']['containers'][0]['image']=pinned(args.candidate_image)
    cluster.patch('rollout',LAB,{'spec':{'template':template}})
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        candidate=cluster.owned('rollout',LAB).get('status',{}).get('currentPodHash')
        if candidate and candidate!=baseline['stable_hash']:
            cluster.mode('canary');print('Candidate started; controlled400-client cohort switches to Canary. Save logs and watch Argo.');return
        time.sleep(2)
    raise ValueError('Candidate not observed in180s. Run collect/recover with this run.json; do not delete csms.')


def collect(cluster,output):
    output.mkdir(parents=True,exist_ok=True);rollout=cluster.owned('rollout',LAB)
    (output/'rollout.json').write_text(json.dumps(rollout,indent=2),encoding='utf-8')
    uid=rollout['metadata']['uid']
    runs=[r for r in cluster.get('analysisruns')['items'] if any(o.get('uid')==uid for o in r['metadata'].get('ownerReferences',[]))]
    (output/'analysisruns.json').write_text(json.dumps(runs,indent=2),encoding='utf-8')
    pods=cluster.get('pods',selector=SELECTOR)
    for pod in pods['items']:pod.pop('spec',None)
    (output/'pods.json').write_text(json.dumps(pods,indent=2),encoding='utf-8')
    for deployment,container in ((LAB+'-evaluator','evaluator'),(LAB+'-load','stable'),(LAB+'-load','canary')):
        try:log=cluster.command('logs','deployment/'+deployment,'-c',container,'--tail=200')
        except RuntimeError:log='Unavailable; inspect the lab Pods and events.'
        (output/(deployment+'-'+container+'.log')).write_text(log,encoding='utf-8')
    print('Saved only Dev lab evidence to '+str(output))


def recover(cluster,output):
    record=json.loads((output/'run.json').read_text(encoding='utf-8'))
    rollout=cluster.owned('rollout',LAB)
    if record['context']!=cluster.context or rollout['metadata']['uid']!=record['rollout_uid']:
        raise ValueError('Recovery context/UID does not match the saved run')
    if rollout['spec']['template']['metadata'].get('annotations',{}).get('chargeops.test/run-id')!=record['run_id']:
        raise ValueError('A newer lab run owns the workload; refusing stale recovery')
    cluster.argo('abort',LAB)
    # Replace the exact previous template; merge would retain the candidate run annotation.
    cluster.command('patch','rollout',LAB,'--type=json','-p',json.dumps([{'op':'replace','path':'/spec/template','value':record['previous_template']}]))
    cluster.mode('general')
    print('Previous lab template restored; wait for Healthy/four Available Pods, then warm120s before another run.')



def restart_candidate(cluster,output):
    record=json.loads((output/'run.json').read_text(encoding='utf-8'))
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        rollout=cluster.owned('rollout',LAB);status=rollout.get('status',{})
        if record['context']!=cluster.context or record['rollout_uid']!=rollout['metadata']['uid']:
            raise ValueError('Fault injection context/UID differs from the saved lab run')
        if rollout['spec']['template']['metadata'].get('annotations',{}).get('chargeops.test/run-id')!=record['run_id']:
            raise ValueError('A newer experiment owns this lab; refusing fault injection')
        candidate=status.get('currentPodHash')
        if status.get('phase')=='Healthy' or candidate==status.get('stableRS') or status.get('abort'):
            raise ValueError('Need an actively progressing unpromoted candidate')
        owners={r['metadata']['uid'] for r in cluster.get('replicasets',selector='app='+LAB)['items']
                if any(o.get('uid')==record['rollout_uid'] and o.get('controller') for o in r['metadata'].get('ownerReferences',[]))}
        pods=cluster.get('pods',selector='app='+LAB+',rollouts-pod-template-hash='+str(candidate))['items']
        for pod in pods:
            if (not pod['metadata'].get('deletionTimestamp') and any(o.get('uid') in owners for o in pod['metadata'].get('ownerReferences',[]))
                and any(c.get('type')=='Ready' and c.get('status')=='True' for c in pod.get('status',{}).get('conditions',[]))):
                collect(cluster,output/'before-restart')
                latest=cluster.owned('rollout',LAB)
                if (latest['metadata']['uid']!=record['rollout_uid'] or latest.get('status',{}).get('stableRS')==candidate
                    or latest['spec']['template']['metadata'].get('annotations',{}).get('chargeops.test/run-id')!=record['run_id']):
                    raise ValueError('Candidate ownership changed during evidence capture; no fault injected')
                cluster.command('exec',pod['metadata']['name'],'-c','csms','--','python','-c',
                    'import os,signal;os.kill(1,signal.SIGKILL)',allowed_exit=(0,137))
                print('Killed only the saved Dev lab candidate process. Expect restart/availability failure; collect evidence.');return
        time.sleep(2)
    raise ValueError('No owned Ready candidate in120s; no fault was injected')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['render','preflight','apply','start','collect','recover','restart-candidate','cleanup'])
    p.add_argument('--context',required=True);p.add_argument('--image');p.add_argument('--candidate-image');p.add_argument('--output',type=Path,required=True)
    p.add_argument('--prom-url',default='http://127.0.0.1:19090');p.add_argument('--mode',choices=['normal','hold'],default='normal');a=p.parse_args()
    cluster=Cluster(a.context)
    if a.command in ('render','preflight','apply'):
        docs=render(a.image);a.output.mkdir(parents=True,exist_ok=True)
        (a.output/'lab.yaml').write_text(yaml.safe_dump_all(docs,sort_keys=False),encoding='utf-8')
        if a.command=='render':print('Rendered dev-only lab.yaml; no cluster changes');return
        preflight(cluster,docs)
        if a.command=='apply':cluster.command('apply','-f',str(a.output/'lab.yaml'));print('Applied only new dev/csms-ws-lab resources')
    elif a.command=='start':start(cluster,Prom(a.prom_url),a)
    elif a.command=='collect':collect(cluster,a.output)
    elif a.command=='recover':recover(cluster,a.output)
    elif a.command=='restart-candidate':restart_candidate(cluster,a.output)
    else:
        docs=validate_documents([d for d in yaml.safe_load_all((a.output/'lab.yaml').read_text(encoding='utf-8')) if d])
        for doc in docs:cluster.owned(doc['kind'],doc['metadata']['name'],optional=True)
        cluster.command('delete','-f',str(a.output/'lab.yaml'),'--ignore-not-found','--wait=false')
        print('Deleted only the recorded dev lab objects; existing Dev CSMS and namespace remain')

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Lab stopped: '+str(error),file=sys.stderr);raise SystemExit(1)
