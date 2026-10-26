import copy
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('service_gates', ROOT / 'chargeops-chatops/overlays/development-canary/service_gates.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
BASE = dict(connections=10, throughput=10, disconnect=5/60, reconnect=2/60)
GOOD = dict(connections=10, throughput=10, disconnect=5/60, reconnect=2/60, total=10, errors=0)


@pytest.mark.parametrize('update,level', [({'connections':7},'3'), ({'disconnect':200/60},'3'),
    ({'reconnect':300/60},'3'), ({'throughput':5},'4'), ({'errors':0.5001},'5')])
def test_user_failure_cases(update, level):
    observer = g.Observer()
    with pytest.raises(ValueError): g.compare(BASE, dict(GOOD, **update), observer)
    assert observer.status[level] == -1


def test_threshold_boundaries_and_zero_baseline_are_not_false_success():
    observer = g.Observer()
    g.compare(BASE, dict(GOOD, errors=0.5, connections=7.01, throughput=5.01), observer)
    assert all(observer.status[level] == 1 for level in ('3','4','5'))
    with pytest.raises(g.MissingData): g.compare(dict(BASE, connections=0), GOOD, g.Observer())
    with pytest.raises(g.MissingData): g.compare(dict(BASE, throughput=0), GOOD, g.Observer())
    with pytest.raises(g.MissingData): g.compare(BASE, dict(GOOD,total=0), g.Observer())


def pod(ready=True, restarts=0, reason=None):
    return {'metadata':{'uid':'one','labels':{'rollouts-pod-template-hash':'abc'}},
            'status':{'conditions':[{'type':'Ready','status':'True' if ready else 'False'}],
                      'containerStatuses':[{'restartCount':restarts, 'state':{'waiting':{'reason':reason}}}]}}


@pytest.mark.parametrize('item', [pod(False), pod(restarts=1), pod(reason='CrashLoopBackOff')])
def test_infrastructure_failure(item):
    with pytest.raises(ValueError, match='L1'): g.pod_check([item], {}, minimum=1)


def test_prometheus_missing_samples_block_and_disabled_is_explicit(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"status":"success","data":{"result":[]}}'
    monkeypatch.setattr(g.TRANSPORT, 'open', lambda *a,**k: Response())
    with pytest.raises(g.MissingData): g.query('metric', at=123)
    monkeypatch.setenv('GATE_ENABLED','false')
    observer = g.Observer()
    g.main(observer)
    assert set(observer.status.values()) == {3}
    assert b'chargeops_canary_gate_status' in observer.render()


def test_candidate_abnormal_disconnect_fails_before_window(monkeypatch):
    import sys
    from types import SimpleNamespace
    monkeypatch.setenv('GATE_ENABLED','true')
    monkeypatch.setenv('GATE_BASELINE_AT', str(int(g.time.time())))
    monkeypatch.setattr(g, 'canary_hash', lambda:'abc')
    monkeypatch.setitem(sys.modules,'probe',SimpleNamespace(pods=lambda:[pod(),dict(pod(),metadata={'uid':'two'})],request=lambda url:(True,0.01)))
    monkeypatch.setattr(g, 'query', lambda expr, at=None: 1)
    observer = g.Observer()
    with pytest.raises(ValueError, match='candidate abnormal'): g.main(observer)
    assert observer.status['3'] == -1


def test_health_failure_stops_before_prometheus_baseline(monkeypatch):
    import sys
    from types import SimpleNamespace
    monkeypatch.setenv('GATE_ENABLED','true')
    monkeypatch.setenv('GATE_BASELINE_AT', str(int(g.time.time())))
    monkeypatch.setattr(g, 'canary_hash', lambda:'abc')
    monkeypatch.setitem(sys.modules,'probe',SimpleNamespace(pods=lambda:[pod(),dict(pod(),metadata={'uid':'two'})],request=lambda url:(False,0.01)))
    observer = g.Observer()
    with pytest.raises(ValueError, match='L2'): g.main(observer)
    assert observer.status['2'] == -1


def test_complete_pass_uses_fixed_pre_delivery_baseline_and_observes_each_step(monkeypatch):
    import sys
    from types import SimpleNamespace
    now, requests = [1000], []
    monkeypatch.setattr(g.time,'time',lambda:now[0])
    monkeypatch.setattr(g.time,'sleep',lambda seconds:now.__setitem__(0,now[0]+seconds))
    monkeypatch.setenv('GATE_ENABLED','true')
    monkeypatch.setenv('GATE_BASELINE_AT','800')
    monkeypatch.setattr(g,'canary_hash',lambda:'abc')
    monkeypatch.setitem(sys.modules,'probe',SimpleNamespace(pods=lambda:[pod(),dict(pod(),metadata={'uid':'two'})],request=lambda url:(True,0.01)))
    def query(expr, at=None):
        requests.append((expr,at))
        if 'csms-abc-' in expr: return 0
        if expr.startswith('avg_over_time'): return 10
        if expr == g.CONNECTIONS: return 10
        if expr == g.THROUGHPUT: return 10
        if expr == g.DISCONNECTS or expr == g.RECONNECTS or expr == g.ERRORS: return 0
        if expr == g.ALL_RESULTS: return 10
        raise AssertionError(expr)
    monkeypatch.setattr(g,'query',query)
    observer = g.Observer()
    g.main(observer)
    assert now[0] == 1100  # wait for the entire 5m post-delivery window
    assert set(observer.status.values()) == {1}
    assert sum(at==800 for _,at in requests) == 4
    assert requests[0][0].startswith('avg_over_time')
