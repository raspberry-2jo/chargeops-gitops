"""Check threshold suggestion arithmetic using explicitly artificial illustrative inputs."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/calibrate_sli_thresholds.py'


class CalibrationSyntheticTests(unittest.TestCase):
    def test_demo_extrema_become_proposed_candidate_limits(self):
        names = ['연결 감소율(최악 60초)', '처리량 감소율(최악 60초)',
                 'OCPP Heartbeat 오류율', '비정상 종료율', 'Heartbeat 성공률', '재연결 p95(초)']
        maxima = [0.8, 6, 0.3, 0.2, 99.7, 3.2]
        config = dict(connections=100, ramp_rate=5, duration_sec=420,
                      heartbeat_interval=5, response_timeout=10)
        with tempfile.TemporaryDirectory() as dir:
            directory = Path(dir)
            for mode in ('steady', 'drain'):
                for i in range(3):
                    gates = [dict(metric=n, actual=v, status='통과') for n, v in zip(names, maxima)]
                    report = dict(scenario=mode, workload_config=config, gates=gates)
                    (directory / f'{mode}{i}_six_slis.json').write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
            out = directory / 'suggestions.json'
            subprocess.run(['python3', str(SCRIPT), '--steady', str(directory/'steady*_six_slis.json'),
                            '--drain', str(directory/'drain*_six_slis.json'), '--out', str(out)],
                           check=True, capture_output=True)
            recs = json.loads(out.read_text(encoding='utf-8'))['recommendations']
            self.assertEqual([r['candidate'] for r in recs], [2, 15, 1, 1, 99, 10])


if __name__ == '__main__':
    unittest.main()
