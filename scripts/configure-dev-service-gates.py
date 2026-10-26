"""Edit Dev gate Git settings; run after >=5 minutes of new connection metrics."""
import argparse
from pathlib import Path
import yaml

SETTINGS = Path(__file__).resolve().parents[1] / 'chargeops-chatops/overlays/development-canary/gate-settings.yaml'


def configure(enabled, throughput_drop=50):
    if throughput_drop not in (30,50,90): raise ValueError('Select a 30, 50 or 90 percent throughput drop')
    config = yaml.safe_load(SETTINGS.read_text(encoding='utf-8'))
    config['data']['GATE_ENABLED'] = 'true' if enabled else 'false'
    config['data']['GATE_THROUGHPUT_DROP_PERCENT'] = str(throughput_drop)
    SETTINGS.write_text(yaml.safe_dump(config, sort_keys=False),encoding='utf-8')
    print('Dev Git settings edited. Commit/push, then run a new baseline-aware Jenkins build.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--enable',action='store_true')
    choice.add_argument('--disable',action='store_true')
    parser.add_argument('--throughput-drop',type=int,default=50)
    args = parser.parse_args()
    configure(args.enable,args.throughput_drop)
