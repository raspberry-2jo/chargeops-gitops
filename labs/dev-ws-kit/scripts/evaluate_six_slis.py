#!/usr/bin/env python3
"""Evaluate six candidate ChargeOps dev SLI thresholds from *real* OCPP lab event CSVs.

No Kubernetes changes, no fake measurements. This tool only evaluates observed
client-side OCPP Heartbeat traffic and its WebSocket sessions. It does not identify
stable vs canary version: that requires server-side, version-labelled metrics.
"""
import argparse
import csv
import json
import math
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from pathlib import Path


def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def percentile(values, fraction):
    if not values:
        return None
    sorted_values = sorted(values)
    i = (len(sorted_values) - 1) * fraction
    lo, hi = math.floor(i), math.ceil(i)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (i - lo)


def load(summary_path, event_path=None):
    p = Path(summary_path)
    summary = json.loads(p.read_text(encoding='utf-8'))
    events_path = Path(event_path) if event_path else p.with_name(p.name.replace('_summary.json', '_events.csv'))
    if events_path == p:
        raise ValueError('Summary filename must end in _summary.json or provide --events explicitly')
    if not events_path.is_file():
        raise FileNotFoundError(f'Missing events CSV: {events_path}')
    rows = []
    with events_path.open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            r['time'] = dt(r['utc'])
            rows.append(r)
    rows.sort(key=lambda r: r['time'])
    return summary, rows, str(events_path)


def same_load_profile(a, b):
    keys = ('connections', 'ramp_rate', 'duration_sec', 'heartbeat_interval', 'response_timeout', 'seed')
    return all(a.get('config', {}).get(k) == b.get('config', {}).get(k) for k in keys)


