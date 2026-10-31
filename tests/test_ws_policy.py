import copy
import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ws_policy',ROOT/'chargeops-chatops/overlays/canary/ws_policy.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def good():
    role={'window':60,'connections':1000,'total':6000,'success':6000,'errors':0,'hb_total':3000,'hb_success':3000,
          'connections_at_start':1000,'accepted':0,'abnormal':0,'received':{a:2000 for a in m.ACTIONS}}
    baseline=copy.deepcopy(role);baseline.update(window=120,total=12000,success=12000)
    baseline['received']={a:4000 for a in m.ACTIONS}
    return {'infra':{'available':4,'oom_events':0,'draining':False,'health_ok':True},'baseline':baseline,'connections':1000,
            'roles':{'canary':copy.deepcopy(role),'stable':copy.deepcopy(role)},'check_stable':True}


def assessment():return m.Assessment(m.Policy({'WS_LOAD_PROFILE_CONFIRMED':'true','WS_LOAD_PROFILE_ID':'controlled-test'}))


def test_two_nonoverlapping_steady_windows_required():
    a=assessment();s=good()
    assert a.evaluate(s,0)['state']==0
    assert a.evaluate(s,60)['state']==0
    assert a.evaluate(s,70)['state']==0
    assert a.evaluate(s,120)['state']==1


@pytest.mark.parametrize('name,field,value',[
    ('canary','errors',61),('canary','hb_success',2969),('canary','abnormal',11),('canary','success',5099)])
def test_user_thresholds_fail(name,field,value):
    a=assessment();s=good();a.evaluate(s,0);s['roles'][name][field]=value
    assert a.evaluate(s,60)['state']==-1


def test_exact_boundaries_pass_and_connection_drop_fails_only_steady():
    a=assessment();s=good();s['connections']=980;s['roles']['canary'].update(errors=60,hb_success=2970,abnormal=10,success=5100)
    assert a.evaluate(s,0)['state']==0
    a.evaluate(s,60);assert a.evaluate(s,120)['state']==1
    s['connections']=979.9;assert a.evaluate(s,130)['state']==-1


@pytest.mark.parametrize('field,value',[('available',3),('oom_events',1),('candidate_restarted',True)])
def test_known_safety_failure_during_drain_and_missing_data(field,value):
    s=good();s['infra'].update(draining=True,**{field:value});s['missing']=['prometheus']
    assert assessment().evaluate(s,0)['state']==-1


@pytest.mark.parametrize('field,value',[('errors',float('nan')),('total',599),('connections',0),('hb_total',299),('accepted',None)])
def test_unknown_or_undersampled_cannot_pass(field,value):
    s=good();s['roles']['canary'][field]=value;a=assessment()
    a.evaluate(s,0);a.evaluate(s,60);assert a.evaluate(s,120)['state']==0


def test_declared_load_is_required_and_input_profiles_must_match():
    s=good();a=m.Assessment();a.evaluate(s,0)
    assert a.evaluate(s,120)['signals']['normalized_throughput']['reason']=='OFFERED_LOAD_NOT_ATTESTED'
    s['roles']['canary']['received']['MeterValues']=100;a=assessment();a.evaluate(s,0)
    assert a.evaluate(s,120)['state']==0


def test_weight_is_not_a_traffic_normalizer():
    s=good();c=s['roles']['canary']
    for key in ('connections','total','success','hb_total','hb_success','connections_at_start'):c[key]/=2
    c['received']={a:v/2 for a,v in c['received'].items()};a=assessment();a.evaluate(s,0);a.evaluate(s,60)
    assert a.evaluate(s,120)['state']==1


def test_100_percent_uses_frozen_stable_not_zero_live_role():
    s=good();s['check_stable']=False;s['roles'].pop('stable');a=assessment()
    a.evaluate(s,0);a.evaluate(s,60);assert a.evaluate(s,120)['state']==1


def test_drain_resets_steady_and_success_history():
    s=good();a=assessment();a.evaluate(s,0);a.evaluate(s,60)
    s['infra']['draining']=True;s['connections']=500;assert a.evaluate(s,70)['state']==0
    s['infra']['draining']=False;s['connections']=1000
    assert a.evaluate(s,80)['state']==0
    assert a.evaluate(s,140)['state']==0
    assert a.evaluate(s,200)['state']==1


def experiment_one():
    return m.Assessment(m.Policy({'WS_HEARTBEAT_MODE':'observe-only',
                                  'WS_THROUGHPUT_MODE':'observe-only'}))


def test_experiment_one_normal_worst_case_does_not_false_abort():
    s=good();s['connections']=998.85
    s['roles']['canary'].update(hb_success=2898,success=4626)
    a=experiment_one();a.evaluate(s,0);a.evaluate(s,60);r=a.evaluate(s,120)
    assert r['state']==1
    for name,value in [('canary_heartbeat_server',96.6),('normalized_throughput',22.9)]:
        assert r['signals'][name]['value']==pytest.approx(value)
        assert r['signals'][name]['mode']=='observe-only'
    assert r['signals']['canary_heartbeat_server']['state']==-1  # Not falsely reported as passed.


def test_experiment_one_transition_spike_excluded_then_fault_detected():
    a=experiment_one();s=good();s['connections']=924;s['infra']['draining']=True
    assert a.evaluate(s,0)['signals']['connections']['reason']=='DRAIN_OR_SETTLING'
    s['infra']['draining']=False
    assert a.evaluate(s,10)['state']==0
    s['connections']=956.25
    r=a.evaluate(s,70)
    assert r['state']==-1 and r['signals']['connections']['value']==pytest.approx(4.375)


def test_experiment_one_missing_observations_are_not_fake_success():
    a=experiment_one();s=good()
    s['roles']['canary'].update(hb_total=None,hb_success=None,success=None)
    a.evaluate(s,0);a.evaluate(s,60);r=a.evaluate(s,120)
    assert r['state']==1
    assert r['signals']['canary_heartbeat_server']['state']==0
    assert r['signals']['normalized_throughput']['state']==0
    s['roles']['canary']['total']=599
    assert a.evaluate(s,130)['state']==0  # Enforced sample floor still blocks.
    s['infra']['available']=3
    assert a.evaluate(s,140)['state']==-1


@pytest.mark.parametrize('field,value',[('errors',61),('abnormal',11)])
def test_experiment_one_unvalidated_candidate_limits_still_enforced(field,value):
    a=experiment_one();s=good();s['roles']['canary'][field]=value
    assert a.evaluate(s,0)['state']==-1


@pytest.mark.parametrize('key',['WS_HEARTBEAT_MODE','WS_THROUGHPUT_MODE'])
def test_invalid_mode_cannot_silently_disable_a_gate(key):
    with pytest.raises(ValueError):m.Policy({key:'disabled'})
