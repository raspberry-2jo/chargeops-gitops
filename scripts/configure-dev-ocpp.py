"""Edit Git's Dev OCPP fixture settings only; no cluster/DB writes or Git push."""
import argparse
from pathlib import Path
import re

import yaml

SETTINGS = Path(__file__).resolve().parents[1] / 'chargeops-chatops/overlays/development-canary/ocpp-settings.yaml'


def configure(charger=None, disable=False):
    if not disable and not re.fullmatch(r'CI-DEV-[A-Za-z0-9_-]+', charger or ''):
        raise ValueError('Provide an exclusively reserved CI-DEV charger ID (Dev allowlisted or DB registered)')
    settings = yaml.safe_load(SETTINGS.read_text(encoding='utf-8'))
    settings['data'] = {'OCPP_PROBE_ENABLED': 'false' if disable else 'true',
                        'OCPP_TEST_CHARGER_ID': '' if disable else charger}
    SETTINGS.write_text(yaml.safe_dump(settings, allow_unicode=True, sort_keys=False), encoding='utf-8')
    print('Updated Dev settings file only. Review/commit/push this file to activate via ArgoCD.')
    print('Default CI-DEV-CANARY-01 uses the Dev in-memory allowlist. Other IDs must be allowlisted or DB registered.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--charger-id')
    group.add_argument('--disable', action='store_true')
    args = p.parse_args()
    configure(args.charger_id, args.disable)