def compute(summary, rows, opts):
    cfg = summary['config']
    start = dt(summary['started_at_utc'])
    duration = float(cfg['duration_sec'])
    ramp = float(cfg['connections']) / float(cfg['ramp_rate'])
    # Exclude warm-up ramp and the tail where in-flight replies are not yet complete.
    steady_from = start + timedelta(seconds=ramp + opts.warmup_seconds)
    steady_until = start + timedelta(seconds=duration - max(2, float(cfg['response_timeout'])))
    offset = (steady_from - start).total_seconds()
    usable_sec = (steady_until - steady_from).total_seconds()
    # Keep events after the measured window, because an in-window CALL may be answered later.
    sent = {(r['charger_id'], r['value']): r for r in rows
            if r['event'] == 'heartbeat_sent' and steady_from <= r['time'] < steady_until}
    ok = set()
    ok_times = {}
    for r in rows:
        if r['event'] != 'heartbeat_ok_ms':
            continue
        key = (r['charger_id'], r['note'])  # fix sender/receiver correlation via OCPP unique ID
        if key in sent:
            try:
                latency_ms = float(r['value'])
            except ValueError:
                continue
            if latency_ms <= float(cfg['response_timeout']) * 1000:
                ok.add(key)
                ok_times[key] = r['time']
    ok &= sent.keys()
    successes = len(ok)
    attempted = len(sent)
    errors = attempted - successes
    heart_percent = 100.0 * successes / attempted if attempted else None
    error_percent = 100.0 * errors / attempted if attempted else None

    # Fraction of established connection *episodes* with unplanned termination.
    handshakes = sum(r['event'] == 'connected' for r in rows)
    unexpected = sum(r['event'] == 'unexpected_disconnection' for r in rows)
    planned = sum(r['event'] == 'planned_disconnection' for r in rows)
    abnormal_percent = 100.0 * unexpected / handshakes if handshakes else None
    recoveries = []
    for r in rows:
        if r['event'] == 'recovered_ms':
            try:
                recoveries.append(float(r['value']))
            except ValueError:
                pass
    reconnect_p95_sec = percentile(recoveries, 0.95) / 1000 if recoveries else None
    # Last-mile coverage is separate from p95, to avoid survivorship bias.
    disconnected = unexpected + planned
    recovery_percent = 100.0 * len(recoveries) / disconnected if disconnected else None

    # Convert connect/disconnect events into an active-connection time series.
    changing = []
    active = 0
    for r in rows:
        if r['event'] == 'connected':
            active += 1
            changing.append((r['time'], active))
        elif r['event'] == 'disconnected':
            active = max(0, active - 1)
            changing.append((r['time'], active))
    change_times = [r[0] for r in changing]
    success_times = sorted(ok_times[k] for k in ok)  # throughput = CALLRESULT completions observed in window

    def active_at(t):
        index = bisect_right(change_times, t) - 1
        return changing[index][1] if index >= 0 else 0

    windows = []
    if usable_sec >= opts.window_seconds:
        shift = 0.0
        while shift + opts.window_seconds <= usable_sec + 0.0001:
            left = steady_from + timedelta(seconds=shift)
            right = left + timedelta(seconds=opts.window_seconds)
            first = active_at(left)
            i = bisect_right(change_times, left)
            j = bisect_right(change_times, right)
            lowest = min([first] + [active for _, active in changing[i:j]])
            drop = 100.0 * max(0, first - lowest) / first if first else None
            success_count = bisect_left(success_times, right) - bisect_left(success_times, left)
            windows.append(dict(relative_start_sec=round(offset + shift, 3),
                                start_utc=left.isoformat(timespec='milliseconds'),
                                active_at_start=first, min_active=lowest,
                                connection_drop_pct=drop,
                                successful_calls=success_count,
                                successful_rps=success_count / opts.window_seconds))
            shift += opts.step_seconds
    enough_connection_windows = [w for w in windows if w['active_at_start'] >= opts.min_connections]
    worst_drop = max((w['connection_drop_pct'] for w in enough_connection_windows), default=None)
    return dict(summary=summary, events_count=len(rows), config=cfg, measured_from_utc=steady_from.isoformat(),
                measured_until_utc=steady_until.isoformat(), usable_seconds=usable_sec,
                attempts=attempted, successes=successes, errors=errors,
                heartbeat_success_pct=heart_percent, ocpp_error_pct=error_percent,
                handshakes=handshakes, unexpected=unexpected, planned=planned,
                abnormal_termination_pct=abnormal_percent, recoveries=len(recoveries),
                reconnect_p95_sec=reconnect_p95_sec, recovery_coverage_pct=recovery_percent,
                worst_60s_connection_drop_pct=worst_drop,
                qualified_connection_windows=len(enough_connection_windows), windows=windows)


