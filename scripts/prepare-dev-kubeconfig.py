"""Create a restricted Jenkins kubeconfig outside Git; never print credentials."""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = 'jenkins-dev-verifier'


def kubectl(*args, payload=None, allow_no=False):
    result = subprocess.run(['kubectl', *args], input=payload, text=True, capture_output=True)
    if result.returncode and not (allow_no and result.returncode == 1 and result.stdout.strip() == 'no'):
        raise RuntimeError('kubectl failed; check administrator context, API connectivity and token/RBAC support')
    return result.stdout.strip()


def prepare_rbac():
    docs = [d for d in yaml.safe_load_all(kubectl('kustomize', str(ROOT / 'chargeops-chatops/overlays/development'))) if d]
    expected = {('Namespace', 'dev'), ('ServiceAccount', ACCOUNT), ('Role', ACCOUNT), ('RoleBinding', ACCOUNT)}
    selected = [d for d in docs if (d['kind'], d['metadata']['name']) in expected]
    if {(d['kind'], d['metadata']['name']) for d in selected} != expected:
        raise ValueError('Development namespace/RBAC definitions are incomplete')
    for d in selected:
        if d['kind'] != 'Namespace' and d['metadata'].get('namespace') != 'dev':
            raise ValueError('Verifier RBAC must remain in dev')
    # Only Namespace and verifier RBAC: never deploy or scale applications.
    kubectl('apply', '--server-side', '--field-manager=chargeops-verifier-bootstrap', '-f', '-',
            payload=yaml.safe_dump_all(selected))
    kubectl('apply', '--server-side', '--field-manager=chargeops-verifier-bootstrap', '-f',
            str(ROOT / 'chargeops-chatops/argocd/dev-verifier-rbac.yaml'))


def make_config(cluster, token):
    # Do not copy current user's client certificate/key, exec auth or admin token.
    server, ca = cluster.get('server', ''), cluster.get('certificate-authority-data', '')
    if not server.startswith('https://') or not ca or cluster.get('insecure-skip-tls-verify'):
        raise ValueError('The current context must provide an HTTPS API endpoint and trusted cluster CA')
    return {'apiVersion': 'v1', 'kind': 'Config',
            'clusters': [{'name': 'chargeops', 'cluster': {'server': server, 'certificate-authority-data': ca}}],
            'users': [{'name': ACCOUNT, 'user': {'token': token}}],
            'contexts': [{'name': 'chargeops-dev', 'context': {'cluster': 'chargeops', 'user': ACCOUNT, 'namespace': 'dev'}}],
            'current-context': 'chargeops-dev'}


def service_account_token(namespace, account, duration='unlimited'):
    """Reuse a manually created SA token Secret; never add it to SA.secrets.

    The Secret holds the credential in Kubernetes, never in tracked YAML.
    Deliberately leave SA.secrets untouched so this is not an auto-generated
    legacy token subject to automatic unused-token cleanup.
    """
    if duration != 'unlimited':
        return kubectl('-n', namespace, 'create', 'token', account, '--duration=' + duration)
    name = account + '-token'
    service_account = json.loads(kubectl('-n', namespace, 'get', 'serviceaccount', account, '-o', 'json'))
    uid = service_account['metadata']['uid']
    raw = kubectl('-n', namespace, 'get', 'secret', name, '--ignore-not-found', '-o', 'json')
    secret = json.loads(raw) if raw else None
    if secret is None:
        document = {'apiVersion': 'v1', 'kind': 'Secret',
                    'metadata': {'name': name, 'namespace': namespace,
                                 'annotations': {'kubernetes.io/service-account.name': account}},
                    'type': 'kubernetes.io/service-account-token'}
        kubectl('create', '-f', '-', payload=json.dumps(document))
    for attempt in range(31):
        if secret is not None:
            annotations = secret['metadata'].get('annotations', {})
            if (secret.get('type') != 'kubernetes.io/service-account-token'
                    or annotations.get('kubernetes.io/service-account.name') != account
                    or annotations.get('kubernetes.io/service-account.uid', uid) != uid):
                raise ValueError('Existing token Secret belongs to another identity; it was not modified')
            if secret['metadata'].get('labels', {}).get('kubernetes.io/legacy-token-invalid-since'):
                raise ValueError('Existing token Secret was invalidated; replace that Secret and regenerate the Jenkins file')
            encoded = secret.get('data', {}).get('token')
            if encoded:
                try:
                    token = base64.b64decode(encoded, validate=True).decode('utf-8')
                    parts = token.split('.')
                    if len(parts) != 3:
                        raise ValueError()
                    claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
                except (ValueError, UnicodeError, IndexError):
                    raise ValueError('Token Secret does not contain a valid service account JWT') from None
                if ('exp' in claims or claims.get('sub') != f'system:serviceaccount:{namespace}:{account}'
                        or annotations.get('kubernetes.io/service-account.uid') != uid):
                    raise ValueError('Token Secret is expiring or belongs to another identity')
                return token
        if attempt == 30:
            break
        time.sleep(2)
        secret = json.loads(kubectl('-n', namespace, 'get', 'secret', name, '-o', 'json'))
    raise ValueError('Service account token controller did not populate the Secret within sixty seconds')


