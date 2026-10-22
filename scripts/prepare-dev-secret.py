"""Create DEV's namespaced Secret using existing DB settings, in memory only.

Run on mgmt with an administrator kubeconfig. No values are printed or saved.
"""
import json
import subprocess


def execute(args, payload=None):
    result = subprocess.run(['kubectl', *args], input=payload, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('kubectl operation failed')
    return result.stdout


def main():
    source = json.loads(execute(['-n', 'ops', 'get', 'secret', 'chargeops-secrets', '-o', 'json']))['data']
    data = {key: source[key] for key in ['DB_DSN', 'CSMS_API_TOKEN']}
    namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'dev'}}
    execute(['apply', '--server-side', '--field-manager=chargeops-dev-bootstrap', '-f', '-'], json.dumps(namespace))
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
              'metadata': {'name': 'chargeops-dev-secrets', 'namespace': 'dev'}, 'data': data}
    execute(['apply', '--server-side', '--field-manager=chargeops-dev-bootstrap', '-f', '-'], json.dumps(secret))
    print('DEV namespace and chargeops-dev-secrets prepared using existing DB settings. No values exported.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Preparation stopped. Check namespace permissions and required source Secret keys. No values printed.')
        raise SystemExit(1)
