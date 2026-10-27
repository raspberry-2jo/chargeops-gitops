"""Read-only Alertmanager API probe from an existing notifier Pod.

Does not send webhooks, modify incidents, send Slack messages or sync Argo CD.
"""
import argparse
import subprocess

URL = 'http://kps-kube-prometheus-stack-alertmanager.monitoring.svc.cluster.local:9093'
PROBE = '''import json,sys,requests
r=requests.get(sys.argv[1].rstrip('/')+'/api/v2/alerts',params={'active':'true'},timeout=5)
r.raise_for_status()
alerts=r.json()
if not isinstance(alerts,list) or any(not isinstance(a,dict) or not isinstance(a.get('labels'),dict) for a in alerts):
    raise ValueError('Unexpected Alertmanager response schema')
print(json.dumps({'api_connected':True,'http_status':r.status_code,'active_alert_count':len(alerts)}))
'''


def command(namespace, url=URL):
    if namespace not in ('dev','ops'):
        raise ValueError('Only dev or ops is allowed')
    if url != URL:
        raise ValueError('Probe only the confirmed internal Alertmanager Service')
    return ['kubectl','-n',namespace,'exec','deployment/notifier','-c','notifier','--',
            'python','-c',PROBE,url]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace',choices=['dev','ops'],default='dev')
    args=parser.parse_args()
    result=subprocess.run(command(args.namespace),capture_output=True,text=True,timeout=30)
    if result.returncode:
        print('NOT VERIFIED: notifier Pod/API access failed. Check Pod existence, kube context, DNS, NetworkPolicy and Service endpoints from mgmt. No resources changed.')
        return 1
    print(result.stdout.strip())
    return 0


if __name__=='__main__':
    raise SystemExit(main())
