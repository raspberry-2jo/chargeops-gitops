"""Dev-only candidate OCPP check using an explicitly reserved charger.

Uses the Dev CSMS simulation allowlist by default; no charger DB writes in that mode.
"""
import asyncio
import json
import os
import re
import time
import uuid

URL = 'ws://csms-canary.dev.svc.cluster.local:9000/ocpp/'
TIMEOUT = 5


async def verify(charger, connect, state, sleep=asyncio.sleep):
    if not re.fullmatch(r'CI-DEV-[A-Za-z0-9_-]+', charger):
        raise ValueError('A reserved CI-DEV fixture is required')
    async def session(heartbeats):
        async with connect(URL + charger, subprotocols=['ocpp1.6'], open_timeout=TIMEOUT,
                           additional_headers={'X-Client-CN': charger}, close_timeout=3) as socket:
            state.connection.set(1)
            try:
                actions = [('BootNotification', {'chargePointVendor': 'ChargeOps-CI',
                                                 'chargePointModel': 'CanaryVerification'})]
                actions += [('Heartbeat', {})] * heartbeats
                for i, (action, payload) in enumerate(actions):
                    if i > 1:
                        await sleep(10)
                    request_id = uuid.uuid4().hex
                    started, result = time.monotonic(), 'success'
                    try:
                        await asyncio.wait_for(socket.send(json.dumps([2, request_id, action, payload])), TIMEOUT)
                        reply = json.loads(await asyncio.wait_for(socket.recv(), TIMEOUT))
                        if (not isinstance(reply, list) or len(reply) != 3 or reply[:2] != [3, request_id]
                                or not isinstance(reply[2], dict)):
                            raise ValueError('Response does not match request')
                        if action == 'BootNotification' and reply[2].get('status') != 'Accepted':
                            raise ValueError('Boot not accepted')
                        if action == 'Heartbeat' and not reply[2].get('currentTime'):
                            raise ValueError('Heartbeat currentTime missing')
                        state.last_success.set(time.time())
                    except (asyncio.TimeoutError, TimeoutError):
                        result = 'timeout'
                        raise
                    except Exception:
                        result = 'error'
                        raise
                    finally:
                        state.requests.labels(action, result).inc()
                        state.latency.labels(action, result).observe(time.monotonic() - started)
            finally:
                state.connection.set(0)
    await session(7)  # Maintain one connection for approximately 60 seconds.
    await session(1)  # Check fresh connection and response after disconnect.


class State:
    def __init__(self, registry):
        from prometheus_client import Counter, Gauge, Histogram
        self.result = Gauge('chargeops_canary_ocpp_result',
            '-1 failed, 0 running, 1 passed, 2 disabled (not verified)', registry=registry)
        self.connection = Gauge('chargeops_canary_ocpp_connection_up', 'Reserved test connection open', registry=registry)
        self.last_success = Gauge('chargeops_canary_ocpp_last_success_timestamp_seconds', 'Last valid test reply', registry=registry)
        self.requests = Counter('chargeops_canary_ocpp_requests_total', 'Reserved OCPP test responses',
            ['action', 'result'], registry=registry)
        self.latency = Histogram('chargeops_canary_ocpp_roundtrip_seconds', 'Reserved OCPP request to reply time',
            ['action', 'result'], buckets=(.01, .05, .1, .25, .5, 1, 2, 5), registry=registry)


def main():
    from prometheus_client import CollectorRegistry, start_http_server
    registry = CollectorRegistry()
    state = State(registry)
    server, thread = start_http_server(9110, registry=registry)
    code = 0
    try:
        enabled = os.environ.get('OCPP_PROBE_ENABLED', 'false')
        if enabled not in ('true', 'false'):
            raise ValueError('OCPP_PROBE_ENABLED must be true or false')
        if enabled == 'false':
            state.result.set(2)
            print('SKIP: Dev fixture not enabled; WebSocket availability is NOT verified.', flush=True)
            return 0
        from websockets.asyncio.client import connect
        asyncio.run(verify(os.environ.get('OCPP_TEST_CHARGER_ID', ''), connect, state))
        state.result.set(1)
        print('PASS: candidate OCPP boot, Heartbeat, 60-second connection and reconnect.', flush=True)
    except Exception as error:
        state.result.set(-1)
        print('FAIL: reserved Dev OCPP candidate check (' + type(error).__name__ + '); no payloads logged.', flush=True)
        code = 1
    finally:
        time.sleep(20)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
