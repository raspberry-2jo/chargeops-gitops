#!/usr/bin/env python3
"""Compare 3+ A/B repeated OCPP load-test summary JSONs, no fake data."""
import argparse
import glob
import json
import statistics
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--a', nargs='+', required=True, help='baseline JSON filenames or globs')
p.add_argument('--b', nargs='+', required=True, help='treatment JSON filenames or globs')
args = p.parse_args()


def expand(patterns):
    paths = []
    for pat in patterns:
        matches = glob.glob(pat)
        paths += matches or [pat]
    return sorted(set(paths))


def read(patterns):
    paths = expand(patterns)
    records = [json.loads(Path(s).read_text(encoding='utf-8')) for s in paths]
    if len(records) < 3:
        raise SystemExit('Each condition must have at least 3 repetitions')
    profiles = {(r['config']['connections'], r['config']['duration_sec'],
                 r['config']['heartbeat_interval'], r['config']['ramp_rate']) for r in records}
    if len(profiles) != 1:
        raise SystemExit('Mismatch within a group (connections/duration/heartbeat/ramp)')
    return records, profiles.pop()

A, a_profile = read(args.a)
B, b_profile = read(args.b)
if a_profile != b_profile:
    raise SystemExit('A/B load profiles differ; comparison would be invalid')

print('Load profile (connections, seconds, heartbeat interval, ramp/s):', a_profile)
print('No statistical significance is claimed with only three runs. Use median and range.')
for label, key in [('OCPP p95 (ms)', 'response_p95_ms'),
                   ('OCPP success (%)', 'heartbeat_success_percent'),
                   ('Throughput (success/s)', 'throughput_success_per_sec'),
                   ('Reconnect p95 (ms)', 'reconnection_p95_ms')]:
    values_a = [x[key] for x in A if x[key] is not None]
    values_b = [x[key] for x in B if x[key] is not None]
    if not values_a or not values_b:
        print(f'{label}: NOT MEASURED (not enough observations)')
        continue
    ma, mb = statistics.median(values_a), statistics.median(values_b)
    delta = 100 * (mb - ma) / ma if ma else float('nan')
    print(f'{label}: A median={ma:.2f} range={min(values_a):.2f}..{max(values_a):.2f}; '
          f'B median={mb:.2f} range={min(values_b):.2f}..{max(values_b):.2f}; '
          f'change={delta:+.2f}%')
