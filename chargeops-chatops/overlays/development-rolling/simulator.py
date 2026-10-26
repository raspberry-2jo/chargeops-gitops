"""Persistent Dev-only OCPP traffic. CSMS owns the metrics; this is a real client."""
import asyncio
import json
import logging
import os
import math
import random
import re
import time
import uuid
from datetime import datetime, timezone

URL = 'ws://csms.dev.svc.cluster.local:9000/ocpp/'
log = logging.getLogger('dev-vcharger')


class ChargerModel:
    """Bounded demo charging cycle; not an electrical or billing simulator."""
    phases = ('Available', 'Preparing', 'Charging', 'Finishing')
    durations = {'Available': (45, 90), 'Preparing': (15, 30),
                 'Charging': (120, 240), 'Finishing': (15, 30)}

    def __init__(self, rng=None, clock=time.monotonic):
        self.rng = rng or random.Random()
        self.clock = clock
        self.status = self.rng.choice(('Available', 'Charging'))
        self.last = clock()
        self.next_phase = self.last + self.rng.uniform(*self.durations[self.status])
        self.voltage = self.rng.uniform(218, 222)
        self.current = self.rng.uniform(12, 24) if self.status == 'Charging' else 0
        self.temperature = self.rng.uniform(26, 31)
        self.energy_wh = 0.0

    def sample(self):
        now = self.clock()
        elapsed = max(0, now - self.last)
        # Integration over the previous sample interval; monotonic across reconnects.
        self.energy_wh += self.current * self.voltage * elapsed / 3600
        target = 27 + self.current * 0.5
        self.temperature += (target - self.temperature) * (1 - math.exp(-elapsed / 120))
        self.temperature = min(50, max(24, self.temperature + self.rng.uniform(-0.1, 0.1)))
        self.last = now
        while now >= self.next_phase:
            self.status = self.phases[(self.phases.index(self.status) + 1) % len(self.phases)]
            self.next_phase += self.rng.uniform(*self.durations[self.status])
        self.current = min(30, max(10, self.current + self.rng.uniform(-1.5, 1.5))) if self.status == 'Charging' else 0
        self.voltage = min(225, max(215, self.voltage + self.rng.uniform(-0.8, 0.8)))
        return {'Current.Import': (self.current, 'A'), 'Voltage': (self.voltage, 'V'),
                'Temperature': (self.temperature, 'Celsius'),
                'Energy.Active.Import.Register': (self.energy_wh, 'Wh')}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


async def request(socket, action, payload):
    identity = uuid.uuid4().hex
    await asyncio.wait_for(socket.send(json.dumps([2, identity, action, payload])), 10)
    response = json.loads(await asyncio.wait_for(socket.recv(), 10))
    if not isinstance(response, list) or len(response) != 3 or response[:2] != [3, identity] or not isinstance(response[2], dict):
        raise ValueError('OCPP response mismatch or CALLERROR')
    return response[2]


async def session(charger, connect, sleep=asyncio.sleep, model=None, clock=time.monotonic):
    model = model or ChargerModel(clock=clock)
    async with connect(URL + charger, subprotocols=['ocpp1.6'], open_timeout=10,
                       close_timeout=3, ping_interval=20, proxy=None) as socket:
        boot = await request(socket, 'BootNotification', {
            'chargePointVendor': 'ChargeOps-Dev', 'chargePointModel': 'InMemorySimulator'})
        if boot.get('status') != 'Accepted':
            raise ValueError('BootNotification rejected')
        log.info('%s connected; BootNotification accepted', charger)
        interval = max(1, int(boot.get('interval', 30)))
        last_status, next_heartbeat = None, clock()
        while True:
            values = model.sample()
            if model.status != last_status:
                await request(socket, 'StatusNotification', {
                    'connectorId': 1, 'status': model.status, 'errorCode': 'NoError', 'timestamp': timestamp()})
                last_status = model.status
                log.info('%s status=%s', charger, model.status)
            if clock() >= next_heartbeat:
                heartbeat = await request(socket, 'Heartbeat', {})
                if not heartbeat.get('currentTime'):
                    raise ValueError('Heartbeat response missing currentTime')
                next_heartbeat = clock() + interval
                log.info('%s Heartbeat accepted', charger)
            await request(socket, 'MeterValues', {'connectorId': 1, 'meterValue': [{
                'timestamp': timestamp(), 'sampledValue': [
                    {'value': f'{value:.2f}', 'measurand': measurand, 'unit': unit}
                    for measurand, (value, unit) in values.items()]}]})
            log.info('%s MeterValues accepted: %s %.1fA %.1fV %.1fC %.1fWh', charger,
                     model.status, model.current, model.voltage, model.temperature, model.energy_wh)
            await sleep(min(10, max(0.1, next_heartbeat - clock())))


async def run_charger(charger, connect):
    delay = 1
    model = ChargerModel()
    while True:
        try:
            await session(charger, connect, model=model)
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
