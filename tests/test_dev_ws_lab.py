import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
import yaml

ROOT=Path(__file__).resolve().parents[1]
# Other tests may have loaded the Ops module: these lab copies must be identical.
spec=importlib.util.spec_from_file_location('dev_ws_lab',ROOT/'scripts/dev-ws-lab.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
IMAGE='registry.chargeops.lab:5000/chargeops/chargeops-csms@sha256:'+'a'*64


def test_lab_overlay_has_real_csms_and_only_fixed_dev_resources():
    docs=m.render(IMAGE);by={(d['kind'],d['metadata']['name']):d for d in docs}
    rollout=by['Rollout',m.LAB]['spec'];assert rollout['replicas']==4 and rollout['minReadySeconds']==30
    assert rollout['strategy']['canary']['maxSurge']==1 and rollout['strategy']['canary']['maxUnavailable']==0
    assert 'workloadRef' not in rollout
    assert all(d['metadata']['namespace']=='dev' for d in docs)
    assert not any(d['kind'] in ('Secret','Namespace','ResourceQuota','HorizontalPodAutoscaler','Application') for d in docs)
    containers=rollout['template']['spec']['containers'];assert containers[0]['image']==IMAGE
    assert containers[0]['command'][:4]==['python','-m','uvicorn','csms.app:app']
    config=by['ConfigMap',m.LAB+'-config']['data'];assert config['DEV_LAB_SIMULATION_COUNT']=='800' and config['DEV_WS_LAB']=='true'
    metric=by['AnalysisTemplate',m.LAB+'-availability']['spec']['metrics']
    assert len(metric)==1 and 'web' in metric[0]['provider'] and 'dryRun' not in metric[0]
    assert '.dev.svc.' in metric[0]['provider']['web']['url']
    peak=m.peak(docs);assert peak['pods']==7 and peak['limits.cpu']==pytest.approx(1.65)
    assert peak['limits.memory']==1792*1024**2
    assert by['ConfigMap',m.LAB+'-settings']['data']['WS_OOM_MAX_SAMPLE_AGE_SECONDS']=='45'
    settings=by['ConfigMap',m.LAB+'-settings']['data']
    assert settings['WS_HEARTBEAT_MODE']==settings['WS_THROUGHPUT_MODE']=='observe-only'
    for module in ('ws_policy.py','ws_evaluator.py'):
        assert (ROOT/'chargeops-chatops/overlays/dev-ws-lab'/module).read_bytes()==(ROOT/'chargeops-chatops/overlays/canary'/module).read_bytes()


@pytest.mark.parametrize('change',[{'metadata':{'name':'csms','namespace':'dev'}},{'metadata':{'name':m.LAB,'namespace':'ops','labels':{m.LABEL:m.LAB}}},{'kind':'Secret'}])
def test_non_lab_mutations_are_rejected(change):
    doc={'kind':'ConfigMap','metadata':{'name':m.LAB+'-config','namespace':'dev','labels':{m.LABEL:m.LAB}}}
    doc.update(change)
    with pytest.raises(ValueError):m.validate_documents([doc])


def test_quota_failure_is_reported_without_raising_the_quota():
    quota={'metadata':{'name':'development-budget'},'spec':{'hard':{'pods':'10','limits.cpu':'4'}},'status':{'used':{'pods':'4','limits.cpu':'2.5'}}}
    assert len(m.quota_errors([quota],{'pods':7,'limits.cpu':1.65}))==2
    quota['status']['used']={'pods':'3','limits.cpu':'2.25'}
    assert m.quota_errors([quota],{'pods':7,'limits.cpu':1.65})==[]


def test_argo_plugin_flags_follow_plugin_name(monkeypatch):
    commands=[]
    monkeypatch.setattr(m.subprocess,'run',lambda args,**kw:(commands.append(args) or SimpleNamespace(returncode=0,stdout='',stderr='')))
    m.Cluster('lab-context').argo('abort',m.LAB)
    assert commands==[['kubectl','argo','rollouts','abort',m.LAB,'--context','lab-context','-n','dev']]


def test_stale_lab_recovery_cannot_overwrite_another_run(tmp_path):
    (tmp_path/'run.json').write_text(json.dumps({'context':'one','rollout_uid':'uid','run_id':'old'}))
    cluster=SimpleNamespace(context='one',owned=lambda *a:{'metadata':{'uid':'uid'},'spec':{'template':{'metadata':{'annotations':{'chargeops.test/run-id':'new'}}}}})
    with pytest.raises(ValueError,match='newer lab run'):m.recover(cluster,tmp_path)


def test_load_generator_is_namespace_bounded():
    spec=importlib.util.spec_from_file_location('ws_lab_load',ROOT/'chargeops-chatops/overlays/dev-ws-lab/load.py')
    load=importlib.util.module_from_spec(spec);spec.loader.exec_module(load)
    env={'POD_NAMESPACE':'dev','LAB_WORKLOAD':m.LAB,'COHORT_SIZE':'400','COHORT_OFFSET':'400'}
    assert load.settings(env)==(400,400)
    for update in ({'POD_NAMESPACE':'ops'},{'LAB_WORKLOAD':'csms'},{'COHORT_SIZE':'401'},{'COHORT_OFFSET':'500'}):
        with pytest.raises(ValueError):load.settings(dict(env,**update))


def test_preflight_collects_only_secret_key_names(monkeypatch):
    commands=[]
    class Cluster:
        def command(self,*args,**kw):
            commands.append(args)
            return 'DB_DSN\nCSMS_API_TOKEN\n' if args[:2]==('get','secret') else ''
        def owned(self,*args,**kwargs):return None
        def get(self,kind,**kwargs):
            assert kind!='secret'
            return {'items':[]}
    m.preflight(Cluster(),m.render(IMAGE))
    args=next(c for c in commands if c[:2]==('get','secret'))
    assert 'go-template=' in args[-1] and '$k' in args[-1]
    assert next(c for c in commands if c[0]=='apply')[1]=='--dry-run=server'


def test_candidate_promoted_during_capture_is_not_killed(tmp_path,monkeypatch):
    record={'context':'one','rollout_uid':'uid','run_id':'experiment'}
    (tmp_path/'run.json').write_text(json.dumps(record))
    rollout={'metadata':{'uid':'uid'},'spec':{'template':{'metadata':{'annotations':{'chargeops.test/run-id':'experiment'}}}},
             'status':{'phase':'Progressing','currentPodHash':'newhash','stableRS':'oldhash'}}
    class Cluster:
        context='one'
        def owned(self,*args):return copy.deepcopy(rollout)
        def get(self,kind,**kwargs):
            if kind=='replicasets':return {'items':[{'metadata':{'uid':'rs','ownerReferences':[{'uid':'uid','controller':True}]}}]}
            return {'items':[{'metadata':{'name':m.LAB+'-newhash-abcd','ownerReferences':[{'uid':'rs'}]},'status':{'conditions':[{'type':'Ready','status':'True'}]}}]}
        def command(self,*args,**kw):raise AssertionError('Candidate was already promoted; must not execute kill')
    def capture(*args):rollout['status'].update(stableRS='newhash',phase='Healthy')
    monkeypatch.setattr(m,'collect',capture)
    with pytest.raises(ValueError,match='ownership changed'):m.restart_candidate(Cluster(),tmp_path)


def test_real_load_client_socket_protocol_and_controlled_reconnect(monkeypatch):
    import asyncio
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import connect
    spec=importlib.util.spec_from_file_location('ws_lab_load_e2e',ROOT/'chargeops-chatops/overlays/dev-ws-lab/load.py')
    load=importlib.util.module_from_spec(spec);spec.loader.exec_module(load)
    async def scenario():
        actions=[];urls=[];mode=['general'];done=asyncio.Event()
        async def server(ws):
            async for raw in ws:
                message=json.loads(raw);assert message[0]==2
                actions.append(message[2])
                payload={'status':'Accepted','interval':30,'currentTime':'2026-10-31T00:00:00Z'} if message[2]=='BootNotification' else {'currentTime':'2026-10-31T00:00:00Z'} if message[2]=='Heartbeat' else {}
                await ws.send(json.dumps([3,message[1],payload]))
                if set(actions)>={'BootNotification','Heartbeat','StatusNotification','MeterValues'}:mode[0]='canary'
                if actions.count('BootNotification')>=2:done.set()
        async with serve(server,'127.0.0.1',0,subprotocols=['ocpp1.6']) as socket_server:
            port=socket_server.sockets[0].getsockname()[1]
            def local(uri,**kwargs):
                urls.append(uri);return connect(f'ws://127.0.0.1:{port}/ocpp/test',**kwargs)
            monkeypatch.setattr(load,'connect',local)
            monkeypatch.setattr(load,'Path',lambda _:SimpleNamespace(read_text=lambda:mode[0]))
            task=asyncio.create_task(load.run({'POD_NAMESPACE':'dev','LAB_WORKLOAD':m.LAB,'COHORT_SIZE':'1','COHORT_OFFSET':'400','COHORT':'canary'}))
            try:await asyncio.wait_for(done.wait(),8)
            finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
        assert any('csms-ws-lab.dev.' in url for url in urls)
        assert any('csms-ws-lab-canary.dev.' in url for url in urls)
    asyncio.run(scenario())