def verify_permissions(path):
    probes = [(['get', 'deployments', '-n', 'dev'], 'yes'),
              (['create', 'jobs', '-n', 'dev'], 'yes'),
              (['get', 'applications.argoproj.io/chargeops-dev', '-n', 'argocd'], 'yes'),
              (['get', 'secrets', '-n', 'ops'], 'no'),
              (['patch', 'deployments', '-n', 'ops'], 'no')]
    for args, expected in probes:
        actual = kubectl('--kubeconfig', str(path), 'auth', 'can-i', *args, allow_no=True)
        if actual != expected:
            raise ValueError('Verifier RBAC differs from the expected dev-only access')


def write_config(config, output, verifier=verify_permissions):
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.dev-kubeconfig-', dir=output.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            yaml.safe_dump(config, handle, sort_keys=False)
        os.chmod(temporary, 0o600)
        verifier(Path(temporary))
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', default='unlimited', help='Default: non-expiring Secret token. Optional: a requested bounded lifetime such as 24h')
    args = parser.parse_args()
    if args.duration != 'unlimited' and not re.fullmatch(r'[1-9][0-9]*(s|m|h)', args.duration):
        raise ValueError('Duration must be unlimited or a positive number followed by s, m or h')
    source = json.loads(kubectl('config', 'view', '--raw', '--minify', '--flatten', '-o', 'json'))
    if len(source.get('clusters', [])) != 1:
        raise ValueError('Select an administrator Kubernetes context first')
    cluster = source['clusters'][0]['cluster']
    make_config(cluster, 'validation-only')
    prepare_rbac()
    token = service_account_token('dev', ACCOUNT, args.duration)
    if not token:
        raise ValueError('The API server did not issue a token')
    output = Path.home() / '.chargeops-jenkins/dev-kubeconfig.yaml'
    write_config(make_config(cluster, token), output)
    print(f'Created: {output}')
    print('Verified: dev deployment reads/jobs allowed; ops secrets/deployment writes denied.')
    if args.duration == 'unlimited':
        print('Token expiration: none. Reuses Secret dev/' + ACCOUNT + '-token; deleting the Secret or ServiceAccount revokes it.')
    else:
        print_token_expiry(token, args.duration)
    print('Upload this file to Jenkins Secret file credential ID: chargeops-dev-kubeconfig')
    print('No token or kubeconfig contents were printed. Replace the previously uploaded Jenkins file once to switch token types.')


def print_token_expiry(token, duration):
    try:
        payload = token.split('.')[1]
        expiry = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))['exp']
        print('Token expires: ' + datetime.fromtimestamp(expiry, timezone.utc).isoformat())
    except (ValueError, IndexError, KeyError):
        print('Requested token lifetime: ' + duration + '; server policy determines actual expiry.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Preparation stopped (' + error.__class__.__name__ + '). Check admin context, API CA, dev RBAC and token support. No credential values printed.')
        raise SystemExit(1)
