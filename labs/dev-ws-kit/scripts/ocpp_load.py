#!/usr/bin/env python3
"""ChargeOps dev-only OCPP 1.6 Heartbeat load experiment.

Real OCPP 1.6 CALL/CALLRESULT over WebSocket protocol ocpp1.6.
No artificial CPU burning, no non-Heartbeat charger behavior.
"""
import argparse
import asyncio
import csv
import json
import math
import random
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from websockets.asyncio.client import connect


def iso():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def pct(vals, p):
    if not vals:
        return None
    x = sorted(vals)
    position = (len(x) - 1) * p
    lo, hi = math.floor(position), math.ceil(position)
    return round(x[lo] + (x[hi] - x[lo]) * (position - lo), 2)


def args_parser():
    ap = argparse.ArgumentParser(description='Dev-only OCPP 1.6 WebSocket Heartbeat experiment')
    ap.add_argument('--url-template', required=True, help='ws://host:port/<real-path>/{id} (must contain {id})')
    ap.add_argument('--connections', type=int, default=20)
    ap.add_argument('--ramp-rate', type=float, default=5, help='new virtual chargers / second')
    ap.add_argument('--duration', type=int, default=180, help='whole run seconds including ramp')
    ap.add_argument('--heartbeat-interval', type=float, default=20)
    ap.add_argument('--response-timeout', type=float, default=10)
    ap.add_argument('--prefix', default='LAB-CHARGEOPS-')
    ap.add_argument('--tag', default='baseline-A1')
    ap.add_argument('--out', default='results')
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--no-reconnect', action='store_true')
    ap.add_argument('--allow-large', action='store_true', help='allow more than 2000 virtual chargers')
    a = ap.parse_args()
    if '{id}' not in a.url_template or not a.url_template.startswith(('ws://', 'wss://')):
        ap.error('--url-template must be ws:// or wss:// and contain literal {id}')
    if not (a.prefix.startswith('LAB-') or a.prefix == 'CI-DEV-WSLAB-'):
        ap.error('Safety: simulated charger --prefix must start with LAB-')
    if a.prefix == 'CI-DEV-WSLAB-' and a.connections > 800:
        ap.error('The Dev lab accepts at most 800 memory-only IDs')
    if a.connections < 1 or a.ramp_rate <= 0 or a.duration < 10 or a.heartbeat_interval <= 0:
        ap.error('connections/rate/interval must be positive and duration >=10')
    if a.connections > 2000 and not a.allow_large:
        ap.error('Above 2000 connections: add --allow-large only after validating Node/DB headroom')
    if a.connections / a.ramp_rate >= a.duration:
        ap.error('Ramp would take the whole test; increase --duration or --ramp-rate')
    return a


