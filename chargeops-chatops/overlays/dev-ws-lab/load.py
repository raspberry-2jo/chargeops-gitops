"""Bounded real OCPP workload for dev/csms-ws-lab only. No database writes.
A controlled /control/target switch closes sessions normally, then reconnects.
"""
import asyncio
from collections import Counter
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import random
import signal
import time
import uuid
from websockets.asyncio.client import connect


def settings(env):
    if env.get('POD_NAMESPACE')!='dev' or env.get('LAB_WORKLOAD')!='csms-ws-lab':
        raise ValueError('This generator only runs in dev/csms-ws-lab')
    size=int(env.get('COHORT_SIZE','400'));offset=int(env.get('COHORT_OFFSET','0'))
    if not 1<=size<=400 or offset not in (0,400):raise ValueError('Invalid bounded cohort')
    return size,offset


async def run(env=os.environ):
    size,offset=settings(env);cohort=env.get('COHORT','canary');stop=asyncio.Event();sockets=set();stats=Counter()
    if cohort not in ('stable','canary'):raise ValueError('Unknown cohort')
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        try:loop.add_signal_handler(sig,stop.set)
        except NotImplementedError:pass
    started=time.monotonic();control=Path('/control/target');mode='general'
    general='ws://csms-ws-lab.dev.svc.cluster.local:9000/ocpp/'
    def current_mode():
        if cohort=='stable':return 'stable'
        try:value=control.read_text().strip()
        except FileNotFoundError:value='general'
        if value not in ('general','canary'):raise ValueError('Invalid target mode')
        return value
    def url(mode):
        return general if mode=='general' else f'ws://csms-ws-lab-{mode}.dev.svc.cluster.local:9000/ocpp/'
    async def request(ws,action,payload):
        identity=uuid.uuid4().hex;stats[action+'_sent']+=1
        await ws.send(json.dumps([2,identity,action,payload]));at=time.monotonic()
        try:
            message=json.loads(await asyncio.wait_for(ws.recv(),10))
            if message[0]!=3 or message[1]!=identity or not isinstance(message[2],dict):raise ValueError('CALLERROR or unmatched OCPP result')
            stats[action+'_success']+=1
            stats['last_response_us']=int((time.monotonic()-at)*1000000)
            return message[2]
        except Exception:stats[action+'_failed']+=1;raise
    async def client(index):
        cid=f'CI-DEV-WSLAB-{offset+index:05d}';await asyncio.sleep(index/50)
        failures=0
        while not stop.is_set():
            selected=current_mode()
            # Only the stable cohort falls back when old Stable endpoints disappear at100%.
            endpoint=general if cohort=='stable' and failures>=2 else url(selected)
            try:
                async with connect(endpoint+cid,subprotocols=['ocpp1.6'],open_timeout=5,
                    close_timeout=2,ping_interval=20,proxy=None) as ws:
                    if ws.subprotocol!='ocpp1.6':raise ValueError('Subprotocol mismatch')
                    failures=0;sockets.add(ws);stats['accepted']+=1
                    try:
                        boot=await request(ws,'BootNotification',{'chargePointVendor':'ChargeOps-Dev-Lab','chargePointModel':'MemoryOnly'})
                        if boot.get('status')!='Accepted':raise ValueError('Boot rejected')
                        await request(ws,'StatusNotification',{'connectorId':1,'errorCode':'NoError','status':'Available'})
                        hb=meter=0
                        while not stop.is_set():
                            if ws.state.name != 'OPEN':break
                            if selected!=current_mode():
                                await ws.close(1000,'controlled cohort redistribution');break
                            now=time.monotonic()
                            if now>=hb:
                                result=await request(ws,'Heartbeat',{})
                                if not result.get('currentTime'):raise ValueError('Heartbeat result missing time')
                                hb=now+30
                            if now>=meter:
                                await request(ws,'MeterValues',{'connectorId':1,'meterValue':[{'timestamp':datetime.now(timezone.utc).isoformat(),'sampledValue':[{'value':'220','measurand':'Voltage','unit':'V'}]}]})
                                meter=now+60
                            await asyncio.sleep(1)
                        if stop.is_set():await ws.close(1000,'lab stopped')
                    finally:sockets.discard(ws)
            except Exception:
                stats['session_errors']+=1;failures+=1
                await asyncio.sleep(1+random.random())
    async def report():
        while not stop.is_set():
            print(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'cohort':cohort,'active':len(sockets),'target':current_mode(),'stats':dict(stats)}),flush=True)
            await asyncio.sleep(5)
    tasks=[asyncio.create_task(client(i)) for i in range(size)];reporter=asyncio.create_task(report())
    try:await asyncio.wait_for(stop.wait(),3600)
    except asyncio.TimeoutError:stop.set()
    finally:
        await asyncio.gather(*(ws.close(1000,'lab stopped') for ws in list(sockets)),return_exceptions=True)
        for task in tasks+[reporter]:task.cancel()
        await asyncio.gather(*tasks,reporter,return_exceptions=True)

if __name__=='__main__':asyncio.run(run())
