import copy,importlib.util,json,subprocess
from pathlib import Path
import pytest,yaml

ROOT=Path(__file__).resolve().parents[1]


def render(environment):
    return [d for d in yaml.safe_load_all(subprocess.check_output(['kubectl','kustomize',str(ROOT/'chargeops-chatops/overlays'/environment)],text=True)) if d]


def test_dev_rolling_preserves_simulators_metrics_and_has_no_rollout():
    docs=render('development-rolling')
    assert all(d['metadata'].get('namespace')=='dev' for d in docs if d['kind']!='Namespace')
    assert not any(d['kind'] in ('Rollout','AnalysisTemplate') for d in docs)
    deployment=next(d for d in docs if d['kind']=='Deployment' and d['metadata']['name']=='csms')
    assert deployment['spec']['replicas']==2
    assert deployment['spec']['strategy']=={'type':'RollingUpdate','rollingUpdate':{'maxSurge':1,'maxUnavailable':0}}
    assert deployment['spec']['template']['metadata']['annotations']['chargeops.io/deployment-mode']=='rolling'
    config=next(d for d in docs if d['kind']=='ConfigMap' and d['metadata']['name']=='chargeops-dev-config')['data']
    assert config['DEV_SIMULATION_ENABLED']=='true' and 'CI-DEV-CANARY-01' in config['DEV_SIMULATION_IDS']
    simulator=next(d for d in docs if d['kind']=='Deployment' and d['metadata']['name']=='dev-vcharger')
    assert '@sha256:' in simulator['spec']['template']['spec']['containers'][0]['image']
    assert any(d['kind']=='ServiceMonitor' and d['metadata']['name']=='csms-dev-release-probe' for d in docs)
    final_quota=next(d for d in docs if d['kind']=='ResourceQuota')['spec']['hard']
    assert final_quota['limits.cpu']=='4' and final_quota['limits.memory']=='4Gi'
    handover=render('development-rolling-handover')
    temporary=next(d for d in handover if d['kind']=='ResourceQuota')['spec']['hard']
    assert temporary['limits.cpu']=='6' and temporary['limits.memory']=='6Gi'


def test_ops_policy_uses_existing_traffic_without_active_fixture():
    docs=render('canary')
    template=next(d for d in docs if d['kind']=='AnalysisTemplate')
    assert {m['name'] for m in template['spec']['metrics']} == {
        'availability-error-latency-restarts', 'five-level-service-gate', 'websocket-session-quality'}
    # No active connection Job / DB secret is needed for standard Ops promotion.
    assert all(m['provider']['job']['spec']['template']['spec']['containers'][0]['command']
               != ['python','-m','csms.release_probe'] for m in template['spec']['metrics'])
    rollout=next(d for d in docs if d['kind']=='Rollout')
    for step in rollout['spec']['strategy']['canary']['steps']:
        if 'analysis' in step:
            assert {'name':'candidate-hash','valueFrom':{'podTemplateHashValue':'Latest'}} in step['analysis']['args']
    service=next(d for d in docs if d['kind']=='Service' and d['metadata']['name']=='csms-canary')
    assert any(p['port']==9000 for p in service['spec']['ports'])
    policy=next(d for d in docs if d['kind']=='NetworkPolicy' and d['metadata']['name']=='csms-canary-probe')
    assert any(p['port']==9000 for p in policy['spec']['ingress'][0]['ports'])


