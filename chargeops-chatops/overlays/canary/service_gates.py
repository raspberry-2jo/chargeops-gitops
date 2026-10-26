"""Dev/Ops five-level service gate using a fixed pre-delivery Prometheus baseline."""
import json
import math
import os
from pathlib import Path
import re
import ssl
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler

PROM = os.environ.get('GATE_PROMETHEUS_URL', 'http://kps-kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090')
NAMESPACE = os.environ.get('POD_NAMESPACE', 'dev')
TRANSPORT = build_opener(ProxyHandler({}))


class MissingData(ValueError): pass


class Observer:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = {str(i): 0 for i in range(1, 6)}
        self.values, self.limits = {}, {}
        self.baseline = 0
        self.started = time.time()

    def render(self):
        with self.lock:
            lines = [f'chargeops_canary_gate_status{{level="{level}"}} {value}' for level, value in self.status.items()]
            for name, data in [('value', self.values.copy()), ('limit', self.limits.copy())]:
                lines += [f'chargeops_canary_gate_{name}{{signal="{key}"}} {value}' for key, value in data.items()]
            lines += [f'chargeops_canary_gate_baseline_timestamp_seconds {self.baseline}']
            lines += [f'chargeops_canary_gate_started_timestamp_seconds {self.started}']
            return ('\n'.join(lines) + '\n').encode()


def query(expr, at=None):
    params = {'query': expr}
    if at is not None: params['time'] = at
    with TRANSPORT.open(PROM + '/api/v1/query?' + urlencode(params), timeout=5) as response:
        payload = json.load(response)
    rows = payload.get('data', {}).get('result', [])
    if payload.get('status') != 'success' or len(rows) != 1:
        raise MissingData('Prometheus metric absent or ambiguous')
    value = float(rows[0]['value'][1])
    if not math.isfinite(value): raise MissingData('Non-finite metric')
    return value


def counter(name, extra='', window='5m'):
    return ('sum(max by (pod, action, result, reason, scope) ('
            f'rate({name}{{namespace="{NAMESPACE}"{extra}}}[{window}]) or '
            f'rate({name}{{exported_namespace="{NAMESPACE}"{extra}}}[{window}])))')


CONNECTIONS = f'sum(max by (pod) (chargeops_csms_connections{{namespace="{NAMESPACE}"}} or chargeops_csms_connections{{exported_namespace="{NAMESPACE}"}}))'
THROUGHPUT = counter('chargeops_ocpp_handler_results_total', ',action=~"Heartbeat|MeterValues|StatusNotification",result="success"')
ALL_RESULTS = counter('chargeops_ocpp_handler_results_total')
ERRORS = counter('chargeops_ocpp_handler_results_total', ',result=~"error|timeout"')
DISCONNECTS = counter('chargeops_csms_disconnect_total', ',kind="service",reason=~"abnormal|handler_error"')
RECONNECTS = counter('chargeops_csms_reconnect_total', ',kind="service",scope="pod_local"')
RECEIVED = counter('chargeops_ocpp_messages_total', ',action=~"Heartbeat|MeterValues|StatusNotification"')
LEGACY_CLOSED = counter('chargeops_ocpp_connections_total', ',event="closed"')
LEGACY_ACCEPTED = counter('chargeops_ocpp_connections_total', ',event="accepted"')


def pod_check(items, previous, minimum=2):
    active = [p for p in items if not p['metadata'].get('deletionTimestamp')]
    if len(active) < minimum: raise ValueError('L1: missing desired Pods')
    current = {}
    for p in active:
        conditions = p.get('status', {}).get('conditions', [])
        if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in conditions):
            raise ValueError('L1: Pod not Ready')
        statuses = p.get('status', {}).get('containerStatuses', [])
        if any(c.get('state', {}).get('waiting', {}).get('reason') == 'CrashLoopBackOff' for c in statuses):
            raise ValueError('L1: CrashLoopBackOff')
        uid = p['metadata']['uid']
        current[uid] = sum(c.get('restartCount', 0) for c in statuses)
        if current[uid] > previous.get(uid, current[uid]):
            raise ValueError('L1: container restarted')
        # All newly created analysis participants must start clean.
        if uid not in previous and current[uid] and p['metadata'].get('labels', {}).get('rollouts-pod-template-hash'):
            raise ValueError('L1: participating Pod has restart history')
    return current


