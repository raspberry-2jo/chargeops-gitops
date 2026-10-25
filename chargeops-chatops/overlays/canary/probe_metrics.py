"""Expose the running Job's exact observation window; never judge a rollout."""
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Observer:
    def __init__(self, error_limit, latency_limit, relative_limit, min_ready, required_samples):
        self.lock = threading.Lock()
        self.http = {}
        self.ready = None
        self.pod_ok = None
        self.result = 0
        self.updated = time.time()
        self.limits = (error_limit, latency_limit, relative_limit, min_ready, required_samples)

    def record(self, results):
        snapshot = {}
        for target, values in results.items():
            if not values:
                continue
            successes = sorted(duration for ok, duration in values if ok)
            snapshot[target] = {'samples': len(values), 'errors': sum(not ok for ok, _ in values),
                                'p95': successes[math.ceil(.95 * len(successes)) - 1] if successes else None}
        with self.lock:
            self.http = snapshot
            self.updated = time.time()

    def pods(self, items, ok):
        ready = sum(not p['metadata'].get('deletionTimestamp') and any(
            c['type'] == 'Ready' and c['status'] == 'True'
            for c in p.get('status', {}).get('conditions', [])) for p in items)
        with self.lock:
            self.ready, self.pod_ok = ready, int(ok)
            self.updated = time.time()

    def finish(self, passed):
        with self.lock:
            self.result = 1 if passed else -1
            self.updated = time.time()

    def render(self):
        with self.lock:
            http = {key: value.copy() for key, value in self.http.items()}
            ready, pod_ok, result, updated = self.ready, self.pod_ok, self.result, self.updated
        lines = []
        def gauge(name, help_text, values):
            metric = 'chargeops_canary_probe_' + name
            lines.extend([f'# HELP {metric} {help_text}', f'# TYPE {metric} gauge'])
            for labels, value in values:
                if value is not None:
                    lines.append(f'{metric}{labels} {value}')
        def targets(field):
            return [(f'{{target="{target}"}}', value[field]) for target, value in http.items()]
        gauge('samples', 'HTTP observations since this analysis Job started.', targets('samples'))
        gauge('errors', 'Failed HTTP observations including timeout and invalid health body.', targets('errors'))
        gauge('error_ratio', 'Cumulative failed observations divided by all observations in this Job.',
              [(f'{{target="{target}"}}', value['errors'] / value['samples']) for target, value in http.items()])
        gauge('p95_seconds', 'Nearest-rank p95 of successful observations in this Job; raw absolute gate value.', targets('p95'))
        relative = []
        if (http.get('canary', {}).get('p95') is not None
                and http.get('stable', {}).get('p95') is not None):
            # Existing evaluate() compares summary() values rounded to six decimals.
            relative = [('', round(http['canary']['p95'], 6) /
                         max(.2, round(http['stable']['p95'], 6) * self.limits[2]))]
        gauge('relative_latency_ratio', 'Rounded canary p95 divided by max(0.2, rounded stable p95 times limit); fail above one.', relative)
        gauge('ready_pods', 'Ready CSMS Pods at the latest Kubernetes check.', [('', ready)])
        gauge('pod_check_ok', 'Latest readiness/restart/disappearance check: one valid, zero failed.', [('', pod_ok)])
        gauge('result', 'Job judgement: zero running, one passed, minus one failed; not Git recovery status.', [('', result)])
        gauge('last_update_timestamp_seconds', 'Unix time of the latest probe observation.', [('', updated)])
        for suffix, value in zip(('error_ratio_limit', 'p95_seconds_limit', 'relative_latency_multiplier',
                                  'ready_pods_minimum', 'samples_required'), self.limits):
            gauge(suffix, 'Configured existing Job gate threshold.', [('', value)])
        return ('\n'.join(lines) + '\n').encode()


def serve(observer, port=9110):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            body = observer.render() if self.path == '/metrics' else b'Not found\n'
            self.send_response(200 if self.path == '/metrics' else 404)
            self.send_header('Content-Type', 'text/plain; version=0.0.4')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
