"""Configure an already registered and exclusively reserved Ops OCPP test ID.

Edits Git configuration only. Does not create a DB charger or touch Kubernetes.
"""
import argparse,re
from pathlib import Path
import yaml


def configure(charger,reserved):
    if not re.fullmatch(r'CI-OPS-CANARY-[A-Za-z0-9_-]{1,40}',charger) or not reserved:
        raise ValueError('Provide a registered CI-OPS-CANARY ID and --confirm-exclusive-reservation')
    path=Path(__file__).resolve().parents[1]/'chargeops-chatops/overlays/canary/ocpp-settings.yaml'
    document=yaml.safe_load(path.read_text(encoding='utf-8'))
    document['data'].update(OCPP_TEST_CHARGER_ID=charger,OCPP_FIXTURE_RESERVED='true')
    path.write_text(yaml.safe_dump(document,sort_keys=False),encoding='utf-8')
    print('Git settings updated. Review/commit/push main before a NEW Jenkins build. The live probe still verifies DB registration and refuses existing sessions.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--charger-id',required=True)
    parser.add_argument('--confirm-exclusive-reservation',action='store_true')
    args=parser.parse_args()
    try:configure(args.charger_id,args.confirm_exclusive_reservation)
    except ValueError as error:print('STOP: '+str(error));raise SystemExit(1)
