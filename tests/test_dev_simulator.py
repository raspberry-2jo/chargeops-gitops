import asyncio
import importlib.util
import json
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
