"""Render isolated Dev native-Web-provider contract tests; never applies anything.
Synthetic fixture data validates controller states, not real OCPP performance.
"""
import argparse
import copy
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
IMAGE='registry.chargeops.lab:5000/chargeops/python:3.13-slim@sha256:d879ed88565a7a3c614b980b369d4a5025a9d057b71f93d09fc6d0b2ca823a18'
SERVER='''import os,json,time
from http.server import BaseHTTPRequestHandler,HTTPServer
from ws_policy import Assessment,Policy,ACTIONS
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  state=0
  if self.path.startswith('/evaluate'):
   role={'window':60,'connections':1000,'total':6000,'success':6000,'errors':0,'hb_total':3000,'hb_success':3000,'connections_at_start':1000,'accepted':0,'abnormal':0,'received':{a:2000 for a in ACTIONS}}
   baseline=dict(role,window=120,total=12000,success=12000,received={a:4000 for a in ACTIONS})
   snapshot={'infra':{'available':4,'oom_events':0,'draining':False,'health_ok':True},'baseline':baseline,'connections':1000,'roles':{'canary':role,'stable':dict(role)},'check_stable':True}
   if os.environ['MODE']=='fail':role['errors']=100
   if os.environ['MODE']=='hold':snapshot['missing']=['SCRAPE_COVERAGE']
   assessment=Assessment(Policy({'WS_LOAD_PROFILE_CONFIRMED':'true','WS_LOAD_PROFILE_ID':'SYNTHETIC_CONTROLLER_TEST'}))
   # Virtual time is deliberate: this fixture proves state wiring, not elapsed stability.
   for at in (0,60,120):result=assessment.evaluate(snapshot,at)
   state=result['state']
  body=json.dumps({'state':state,'ok':True}).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
HTTPServer(('0.0.0.0',8000),Handler).serve_forever()
'''


def manifests(scenario):
    name='ws-policy-'+scenario;labels={'app':name};meta={'namespace':'dev','labels':{'chargeops.test/ws-policy':'true'}}
    def obj(kind,spec,api='v1',suffix=''):
        return {'apiVersion':api,'kind':kind,'metadata':dict(meta,name=name+suffix),**spec}
    config=obj('ConfigMap',{'data':{'server.py':SERVER,'ws_policy.py':(ROOT/'chargeops-chatops/overlays/canary/ws_policy.py').read_text()}})
    services=[obj('Service',{'spec':{'selector':labels,'ports':[{'port':8000,'targetPort':8000}]}},suffix=s) for s in ('','-stable','-canary')]
    metric={'name':'websocket-service-policy','count':1,'failureLimit':0,'successCondition':'result == 1','failureCondition':'result == -1','provider':{'web':{'url':f'http://{name}-canary.dev.svc.cluster.local:8000/evaluate','timeoutSeconds':10,'jsonPath':'{$.state}'}}}
    analysis=obj('AnalysisTemplate',{'spec':{'metrics':[metric]}},'argoproj.io/v1alpha1')
    rollout=obj('Rollout',{'spec':{'replicas':4,'minReadySeconds':2,'progressDeadlineSeconds':180,'progressDeadlineAbort':True,'selector':{'matchLabels':labels},
      'template':{'metadata':{'labels':labels},'spec':{'automountServiceAccountToken':False,'securityContext':{'runAsNonRoot':True,'runAsUser':10001},'containers':[{
       'name':'fixture','image':IMAGE,'command':['python','/fixture/server.py'],'env':[{'name':'MODE','value':scenario}],
       'ports':[{'containerPort':8000}],'readinessProbe':{'httpGet':{'path':'/healthz','port':8000},'periodSeconds':2},
       'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},
       'resources':{'requests':{'cpu':'20m','memory':'32Mi'},'limits':{'cpu':'200m','memory':'64Mi'}},
       'volumeMounts':[{'name':'fixture','mountPath':'/fixture','readOnly':True}]}],'volumes':[{'name':'fixture','configMap':{'name':name}}]}},
       'strategy':{'canary':{'maxSurge':1,'maxUnavailable':0,'stableService':name+'-stable','canaryService':name+'-canary','steps':[step for weight in (25,50,100) for step in ({'setWeight':weight},{'analysis':{'templates':[{'templateName':name}]}})]}}}},'argoproj.io/v1alpha1')
    policy=obj('NetworkPolicy',{'spec':{'podSelector':{'matchLabels':labels},'policyTypes':['Ingress'],'ingress':[{'from':[{'namespaceSelector':{'matchExpressions':[{'key':'kubernetes.io/metadata.name','operator':'In','values':['argocd','argo-rollouts']} ]},'podSelector':{'matchLabels':{'app.kubernetes.io/name':'argo-rollouts'}}}],'ports':[{'protocol':'TCP','port':8000}]}]}},'networking.k8s.io/v1')
    baseline=[config,*services,analysis,policy,rollout]
    candidate=copy.deepcopy(rollout);candidate['spec']['template']['metadata']['annotations']={'chargeops.test/revision':'candidate'}
    return baseline,candidate


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--scenario',choices=['pass','fail','hold'],required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    baseline,candidate=manifests(args.scenario);args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'baseline.yaml').write_text(yaml.safe_dump_all(baseline))
    (args.output/'candidate.yaml').write_text(yaml.safe_dump(candidate))
    print('Rendered only dev/ws-policy-'+args.scenario+'; no cluster changes made')

if __name__=='__main__':main()
