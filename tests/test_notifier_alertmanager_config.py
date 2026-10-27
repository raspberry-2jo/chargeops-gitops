import importlib.util
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
URL='http://kps-kube-prometheus-stack-alertmanager.monitoring.svc.cluster.local:9093'


def test_notifier_uses_internal_service_and_code_default_intervals():
    doc=yaml.safe_load((ROOT/'chargeops-chatops/base/notifier/deployment.yaml').read_text(encoding='utf-8'))
    container=next(c for c in doc['spec']['template']['spec']['containers'] if c['name']=='notifier')
    env={e['name']:e.get('value') for e in container['env']}
    assert env['ALERTMANAGER_URL']==URL
    assert 'RECONCILE_GRACE_SEC' not in env and 'RECONCILE_EVERY_SEC' not in env


def test_probe_only_executes_read_only_api_check():
    spec=importlib.util.spec_from_file_location('notifier_probe',ROOT/'scripts/check-notifier-alertmanager.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    cmd=module.command('ops')
    assert cmd[:6]==['kubectl','-n','ops','exec','deployment/notifier','-c']
    assert 'requests.get(' in module.PROBE and 'raise_for_status()' in module.PROBE
    assert cmd[-1]==URL
