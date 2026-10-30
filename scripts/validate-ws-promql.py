"""Validate production-generated PromQL with official promtool, no live cluster needed."""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml

ROOT=Path(__file__).resolve().parents[1]
DIRECTORY=ROOT/'chargeops-chatops/overlays/canary'
sys.path.insert(0,str(DIRECTORY))
from ws_evaluator import Queries


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--promtool',required=True);args=parser.parse_args()
    q=Queries('dev','csms-lab','newhash','oldhash')
    expressions=q.role('newhash')
    suffix=',pod=~"csms-lab-newhash-.*",kind="service",chargeops_policy_scrape="ws-v1"'
    expressions['coverage']='min(count_over_time(chargeops_csms_active_sessions{namespace="dev"'+suffix+'}[60s]))'
    expressions['age']='max(time() - timestamp(chargeops_csms_active_sessions{namespace="dev"'+suffix+'}))'
    expressions['oom']='container_oom_events_total{namespace="dev",container="csms"}'
    expressions['all_connections']=q.average('chargeops_csms_active_sessions',extra=',kind="service"')
    expressions['missing_role']=q.count('chargeops_ocpp_handler_results_total','absenthash',extra=',action="Heartbeat",result="success"')
    rules={'groups':[{'name':'policy-syntax','rules':[{'record':'policy_test_'+name,'expr':expr} for name,expr in expressions.items()]}]}
    base='namespace="dev",pod="csms-lab-newhash-abc12",chargeops_policy_scrape="ws-v1"'
    series=[]
    for job in ('first','duplicate'):
        labels=base+',job="'+job+'"'
        series.extend([
          {'series':'chargeops_csms_active_sessions{'+labels+',kind="service"}','values':'1000+0x24'},
          {'series':'chargeops_ocpp_handler_results_total{'+labels+',action="Heartbeat",result="success"}','values':'0+5x24'},
          {'series':'chargeops_ocpp_handler_results_total{'+labels+',action="Heartbeat",result="error"}','values':'0+0x24'}])
    series.append({'series':'chargeops_csms_accepted_sessions_total{'+base+',kind="service"}', 'values':'0+5x6 0+5x17'})
    exported=base.replace('namespace="dev"','namespace="monitoring",exported_namespace="dev"')
    series.append({'series':'chargeops_csms_disconnect_total{'+exported+',kind="service",reason="abnormal"}', 'values':'0+0x24'})
    # Another namespace must never enter the candidate denominator.
    series.append({'series':'chargeops_csms_active_sessions{'+base.replace('namespace="dev"','namespace="ops"')+',kind="service"}','values':'99999+0x24'})
    tests={'fuzzy_compare':True,'rule_files':[],'evaluation_interval':'5s','tests':[{'interval':'5s','input_series':series,'promql_expr_test':[
      {'expr':expressions['connections'],'eval_time':'2m','exp_samples':[{'labels':'{}','value':1000}]},
      {'expr':q.count('chargeops_ocpp_handler_results_total','newhash',extra=',action="Heartbeat",result="success"'),'eval_time':'2m','exp_samples':[{'labels':'{}','value':60}]},
      {'expr':expressions['accepted'],'eval_time':'2m','exp_samples':[{'labels':'{}','value':60}]},
      {'expr':expressions['accepted'],'eval_time':'1m','exp_samples':[{'labels':'{}','value':600/11}]},
      {'expr':expressions['abnormal'],'eval_time':'2m','exp_samples':[{'labels':'{}','value':0}]},
      {'expr':expressions['age'],'eval_time':'2m','exp_samples':[{'labels':'{}','value':0}]},
      {'expr':expressions['errors'],'eval_time':'2m','exp_samples':[{'labels':'{}','value':0}]},
      {'expr':expressions['missing_role'],'eval_time':'2m','exp_samples':[]}]}]}
    with tempfile.TemporaryDirectory(prefix='chargeops-promql-') as directory:
        directory=Path(directory);rule=directory/'rules.yaml';test=directory/'test.yaml'
        rule.write_text(yaml.safe_dump(rules));test.write_text(yaml.safe_dump(tests))
        subprocess.run([args.promtool,'check','rules',str(rule)],check=True)
        subprocess.run([args.promtool,'test','rules',str(test)],check=True)

if __name__=='__main__':main()