async def run(a):
    random.seed(a.seed)
    folder = Path(a.out)
    folder.mkdir(parents=True, exist_ok=True)
    csv_file = folder / f'{a.tag}_events.csv'
    json_file = folder / f'{a.tag}_summary.json'
    started_at_utc = iso()
    started = time.monotonic()
    deadline = started + a.duration
    stats = dict(attempts=0, handshakes=0, connect_failures=0, reconnects=0,
                 planned_disconnects=0, abnormal_disconnects=0, ocpp_calls_sent=0, ocpp_callresults=0,
                 ocpp_callerrors=0, unanswered=0, response_over_timeout=0, on_time_callresults=0,
                 server_calls=0, max_active=0)
    latencies = []
    recoveries = []
    active = 0

    with csv_file.open('w', encoding='utf-8', newline='') as fp:
        log = csv.writer(fp)
        log.writerow(['utc', 'charger_id', 'event', 'value', 'note'])

        def event(cid, name, value='', note=''):
            log.writerow([iso(), cid, name, value, str(note)[:200]])

        async def client(index):
            nonlocal active
            cid = f'{a.prefix}{index:05d}'
            await asyncio.sleep(index / a.ramp_rate)
            if time.monotonic() >= deadline:
                return
            pending_recovery = None
            first_connection = True
            reconnect_backoff = 0
            while time.monotonic() < deadline:
                stats['attempts'] += 1
                try:
                    url = a.url_template.format(id=cid)
                    async with connect(url, subprotocols=['ocpp1.6'], open_timeout=10,
                                       close_timeout=3, ping_interval=20, ping_timeout=20) as ws:
                        if ws.subprotocol != 'ocpp1.6':
                            event(cid, 'bad_subprotocol', ws.subprotocol)
                            raise RuntimeError('OCPP subprotocol was not negotiated')
                        stats['handshakes'] += 1
                        active += 1
                        stats['max_active'] = max(stats['max_active'], active)
                        event(cid, 'connected', active)
                        if pending_recovery is not None:
                            recovery_ms = (time.monotonic() - pending_recovery) * 1000
                            recoveries.append(recovery_ms)
                            stats['reconnects'] += 1
                            event(cid, 'recovered_ms', round(recovery_ms, 2))
                            pending_recovery = None
                        first_connection = False
                        reconnect_backoff = 0
                        pending = {}

                        async def sender():
                            # Send a true OCPP 1.6 CALL [2, id, "Heartbeat", {}]
                            while time.monotonic() < deadline:
                                uid = uuid.uuid4().hex
                                pending[uid] = time.monotonic()
                                await ws.send(json.dumps([2, uid, 'Heartbeat', {}]))
                                stats['ocpp_calls_sent'] += 1
                                event(cid, 'heartbeat_sent', uid)
                                wait = min(a.heartbeat_interval, max(0, deadline - time.monotonic()))
                                await asyncio.sleep(wait)

                        async def receiver():
                            async for raw in ws:
                                try:
                                    msg = json.loads(raw)
                                    if not isinstance(msg, list) or len(msg) < 2:
                                        event(cid, 'unparsed_message')
                                        continue
                                    if msg[0] in (3, 4) and msg[1] in pending:
                                        latency_ms = (time.monotonic() - pending.pop(msg[1])) * 1000
                                        if msg[0] == 3:
                                            stats['ocpp_callresults'] += 1
                                            latencies.append(latency_ms)
                                            event(cid, 'heartbeat_ok_ms', round(latency_ms, 2), msg[1])
                                            if latency_ms > a.response_timeout * 1000:
                                                stats['response_over_timeout'] += 1
                                            else:
                                                stats['on_time_callresults'] += 1
                                        else:
                                            stats['ocpp_callerrors'] += 1
                                            event(cid, 'ocpp_callerror', msg[2] if len(msg) > 2 else '', msg[1])
                                    elif msg[0] == 2 and len(msg) >= 4:
                                        stats['server_calls'] += 1
                                        # Only Heartbeat is implemented in this lab client.
                                        await ws.send(json.dumps([4, msg[1], 'NotImplemented',
                                                                  'Lab simulator supports Heartbeat only', {}]))
                                    else:
                                        event(cid, 'other_message', str(msg[0]))
                                except (ValueError, TypeError, IndexError) as exc:
                                    event(cid, 'parse_error', '', type(exc).__name__)

                        writer = asyncio.create_task(sender())
                        reader = asyncio.create_task(receiver())
                        done, pend = await asyncio.wait([writer, reader], return_when=asyncio.FIRST_COMPLETED)
                        writer_failed = (writer in done and not writer.cancelled()
                                         and writer.exception() is not None)
                        unexpected = time.monotonic() < deadline and (reader in done or writer_failed)
                        for task in pend:
                            task.cancel()
                        await asyncio.gather(writer, reader, return_exceptions=True)
                        stats['unanswered'] += len(pending)
                        if pending:
                            event(cid, 'unanswered', len(pending))
                        if unexpected:
                            # A protocol-level planned close is still a reconnect event,
                            # but it must not be recorded as an unexpected/abnormal close.
                            # Verify application drain logs too: close codes are not
                            # proof that the application completed its drain correctly.
                            close_code = ws.close_code
                            if close_code in (1000, 1001, 1012):
                                stats['planned_disconnects'] += 1
                                event(cid, 'planned_disconnection', close_code)
                            else:
                                stats['abnormal_disconnects'] += 1
                                event(cid, 'unexpected_disconnection', close_code)
                            pending_recovery = time.monotonic()
                        active -= 1
                        event(cid, 'disconnected', active)
                        if not unexpected:
                            break
                except (Exception,) as exc:
                    if time.monotonic() >= deadline:
                        break
                    if first_connection or pending_recovery is None:
                        stats['connect_failures'] += 1
                    event(cid, 'connect_or_session_error', '', f'{type(exc).__name__}: {exc}')
                    if pending_recovery is None and not first_connection:
                        pending_recovery = time.monotonic()
                    if a.no_reconnect:
                        break
                if a.no_reconnect:
                    break
                reconnect_backoff = min(reconnect_backoff + 1, 5)
                await asyncio.sleep(min(2 ** reconnect_backoff + random.random(),
                                        max(0, deadline - time.monotonic())))

        tasks = [asyncio.create_task(client(i)) for i in range(a.connections)]
        await asyncio.gather(*tasks)
        fp.flush()

    success = stats['on_time_callresults']
    sent = stats['ocpp_calls_sent']
    # This is a full-run rate including ramps, NOT post-warmup steady state.
    report = dict(
        tag=a.tag, started_at_utc=started_at_utc, at_utc=iso(), config=dict(url_template=a.url_template, connections=a.connections,
            ramp_rate=a.ramp_rate, duration_sec=a.duration, heartbeat_interval=a.heartbeat_interval,
            response_timeout=a.response_timeout, prefix=a.prefix, seed=a.seed),
        counters=stats,
        heartbeat_success_percent=round(100 * success / sent, 3) if sent else None,
        response_p50_ms=pct(latencies, .50), response_p95_ms=pct(latencies, .95),
        response_p99_ms=pct(latencies, .99),
        reconnection_p95_ms=pct(recoveries, .95),
        throughput_success_per_sec=round(success / a.duration, 3),
        notes=['Client-side end-to-end OCPP Heartbeat latency only',
               'Throughput includes ramp-up; compare with same ramp, duration and message rate',
               'Success = CALLRESULT received within response-timeout; late responses are failures',
               'A CALLERROR counts as a failure; compare server-side CPU and DB metrics separately',
               'Planned shutdown at test deadline is not an abnormal disconnection',
               'Codes 1000/1001/1012 counted as planned reconnect triggers; confirm close reason in CSMS logs'],
    )
    json_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f'\nEvents: {csv_file}\nSummary: {json_file}')


if __name__ == '__main__':
    asyncio.run(run(args_parser()))
