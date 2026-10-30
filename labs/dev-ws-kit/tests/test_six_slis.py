"""Purely synthetic unit test. Not ChargeOps performance evidence."""
import csv
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/evaluate_six_slis.py'


class SixSliSyntheticTests(unittest.TestCase):
    def build(self, folder, tag, degraded):
        base = datetime(2026, 10, 30, 0, 0, 0, tzinfo=timezone.utc)
        config = dict(url_template='ws://example.test/{id}', connections=100,
                      ramp_rate=1000, duration_sec=240, heartbeat_interval=5,
                      response_timeout=10, prefix='LAB-TEST-', seed=42)
        summary = dict(tag=tag, started_at_utc=base.isoformat(), config=config)
        summary_path = folder / (tag + '_summary.json')
        summary_path.write_text(json.dumps(summary), encoding='utf-8')
        events = []

        def add(t, cid, event, value='', note=''):
            events.append(dict(utc=(base + timedelta(seconds=t)).isoformat(timespec='milliseconds'),
                               charger_id=cid, event=event, value=value, note=note))

        for i in range(100):
            add(1, f'LAB-TEST-{i:05d}', 'connected', i+1)
        if degraded:
            add(80, 'LAB-TEST-00000', 'unexpected_disconnection', 1006)
            add(80.001, 'LAB-TEST-00000', 'disconnected', 99)
            add(82, 'LAB-TEST-00000', 'connected', 100)
            add(82, 'LAB-TEST-00000', 'recovered_ms', 3200)
        for sec in range(32, 226):
            for j in range(10):
                cid = f'LAB-TEST-{j:05d}'
                uid = f'{sec}-{j}'
                add(sec + j * .03, cid, 'heartbeat_sent', uid)
                if (not degraded) or (not (120 <= sec < 205 and j % 4 == 0)):
                    add(sec + j * .03 + .012, cid, 'heartbeat_ok_ms', 12, uid)
        events.sort(key=lambda item: item['utc'])
        events_path = folder / (tag + '_events.csv')
        with events_path.open('w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['utc', 'charger_id', 'event', 'value', 'note'])
            w.writeheader()
            w.writerows(events)
        return summary_path

    def evaluate(self, folder, degraded, mode='steady', baseline=True):
        base = self.build(folder, 'base', False)
        run = self.build(folder, 'test', degraded)
        cmd = ['python3', str(SCRIPT), '--run', str(run), '--mode', mode,
               '--min-connections', '20', '--min-heartbeats', '100', '--min-recoveries', '1',
               '--out', str(folder / 'report')]
        if baseline:
            cmd.extend(['--baseline', str(base)])
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return json.loads((folder / 'report.json').read_text(encoding='utf-8'))

    def test_normal_load_calculates_all_non_drain_metrics(self):
        with tempfile.TemporaryDirectory() as dir:
            report = self.evaluate(Path(dir), False)
            self.assertEqual(report['overall'], '통과')
            self.assertAlmostEqual(report['measurements']['worst_60s_connection_drop_pct'], 0)
            self.assertAlmostEqual(report['measurements']['worst_60s_throughput_drop_pct'], 0)
            self.assertEqual(report['gates'][-1]['status'], '해당없음')

    def test_controlled_failure_exposes_drop_errors_and_reconnect_p95(self):
        with tempfile.TemporaryDirectory() as dir:
            report = self.evaluate(Path(dir), True)
            measures = report['measurements']
            self.assertEqual(report['overall'], '실패')
            self.assertAlmostEqual(measures['worst_60s_connection_drop_pct'], 1)
            self.assertGreater(measures['worst_60s_throughput_drop_pct'], 15)
            self.assertGreater(measures['ocpp_error_pct'], 1)
            self.assertLess(measures['heartbeat_success_pct'], 99)
            self.assertAlmostEqual(measures['reconnect_p95_sec'], 3.2)

    def test_missing_baseline_is_inconclusive_not_zero(self):
        with tempfile.TemporaryDirectory() as dir:
            report = self.evaluate(Path(dir), False, baseline=False)
            self.assertEqual(report['overall'], '보류')
            self.assertIsNone(report['measurements']['worst_60s_throughput_drop_pct'])

    def test_drain_requires_reconnection_evidence(self):
        with tempfile.TemporaryDirectory() as dir:
            report = self.evaluate(Path(dir), False, mode='drain')
            self.assertEqual(report['overall'], '보류')
            self.assertEqual(report['gates'][-1]['status'], '보류')


if __name__ == '__main__':
    unittest.main()
