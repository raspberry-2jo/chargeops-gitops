import asyncio
import importlib.util
import json
from pathlib import Path

from prometheus_client import CollectorRegistry
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / 'chargeops-chatops/overlays/development-canary'
spec = importlib.util.spec_from_file_location('ocpp_probe', P / 'ocpp_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize('failure', ['none', 'rejected', 'wrong_id', 'call_error', 'timeout', 'disconnect'])
def test_candidate_protocol_and_connection_failures(monkeypatch, failure):
    registry = CollectorRegistry()
    state = probe.State(registry)
    sockets, actions, waits = [], [], []
    class Socket:
        async def __aenter__(self):
            sockets.append(self)
            return self
        async def __aexit__(self, *args): pass
        async def send(self, data):
            self.message = json.loads(data)
            actions.append(self.message[2])
        async def recv(self):
            action = self.message[2]
            if action == 'Heartbeat' and failure == 'timeout': raise TimeoutError()
            if action == 'Heartbeat' and failure == 'disconnect': raise OSError()
            identity = 'wrong' if failure == 'wrong_id' else self.message[1]
            if failure == 'call_error': return json.dumps([4, identity, 'InternalError', '', {}])
            payload = {'status': 'Rejected' if failure == 'rejected' else 'Accepted'} if action == 'BootNotification' else {'currentTime': '2026-10-26T00:00:00Z'}
            return json.dumps([3, identity, payload])
    def connect(url, **kw):
        assert url == 'ws://csms-canary.dev.svc.cluster.local:9000/ocpp/CI-DEV-TEST'
        assert kw['subprotocols'] == ['ocpp1.6']
        return Socket()
    async def sleep(seconds): waits.append(seconds)
    if failure == 'none':
        asyncio.run(probe.verify('CI-DEV-TEST', connect, state, sleep))
        assert len(sockets) == 2
        assert actions.count('Heartbeat') == 8
        assert actions.count('BootNotification') == 2
        assert sum(waits) == 60
        assert registry.get_sample_value('chargeops_canary_ocpp_requests_total', {'action': 'Heartbeat', 'result': 'success'}) == 8
    else:
        with pytest.raises((ValueError, OSError, TimeoutError)):
            asyncio.run(probe.verify('CI-DEV-TEST', connect, state, sleep))
    assert registry.get_sample_value('chargeops_canary_ocpp_connection_up') == 0


def test_unreserved_id_rejected_before_connect():
    with pytest.raises(ValueError, match='reserved'):
        asyncio.run(probe.verify('real-charger', lambda *a, **k: pytest.fail('must not connect'), probe.State(CollectorRegistry())))


def test_candidate_image_binding_and_disabled_default():
    import subprocess
    docs = [d for d in yaml.safe_load_all(subprocess.check_output(['kubectl', 'kustomize', str(P)], text=True)) if d]
    deployment = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'csms')
    analysis = next(d for d in docs if d['kind'] == 'AnalysisTemplate')
    job = next(m for m in analysis['spec']['metrics'] if m['name'] == 'ocpp-websocket')['provider']['job']['spec']
    container = job['template']['spec']['containers'][0]
    image = next(c['image'] for c in deployment['spec']['template']['spec']['containers'] if c['name'] == 'csms')
    assert container['image'] == image
    assert job['template']['spec']['automountServiceAccountToken'] is False
    settings = next(d for d in docs if d['kind'] == 'ConfigMap' and d['metadata']['name'] == 'csms-dev-ocpp-settings')
    assert settings['data'] == {'OCPP_PROBE_ENABLED': 'false', 'OCPP_TEST_CHARGER_ID': ''}


def test_real_local_websocket_roundtrip_and_reconnect(monkeypatch):
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    async def exercise():
        calls = []
        async def handle(socket):
            async for message in socket:
                message = json.loads(message)
                calls.append(message[2])
                payload = {'status': 'Accepted', 'currentTime': '2026-10-26T00:00:00Z', 'interval': 10}
                await socket.send(json.dumps([3, message[1], payload]))
        async def no_wait(seconds): pass
        async with serve(handle, '127.0.0.1', 0, subprotocols=['ocpp1.6']) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(probe, 'URL', f'ws://127.0.0.1:{port}/ocpp/')
            await probe.verify('CI-DEV-LOCAL', connect, probe.State(CollectorRegistry()), no_wait)
        assert calls.count('BootNotification') == 2
        assert calls.count('Heartbeat') == 8
    asyncio.run(exercise())


def test_fixture_configuration_requires_reserved_prefix_and_only_edits_dev_file(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('configure_ocpp', ROOT / 'scripts/configure-dev-ocpp.py')
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    path = tmp_path / 'settings.yaml'
    path.write_text((P / 'ocpp-settings.yaml').read_text())
    monkeypatch.setattr(config, 'SETTINGS', path)
    before = path.read_text()
    with pytest.raises(ValueError): config.configure('real-charger')
    assert path.read_text() == before
    config.configure('CI-DEV-RESERVED')
    assert yaml.safe_load(path.read_text())['data'] == {'OCPP_PROBE_ENABLED': 'true', 'OCPP_TEST_CHARGER_ID': 'CI-DEV-RESERVED'}
    config.configure(disable=True)
    assert yaml.safe_load(path.read_text())['data'] == {'OCPP_PROBE_ENABLED': 'false', 'OCPP_TEST_CHARGER_ID': ''}


@pytest.mark.parametrize('mode,code,verdict', [('disabled', 0, 2), ('passed', 0, 1), ('failed', 1, -1)])
def test_job_exit_and_disabled_verdict_are_distinct(monkeypatch, mode, code, verdict):
    import prometheus_client
    registry = CollectorRegistry()
    state = probe.State(registry)
    monkeypatch.setattr(probe, 'State', lambda unused: state)
    class NoServer:
        def shutdown(self): pass
        def server_close(self): pass
        def join(self, **kwargs): pass
    monkeypatch.setattr(prometheus_client, 'start_http_server', lambda *a, **k: (NoServer(), NoServer()))
    monkeypatch.setattr(probe.time, 'sleep', lambda *a: None)
    monkeypatch.setenv('OCPP_PROBE_ENABLED', 'false' if mode == 'disabled' else 'true')
    monkeypatch.setenv('OCPP_TEST_CHARGER_ID', 'CI-DEV-UNIT')
    async def check(*args):
        assert mode != 'disabled'
        if mode == 'failed': raise TimeoutError()
    monkeypatch.setattr(probe, 'verify', check)
    assert probe.main() == code
    assert registry.get_sample_value('chargeops_canary_ocpp_result') == verdict