def evaluate(test, base, opts):
    measurements = {
        '연결 감소율(최악 60초)': ('worst_60s_connection_drop_pct', '<=', opts.max_connection_drop),
        '처리량 감소율(최악 60초)': ('worst_60s_throughput_drop_pct', '<=', opts.max_throughput_drop),
        'OCPP Heartbeat 오류율': ('ocpp_error_pct', '<=', opts.max_error),
        '비정상 종료율': ('abnormal_termination_pct', '<=', opts.max_abnormal),
        'Heartbeat 성공률': ('heartbeat_success_pct', '>=', opts.min_heartbeat),
        '재연결 p95(초)': ('reconnect_p95_sec', '<=', opts.max_reconnect_p95),
    }
    worst_throughput = None
    compared = 0
    if base is not None:
        baseline_map = {w['relative_start_sec']: w for w in base['windows']}
        for w in test['windows']:
            original = baseline_map.get(w['relative_start_sec'])
            if not original or original['successful_calls'] < opts.min_per_window or w['active_at_start'] < opts.min_connections:
                continue
            change = 100.0 * (original['successful_rps'] - w['successful_rps']) / original['successful_rps']
            worst_throughput = max(max(0, change), worst_throughput or 0)
            compared += 1
    test['worst_60s_throughput_drop_pct'] = worst_throughput
    test['paired_throughput_windows'] = compared
    report_rows = []
    for name, (field, comparator, limit) in measurements.items():
        v = test.get(field)
        reason = ''
        if name.startswith('재연결') and test['recoveries'] == 0 and opts.mode == 'steady':
            status = '해당없음'
            reason = '정상 부하 시험에 재접속 사건이 없어 p95가 정의되지 않습니다. Drain 시험에서 별도 검증합니다.'
        elif name.startswith('재연결') and test['recoveries'] < opts.min_recoveries:
            status = '보류'
            reason = f"재연결 성공 표본 {test['recoveries']}회 < 최소 {opts.min_recoveries}회"
        elif field == 'abnormal_termination_pct' and test['handshakes'] < opts.min_connections:
            status = '보류'
            reason = f"세션 표본 {test['handshakes']}회 < 최소 {opts.min_connections}회"
        elif field in ('ocpp_error_pct', 'heartbeat_success_pct') and test['attempts'] < opts.min_heartbeats:
            status = '보류'
            reason = f"Heartbeat 표본 {test['attempts']}건 < 최소 {opts.min_heartbeats}건"
        elif v is None:
            status = '보류'
            reason = '데이터 없음 / 정상 관측 구간 부족 / 비교 기준선 누락'
        elif opts.mode == 'drain' and field in ('worst_60s_connection_drop_pct', 'worst_60s_throughput_drop_pct'):
            status = '참고'
            reason = '의도적인 Pod 종료로 연결·처리량 감소 가능. 일반 시기 임계값을 그대로 강제하지 않습니다.'
        elif (v <= limit if comparator == '<=' else v >= limit):
            status = '통과'
        else:
            status = '실패'
        report_rows.append(dict(metric=name, actual=round(v, 4) if v is not None else None,
                                criterion=f'{comparator} {limit}', status=status, note=reason))
    if opts.mode == 'drain':
        coverage = test['recovery_coverage_pct']
        if test['planned'] + test['unexpected'] < opts.min_recoveries:
            recovery_status = '보류'
            recovery_note = '종료 영향을 받은 세션 표본이 충분하지 않습니다.'
        elif coverage is None:
            recovery_status = '보류'
            recovery_note = '재연결 대상이 없습니다.'
        else:
            recovery_status = '통과' if coverage >= opts.min_recovery_coverage else '실패'
            recovery_note = '재연결 p95는 성공자만 반영하므로 미복구 세션 비율도 확인합니다.'
        report_rows.append(dict(metric='재연결 복구율(보조 필수)', actual=round(coverage, 4) if coverage is not None else None,
                                criterion=f'>= {opts.min_recovery_coverage}', status=recovery_status, note=recovery_note))
    important = [r['status'] for r in report_rows if r['status'] not in ('참고', '해당없음')]
    overall = '실패' if '실패' in important else ('보류' if '보류' in important else '통과')
    return overall, report_rows