def compare(baseline, current, observer, drop_limit=50):
    if baseline['connections'] <= 0 or baseline['throughput'] * 300 < 10:
        raise MissingData('L3/L4: insufficient pre-delivery traffic baseline')
    values = {'connections_drop_percent': 100 * (1 - current['connections'] / baseline['connections']),
              'throughput_drop_percent': 100 * (1 - current['throughput'] / baseline['throughput']),
              'disconnect_perminute': current['disconnect'] * 60,
              'reconnect_perminute': current['reconnect'] * 60,
              'baseline_connections': baseline['connections'], 'current_connections': current['connections'],
              'baseline_ocpp_perminute': baseline['throughput'] * 60,
              'current_ocpp_perminute': current['throughput'] * 60}
    limits = {'connections_drop_percent': 30, 'throughput_drop_percent': drop_limit,
              'disconnect_perminute': max(baseline['disconnect'] * 180, baseline['disconnect'] * 60 + 10),
              'reconnect_perminute': max(baseline['reconnect'] * 180, baseline['reconnect'] * 60 + 10),
              'error_percent': 5, 'canary_abnormal_disconnects': 0}
    with observer.lock:
        observer.values.update(values)
        observer.limits.update(limits)
    if (values['connections_drop_percent'] >= 30 or values['disconnect_perminute'] > limits['disconnect_perminute']
            or values['reconnect_perminute'] > limits['reconnect_perminute']):
        observer.status['3'] = -1
        raise ValueError('L3: connection loss or disconnect/reconnect surge')
    observer.status['3'] = 1
    if values['throughput_drop_percent'] >= drop_limit:
        observer.status['4'] = -1
        raise ValueError('L4: aggregate successful OCPP throughput dropped')
    observer.status['4'] = 1
    if current['total'] * 300 < 10:
        observer.status['5'] = 2
        raise MissingData('L5: fewer than ten completed messages')
    error = 100 * current['errors'] / current['total']
    observer.values['error_percent'] = error
    observer.status['5'] = -1 if error > 5 else 1
    if error > 5: raise ValueError('L5: OCPP errors/timeouts exceeded five percent')


def canary_hash():
    directory = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    transport = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context(cafile=str(directory / 'ca.crt'))))
    req = Request(f'https://kubernetes.default.svc/api/v1/namespaces/{NAMESPACE}/services/csms-canary',
                  headers={'Authorization': 'Bearer ' + (directory / 'token').read_text().strip()})
    with transport.open(req, timeout=5) as response:
        value = json.load(response)['spec']['selector'].get('rollouts-pod-template-hash', '')
    if not re.fullmatch('[a-z0-9]+', value): raise MissingData('Candidate Pod hash unavailable')
    return value


def candidate_traffic(observer, candidate):
    """Observe real candidate traffic; never open a charger connection.

    Called after at least 60 seconds. The one-minute window therefore cannot
    count earlier analysis-step traffic as evidence for this step.
    """
    selector = f',pod=~"csms-{candidate}-.*"'
    try:
        heartbeat = query(counter('chargeops_ocpp_handler_results_total',
            selector + ',action="Heartbeat",result="success"', '1m')) * 60
        total = query(counter('chargeops_ocpp_handler_results_total', selector, '1m')) * 60
        errors = query(counter('chargeops_ocpp_handler_results_total',
            selector + ',result=~"error|timeout"', '1m')) * 60
        observer.values['candidate_heartbeat_perminute'] = heartbeat
        if heartbeat < 1 or total < 1:
            raise MissingData('Candidate has no recent successful Heartbeat sample')
    except MissingData:
        observer.status['4'] = 2
        raise
    error_percent = 100 * errors / total
    observer.values['candidate_error_percent'] = error_percent
    observer.limits['candidate_error_percent'] = 5
    if error_percent > 5:
        observer.status['5'] = -1
        raise ValueError('L5: candidate OCPP errors/timeouts exceeded five percent')


