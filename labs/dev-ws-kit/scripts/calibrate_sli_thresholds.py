#!/usr/bin/env python3
"""Propose (not certify) six dev SLI thresholds from >=3 normal and drain repeats.

Read JSON reports produced by evaluate_six_slis.py. No sample or demo data is
included in this tool. Healthy production SLOs require service requirements,
statistical sufficiency and independent operational review.
"""
import argparse
import glob
import json
import math
from pathlib import Path


def expand(patterns):
    found = []
    for item in patterns:
        found.extend(glob.glob(item) or [item])
    return [Path(s) for s in sorted(set(found))]


def read_group(patterns, mode):
    files = expand(patterns)
    reports = [json.loads(p.read_text(encoding='utf-8')) for p in files]
    if len(reports) < 3:
        raise SystemExit(f'{mode}: at least 3 measured repeat JSONs are required; got {len(reports)}')
    if any(r.get('scenario') != mode for r in reports):
        raise SystemExit(f'{mode}: mismatched scenarios: {files}')
    config_fields = ('connections', 'ramp_rate', 'duration_sec', 'heartbeat_interval', 'response_timeout')
    profiles = {tuple(r.get('workload_config', {}).get(k) for k in config_fields) for r in reports}
    if len(profiles) != 1:
        raise SystemExit(f'{mode}: unequal connection/ramp/duration/heartbeat/timeout; compare same loads')
    return reports, [str(x) for x in files]


def ceil_step(value, step):
    return round(math.ceil((value - 1e-10) / step) * step, 4)


def floor_step(value, step):
    return round(math.floor((value + 1e-10) / step) * step, 4)


def observe(reports, metric, min_samples=3):
    observed = []
    for r in reports:
        row = next((item for item in r['gates'] if item['metric'] == metric), None)
        if row and row['actual'] is not None and row['status'] not in ('보류', '해당없음'):
            observed.append(float(row['actual']))
    return observed if len(observed) >= min_samples else None


def main():
    ap = argparse.ArgumentParser(description='Calibrate provisional gates from healthy repeated dev experiments')
    ap.add_argument('--steady', nargs='+', required=True, help='>=3 normal repeat *_six_slis.json files or glob')
    ap.add_argument('--drain', nargs='+', help='>=3 drain repeat *_six_slis.json files or glob')
    ap.add_argument('--factor', type=float, default=2.0, help='Safety margin for upper-bound metrics, default 2.0')
    ap.add_argument('--heartbeat-margin', type=float, default=0.7, help='Subtract this many percentage points from minimum healthy result')
    ap.add_argument('--out', default='results/sli_threshold_candidates.json')
    args = ap.parse_args()
    if args.factor < 1 or args.heartbeat_margin < 0:
        ap.error('factor must be >=1 and heartbeat-margin >=0')
    steady, sources = read_group(args.steady, 'steady')
    if args.drain:
        drain, drain_sources = read_group(args.drain, 'drain')
    else:
        drain, drain_sources = [], []
    policy = [
        ('연결 감소율(최악 60초)', steady, 'upper', 0.5, 0.0),
        ('처리량 감소율(최악 60초)', steady, 'upper', 5.0, 0.0),
        ('OCPP Heartbeat 오류율', steady, 'upper', 0.5, 0.0),
        ('비정상 종료율', steady, 'upper', 0.5, 1.0),
        ('Heartbeat 성공률', steady, 'lower', 0.5, 0.0),
        ('재연결 p95(초)', drain, 'upper', 5.0, 0.0),
    ]
    candidates = []
    for metric, group, direction, step, floor in policy:
        vals = observe(group, metric)
        if vals is None:
            candidates.append(dict(metric=metric, status='보류', observed=None, candidate=None,
                                   reason='동일 부하의 유효 실측 자료 최소 3회가 필요합니다.'))
            continue
        worst = min(vals) if direction == 'lower' else max(vals)
        if direction == 'lower':
            suggested = floor_step(max(0, worst - args.heartbeat_margin), step)
        else:
            suggested = ceil_step(max(floor, worst * args.factor), step)
        candidates.append(dict(metric=metric, status='후보', observed=[round(v, 4) for v in vals],
                               worst_healthy=round(worst, 4), candidate=suggested,
                               rule=('healthy minimum - percentage-point margin, round down' if direction == 'lower'
                                     else f'healthy maximum × {args.factor}, round up to step {step}'),
                               unit='sec' if 'p95' in metric else 'percent'))
    out = dict(steady_sources=sources, drain_sources=drain_sources, factor=args.factor,
               heartbeat_margin_percentage_points=args.heartbeat_margin, recommendations=candidates,
               warning='Candidate engineering guardrails only: validate with controlled fault injections, alert noise, load patterns, and business SLO. Never treat as proven thresholds.')
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    for item in candidates:
        print(f"{item['metric']:<28} {item['status']:<4} observed={item['observed']} recommended={item['candidate']}")
    print('Threshold proposals:', dest)


if __name__ == '__main__':
    main()