def main():
    ap = argparse.ArgumentParser(description='Six candidate SLI gates: real dev OCPP events only')
    ap.add_argument('--run', required=True, help='Test _summary.json')
    ap.add_argument('--events', help='Test _events.csv; auto inferred if omitted')
    ap.add_argument('--baseline', help='Matched baseline _summary.json; needed for throughput drop')
    ap.add_argument('--baseline-events', help='Matched baseline _events.csv')
    ap.add_argument('--mode', choices=['steady', 'drain'], default='steady')
    ap.add_argument('--out', help='Output prefix; defaults to run summary name + _six_slis')
    ap.add_argument('--warmup-seconds', type=float, default=30)
    ap.add_argument('--window-seconds', type=int, default=60)
    ap.add_argument('--step-seconds', type=int, default=5)
    ap.add_argument('--min-connections', type=int, default=100)
    ap.add_argument('--min-heartbeats', type=int, default=100)
    ap.add_argument('--min-recoveries', type=int, default=20)
    ap.add_argument('--min-per-window', type=int, default=30)
    # These are proposed example gates, NOT confirmed production SLOs.
    ap.add_argument('--max-connection-drop', type=float, default=2.0)
    ap.add_argument('--max-throughput-drop', type=float, default=15.0)
    ap.add_argument('--max-error', type=float, default=1.0)
    ap.add_argument('--max-abnormal', type=float, default=1.0)
    ap.add_argument('--min-heartbeat', type=float, default=99.0)
    ap.add_argument('--max-reconnect-p95', type=float, default=10.0)
    ap.add_argument('--min-recovery-coverage', type=float, default=99.0)
    args = ap.parse_args()
    if args.step_seconds < 1 or args.window_seconds < 1 or args.warmup_seconds < 0:
        ap.error('Invalid window/step/warm-up settings')
    summary, rows, filename = load(args.run, args.events)
    test = compute(summary, rows, args)
    if args.baseline:
        base_summary, base_rows, base_file = load(args.baseline, args.baseline_events)
        if not same_load_profile(summary, base_summary):
            ap.error('Baseline/test workload settings differ; do not compare throughput')
        if Path(filename).resolve() == Path(base_file).resolve():
            ap.error('The same events cannot be used for baseline and test')
        baseline = compute(base_summary, base_rows, args)
    else:
        baseline = None
    verdict, gates = evaluate(test, baseline, args)
    prefix = Path(args.out) if args.out else Path(args.run).with_name(Path(args.run).stem + '_six_slis')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    report_path = Path(str(prefix) + '.json')
    windows_path = Path(str(prefix) + '_windows.csv')
    payload = dict(run=str(args.run), baseline=str(args.baseline) if args.baseline else None,
                   scenario=args.mode, workload_config=summary['config'], overall=verdict, hypothetical_thresholds_only=True,
                   measurements={k: v for k, v in test.items() if k not in ('windows', 'summary', 'config')},
                   gates=gates, notes=[
                       'All six metrics come from client-side OCPP Heartbeat load. Not full-action OCPP error rate.',
                       'The 1m worst connection drop is start-of-rolling-minute active minus within-minute minimum.',
                       'The throughput worst drop compares time-aligned 60s baseline and test successful calls/sec.',
                       'Error rate and Heartbeat success share denominator and are complementary here.',
                       'Abnormal termination rate = unexpected closes / established WebSocket connection episodes.',
                       'Reconnect p95 includes client retry backoff; only successfully recovered sessions are timed.',
                       'Use server-side version-labelled metrics to decide actual stable vs canary promotion.',
                       'Rolling windows overlap; do not treat them as independent statistical samples.',
                       'Never interpret demo/mock data as actual ChargeOps dev outcomes.',
                   ])
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    with windows_path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['relative_start_sec', 'start_utc', 'active_at_start',
                   'min_active', 'connection_drop_pct', 'successful_calls', 'successful_rps',
                   'baseline_successful_calls', 'throughput_drop_pct'])
        writer.writeheader()
        baseline_map = {w['relative_start_sec']: w for w in baseline['windows']} if baseline else {}
        for w in test['windows']:
            rec = w.copy()
            b = baseline_map.get(w['relative_start_sec'])
            rec['baseline_successful_calls'] = b['successful_calls'] if b else ''
            rec['throughput_drop_pct'] = (round(max(0.0, 100 * (b['successful_rps'] - w['successful_rps']) / b['successful_rps']), 4)
                                           if b and b['successful_calls'] > 0 else '')
            writer.writerow(rec)
    print('DEV six-SLI evaluation | Candidate limits are EXAMPLES, not calibrated SLOs')
    for r in gates:
        value = 'N/A' if r['actual'] is None else f"{r['actual']:.3f}"
        print(f"{r['metric']:<27} actual={value:<9} candidate={r['criterion']:<9} {r['status']} {r['note']}")
    print(f'Overall: {verdict} | full report: {report_path} | time windows: {windows_path}')
    print('For Canary, a client-side PASS does not substitute for version-specific Prometheus evidence.')


if __name__ == '__main__':
    main()
