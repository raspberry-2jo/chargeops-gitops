"""Bounded, read-only HTTP availability checks; never call charger write APIs."""
import concurrent.futures
import json
import math
import os
from pathlib import Path
import ssl
import time
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler


SAMPLES = 60
MAX_ERROR_RATE = 0.0
MAX_P95_SECONDS = 1.0
MAX_RELATIVE_LATENCY = 2.0
MIN_READY_PODS = 4


def request(url, opener=None):
    started = time.monotonic()
    try:
        transport = opener or build_opener(ProxyHandler({}))
        with transport.open(Request(url, headers={'Connection': 'close'}), timeout=2) as response:
            document = json.loads(response.read())
            ok = response.status == 200 and document.get('ok') is True
        return ok, time.monotonic() - started
    except Exception:
        return False, time.monotonic() - started


def pods():
    directory = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    context = ssl.create_default_context(cafile=str(directory / 'ca.crt'))
    transport = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
    request = Request('https://kubernetes.default.svc/api/v1/namespaces/ops/pods?labelSelector=app%3Dcsms',
                      headers={'Authorization': 'Bearer ' + (directory / 'token').read_text().strip()})
    with transport.open(request, timeout=5) as response:
        return json.loads(response.read())['items']


def check_pods(items, previous):
    active = [p for p in items if not p['metadata'].get('deletionTimestamp')]
    ready = [p for p in active if any(c['type'] == 'Ready' and c['status'] == 'True'
                                    for c in p.get('status', {}).get('conditions', []))]
    if len(ready) < MIN_READY_PODS or len(ready) != len(active):
        raise ValueError('Fewer than four Ready CSMS Pods')
    current = {p['metadata']['uid']: sum(c.get('restartCount', 0)
               for c in p.get('status', {}).get('containerStatuses', [])) for p in active}
    if any(count > previous.get(uid, 0) for uid, count in current.items()):
        raise ValueError('CSMS container restarted during analysis')
    if set(previous) - set(current):
        raise ValueError('CSMS Pod disappeared during a fixed-weight analysis')
    return current


def summary(results):
    if len(results) < SAMPLES:
        raise ValueError('Insufficient availability samples')
    successes = sorted(duration for ok, duration in results if ok)
    errors = sum(not ok for ok, _ in results)
    if not successes or errors:
        raise ValueError('Health/DB check failed')
    p95 = successes[math.ceil(0.95 * len(successes)) - 1]
    return {'samples': len(results), 'errors': errors, 'p95_seconds': round(p95, 6)}


def evaluate(results):
    metrics = {name: summary(values) for name, values in results.items()}
    return metrics


def main(observer=None):
    names = ['service', 'canary']
    if os.environ.get('CHECK_STABLE', 'true') == 'true':
        names.append('stable')
    urls = {'service': 'http://csms.ops.svc.cluster.local:8000/healthz',
            'canary': 'http://csms-canary.ops.svc.cluster.local:8000/healthz',
            'stable': 'http://csms-stable.ops.svc.cluster.local:8000/healthz'}
    results = {name: [] for name in names}
    def observe_pods(previous):
        items = pods()
        try:
            current = check_pods(items, previous)
        except ValueError:
            if observer:
                observer.pods(items, False)
            raise
        if observer:
            observer.pods(items, True)
        return current
    previous = observe_pods({})
    # One new HTTP connection per target per second. No concurrency/load flood.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as executor:
        for i in range(SAMPLES):
            tick = time.monotonic()
            pending = {name: executor.submit(request, urls[name]) for name in names}
            for name, future in pending.items():
                results[name].append(future.result())
                if not results[name][-1][0]:
                    if observer: observer.record(results)
                    raise ValueError('Health/DB check failed: ' + name)
            if observer:
                observer.record(results)
            if i % 10 == 0 or i == SAMPLES - 1:
                previous = observe_pods(previous)
            time.sleep(max(0, 1 - (time.monotonic() - tick)))
    print(json.dumps({'result': 'passed', 'http': evaluate(results)}, sort_keys=True))


def run():
    observer = server = None
    if os.environ.get('PROBE_METRICS') == 'true':
        try:
            from probe_metrics import Observer, serve
            observer = Observer(MAX_ERROR_RATE, MAX_P95_SECONDS, MAX_RELATIVE_LATENCY, MIN_READY_PODS, SAMPLES)
            server = serve(observer)
        except Exception:
            observer = None
            print('Probe metrics unavailable; existing availability checks continue', flush=True)
    code = 0
    try:
        main(observer)
    except ValueError as error:
        print('Analysis failed: ' + str(error))
        code = 1
    except Exception:
        print('Analysis failed: HTTP or Kubernetes observation was unavailable; no response bodies printed')
        code = 1
    finally:
        if observer:
            observer.finish(code == 0)
            # Retain the final judgement for four configured 5s scrape intervals.
            # Best-effort telemetry does not change the existing pass/fail result.
            time.sleep(20)
        if server:
            server.shutdown()
            server.server_close()
    return code


if __name__ == '__main__':
    raise SystemExit(run())