def load_migration():
    spec=importlib.util.spec_from_file_location('rolling_migration',ROOT/'scripts/migrate-dev-rolling.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


@pytest.mark.parametrize('replacement_ready',[False,True])
def test_migration_never_deletes_rollout_before_replacement_ready(tmp_path,monkeypatch,replacement_ready):
    m=load_migration();monkeypatch.setattr(m,'RECEIPT',tmp_path/'receipt.json')
    img='registry.chargeops.lab:5000/chargeops/chargeops-csms@sha256:'+'a'*64
    deployment={'metadata':{'generation':2},'spec':{'replicas':2,'template':{'spec':{'containers':[{'name':'csms','image':img}]}}},
        'status':{'observedGeneration':2,'replicas':2,'readyReplicas':2 if replacement_ready else 1,'updatedReplicas':2,'availableReplicas':2}}
    app={'spec':{'destination':{'namespace':'dev'},'source':{'path':m.OLD,'targetRevision':'main','repoURL':'https://github.com/raspberry-2jo/chargeops-gitops.git'},
        'syncPolicy':{'automated':{'prune':False,'selfHeal':True}}}}
    rollout={'metadata':{'uid':'rollout'},'status':{'phase':'Healthy','stableRS':'hash','currentPodHash':'hash','workloadObservedGeneration':2}}
    baseline=copy.deepcopy(deployment);baseline['spec']['replicas']=0
    stable=copy.deepcopy(deployment);stable['metadata']['ownerReferences']=[{'controller':True,'uid':'rollout'}]
    monkeypatch.setattr(m,'plan',lambda appname:(app,baseline,deployment))
    commands=[]
    def run(*args,**kwargs):
        commands.append(args)
        if args[:2]==('git','rev-parse'):return 'c'*40
        return ''
    monkeypatch.setattr(m,'run',run)
    syncs=[]
    monkeypatch.setattr(m,'sync_path',lambda appname,path,rev,**kwargs:syncs.append(path))
    monkeypatch.setattr(m,'validate_pruning',lambda *a:None)
    monkeypatch.setattr(m,'get',lambda kind,*a,**k:rollout if kind=='rollout' else
        stable if kind=='replicaset' else
        deployment if kind=='deployment' else {'status':{'operationState':{'phase':'Succeeded','syncResult':{'revision':'c'*40}}}})
    if replacement_ready:
        m.apply('chargeops-dev')
        assert json.loads((tmp_path/'receipt.json').read_text())['phase']=='completed'
        assert any('delete' in c and 'rollout' in c for c in commands)
        assert syncs==[m.HANDOVER,m.PATH]
    else:
        with pytest.raises(ValueError,match='not exactly Ready'):m.apply('chargeops-dev')
        assert not any('delete' in c for c in commands)
        assert syncs==[m.HANDOVER]


def test_final_prune_refuses_unrelated_resources_and_cross_namespace_objects():
    m=load_migration()
    intended=[{'kind':'Deployment','metadata':{'name':'csms'}}]
    m.validate_pruning({'status':{'resources':[{'kind':'ConfigMap','name':'csms-dev-canary-probe','namespace':'dev'}]}},intended)
    for resource in [{'kind':'PersistentVolumeClaim','name':'data','namespace':'dev'},
        {'kind':'ConfigMap','name':'csms-dev-canary-probe','namespace':'ops'},
        {'kind':'Deployment','name':'csms','namespace':'ops'},
        {'kind':'Deployment','name':'other','namespace':'dev'}]:
        with pytest.raises(ValueError,match='Unexpected obsolete'):
            m.validate_pruning({'status':{'resources':[resource]}},intended)


def test_sync_waits_for_its_own_operation_not_stale_success(monkeypatch):
    m=load_migration();markers=[];reads=[]
    def patch(name,value):
        if 'operation' in value:markers.append(value['operation']['initiatedBy']['username'])
    monkeypatch.setattr(m,'patch_app',patch)
    def get(*args,**kwargs):
        reads.append(1)
        return {'status':{'operationState':{'operation':{'initiatedBy':{'username':'old' if len(reads)==1 else markers[-1]}},
            'phase':'Succeeded','syncResult':{'revision':'c'*40}}}}
    monkeypatch.setattr(m,'get',get)
    monkeypatch.setattr(m.time,'sleep',lambda seconds:None)
    m.sync_path('chargeops-dev',m.PATH,'c'*40)
    assert len(reads)==2
