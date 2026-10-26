"""One-time Dev handover: bring Deployment up before retiring its old Rollout.

Run from the published GitOps main checkout on mgmt with Jenkins idle.
No Ops writes. A local receipt preserves Argo sync settings across retries.
"""
import argparse,json,subprocess,time,uuid
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
PATH='chargeops-chatops/overlays/development-rolling'
HANDOVER=PATH+'-handover'
OLD='chargeops-chatops/overlays/development-canary'
RECEIPT=ROOT/'.dev-rolling-migration.json'
OBSOLETE={('Rollout','csms'),('AnalysisTemplate','csms-dev-availability'),
    ('ConfigMap','csms-dev-canary-probe'),('ConfigMap','csms-dev-ocpp-settings'),('ConfigMap','csms-dev-service-gate-settings'),
    ('Service','csms-stable'),('Service','csms-canary'),('Service','csms-dev-canary-probe-metrics'),
    ('ServiceMonitor','csms-dev-canary-probe'),('NetworkPolicy','csms-dev-canary-probe'),
    ('ServiceAccount','csms-dev-canary-probe'),('Role','csms-dev-canary-probe'),('RoleBinding','csms-dev-canary-probe')}


def run(*args,payload=None):
    result=subprocess.run(args,input=payload,text=True,capture_output=True,cwd=ROOT)
    if result.returncode:raise ValueError('Command failed: '+' '.join(args[:4])+'; inspect the named resource on mgmt')
    return result.stdout.strip()


def get(kind,name,namespace='dev',optional=False):
    args=['kubectl','-n',namespace,'get',kind,name,'-o','json']
    if optional:args.append('--ignore-not-found')
    data=run(*args)
    return json.loads(data) if data else None


def patch_app(name,value):
    run('kubectl','-n','argocd','patch','application',name,'--type=merge','-p',json.dumps(value))


def validate_app(app):
    spec=app['spec']
    if (spec['destination']['namespace']!='dev' or spec['source']['path'] not in (OLD,PATH,HANDOVER)
        or spec['source'].get('targetRevision')!='main'
        or spec['source'].get('repoURL')!='https://github.com/raspberry-2jo/chargeops-gitops.git'):
        raise ValueError('Expected the existing main-tracking Dev Application')


def image(deployment):
    return next(c['image'] for c in deployment['spec']['template']['spec']['containers'] if c['name']=='csms')


def desired():
    docs=[d for d in yaml.safe_load_all(run('kubectl','kustomize',PATH)) if d]
    deployment=next(d for d in docs if d['kind']=='Deployment' and d['metadata']['name']=='csms')
    if deployment['spec']['replicas']!=2 or any(d['kind']=='Rollout' for d in docs):
        raise ValueError('Expected a two-replica Deployment and no Dev Rollout')
    return deployment


def plan(application):
    app=get('application',application,'argocd');validate_app(app)
    live=get('deployment','csms');target=desired()
    print('Application path: '+app['spec']['source']['path'])
    print('Live CSMS image: '+image(live))
    print('Rolling image: '+image(target))
    if image(live)!=image(target):
        print('STOP: pin development-rolling/kustomization.yaml to the LIVE stable digest, commit/push main, then retry.')
    print('Apply order: pause auto Sync/prune -> temporary handover quota 6 CPU/6Gi -> verify Deployment 2/2 -> retire Dev Rollout -> final quota 4 CPU/4Gi -> restore Sync policy.')
    return app,live,target


def validate_pruning(app,docs):
    resources=app.get('status',{}).get('resources')
    if not resources:raise ValueError('Argo resource inventory unavailable; refusing final prune')
    intended={(d['kind'],d['metadata']['name']) for d in docs}
    for resource in resources:
        key=(resource['kind'],resource['name'])
        if resource.get('namespace')!='dev' and key!=('Namespace','dev'):
            raise ValueError('Unexpected obsolete resource namespace; refusing prune')
        if key in intended:continue
        if resource.get('namespace')!='dev' or key not in OBSOLETE:
            raise ValueError('Unexpected obsolete resource; inspect Argo inventory before pruning')


def sync_path(application,path,revision,prune=False):
    marker='chargeops-dev-rolling-'+uuid.uuid4().hex[:12]
    patch_app(application,{'spec':{'source':{'path':path},'syncPolicy':{'automated':None}}})
    patch_app(application,{'operation':{'initiatedBy':{'username':marker},'sync':{'revision':revision,'prune':prune}}})
    deadline=time.monotonic()+600
    while time.monotonic()<deadline:
        state=get('application',application,'argocd').get('status',{}).get('operationState',{})
        own=state.get('operation',{}).get('initiatedBy',{}).get('username')==marker
        if own and state.get('syncResult',{}).get('revision')==revision:
            if state.get('phase') in ('Failed','Error'):raise ValueError('Dev sync failed; inspect migration receipt before resuming')
            if state.get('phase')=='Succeeded':return
        time.sleep(5)
    raise ValueError('Dev sync timed out; inspect and resume with the same receipt')


