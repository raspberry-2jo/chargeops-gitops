import asyncio
import importlib.util
import json
import random
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dev_simulator', ROOT / 'chargeops-chatops/overlays/development-canary/simulator.py')
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)


def test_real_websocket_session_sends_all_four_actions(monkeypatch):
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    async def exercise():
        actions = []
        async def handle(socket):
            async for data in socket:
                message = json.loads(data)
                actions.append(message[2])
                await socket.send(json.dumps([3, message[1], {
                    'status': 'Accepted', 'interval': 30, 'currentTime': '2026-10-26T00:00:00Z'}]))
        async def stop(seconds): raise asyncio.CancelledError()
        async with serve(handle, '127.0.0.1', 0, subprotocols=['ocpp1.6']) as server:
            monkeypatch.setattr(sim, 'URL', f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ocpp/')
            with pytest.raises(asyncio.CancelledError):
                await sim.session('CI-DEV-SIM-01', connect, stop)
        assert actions == ['BootNotification', 'StatusNotification', 'Heartbeat', 'MeterValues']
    asyncio.run(exercise())


@pytest.mark.parametrize('namespace,ids', [('ops', 'CI-DEV-SIM-01'), ('dev', 'CH-01'), ('dev', '')])
def test_simulator_rejects_ops_and_nonreserved_ids(monkeypatch, namespace, ids):
    monkeypatch.setenv('POD_NAMESPACE', namespace)
    monkeypatch.setenv('SIMULATOR_IDS', ids)
    with pytest.raises(ValueError): asyncio.run(sim.main())


def test_model_cycles_and_integrates_energy_without_random_jumps():
    elapsed = [0]
    model = sim.ChargerModel(random.Random(42), clock=lambda: elapsed[0])
    statuses, readings = set(), []
    for _ in range(150):
        previous_current, previous_voltage, previous_energy = model.current, model.voltage, model.energy_wh
        elapsed[0] += 10
        model.sample()
        statuses.add(model.status)
        assert model.energy_wh == pytest.approx(previous_energy + previous_current * previous_voltage * 10 / 3600)
        assert 215 <= model.voltage <= 225
        assert 24 <= model.temperature <= 50
        assert (10 <= model.current <= 30) if model.status == 'Charging' else model.current == 0
        readings.append((model.voltage, model.temperature))
    assert statuses == set(model.phases)
    assert len(set(readings)) > 100


def test_meter_sampling_is_faster_than_heartbeat_and_status_changes_are_sent(monkeypatch):
    elapsed, sent = [0], []
    model = sim.ChargerModel(random.Random(1), clock=lambda: elapsed[0])
    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def send(self, data):
            self.message = json.loads(data)
            sent.append((elapsed[0], self.message[2], self.message[3]))
        async def recv(self):
            return json.dumps([3, self.message[1], {'status': 'Accepted', 'interval': 30,
                                                  'currentTime': '2026-10-26T00:00:00Z'}])
    async def sleep(seconds):
        elapsed[0] += seconds
        if elapsed[0] > 600: raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sim.session('CI-DEV-SIM-01', lambda *a, **k: Socket(), sleep,
                                model=model, clock=lambda: elapsed[0]))
    heartbeats = [t for t, action, _ in sent if action == 'Heartbeat']
    assert all(b - a == 30 for a, b in zip(heartbeats, heartbeats[1:]))
    meters = [payload for _, action, payload in sent if action == 'MeterValues']
    assert len(meters) > 2 * len(heartbeats)
    assert len({p['meterValue'][0]['sampledValue'][1]['value'] for p in meters}) > 1
    assert {p['status'] for _, action, p in sent if action == 'StatusNotification'} == set(model.phases)
