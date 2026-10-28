"""Observe session quality separately from the existing gates. Never change traffic."""
import json
import math
import os
import re
import time

from service_gates import MissingData, Observer, query

SIGNALS = {
    'disconnect_percent': ('WS_DISCONNECT_MAX_PERCENT', 'max'),
    'reconnect_success_percent': ('WS_RECONNECT_SUCCESS_MIN_PERCENT', 'min'),
    'reconnect_p95_seconds': ('WS_RECONNECT_P95_MAX_SECONDS', 'max'),
}


def threshold(signal):
    raw = os.environ.get(SIGNALS[signal][0], '').strip()
    if not raw:
        return None
    value = float(raw)
    if not math.isfinite(value) or value < 0 or ('percent' in signal and value > 100):
        raise ValueError('Invalid threshold: ' + signal)
    return value


def assess(signal, value, samples, minimum, limit):
    if (not math.isfinite(value) or not math.isfinite(samples)
            or samples < minimum or samples <= 0):
        return 2  # UNVERIFIED, never fill with zero.
    if limit is None:
        return 3  # OBSERVE: threshold deliberately not configured.
    failed = value > limit if SIGNALS[signal][1] == 'max' else value < limit
    return -1 if failed else 1


def server_count(revision, reason):
    from service_gates import NAMESPACE
    selector = f'kind="service",reason=~"{reason}",pod=~"csms-{revision}-.*"'
    expressions = [f'increase(chargeops_csms_disconnect_total{{{label}="{NAMESPACE}",{selector}}}[5m])'
                   for label in ('namespace', 'exported_namespace')]
    return 'sum(max by(pod,kind,reason) (' + ' or '.join(expressions) + '))'


def recovery_count(revision, result='success|timeout'):
    from service_gates import NAMESPACE
    terms = [f'increase(chargeops_csms_recovery_outcomes_total{{{label}="{NAMESPACE}",'
             f'pod=~"csms-{revision}-.*",kind="service",scope="pod_local",result=~"{result}"}}[5m])'
             for label in ('namespace', 'exported_namespace')]
    return 'sum(max by(pod,kind,scope,result) (' + ' or '.join(terms) + '))'


def recovery_discarded(revision):
    from service_gates import NAMESPACE
    terms = [f'increase(chargeops_csms_recovery_discarded_total{{{label}="{NAMESPACE}",'
             f'pod=~"csms-{revision}-.*"}}[5m])' for label in ('namespace', 'exported_namespace')]
    return 'sum(max by(pod) (' + ' or '.join(terms) + '))'


def expressions(signal, revision):
    if not re.fullmatch('[a-z0-9]+', revision):
        raise MissingData('Revision unavailable')
    if signal == 'disconnect_percent':
        denominator = server_count(revision, 'normal|abnormal|handler_error')
        return f'100 * ({server_count(revision, "abnormal|handler_error")}) / (({denominator}) > 0)', denominator
    if signal == 'reconnect_success_percent':
        denominator = recovery_count(revision)
        return f'100 * ({recovery_count(revision, "success")}) / (({denominator}) > 0)', denominator
    from service_gates import NAMESPACE
    def series(suffix, function):
        terms = [f'{function}(chargeops_csms_recovery_seconds_{suffix}{{{label}="{NAMESPACE}",'
                 f'pod=~"csms-{revision}-.*",kind="service",scope="pod_local"}}[5m])'
                 for label in ('namespace','exported_namespace')]
        return 'max by(pod,le) (' + ' or '.join(terms) + ')'
    histogram = 'histogram_quantile(0.95,sum by(le) (' + series('bucket','rate') + '))'
    return histogram, 'sum(' + series('count','increase') + ')'


def revisions():
    # Reuse the existing read-only Service API transport; no extra RBAC or Pod mutation.
    import ssl
    from pathlib import Path
    from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler
    from service_gates import NAMESPACE
    directory = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    transport = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context(
        cafile=str(directory / 'ca.crt'))))
    result = {}
    for role in ('stable', 'canary'):
        if role == 'stable' and os.environ.get('WS_CHECK_STABLE', 'true') == 'false':
            continue
        req = Request(f'https://kubernetes.default.svc/api/v1/namespaces/{NAMESPACE}/services/csms-{role}',
                      headers={'Authorization': 'Bearer ' + (directory / 'token').read_text().strip()})
        with transport.open(req, timeout=5) as response:
            revision = json.load(response)['spec']['selector'].get('rollouts-pod-template-hash', '')
        if not re.fullmatch('[a-z0-9]+', revision):
            raise MissingData('Service revision unavailable: ' + role)
        result[role] = revision
    return result


class QualityObserver(Observer):
    def __init__(self):
        super().__init__()
        self.measurements = {}

    def render(self):
        with self.lock:
            lines = [f'chargeops_canary_ws_quality_started_timestamp_seconds {self.started}']
            for (role, signal), item in self.measurements.items():
                labels = f'role="{role}",signal="{signal}",revision="{item["revision"]}"'
                for key in ('value', 'samples', 'limit', 'status'):
                    if item.get(key) is not None and math.isfinite(item[key]):
                        lines.append(f'chargeops_canary_ws_quality_{key}{{{labels}}} {item[key]}')
            return ('\n'.join(lines) + '\n').encode()


def inspect(observer, roles):
    minimum = float(os.environ.get('WS_QUALITY_MIN_SAMPLES', '10'))
    if not math.isfinite(minimum) or minimum < 1:
        raise ValueError('WS_QUALITY_MIN_SAMPLES must be >= 1')
    statuses = []
    for role, revision in roles.items():
        for signal in SIGNALS:
            item = {'revision': revision, 'limit': threshold(signal), 'status': 2}
            try:
                expr, samples_expr = expressions(signal, revision)
                if signal != 'disconnect_percent' and query(recovery_discarded(revision)) > 0:
                    raise MissingData('Recovery observations evicted')
                item['samples'] = query(samples_expr)
                item['value'] = query(expr)
                item['status'] = assess(signal, item['value'], item['samples'], minimum, item['limit'])
            except (MissingData, OSError, TimeoutError):
                pass
            with observer.lock:
                observer.measurements[role, signal] = item
            statuses.append(item['status'])
            print(json.dumps(dict(role=role, signal=signal, **item), sort_keys=True), flush=True)
    return statuses


def run():
    from probe_metrics import serve
    observer = QualityObserver()
    server = serve(observer)
    try:
        # Match each analysis step's >=60s observation; a 5m query is not five minutes of extra sleep.
        time.sleep(60)
        states = inspect(observer, revisions())
        if not states or any(state in (-1, 2) for state in states):
            print('FAIL/UNVERIFIED: websocket quality; inspect per-signal JSON', flush=True)
            return 1
        if 3 in states:
            print('OBSERVE: websocket quality collected; thresholds not configured (not PASS)', flush=True)
            return 1
        print('PASS: websocket quality', flush=True)
        return 0
    except Exception as error:
        print('UNVERIFIED: websocket quality ' + type(error).__name__, flush=True)
        return 1
    finally:
        time.sleep(20)
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    raise SystemExit(run())
