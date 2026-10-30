#!/usr/bin/env python3
"""LOCAL TRAINING ONLY. Not ChargeOps CSMS, not for portfolio result measurements."""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from websockets.asyncio.server import serve

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='127.0.0.1')
parser.add_argument('--port', type=int, default=18766)
parser.add_argument('--response-delay-ms', type=float, default=0)
parser.add_argument('--disconnect-after', type=float, default=0, help='seconds; 0 disables planned mock disconnect')
a = parser.parse_args()

async def handler(ws):
    async def disconnect():
        if a.disconnect_after > 0:
            await asyncio.sleep(a.disconnect_after)
            await ws.close(code=1012, reason='mock restart')
    worker = asyncio.create_task(disconnect())
    try:
        async for raw in ws:
            try:
                message = json.loads(raw)
                if len(message) > 3 and message[0] == 2 and message[2] == 'Heartbeat':
                    if a.response_delay_ms:
                        await asyncio.sleep(a.response_delay_ms / 1000)
                    await ws.send(json.dumps([3, message[1], {
                        'currentTime': datetime.now(timezone.utc).isoformat()}]))
                elif len(message) > 3 and message[0] == 2:
                    await ws.send(json.dumps([4, message[1], 'NotImplemented', 'mock only', {}]))
            except (ValueError, TypeError, IndexError):
                pass
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

async def main():
    async with serve(handler, a.host, a.port, subprotocols=['ocpp1.6']):
        print(f'Local mock only: ws://{a.host}:{a.port}/{{id}}', flush=True)
        await asyncio.Future()

asyncio.run(main())
