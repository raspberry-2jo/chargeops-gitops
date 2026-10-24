"""Bind existing Jenkins service accounts to verified live Application names.

Run once on mgmt as an administrator. Reads Apps/ServiceAccounts and writes
only the four Argo namespace Role/RoleBinding objects, never Apps or workloads.
"""
import argparse
import json
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]


def run(*args, payload=None):
    result = subprocess.run(args, text=True, input=payload, capture_output=True, cwd=ROOT)
    if result.returncode:
        raise ValueError('kubectl failed; check administrator access and the supplied Application names; child output withheld')
    return result.stdout


def configure(dev_name, ops_name):
    if not dev_name or dev_name == ops_name:
        raise ValueError('Dev and ops must have different Application names')
    for name, namespace, path in [(dev_name, 'dev', 'development'), (ops_name, 'ops', 'canary')]:
        app = json.loads(run('kubectl', '-n', 'argocd', 'get', 'application', name, '-o', 'json'))
        spec = app['spec']
        if (spec['destination']['namespace'] != namespace
                or spec['source']['path'] != 'chargeops-chatops/overlays/' + path
                or spec['source'].get('targetRevision') != 'main'):
            raise ValueError('Application namespace, path or GitOps branch does not match the intended environment')
        auto = spec.get('syncPolicy', {}).get('automated')
        if namespace == 'ops' and auto is not None and auto.get('enabled') is not False:
            raise ValueError('Ops must use manual Sync; Jenkins requests selective CSMS Sync after reviewed dev verification')
    for namespace, account in [('dev', 'jenkins-dev-verifier'), ('ops', 'jenkins-canary')]:
        run('kubectl', '-n', namespace, 'get', 'serviceaccount', account, '-o', 'name')
    documents = []
    for filename, app_name in [('dev-verifier-rbac.yaml', dev_name), ('canary-rbac.yaml', ops_name)]:
        for document in yaml.safe_load_all((ROOT / 'chargeops-chatops/argocd' / filename).read_text(encoding='utf-8')):
            if document and document['metadata'].get('namespace') == 'argocd' and document['kind'] in ['Role', 'RoleBinding']:
                if document['kind'] == 'Role':
                    for rule in document['rules']:
                        if rule['resources'] != ['applications']:
                            raise ValueError('Unexpected RBAC resource; no changes applied')
                        rule['resourceNames'] = [app_name]
                documents.append(document)
    assert len(documents) == 4
    run('kubectl', 'apply', '-f', '-', payload=yaml.safe_dump_all(documents))
    print('PASS: exact Application access configured. Reuse the existing dev/canary kubeconfigs; no token was read or printed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dev-application', default='chargeops-dev')
    parser.add_argument('--ops-application', default='chargeops-chatops-dev')
    args = parser.parse_args()
    try:
        configure(args.dev_application, args.ops_application)
    except Exception as error:
        print('Application access setup stopped: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__))
        raise SystemExit(1)
