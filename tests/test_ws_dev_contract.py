import importlib.util
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('dev_contract',ROOT/'scripts/render-ws-dev-contract.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def test_dev_contract_never_replaces_existing_workloads():
    for scenario in ('pass','fail','hold'):
        baseline,candidate=m.manifests(scenario)
        assert all(d['metadata']['namespace']=='dev' for d in baseline+[candidate])
        assert all(d['metadata']['name'].startswith('ws-policy-'+scenario) for d in baseline+[candidate])
        rollout=baseline[-1]
        assert 'workloadRef' not in rollout['spec']
        assert rollout['spec']['replicas']==4
        assert candidate['spec']['template']['metadata']['annotations']
        assert not any(d['kind'] in ('Secret','HorizontalPodAutoscaler','Namespace') for d in baseline)
        metric=next(d for d in baseline if d['kind']=='AnalysisTemplate')['spec']['metrics'][0]
        assert 'dryRun' not in metric and metric['failureCondition']=='result == -1'


def test_evaluator_network_and_read_only_scope_are_valid():
    docs=list(yaml.safe_load_all((ROOT/'chargeops-chatops/overlays/canary/evaluator.yaml').read_text()))
    by_kind={d['kind']:d for d in docs if d['kind']!='NetworkPolicy'}
    for rule in by_kind['Role']['rules']:
        assert set(rule['verbs'])<=set(('get','list'))
        assert 'secrets' not in rule['resources']
    for doc in docs:
        if doc['kind']=='NetworkPolicy':
            assert all('from' not in r for r in doc['spec'].get('egress',[]))
            assert all('to' not in r for r in doc['spec'].get('ingress',[]))
    assert by_kind['PodMonitor']['spec']['podMetricsEndpoints'][0]['interval']=='5s'