def main(observer):
    if NAMESPACE not in ('dev','ops'):
        raise ValueError('Gate requires namespace dev or ops')
    if os.environ.get('GATE_ENABLED', 'false') != 'true':
        observer.status = {str(i): 3 for i in range(1, 6)}
        print('SKIP: five-level gate not activated by a baseline-aware Jenkins build', flush=True)
        return
    baseline_at = float(os.environ.get('GATE_BASELINE_AT', '0'))
    if not 0 <= time.time() - baseline_at <= 7200:
        raise MissingData('Missing or expired pre-delivery baseline timestamp')
    observer.baseline = baseline_at
    from probe import pods, request
    expressions = {'connections': CONNECTIONS, 'throughput': THROUGHPUT,
                   'disconnect': DISCONNECTS, 'reconnect': RECONNECTS}
    baseline = None
    previous, started = {}, time.time()
    candidate_hash = canary_hash()
    while True:
        try:
            previous = pod_check(pods(), previous, minimum=2 if NAMESPACE == 'dev' else 4)
            observer.status['1'] = 1
        except ValueError:
            observer.status['1'] = -1
            raise
        for name in ('csms', 'csms-canary'):
            if not request(f'http://{name}.{NAMESPACE}.svc.cluster.local:8000/healthz')[0]:
                observer.status['2'] = -1
                raise ValueError('L2: health/DB check failed')
        observer.status['2'] = 1
        if baseline is None:
            baseline = {}
            compatibility = 0
            for key, expr in expressions.items():
                try:
                    baseline[key] = query(f'avg_over_time(({expr})[5m:15s])' if key == 'connections' else expr, baseline_at)
                except MissingData:
                    # First metric migration: compare the same old/new received counter;
                    # all closes/accepts are conservative upper bounds for abnormal closes/reconnects.
                    fallback = {'throughput':RECEIVED,'disconnect':LEGACY_CLOSED,'reconnect':LEGACY_ACCEPTED}.get(key)
                    if fallback is None: raise
                    baseline[key] = query(fallback, baseline_at)
                    compatibility = 1
                    if key == 'throughput': expressions[key] = RECEIVED
            observer.values['baseline_compatibility_mode'] = compatibility
        # Candidate abnormal closures only: excludes normal closes, drain and reserved fixture.
        abnormal = query(counter('chargeops_csms_disconnect_total',
            f',kind="service",reason=~"abnormal|handler_error",pod=~"csms-{candidate_hash}-.*"', '1m')) * 60
        observer.values['canary_abnormal_disconnects'] = abnormal
        observer.limits['canary_abnormal_disconnects'] = 0
        if abnormal > 0:
            observer.status['3'] = -1
            raise ValueError('L3: candidate abnormal disconnect observed')
        # Do not compare windows containing pre-delivery traffic. Observe each step for >=60s.
        if time.time() >= max(baseline_at + 300, started + 60):
            current = {key: query(expr) for key, expr in expressions.items()}
            current.update(total=query(ALL_RESULTS), errors=query(ERRORS))
            compare(baseline, current, observer, float(os.environ.get('GATE_THROUGHPUT_DROP_PERCENT', '50')))
            if NAMESPACE == 'ops':
                candidate_traffic(observer, candidate_hash)
            print(f'PASS: five-level {NAMESPACE} gate', flush=True)
            return
        time.sleep(5)


def run():
    from probe_metrics import serve
    observer = Observer()
    server = serve(observer)
    code = 0
    try:
        main(observer)
    except MissingData as error:
        observer.status = {k: (2 if v == 0 else v) for k, v in observer.status.items()}
        print('UNVERIFIED: ' + str(error), flush=True)
        code = 1
    except ValueError as error:
        print('FAIL: ' + str(error), flush=True)
        code = 1
    except Exception:
        observer.status = {k: (2 if v == 0 else v) for k, v in observer.status.items()}
        print('UNVERIFIED: Kubernetes/Prometheus observation unavailable', flush=True)
        code = 1
    finally:
        time.sleep(20)
        server.shutdown()
        server.server_close()
    return code


if __name__ == '__main__':
    raise SystemExit(run())
