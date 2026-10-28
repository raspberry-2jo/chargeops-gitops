import importlib.util
from pathlib import Path
import sys

import pytest

BASE = Path(__file__).resolve().parents[1] / 'chargeops-chatops/overlays/canary'
spec = importlib.util.spec_from_file_location('service_gates', BASE/'service_gates.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
sys.modules['service_gates'] = g
spec = importlib.util.spec_from_file_location('websocket_quality', BASE/'websocket_quality.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


@pytest.mark.parametrize('signal,value,limit,state',[
    ('disconnect_percent',5,5,1),('disconnect_percent',5.01,5,-1),
    ('reconnect_success_percent',90,99,-1),('reconnect_success_percent',99,99,1),
    ('reconnect_p95_seconds',8,2,-1),('reconnect_p95_seconds',2,2,1)])
def test_threshold_directions(signal,value,limit,state):
    assert w.assess(signal,value,20,10,limit) == state
    assert w.assess(signal,value,0,10,limit) == 2
    assert w.assess(signal,value,20,10,None) == 3


def test_unconfigured_invalid_threshold_and_nan(monkeypatch):
    monkeypatch.delenv('WS_RECONNECT_P95_MAX_SECONDS',raising=False)
    assert w.threshold('reconnect_p95_seconds') is None
    monkeypatch.setenv('WS_RECONNECT_P95_MAX_SECONDS','nan')
    with pytest.raises(ValueError):w.threshold('reconnect_p95_seconds')
    assert w.assess('disconnect_percent',float('nan'),20,10,5)==2


def test_all_signals_isolate_revision_and_no_fake_zero_fallback():
    for signal in w.SIGNALS:
        expr,samples=w.expressions(signal,'abc123')
        assert 'csms-abc123-.*' in expr and '[5m]' in expr
        assert 'or vector(0)' not in expr
        assert 'namespace=' in samples and 'exported_namespace=' in samples
    assert 'server_shutdown' not in w.expressions('disconnect_percent','abc')[0]
    assert 'recovery_seconds_bucket' in w.expressions('reconnect_p95_seconds','abc')[0]


def test_missing_and_low_samples_remain_unverified(monkeypatch):
    def query(expr):
        if 'discarded' in expr:return 0
        raise w.MissingData('absent')
    monkeypatch.setattr(w,'query',query)
    observer=w.QualityObserver()
    assert w.inspect(observer,{'canary':'abc'}) == [2,2,2]
    assert b'quality_value' not in observer.render()
    assert b'quality_status' in observer.render()
