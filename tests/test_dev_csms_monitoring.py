"""Verify Dev-only scraping and unchanged production/canary desired state."""
from pathlib import Path
import shutil
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(root, name):
    result = subprocess.check_output(
        ['kubectl', 'kustomize', str(root / 'chargeops-chatops/overlays' / name)],
        text=True, encoding='utf-8')
    return {(d['kind'], d['metadata']['name']): d for d in yaml.safe_load_all(result) if d}


def test_dev_monitor_selects_only_labeled_dev_service():
    docs = render(ROOT, 'development')
    service = docs['Service', 'csms']
    monitor = docs['ServiceMonitor', 'csms-dev']
    assert service['metadata']['namespace'] == monitor['metadata']['namespace'] == 'dev'
    assert monitor['spec']['namespaceSelector'] == {'matchNames': ['dev']}
    assert monitor['spec']['selector']['matchLabels'] == {'chargeops.io/metrics': 'csms-dev'}
    assert service['metadata']['labels']['chargeops.io/metrics'] == 'csms-dev'
    assert service['spec']['selector'] == {'app.kubernetes.io/name': 'csms'}
    endpoint = monitor['spec']['endpoints'][0]
    assert endpoint == {'port': 'metrics', 'interval': '5s', 'scrapeTimeout': '3s', 'honorLabels': False}
    assert next(p for p in service['spec']['ports'] if p['name'] == endpoint['port'])['port'] == 9100


def test_additive_rule_admits_only_monitoring_to_csms_metrics():
    docs = render(ROOT, 'development')
    policy = docs['NetworkPolicy', 'csms-prometheus-ingress']
    assert policy['metadata']['namespace'] == 'dev'
    assert policy['spec'] == {
        'podSelector': {'matchLabels': {'app': 'csms'}}, 'policyTypes': ['Ingress'],
        'ingress': [{'from': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'monitoring'}}}],
                     'ports': [{'protocol': 'TCP', 'port': 9100}]}]}
    assert 'egress' not in policy['spec']


def test_no_ops_resource_or_existing_dev_workload_changes(tmp_path):
    shutil.copytree(ROOT / 'chargeops-chatops', tmp_path / 'chargeops-chatops')
    # Render a baseline with this optional Dev monitoring removed. This also
    # works after the change is committed, in a shallow CI checkout.
    config_path = tmp_path / 'chargeops-chatops/overlays/development/kustomization.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    config['resources'] = [r for r in config['resources']
                           if r not in ['metrics-servicemonitor.yaml', 'metrics-networkpolicy.yaml']]
    config['patches'] = [p for p in config['patches'] if p.get('path') != 'metrics-service-patch.yaml']
    config_path.write_text(yaml.safe_dump(config), encoding='utf-8')
    for overlay in ['prod', 'canary']:
        assert render(ROOT, overlay) == render(tmp_path, overlay)
    before, after = render(tmp_path, 'development'), render(ROOT, 'development')
    assert set(after) - set(before) == {('ServiceMonitor', 'csms-dev'), ('NetworkPolicy', 'csms-prometheus-ingress')}
    for key in before:
        if key != ('Service', 'csms'):
            assert before[key] == after[key]
    patched = after['Service', 'csms']
    patched['metadata']['labels'].pop('chargeops.io/metrics')
    if not patched['metadata']['labels']:
        patched['metadata'].pop('labels')
    assert patched == before['Service', 'csms']
