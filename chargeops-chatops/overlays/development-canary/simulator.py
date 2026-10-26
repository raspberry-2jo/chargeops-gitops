"""Persistent Dev-only OCPP traffic. CSMS owns the metrics; this is a real client."""
import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

URL = 'ws://csms.dev.svc.cluster.local:9000/ocpp/'
log = logging.getLogger('dev-vcharger')


def timestamp():
    return datetime.now(timezone.utc).isoformat()


async def request(socket, action, payload):
    identity = uuid.uuid4().hex
    await asyncio.wait_for(socket.send(json.dumps([2, identity, action, payload])), 10)
    response = json.loads(await asyncio.wait_for(socket.recv(), 10))
    if not isinstance(response, list) or len(response) != 3 or response[:2] != [3, identity] or not isinstance(response[2], dict):
        raise ValueError('OCPP response mismatch or CALLERROR')
    return response[2]


async def session(charger, connect, sleep=asyncio.sleep):
    async with connect(URL + charger, subprotocols=['ocpp1.6'], open_timeout=10,
                       close_timeout=3, ping_interval=20, proxy=None) as socket:
        boot = await request(socket, 'BootNotification', {
            'chargePointVendor': 'ChargeOps-Dev', 'chargePointModel': 'InMemorySimulator'})
        if boot.get('status') != 'Accepted':
            raise ValueError('BootNotification rejected')
        await request(socket, 'StatusNotification', {
            'connectorId': 1, 'status': 'Available', 'errorCode': 'NoError', 'timestamp': timestamp()})
        log.info('%s connected; BootNotification accepted', charger)
        interval = max(1, min(int(boot.get('interval', 30)), 60))
        while True:
            heartbeat = await request(socket, 'Heartbeat', {})
            if not heartbeat.get('currentTime'):
                raise ValueError('Heartbeat response missing currentTime')
            await request(socket, 'MeterValues', {'connectorId': 1, 'meterValue': [{
                'timestamp': timestamp(), 'sampledValue': [
                    {'value': '0', 'measurand': 'Current.Import'},
                    {'value': '220', 'measurand': 'Voltage'},
                    {'value': '35', 'measurand': 'Temperature'}]}]})
            log.info('%s Heartbeat and MeterValues accepted', charger)
            await sleep(interval)


async def run_charger(charger, connect):
    delay = 1
    while True:
        try:
            await session(charger, connect)
        except Exception as error:
            log.warning('%s disconnected (%s); retry in %ss', charger, type(error).__name__, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


async def main():
    if os.environ.get('POD_NAMESPACE') != 'dev':
        raise ValueError('Simulator may run only in namespace dev')
    ids = os.environ.get('SIMULATOR_IDS', '').split(',')
    if not 1 <= len(ids) <= 3 or any(not re.fullmatch(r'CI-DEV-SIM-[0-9]{2}', cid) for cid in ids):
        raise ValueError('Provide 1-3 dedicated CI-DEV-SIM IDs')
    from websockets.asyncio.client import connect
    await asyncio.gather(*(run_charger(charger, connect) for charger in ids))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())
