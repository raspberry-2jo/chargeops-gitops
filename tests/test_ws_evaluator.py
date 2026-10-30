import copy
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from urllib.request import urlopen
from urllib.parse import urlencode
import pytest
from test_ws_policy import good,m

sys.modules['ws_policy']=m
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ws_evaluator',ROOT/'chargeops-chatops/overlays/canary/ws_evaluator.py')
e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)


def run(snapshot):
    now=[0]
    collector=SimpleNamespace(policy=m.Policy({'WS_LOAD_PROFILE_CONFIRMED':'true','WS_LOAD_PROFILE_ID':'test'}),snapshot=lambda at:copy.deepcopy(snapshot))
    result=e.execute(collector,clock=lambda:now[0],sleep=lambda n:now.__setitem__(0,now[0]+n),wall=lambda:1000+now[0])
    return result,now[0]


def test_real_loop_waits_120_seconds_and_expires_missing_data_without_success():
    result,seconds=run(good());assert result['state']==1 and seconds==120
    bad=good();bad['missing']=['SCRAPE_COVERAGE']
    result,seconds=run(bad);assert result['state']==0 and result['reason']=='OBSERVATION_DEADLINE' and seconds==540


def test_oom_fails_on_first_observation_and_progress_headroom_is_hold():
    s=good();s['infra']['oom_events']=1
    result,seconds=run(s);assert result['state']==-1 and seconds==0
    s=good();s['infra']['progress_remaining_seconds']=20
    assert run(s)[0]['reason']=='PROGRESS_DEADLINE_HEADROOM'


@pytest.mark.parametrize('mode,expected',[('good',1),('error',-1),('missing',0)])
def test_actual_http_argo_jsonpath_contract(monkeypatch,mode,expected):
    payload=good()
    if mode=='error':payload['roles']['canary']['errors']=100
    if mode=='missing':payload['missing']=['SCRAPE_COVERAGE']
    policy=m.Policy({'WS_LOAD_PROFILE_CONFIRMED':'true','WS_LOAD_PROFILE_ID':'replay-only'})
    fake=SimpleNamespace(policy=policy,baseline_at=1000,snapshot=lambda at:copy.deepcopy(payload))
    monkeypatch.setattr(e,'Collector',lambda *a,**kw:fake)
    monkeypatch.setattr(e,'Kube',lambda ns:SimpleNamespace(get=lambda *a:{'data':{'GATE_PROMETHEUS_URL':'http://unused'}}))
    original=e.execute;now=[0]
    monkeypatch.setattr(e,'execute',lambda collector,publish:original(collector,clock=lambda:now[0],sleep=lambda n:now.__setitem__(0,now[0]+n),wall=lambda:1000+now[0],publish=publish))
    server=e.ThreadingHTTPServer(('127.0.0.1',0),e.Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        query=urlencode({'candidate':'newhash','stable':'oldhash','stage':'25','uid':'owned-rollout-uid'})
        with urlopen(f'http://127.0.0.1:{server.server_port}/evaluate?'+query,timeout=5) as response:actual=json.load(response)
        assert actual['state']==expected
        # Exactly the scalar selected by AnalysisTemplate provider.web.jsonPath.
        yaml=__import__('yaml');template=yaml.safe_load((ROOT/'chargeops-chatops/overlays/canary/analysis.yaml').read_text())
        metric=next(x for x in template['spec']['metrics'] if x['name']=='websocket-service-policy')
        assert metric['provider']['web']['jsonPath']=='{$.state}'
        assert (actual['state']==1)==(expected==1)
        assert (actual['state']==-1)==(expected==-1)
        with urlopen(f'http://127.0.0.1:{server.server_port}/metrics') as response:metrics=response.read().decode()
        assert 'chargeops_ws_canary_state' in metrics
    finally:server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('values',[[],['NaN'],['+Inf'],['-Inf'],['-1'],['1','2']])
def test_prometheus_empty_nonfinite_and_ambiguous_are_unknown(monkeypatch,values):
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self):return json.dumps({'status':'success','data':{'resultType':'vector','result':[{'metric':{},'value':[1000,x]} for x in values]}}).encode()
    p=e.Prom('http://prometheus');monkeypatch.setattr(p.transport,'open',lambda *a,**kw:Response())
    with pytest.raises(e.Missing):p.scalar('metric',1000)


def test_queries_are_namespace_and_revision_scoped_and_preserve_counter_resets():
    q=e.Queries('ops','csms','newhash','oldhash')
    for key,expr in q.role('newhash').items():
        assert 'csms-newhash-.*' in expr and 'namespace="ops"' in expr
        assert 'vector(0)' not in expr and 'namespace="dev"' not in expr
        if key!='connections':assert 'increase(' in expr and 'max by' in expr


def test_stale_context_cannot_become_success():
    def snapshot(now):raise e.Missing('STALE_ROLLOUT_CONTEXT')
    c=SimpleNamespace(policy=m.Policy(),snapshot=snapshot)
    assert e.execute(c)['state']==0


def test_old_oom_is_not_a_new_failure_and_new_oom_is_counted():
    c=e.Collector.__new__(e.Collector);c.candidate='newhash';c.stable='oldhash';c.workload='csms';c.baseline_at=1000
    c.kube=SimpleNamespace(namespace='ops');c.base_oom=None
    row=lambda value:{'metric':{'pod':'csms-oldhash-aaaaa','container':'csms','id':'one'},'value':[0,str(value)]}
    class Prom:
        current=2
        def vector(self,expr,at):
            if 'timestamp' in expr:return [row(1)]
            return [row(2 if at==1000 else self.current)]
    c.prom=Prom()
    pods=[{'metadata':{'name':'csms-oldhash-aaaaa'},'status':{'containerStatuses':[]}}]
    assert c.oom(pods,1010)==0
    c.prom.current=3;assert c.oom(pods,1020)==1


def test_missing_oom_coverage_and_counter_reset_are_not_zero():
    c=e.Collector.__new__(e.Collector);c.candidate='newhash';c.stable='oldhash';c.workload='csms';c.baseline_at=1000
    c.kube=SimpleNamespace(namespace='ops');c.base_oom={('csms-oldhash-aaaaa','csms','one'):2}
    row={'metric':{'pod':'csms-oldhash-aaaaa','container':'csms','id':'one'},'value':[1000,'1']}
    c.prom=SimpleNamespace(vector=lambda *a:[copy.deepcopy(row)])
    pods=[{'metadata':{'name':'csms-oldhash-aaaaa'},'status':{'containerStatuses':[]}}]
    with pytest.raises(e.Missing):c.oom(pods,1010)


def test_lab_oom_scrape_age_is_explicit_without_relaxing_ops_default():
    assert m.Policy().oom_max_age==15
    assert m.Policy({'WS_OOM_MAX_SAMPLE_AGE_SECONDS':'45'}).oom_max_age==45
    for invalid in ('0','61','NaN'):
        with pytest.raises(ValueError):m.Policy({'WS_OOM_MAX_SAMPLE_AGE_SECONDS':invalid})
