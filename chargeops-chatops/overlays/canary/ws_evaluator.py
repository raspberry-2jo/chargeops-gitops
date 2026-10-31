"""Read-only, bounded HTTP assessor for Argo Web provider's real three-state result.

All workload changes remain GitOps-owned. No charger/DB writes or synthetic traffic.
Unknown proof returns state=0 (Inconclusive), never a Job exit-code convention.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import ssl
import threading
import time
from urllib.parse import urlencode,urlparse,parse_qs
from urllib.request import Request,build_opener,ProxyHandler,HTTPSHandler
from urllib.error import HTTPError
from ws_policy import Policy,Assessment,ACTIONS

HASH=re.compile(r'[a-z0-9]{5,20}')
NAME=re.compile(r'[a-z0-9][a-z0-9-]{0,61}[a-z0-9]|[a-z0-9]')
LIMIT=threading.BoundedSemaphore(2)
REPORTS={};REPORT_LOCK=threading.Lock()


class Missing(Exception):pass


def timestamp(value):
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (TypeError,ValueError,AttributeError):return None


class Kube:
    def __init__(self,namespace):
        if namespace not in ('dev','ops'):raise ValueError('Only dev/ops assessment is permitted')
        self.namespace=namespace
        directory=Path('/var/run/secrets/kubernetes.io/serviceaccount')
        self.token=(directory/'token').read_text().strip()
        self.transport=build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context(cafile=str(directory/'ca.crt'))))

    def get(self,kind,name=None,label=None):
        allowed={'pods':('v1',''), 'services':('v1',''), 'configmaps':('v1',''),
                 'replicasets':('apps/v1','apis'), 'rollouts':('argoproj.io/v1alpha1','apis')}
        version,group=allowed[kind]
        prefix='/'+group+'/'+version if group else '/api/'+version
        if name and not NAME.fullmatch(name):raise ValueError('Invalid object name')
        path=f'{prefix}/namespaces/{self.namespace}/{kind}'+('/'+name if name else '')
        if label:path+='?'+urlencode({'labelSelector':label})
        request=Request('https://kubernetes.default.svc'+path,headers={'Authorization':'Bearer '+self.token})
        with self.transport.open(request,timeout=3) as response:return json.load(response)


class Prom:
    def __init__(self,url):
        parsed=urlparse(url)
        if parsed.scheme not in ('http','https') or parsed.username or parsed.password:raise ValueError('Invalid Prometheus URL')
        self.url=url.rstrip('/');self.transport=build_opener(ProxyHandler({}))

    def vector(self,expr,at):
        with self.transport.open(self.url+'/api/v1/query?'+urlencode({'query':expr,'time':at}),timeout=3) as response:
            data=json.load(response)
        if data.get('status')!='success' or data.get('data',{}).get('resultType')!='vector':raise Missing('PROMETHEUS_RESPONSE')
        rows=data['data']['result']
        if not rows:raise Missing('NO_SERIES')
        for row in rows:
            number=float(row['value'][1])
            if not math.isfinite(number) or number<0:raise Missing('NONFINITE_OR_NEGATIVE')
        return rows

    def scalar(self,expr,at):
        rows=self.vector(expr,at)
        if len(rows)!=1:raise Missing('AMBIGUOUS_SERIES')
        return float(rows[0]['value'][1])


class Queries:
    """Actual application metrics; target pod/namespace labels are preflight requirements."""
    def __init__(self,namespace,workload,candidate,stable):
        self.namespace=namespace;self.workload=workload;self.candidate=candidate;self.stable=stable

    def metric(self,name,role=None,extra=''):
        suffix=f',pod=~"{self.workload}-{role}-.*"' if role else f',pod=~"{self.workload}-[a-z0-9]+-[a-z0-9]+"'
        labels=suffix+extra+',chargeops_policy_scrape="ws-v1"'
        # Deduplicate alternate ServiceMonitor scrape paths without summing twice.
        return f'max by (pod,action,result,kind,reason) ({name}{{namespace="{self.namespace}"{labels}}} or {name}{{exported_namespace="{self.namespace}"{labels}}})'

    def average(self,name,role=None,window=60,extra=''):
        return f'avg_over_time((sum({self.metric(name,role,extra)}))[{window}s:5s])'

    def count(self,name,role,window=60,extra=''):
        # rate/increase before deduplication and aggregation preserves per-process resets.
        suffix=f',pod=~"{self.workload}-{role}-.*"'+extra+',chargeops_policy_scrape="ws-v1"'
        return (f'sum(max by (pod,action,result,kind,reason) ('
          f'increase({name}{{namespace="{self.namespace}"{suffix}}}[{window}s]) or '
          f'increase({name}{{exported_namespace="{self.namespace}"{suffix}}}[{window}s])))')

    def role(self,role,window=60):
        results='chargeops_ocpp_handler_results_total';actions=',action=~"Heartbeat|MeterValues|StatusNotification"'
        return {'connections':self.average('chargeops_csms_active_sessions',role,window,',kind="service"'),
          'total':self.count(results,role,window,actions),
          'success':self.count(results,role,window,actions+',result="success"'),
          'errors':self.count(results,role,window,actions+',result=~"error|timeout"'),
          'hb_total':self.count(results,role,window,',action="Heartbeat"'),
          'hb_success':self.count(results,role,window,',action="Heartbeat",result="success"'),
          'accepted':self.count('chargeops_csms_accepted_sessions_total',role,window,',kind="service"'),
          'abnormal':self.count('chargeops_csms_disconnect_total',role,window,',kind="service",reason=~"abnormal|handler_error"'),
          **{'received_'+a:self.count('chargeops_ocpp_messages_total',role,window,f',action="{a}"') for a in ACTIONS}}


class Collector:
    def __init__(self,kube,prom,workload,settings_name,candidate,stable,stage,rollout_uid):
        if not NAME.fullmatch(workload) or not HASH.fullmatch(candidate) or not HASH.fullmatch(stable) or stage not in ('25','50','100'):
            raise ValueError('Invalid rollout context')
        self.kube,self.prom,self.workload,self.settings_name=kube,prom,workload,settings_name
        self.candidate,self.stable,self.stage,self.uid=candidate,stable,stage,rollout_uid
        self.settings=kube.get('configmaps',settings_name)['data']
        self.policy=Policy(self.settings)
        value=self.settings.get('GATE_BASELINE_AT','0')
        if not re.fullmatch(r'[0-9]{10}',value):raise Missing('BASELINE_NOT_STAMPED')
        self.baseline_at=int(value)
        if not 0<=time.time()-self.baseline_at<=7200:raise Missing('BASELINE_EXPIRED')
        self.q=Queries(kube.namespace,workload,candidate,stable);self.previous=None;self.base_oom=None
        self.baseline=self.role(stable,self.baseline_at,120)
        self.policy_version=json.dumps(self.settings,sort_keys=True)

    def role(self,hash_value,at,window=60):
        expressions=self.q.role(hash_value,window);values={};missing=[]
        def fetch(item):
            key,expr=item
            try:return key,self.prom.scalar(expr,at)
            except Exception:return key,None
        with ThreadPoolExecutor(max_workers=6) as pool:
            values=dict(pool.map(fetch,expressions.items()))
        # The entire 60s window must actually contain samples, not one extrapolated scrape.
        freshness=self.q.metric('chargeops_csms_active_sessions',hash_value,',kind="service"')
        minimum=(window//5)-2
        suffix=f',pod=~"{self.workload}-{hash_value}-.*",kind="service",chargeops_policy_scrape="ws-v1"'
        quality=(f'min(max by(pod,kind) (count_over_time(chargeops_csms_active_sessions{{namespace="{self.kube.namespace}"{suffix}}}[{window}s]) or '
                 f'count_over_time(chargeops_csms_active_sessions{{exported_namespace="{self.kube.namespace}"{suffix}}}[{window}s])))')
        try:
            # timestamp before aggregation: timestamp(aggregate) would falsely look fresh.
            age=f'max(time() - max by(pod,kind) (timestamp(chargeops_csms_active_sessions{{namespace="{self.kube.namespace}"{suffix}}}) or timestamp(chargeops_csms_active_sessions{{exported_namespace="{self.kube.namespace}"{suffix}}})))'
            if self.prom.scalar(quality,at)<minimum or self.prom.scalar(age,at)>15:raise Missing('SCRAPE_COVERAGE')
        except Exception:missing.append('SCRAPE_COVERAGE')
        try:values['connections_at_start']=self.prom.scalar('sum('+freshness+')',at-window)
        except Exception:values['connections_at_start']=None
        values['received']={a:values.pop('received_'+a) for a in ACTIONS}
        values['window']=window
        if missing:values['coverage']=False
        else:values['coverage']=True
        return values

    def infrastructure(self,now):
        rollout=self.kube.get('rollouts',self.workload);status=rollout.get('status',{})
        if (rollout['metadata']['uid']!=self.uid or status.get('currentPodHash')!=self.candidate
            or status.get('stableRS')!=self.stable or status.get('currentStepIndex')!={'25':1,'50':3,'100':5}[self.stage]
            or str(status.get('observedGeneration'))!=str(rollout['metadata']['generation'])):raise Missing('STALE_ROLLOUT_CONTEXT')
        if rollout['spec'].get('replicas')!=4:raise Missing('EXPECTED_FOUR_REPLICAS')
        for suffix,expected in (('canary',self.candidate),('stable',self.stable)):
            service=self.kube.get('services',self.workload+'-'+suffix)
            if service['spec']['selector'].get('rollouts-pod-template-hash')!=expected:raise Missing('STALE_ROLLOUT_CONTEXT')
        if self.kube.get('configmaps',self.settings_name)['data']!=self.settings:raise Missing('POLICY_OR_BASELINE_CHANGED')
        sets=self.kube.get('replicasets',label='app='+self.workload)['items']
        owners={r['metadata']['uid'] for r in sets if any(o.get('uid')==self.uid and o.get('controller') for o in r['metadata'].get('ownerReferences',[]))}
        pods=[p for p in self.kube.get('pods',label='app='+self.workload)['items'] if any(o.get('uid') in owners for o in p['metadata'].get('ownerReferences',[]))]
        if not pods:raise Missing('NO_OWNED_PODS')
        members={p['metadata']['uid'] for p in pods};changed=self.previous is not None and self.previous!=members;self.previous=members
        infra={'available':status.get('availableReplicas'),'oom_events':None,'draining':None,'membership_changed':changed}
        progress=next((c for c in status.get('conditions',[]) if c.get('type')=='Progressing'),{})
        last=timestamp(progress.get('lastUpdateTime') or progress.get('lastTransitionTime'))
        if last is not None:infra['progress_remaining_seconds']=rollout['spec'].get('progressDeadlineSeconds',600)-(now-last)
        for pod in pods:
            for container in pod.get('status',{}).get('containerStatuses',[]):
                if container.get('name')!='csms':continue
                for s in (container.get('state',{}),container.get('lastState',{})):
                    end=s.get('terminated',{});when=timestamp(end.get('finishedAt'))
                    if end.get('reason')=='OOMKilled' and when is not None and when>=self.baseline_at:infra['oom_events']=1
                if pod['metadata'].get('labels',{}).get('rollouts-pod-template-hash')==self.candidate and container.get('restartCount',0)>0:
                    infra['candidate_restarted']=True
        return infra,pods

    def oom(self,pods,now):
        expr=f'container_oom_events_total{{namespace="{self.kube.namespace}",container="csms",pod=~"{self.workload}-({self.candidate}|{self.stable})-.*"}}'
        if self.base_oom is None:
            try:self.base_oom={self.oom_key(r):float(r['value'][1]) for r in self.prom.vector(expr,self.baseline_at)}
            except Missing:self.base_oom={}
        current=self.prom.vector(expr,now);ages=self.prom.vector('time()-timestamp('+expr+')',now)
        freshness={self.oom_key(r):float(r['value'][1]) for r in ages}
        owned={p['metadata']['name']:p for p in pods};covered=set();events=0;unknown=False
        for row in current:
            key=self.oom_key(row);name=row['metric'].get('pod');value=float(row['value'][1])
            if name not in owned:continue
            if freshness.get(key,math.inf)>getattr(getattr(self,'policy',None),'oom_max_age',15):unknown=True;continue
            covered.add(name)
            if key in self.base_oom:
                if value<self.base_oom[key]:unknown=True
                else:events+=value-self.base_oom[key]
            else:
                pod=owned[name]
                starts=[timestamp(c.get('state',{}).get('running',{}).get('startedAt')) for c in pod.get('status',{}).get('containerStatuses',[]) if c.get('name')=='csms']
                if any(t is not None and t>=self.baseline_at for t in starts):events+=value
                else:unknown=True
        if set(owned)-covered or unknown:raise Missing('OOM_COUNTER_COVERAGE')
        return events

    @staticmethod
    def oom_key(row):return tuple(row['metric'].get(k,'') for k in ('pod','container','id'))

    def snapshot(self,now):
        infra,pods=self.infrastructure(now);missing=[]
        if not self.baseline['coverage']:self.baseline=self.role(self.stable,self.baseline_at,120)
        try:
            measured=self.oom(pods,now)
            infra['oom_events']=max(infra['oom_events'] or 0,measured)
        except Exception:missing.append('OOM_COUNTER_COVERAGE')
        # Match coverage to the owned Pod inventory, not merely to whichever series exist.
        try:
            rows=self.prom.vector(self.q.metric('chargeops_csms_active_sessions',extra=',kind="service"'),now)
            actual={r['metric'].get('pod') for r in rows}
            expected={p['metadata']['name'] for p in pods}
            if actual!=expected:missing.append('SCRAPE_COVERAGE')
        except Exception:missing.append('SCRAPE_COVERAGE')
        try:infra['draining']=self.prom.scalar('max('+self.q.metric('chargeops_csms_draining')+')',now)>0 or any(p['metadata'].get('deletionTimestamp') for p in pods)
        except Exception:missing.append('DRAIN_STATE')
        states=[]
        for target in (self.workload,self.workload+'-canary')+((self.workload+'-stable',) if self.stage!='100' else ()):
            try:
                with self.prom.transport.open(f'http://{target}.{self.kube.namespace}.svc.cluster.local:8000/healthz',timeout=2) as response:
                    states.append(response.status==200 and json.load(response).get('ok') is True)
            except HTTPError as error:states.append(False if error.code>=500 else None)
            except Exception:states.append(None)
        infra['health_ok']=False if False in states else True if all(v is True for v in states) else None
        roles={'canary':self.role(self.candidate,now)}
        if self.stage!='100':roles['stable']=self.role(self.stable,now)
        if any(not r['coverage'] for r in roles.values()) or not self.baseline['coverage']:missing.append('SCRAPE_COVERAGE')
        try:connections=self.prom.scalar(self.q.average('chargeops_csms_active_sessions',extra=',kind="service"'),now)
        except Exception:connections=None;missing.append('CONNECTIONS')
        result={'infra':infra,'connections':connections,'baseline':self.baseline,'roles':roles,'check_stable':self.stage!='100','missing':missing}
        return result


def execute(collector,clock=time.monotonic,sleep=time.sleep,wall=time.time,publish=lambda x:None):
    assessment=Assessment(collector.policy);started=clock();latest={'state':0,'status':'UNVERIFIED','reason':'NOT_OBSERVED'}
    # Leave controller progressDeadline headroom; the Web request is bounded independently.
    limit=min(collector.policy.max_seconds,540)
    while clock()-started<limit:
        try:
            snapshot=collector.snapshot(wall())
            remaining=snapshot.get('infra',{}).get('progress_remaining_seconds')
            if remaining is not None and remaining<=30:
                latest={'state':0,'status':'UNVERIFIED','reason':'PROGRESS_DEADLINE_HEADROOM'};publish(latest);return latest
            latest=assessment.evaluate(snapshot,clock())
        except Missing as error:
            latest={'state':0,'status':'UNVERIFIED','reason':str(error)}
            if str(error) in ('STALE_ROLLOUT_CONTEXT','POLICY_OR_BASELINE_CHANGED'):publish(latest);return latest
            assessment=Assessment(collector.policy)
        except Exception:
            latest={'state':0,'status':'UNVERIFIED','reason':'OBSERVATION_UNAVAILABLE'};assessment=Assessment(collector.policy)
        publish(latest)
        if latest['state'] in (1,-1):return latest
        sleep(min(10,max(0,limit-(clock()-started))))
    latest.update(state=0,status='UNVERIFIED',reason='OBSERVATION_DEADLINE');publish(latest);return latest


def render_metrics():
    with REPORT_LOCK:reports=list(REPORTS.items())
    lines=[]
    for (candidate,stage),result in reports:
        labels=f'candidate="{candidate}",stage="{stage}"'
        lines.append(f'chargeops_ws_canary_state{{{labels}}} {result["state"]}')
        lines.append(f'chargeops_ws_canary_observed_timestamp_seconds{{{labels}}} {result.get("observed_at",0)}')
        for name,signal in result.get('signals',{}).items():
            tag=labels+f',signal="{name}",mode="{signal.get("mode","enforce")}"'
            lines.append(f'chargeops_ws_canary_signal_state{{{tag}}} {signal["state"]}')
            for suffix,key in (('value','value'),('limit','limit')):
                if signal.get(key) is not None:lines.append(f'chargeops_ws_canary_signal_{suffix}{{{tag}}} {signal[key]}')
    return ('\n'.join(lines)+'\n').encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def do_GET(self):
        path=urlparse(self.path)
        if path.path=='/healthz':self.send_json({'ok':True});return
        if path.path=='/metrics':
            data=render_metrics();self.send_response(200);self.send_header('Content-Type','text/plain; version=0.0.4');self.end_headers();self.wfile.write(data);return
        if path.path!='/evaluate':self.send_error(404);return
        if not LIMIT.acquire(blocking=False):self.send_json({'state':0,'status':'UNVERIFIED','reason':'EVALUATOR_BUSY'});return
        try:
            args=parse_qs(path.query,strict_parsing=True)
            if set(args)!=set(('candidate','stable','stage','uid')) or any(len(v)!=1 for v in args.values()):raise ValueError('Invalid context')
            candidate,stable,stage,uid=(args[k][0] for k in ('candidate','stable','stage','uid'))
            if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}',uid):raise ValueError('Invalid UID')
            kube=Kube(os.environ.get('POD_NAMESPACE','ops'))
            settings_name=os.environ.get('SETTINGS_NAME','csms-ops-service-gate-settings')
            settings=kube.get('configmaps',settings_name)['data']
            collector=Collector(kube,Prom(settings['GATE_PROMETHEUS_URL']),os.environ.get('WORKLOAD_NAME','csms'),settings_name,candidate,stable,stage,uid)
            def publish(result):
                result['observed_at']=time.time()
                with REPORT_LOCK:
                    REPORTS[candidate,stage]=result
                    while len(REPORTS)>24:REPORTS.pop(next(iter(REPORTS)))
            result=execute(collector,publish=publish)
            result.update(candidate=candidate,stable=stable,stage=stage,baseline_at=collector.baseline_at)
            print(json.dumps(result,allow_nan=False),flush=True);self.send_json(result)
        except Missing as error:
            self.send_json({'state':0,'status':'UNVERIFIED','reason':str(error)})
        except Exception:
            self.send_json({'state':0,'status':'UNVERIFIED','reason':'INVALID_OR_UNAVAILABLE_CONTEXT'})
        finally:LIMIT.release()

    def send_json(self,data):
        body=json.dumps(data,allow_nan=False).encode();self.send_response(200)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass


if __name__=='__main__':
    ThreadingHTTPServer(('0.0.0.0',9111),Handler).serve_forever()