def apply(application):
    app,live,target=plan(application)
    if app.get('operation') or (app.get('status',{}).get('operationState') or {}).get('phase') in ('Running','Terminating'):
        raise ValueError('A Dev sync is running; wait before migration')
    if run('git','status','--porcelain','--untracked-files=no'):
        raise ValueError('Commit and publish the RollingUpdate configuration first')
    run('git','fetch','origin','main')
    revision=run('git','rev-parse','HEAD')
    if revision!=run('git','rev-parse','origin/main'):raise ValueError('Checkout the published main revision first')
    if image(live)!=image(target) or '@sha256:' not in image(target):
        raise ValueError('Migration must preserve the exact live stable digest')
    rollout=get('rollout','csms',optional=True)
    if rollout and app['spec']['source']['path']==OLD:
        state=rollout.get('status',{})
        if (state.get('phase')!='Healthy' or state.get('stableRS')!=state.get('currentPodHash') or state.get('abort')
            or live['spec'].get('replicas')!=0
            or int(state.get('workloadObservedGeneration',0))<live['metadata']['generation']):
            raise ValueError('Wait for a stable Dev Rollout; do not migrate during a release')
        stable=get('replicaset','csms-'+state['stableRS'])
        if image(stable)!=image(live) or not any(o.get('controller') and o.get('uid')==rollout['metadata']['uid']
                                               for o in stable['metadata'].get('ownerReferences',[])):
            raise ValueError('Live template is not the stable Rollout image; finish release/recovery first')
    if not RECEIPT.exists():
        if app['spec']['source']['path']!=OLD:raise ValueError('Original sync policy receipt missing; inspect manually')
        RECEIPT.write_text(json.dumps({'application':application,'syncPolicy':app['spec'].get('syncPolicy',{}),
            'rollout_uid':rollout['metadata']['uid'] if rollout else None,'phase':'prepared'},indent=2),encoding='utf-8')
    saved=json.loads(RECEIPT.read_text(encoding='utf-8'))
    if saved['application']!=application:raise ValueError('Migration receipt belongs to another Application')
    # Prune stays off until the replacement is Ready; old Rollout-owned Pods remain.
    sync_path(application,HANDOVER,revision)
    run('kubectl','-n','dev','rollout','status','deployment/csms','--timeout=300s')
    ready=get('deployment','csms')
    status=ready.get('status',{})
    if (image(ready)!=image(target) or ready['spec']['replicas']!=2 or status.get('observedGeneration',0)<ready['metadata']['generation']
        or any(status.get(k)!=2 for k in ('replicas','readyReplicas','updatedReplicas','availableReplicas'))):
        raise ValueError('Replacement Deployment not exactly Ready 2/2; old Rollout retained')
    rollout=get('rollout','csms',optional=True)
    if rollout:
        if rollout['metadata']['uid']!=saved['rollout_uid']:raise ValueError('Another Rollout exists; refusing to retire it')
        run('kubectl','-n','dev','delete','rollout','csms','--cascade=foreground','--wait=true','--timeout=180s')
    final_docs=[d for d in yaml.safe_load_all(run('kubectl','kustomize',PATH)) if d]
    validate_pruning(get('application',application,'argocd'),final_docs)
    sync_path(application,PATH,revision,prune=True)
    # JSON patch replaces the whole policy; merge patch could retain unwanted keys.
    run('kubectl','-n','argocd','patch','application',application,'--type=json','-p',json.dumps([
        {'op':'add','path':'/spec/syncPolicy','value':saved['syncPolicy']}]))
    saved['phase']='completed';RECEIPT.write_text(json.dumps(saved,indent=2),encoding='utf-8')
    print('PASS: Dev RollingUpdate active, old Rollout retired, original sync policy restored. Run the new Jenkins build for OCPP verification.')


def pin(application):
    app,live,_=plan(application)
    rollout=get('rollout','csms',optional=True)
    if rollout and rollout.get('status',{}).get('phase')!='Healthy':
        raise ValueError('Wait for the Dev release/recovery to finish before pinning')
    reference=image(live)
    if not reference.startswith('registry.chargeops.lab:5000/chargeops/chargeops-csms@sha256:'):
        raise ValueError('Expected a digest-pinned live CSMS')
    path=ROOT/PATH/'kustomization.yaml'
    document=yaml.safe_load(path.read_text(encoding='utf-8'))
    item=next(i for i in document['images'] if i['name']=='registry.chargeops.lab:5000/chargeops/chargeops-csms')
    item['digest']=reference.split('@')[1]
    path.write_text(yaml.safe_dump(document,sort_keys=False),encoding='utf-8')
    print('Pinned only the new RollingUpdate overlay to the live digest. Review, commit and push main; then run apply.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','pin','apply'])
    parser.add_argument('--application',default='chargeops-dev')
    args=parser.parse_args()
    try:{'apply':apply,'pin':pin,'plan':plan}[args.action](args.application)
    except ValueError as error:print('STOP: '+str(error));raise SystemExit(1)
